-- nvtour runtime: read-only guided code walkthroughs, injected over RPC.
-- Every CLI command is one call to _G.nvtour.dispatch(cmd, args).
local M = {}
M.VERSION = (...) or "dev" -- the client passes a hash of this source

local PREV_VERSION = _G.nvtour and _G.nvtour.VERSION

local api = vim.api
local uv = vim.uv or vim.loop

-- Safety rule: we only ever write lines to buffers named nvtour://...
vim.o.hidden = true

---------------------------------------------------------------------------
-- State (preserved across reloads)
---------------------------------------------------------------------------

local function new_state()
  return {
    tour = { title = "", steps = {}, current = 0, qf_id = nil },
    ns_steps = api.nvim_create_namespace("nvtour_steps"),
    ns_focus = api.nvim_create_namespace("nvtour_focus"),
    ns_panel = api.nvim_create_namespace("nvtour_panel"),
    panel = { buf = nil, win = nil, text = {}, user_closed = false, line_map = {} },
    focus = {},
    diff_tabs = {},
    keymaps = {},
    keymaps_installed = false,
    workspace = nil,
    warned = {},
    tour_win = nil,
    pending_folds = {},
    added_bufs = {},
  }
end

local S = (_G.nvtour and _G.nvtour.state) or new_state()
for k, v in pairs(new_state()) do
  if S[k] == nil then
    S[k] = v
  end
end
M.state = S

local DEFAULT_KEYS =
  { next = "]w", prev = "[w", first = "[W", last = "]W", panel = "<leader>wp", clear = "<leader>wc" }

---------------------------------------------------------------------------
-- Highlights
---------------------------------------------------------------------------

-- Each role takes its accent colour from a Diagnostic* group of the active colorscheme (with a
-- fallback) and tints the line background by blending that accent into the Normal background.
-- Diff* groups are deliberately not used: many colorschemes (gruvbox) define them with `reverse`,
-- which paints the whole range in one solid colour and destroys syntax highlighting.
local ROLES = {
  fault = { accent = "DiagnosticError", fallback = 0xfb4934, tint = 0.18 },
  flow = { accent = "DiagnosticInfo", fallback = 0x83a598, tint = 0.16 },
  fix = { accent = "DiagnosticOk", fallback = 0xb8bb26, tint = 0.16 },
  context = { accent = "Comment", fallback = 0x928374, tint = 0.10 },
  info = { accent = "DiagnosticHint", fallback = 0x8ec07c, tint = nil },
}

local function cap(s)
  return s:sub(1, 1):upper() .. s:sub(2)
end

local function hl_attr(name, attr)
  local ok, h = pcall(api.nvim_get_hl, 0, { name = name, link = false })
  if ok and type(h) == "table" and h[attr] then
    return h[attr]
  end
end

--- Blend colour `a` over colour `b` with weight `w` (0..1). Colours are 0xRRGGBB integers.
local function blend(a, b, w)
  local function ch(c, shift)
    return bit.band(bit.rshift(c, shift), 0xff)
  end
  local function mix(shift)
    return math.floor(ch(a, shift) * w + ch(b, shift) * (1 - w) + 0.5)
  end
  return bit.bor(bit.lshift(mix(16), 16), bit.lshift(mix(8), 8), mix(0))
end

local function define_highlights(force)
  local dark = vim.o.background ~= "light"
  local bg = hl_attr("Normal", "bg") or (dark and 0x1d2021 or 0xfbf1c7)
  local fg = hl_attr("Normal", "fg") or (dark and 0xebdbb2 or 0x3c3836)
  local function set(name, spec)
    if not force then
      spec.default = true
    end
    api.nvim_set_hl(0, name, spec)
  end
  for role, g in pairs(ROLES) do
    local R = cap(role)
    local accent = hl_attr(g.accent, "fg") or g.fallback
    if g.tint then
      set("NvtourLine" .. R, { bg = blend(accent, bg, g.tint) })
    end
    set("NvtourNumber" .. R, { fg = accent, bold = true })
    set("NvtourSign" .. R, { fg = accent, bold = true })
    set("NvtourLabel" .. R, { fg = accent, bold = true, italic = true })
  end
  set("NvtourNote", { fg = blend(fg, bg, 0.80), italic = true })
  set("NvtourNoteBorder", { fg = blend(fg, bg, 0.40) })
  set("NvtourDim", { fg = blend(fg, bg, 0.35) })
  set("NvtourPanelCurrent", { bg = blend(fg, bg, 0.12), bold = true })
  -- Lets init.lua re-apply overrides after a reload or colorscheme change:
  --   vim.api.nvim_create_autocmd("User", { pattern = "NvtourHighlights", callback = function() ... end })
  pcall(api.nvim_exec_autocmds, "User", { pattern = "NvtourHighlights", modeline = false })
end
-- On a runtime upgrade the old definitions already exist, so `default` would keep them: force.
define_highlights(PREV_VERSION ~= nil and PREV_VERSION ~= M.VERSION)

local aug = api.nvim_create_augroup("nvtour", { clear = true })
api.nvim_create_autocmd("ColorScheme", {
  group = aug,
  callback = function()
    define_highlights(false)
  end,
})

---------------------------------------------------------------------------
-- Helpers
---------------------------------------------------------------------------

local function fail(msg, code)
  error({ nvtour = true, msg = msg, code = code or 5 }, 0)
end

local function notify(msg, level)
  pcall(vim.notify, msg, level or vim.log.levels.INFO)
end

-- Warnings collected during one dispatch; returned to the CLI as `warnings`.
local W

local function warn(msg)
  if W then
    W[#W + 1] = msg
  else
    notify("nvtour: " .. msg, vim.log.levels.WARN)
  end
end

local function valid_win(w)
  return w ~= nil and api.nvim_win_is_valid(w)
end
local function valid_buf(b)
  return b ~= nil and api.nvim_buf_is_valid(b)
end
local function valid_tab(t)
  return t ~= nil and api.nvim_tabpage_is_valid(t)
end

local function is_diff_tab(tab)
  for _, t in ipairs(S.diff_tabs) do
    if t == tab then
      return true
    end
  end
  return false
end

local function is_normal_win(w)
  return valid_win(w) and api.nvim_win_get_config(w).relative == ""
end

--- A normal window that shows a file buffer (not a terminal, quickfix, help, or the panel).
local function file_win(w)
  return is_normal_win(w) and w ~= S.panel.win and vim.bo[api.nvim_win_get_buf(w)].buftype == ""
end

local function usable_tour_win(w)
  return file_win(w) and not is_diff_tab(api.nvim_win_get_tabpage(w))
end

--- Window where files are shown (see DESIGN.md section 5). With `buf`, a usable window of the
--- current tab that already shows it wins. Returns nil when no window is usable, unless `create`
--- is set: then a split is opened for `buf` next to the first normal window of the current tab.
local function tour_win(buf, create)
  local tab = api.nvim_get_current_tabpage()
  if buf and valid_buf(buf) then
    for _, w in ipairs(vim.fn.win_findbuf(buf)) do
      if usable_tour_win(w) and api.nvim_win_get_tabpage(w) == tab then
        return w
      end
    end
  end
  local cur = api.nvim_get_current_win()
  if usable_tour_win(cur) then
    return cur
  end
  local prev = vim.fn.win_getid(vim.fn.winnr("#"))
  if prev ~= 0 and usable_tour_win(prev) then
    return prev
  end
  if valid_win(S.tour_win) and usable_tour_win(S.tour_win) then
    return S.tour_win
  end
  local tabs = { tab }
  for _, t in ipairs(api.nvim_list_tabpages()) do
    if t ~= tab then
      tabs[#tabs + 1] = t
    end
  end
  for _, t in ipairs(tabs) do
    if not is_diff_tab(t) then
      for _, w in ipairs(api.nvim_tabpage_list_wins(t)) do
        if usable_tour_win(w) then
          return w
        end
      end
    end
  end
  if not (create and buf) then
    return nil
  end
  local anchor = -1
  for _, w in ipairs(api.nvim_tabpage_list_wins(tab)) do
    if is_normal_win(w) and w ~= S.panel.win then
      anchor = w
      break
    end
  end
  return api.nvim_open_win(buf, false, { split = "left", win = anchor })
end

--- Make `win` current. By default the user's terminal window keeps the focus
--- (vim.g.nvtour_steal_focus = "unless_terminal" | "always" | "never").
local function enter_win(win)
  local cur = api.nvim_get_current_win()
  if cur == win then
    return
  end
  local policy = vim.g.nvtour_steal_focus or "unless_terminal"
  if policy == "never" then
    return
  end
  if policy ~= "always" and vim.bo[api.nvim_win_get_buf(cur)].buftype == "terminal" then
    return
  end
  api.nvim_set_current_win(win)
end

local function split_lines(text)
  local out = {}
  text = (text or ""):gsub("\r\n", "\n")
  text = text:gsub("\n$", "")
  for line in (text .. "\n"):gmatch("(.-)\n") do
    out[#out + 1] = line
  end
  return out
end

local function relpath(path)
  local w = S.workspace
  if w and path:sub(1, #w + 1) == w .. "/" then
    return path:sub(#w + 2)
  end
  return path
end

--- Load a file into a (listed) buffer without displaying it.
local function load_buf(path)
  local st = uv.fs_stat(path)
  if not st or st.type ~= "file" then
    fail("file not found: " .. path, 6)
  end
  local buf = vim.fn.bufadd(path)
  if not vim.bo[buf].buflisted then
    S.added_bufs[buf] = true -- unlisted again by clear
  end
  -- bufload() shows no swap-file dialog; inside pcall its ATTENTION message (E325) becomes an
  -- error instead of a hit-enter prompt, and the buffer is loaded anyway.
  local ok, err = pcall(vim.fn.bufload, buf)
  if not ok and tostring(err):match("E325") and api.nvim_buf_is_loaded(buf) then
    warn(path .. " has a swap file (open in another nvim?); showing the file anyway")
  elseif not ok then
    fail("cannot read " .. path .. ": " .. tostring(err), 6)
  end
  vim.bo[buf].buflisted = true
  return buf
end

---------------------------------------------------------------------------
-- Focus (folds / dim)
---------------------------------------------------------------------------

local function merge_ranges(ranges, ctx, count)
  local sorted = {}
  for _, r in ipairs(ranges) do
    sorted[#sorted + 1] = { math.max(1, r[1] - ctx), math.min(count, r[2] + ctx) }
  end
  table.sort(sorted, function(a, b)
    return a[1] < b[1]
  end)
  local out = {}
  for _, r in ipairs(sorted) do
    local last = out[#out]
    if last and r[1] <= last[2] + 1 then
      last[2] = math.max(last[2], r[2])
    else
      out[#out + 1] = { r[1], r[2] }
    end
  end
  return out
end

local function gaps_of(ranges, count)
  local gaps, pos = {}, 1
  for _, r in ipairs(ranges) do
    if r[1] > pos then
      gaps[#gaps + 1] = { pos, r[1] - 1 }
    end
    pos = r[2] + 1
  end
  if pos <= count then
    gaps[#gaps + 1] = { pos, count }
  end
  return gaps
end

local FOLD_OPTS = { "foldmethod", "foldenable", "foldlevel", "foldminlines" }

--- Window-local fold options of `win` (`:setlocal` values, so other buffers are not affected).
local function get_fold_opts(win)
  local sv = {}
  for _, o in ipairs(FOLD_OPTS) do
    sv[o] = api.nvim_get_option_value(o, { scope = "local", win = win })
  end
  return sv
end

local function set_fold_opts(win, sv)
  for _, o in ipairs(FOLD_OPTS) do
    api.nvim_set_option_value(o, sv[o], { scope = "local", win = win })
  end
end

local function apply_folds(buf, win)
  local f = S.focus[buf]
  if not f or f.dim or not valid_win(win) or not valid_buf(buf) then
    return
  end
  f.saved = f.saved or {}
  if not f.saved[win] then
    -- A restore still pending for this window holds the user's values; the live ones are ours.
    local pending = S.pending_folds[win]
    f.saved[win] = (pending and pending[buf]) or get_fold_opts(win)
    if pending then
      pending[buf] = nil
    end
  end
  local count = api.nvim_buf_line_count(buf)
  api.nvim_win_call(win, function()
    local cursor = api.nvim_win_get_cursor(win)
    set_fold_opts(win, { foldmethod = "manual", foldenable = true, foldminlines = 0, foldlevel = 0 })
    vim.cmd("normal! zE")
    for _, g in ipairs(gaps_of(f.ranges, count)) do
      vim.cmd(("%d,%dfold"):format(g[1], g[2]))
      vim.cmd(("%d,%dfoldclose"):format(g[1], g[2]))
    end
    pcall(api.nvim_win_set_cursor, win, cursor)
  end)
end

local function unfocus_buf(buf)
  local f = S.focus[buf]
  if not f then
    return
  end
  if valid_buf(buf) then
    api.nvim_buf_clear_namespace(buf, S.ns_focus, 0, -1)
  end
  for win, sv in pairs(f.saved or {}) do
    if valid_win(win) and api.nvim_win_get_buf(win) == buf then
      api.nvim_win_call(win, function()
        vim.cmd("normal! zE")
        set_fold_opts(win, sv)
      end)
    elseif valid_win(win) and valid_buf(buf) then
      -- The window shows another buffer now; restore when this buffer comes back to it.
      S.pending_folds[win] = S.pending_folds[win] or {}
      S.pending_folds[win][buf] = sv
    end
  end
  S.focus[buf] = nil
end

api.nvim_create_autocmd("BufWinEnter", {
  group = aug,
  callback = function(ev)
    local win = api.nvim_get_current_win()
    local pending = S.pending_folds[win]
    local sv = pending and pending[ev.buf]
    if not sv or S.focus[ev.buf] then
      return
    end
    pending[ev.buf] = nil
    vim.cmd("normal! zE")
    set_fold_opts(win, sv)
  end,
})

--- Show buf in win without :edit; re-apply fold focus when the window switched buffers.
--- Returns the window that shows buf. A modified buffer that cannot be hidden is never replaced
--- (that would run 'autowrite' or fail with E37); a split is opened next to it instead.
local function show_buf(win, buf)
  local cur = api.nvim_win_get_buf(win)
  if cur == buf then
    return win
  end
  local bh = vim.bo[cur].bufhidden
  local hideable = bh == "hide" or (bh == "" and vim.o.hidden)
  if vim.bo[cur].modified and not hideable and #vim.fn.win_findbuf(cur) <= 1 then
    win = api.nvim_open_win(buf, false, { split = "right", win = win })
    warn(("kept modified buffer %s in its window; opened a split"):format(api.nvim_buf_get_name(cur)))
  else
    api.nvim_win_set_buf(win, buf)
  end
  apply_folds(buf, win)
  return win
end

---------------------------------------------------------------------------
-- Panel
---------------------------------------------------------------------------

local function ensure_panel_buf()
  if valid_buf(S.panel.buf) then
    return S.panel.buf
  end
  local buf = api.nvim_create_buf(false, true)
  api.nvim_buf_set_name(buf, "nvtour://panel")
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "hide"
  vim.bo[buf].swapfile = false
  vim.bo[buf].filetype = "markdown"
  vim.bo[buf].modifiable = false
  S.panel.buf = buf
  return buf
end

--- True when the panel window exists and still shows the panel buffer.
local function panel_shown()
  local w = S.panel.win
  return valid_win(w) and valid_buf(S.panel.buf) and api.nvim_win_get_buf(w) == S.panel.buf
end

local function panel_close(user)
  if panel_shown() then
    local w = S.panel.win
    local others = 0
    for _, x in ipairs(api.nvim_tabpage_list_wins(api.nvim_win_get_tabpage(w))) do
      if x ~= w and is_normal_win(x) then
        others = others + 1
      end
    end
    if others > 0 or #api.nvim_list_tabpages() > 1 then
      pcall(api.nvim_win_close, w, true)
    end
  end
  S.panel.win = nil
  if user then
    S.panel.user_closed = true
  end
end

local step_goto -- forward declaration

local function render_panel()
  if not valid_buf(S.panel.buf) then
    return
  end
  local buf = S.panel.buf
  local lines = { "# " .. ((S.tour.title ~= "" and S.tour.title) or "Walkthrough"), "" }
  local map = {}
  local cur_line
  for _, s in ipairs(S.tour.steps) do
    local range = s.l1 == s.l2 and tostring(s.l1) or (s.l1 .. "-" .. s.l2)
    local mark = (s.n == S.tour.current) and "▶ " or "  "
    local line = ("%s%d. %s:%s"):format(mark, s.n, relpath(s.file), range)
    if s.label and s.label ~= "" then
      line = line .. "  " .. s.label
    end
    lines[#lines + 1] = line
    map[#lines] = s.n
    if s.n == S.tour.current then
      cur_line = #lines
    end
  end
  if #S.panel.text > 0 then
    lines[#lines + 1] = ""
    lines[#lines + 1] = "---"
    for _, l in ipairs(S.panel.text) do
      lines[#lines + 1] = l
    end
  end
  S.panel.line_map = map
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  api.nvim_buf_clear_namespace(buf, S.ns_panel, 0, -1)
  if cur_line then
    api.nvim_buf_set_extmark(buf, S.ns_panel, cur_line - 1, 0, { line_hl_group = "NvtourPanelCurrent" })
    if panel_shown() then
      pcall(api.nvim_win_set_cursor, S.panel.win, { cur_line, 0 })
    end
  end
end

--- Buffer-local panel keys; they call through _G so they survive a runtime upgrade.
local function map_panel_keys(buf)
  local function map(lhs, fn)
    vim.keymap.set("n", lhs, fn, { buffer = buf, nowait = true, silent = true })
  end
  map("<CR>", function()
    local n = S.panel.line_map[api.nvim_win_get_cursor(0)[1]]
    if n then
      local res = _G.nvtour.dispatch("goto", { n = n })
      if not res.ok then
        notify(res.error, vim.log.levels.WARN)
      end
    end
  end)
  map("q", function()
    _G.nvtour.dispatch("panel", { toggle = true })
  end)
end

local function panel_open()
  -- The panel lives in the tab of the tour window.
  local target = tour_win() or api.nvim_get_current_win()
  local tab = api.nvim_win_get_tabpage(target)
  if panel_shown() then
    if api.nvim_win_get_tabpage(S.panel.win) == tab then
      return
    end
    pcall(api.nvim_win_close, S.panel.win, true)
  end
  S.panel.win = nil
  local buf = ensure_panel_buf()
  local width = math.max(40, math.min(70, math.floor(vim.o.columns * 0.3)))
  local win
  api.nvim_win_call(target, function()
    win = api.nvim_open_win(buf, false, { split = "right", win = -1, width = width })
  end)
  S.panel.win = win
  local wo = vim.wo[win]
  wo.winfixwidth = true
  wo.wrap = true
  wo.linebreak = true
  wo.number = false
  wo.relativenumber = false
  wo.signcolumn = "no"
  wo.foldcolumn = "0"
  wo.cursorline = true
  wo.foldenable = false
  wo.list = false
  map_panel_keys(buf)
  render_panel()
end

---------------------------------------------------------------------------
-- Step rendering
---------------------------------------------------------------------------

--- Split a word that is wider than `width` display cells.
local function split_word(word, width)
  local parts, cur = {}, ""
  for i = 0, vim.fn.strchars(word) - 1 do
    local ch = vim.fn.strcharpart(word, i, 1)
    if cur ~= "" and vim.fn.strdisplaywidth(cur .. ch) > width then
      parts[#parts + 1] = cur
      cur = ch
    else
      cur = cur .. ch
    end
  end
  parts[#parts + 1] = cur
  return parts
end

local function wrap_text(s, width)
  local out, cur = {}, ""
  for word in s:gmatch("%S+") do
    if vim.fn.strdisplaywidth(word) > width then
      if cur ~= "" then
        out[#out + 1] = cur
      end
      local parts = split_word(word, width)
      for i = 1, #parts - 1 do
        out[#out + 1] = parts[i]
      end
      cur = parts[#parts]
    elseif cur == "" then
      cur = word
    elseif vim.fn.strdisplaywidth(cur .. " " .. word) <= width then
      cur = cur .. " " .. word
    else
      out[#out + 1] = cur
      cur = word
    end
  end
  if cur ~= "" or #out == 0 then
    out[#out + 1] = cur
  end
  return out
end

local function note_chunks(note, width, n, R)
  local texts = {}
  for _, para in ipairs(split_lines((note or ""):gsub("\t", "  "))) do
    for _, l in ipairs(wrap_text(para, width)) do
      texts[#texts + 1] = l
    end
  end
  while #texts > 0 and texts[1] == "" do
    table.remove(texts, 1)
  end
  while #texts > 0 and texts[#texts] == "" do
    table.remove(texts)
  end
  local lines = {}
  for i, t in ipairs(texts) do
    local prefix
    if #texts == 1 then
      prefix = "▸ "
    elseif i == 1 then
      prefix = "╭ "
    elseif i == #texts then
      prefix = "╰ "
    else
      prefix = "│ "
    end
    if t == "" then
      lines[#lines + 1] = { { "│", "NvtourNoteBorder" } }
    elseif i == 1 and n then
      lines[#lines + 1] = { { prefix, "NvtourNoteBorder" }, { tostring(n) .. " ", "NvtourSign" .. R }, { t, "NvtourNote" } }
    else
      lines[#lines + 1] = { { prefix, "NvtourNoteBorder" }, { t, "NvtourNote" } }
    end
  end
  return lines
end

local function render_step(step, win)
  local buf = step.buf
  local ns = S.ns_steps
  local R = cap(step.role)
  for _, id in ipairs(step.extmark_ids or {}) do
    pcall(api.nvim_buf_del_extmark, buf, ns, id)
  end
  step.extmark_ids = {}
  local ids = step.extmark_ids
  local function add(row, opts)
    ids[#ids + 1] = api.nvim_buf_set_extmark(buf, ns, row, 0, opts)
  end
  for l = step.l1, step.l2 do
    local opts = { number_hl_group = "NvtourNumber" .. R, priority = 50 }
    if ROLES[step.role].tint then
      opts.line_hl_group = "NvtourLine" .. R
    end
    add(l - 1, opts)
  end
  add(step.l1 - 1, {
    sign_text = step.n >= 100 and "++" or tostring(step.n),
    sign_hl_group = "NvtourSign" .. R,
    priority = 100,
  })
  if step.note and step.note ~= "" then
    local width = 80
    if valid_win(win) then
      local info = vim.fn.getwininfo(win)[1]
      width = info.width - info.textoff
    end
    width = math.max(30, width - 4)
    local vl = note_chunks(step.note, width, step.n, R)
    if #vl > 0 then
      add(step.l1 - 1, { virt_lines = vl, virt_lines_above = true })
    end
  end
  if step.label and step.label ~= "" then
    add(step.l1 - 1, { virt_text = { { "  ← " .. step.label, "NvtourLabel" .. R } }, virt_text_pos = "eol" })
  end
end

---------------------------------------------------------------------------
-- Quickfix
---------------------------------------------------------------------------

local function qf_title()
  return "nvtour: " .. ((S.tour.title ~= "" and S.tour.title) or "Walkthrough")
end

local function qf_valid()
  return S.tour.qf_id ~= nil and vim.fn.getqflist({ id = S.tour.qf_id }).id ~= 0
end

local function new_qf()
  vim.fn.setqflist({}, " ", { title = qf_title(), items = {} })
  S.tour.qf_id = vim.fn.getqflist({ id = 0 }).id
end

local function update_qf(idx)
  if not qf_valid() then
    new_qf()
  end
  local items = {}
  for _, s in ipairs(S.tour.steps) do
    local text = s.label
    if not text or text == "" then
      text = (s.note or ""):match("[^\n]+") or (relpath(s.file) .. ":" .. s.l1)
    end
    items[#items + 1] = { filename = s.file, lnum = s.l1, end_lnum = s.l2, text = ("[%s] %s"):format(s.role, text) }
  end
  local what = { id = S.tour.qf_id, title = qf_title(), items = items }
  if idx then
    what.idx = idx
  end
  vim.fn.setqflist({}, "r", what)
end

---------------------------------------------------------------------------
-- Keymaps / tour lifecycle
---------------------------------------------------------------------------

local dispatch -- forward declaration

local function key_actions()
  -- Resolved through _G at call time, so the keys keep working after a runtime upgrade.
  local function run(cmd, args)
    local res = _G.nvtour.dispatch(cmd, args or {})
    if not res.ok then
      notify(res.error, vim.log.levels.WARN)
    elseif res.message then
      notify(res.message)
    end
  end
  return {
    next = function()
      run("next")
    end,
    prev = function()
      run("prev")
    end,
    first = function()
      run("first")
    end,
    last = function()
      run("last")
    end,
    panel = function()
      run("panel", { toggle = true })
    end,
    clear = function()
      run("clear")
    end,
  }
end

local function install_keymaps()
  if S.keymaps_installed then
    return
  end
  local cfg = vim.g.nvtour_keys
  if type(cfg) ~= "table" then
    cfg = {}
  end
  local actions = key_actions()
  for name, fn in pairs(actions) do
    local lhs = cfg[name]
    if lhs == nil then
      lhs = DEFAULT_KEYS[name]
    end
    if type(lhs) == "string" and lhs ~= "" then
      if vim.fn.maparg(lhs, "n") == "" then
        vim.keymap.set("n", lhs, fn, { desc = "nvtour " .. name })
        S.keymaps[lhs] = true
      else
        local msg = "key " .. lhs .. " is already mapped; skipping '" .. name .. "'"
        if not S.warned[lhs] then
          S.warned[lhs] = true
          notify("nvtour: " .. msg, vim.log.levels.WARN)
        end
        if W then
          W[#W + 1] = msg
        end
      end
    end
  end
  S.keymaps_installed = true
end

local function remove_keymaps()
  for lhs in pairs(S.keymaps) do
    pcall(vim.keymap.del, "n", lhs)
  end
  S.keymaps = {}
  S.keymaps_installed = false
end

local function clear_step_marks()
  for _, b in ipairs(api.nvim_list_bufs()) do
    if valid_buf(b) then
      api.nvim_buf_clear_namespace(b, S.ns_steps, 0, -1)
      api.nvim_buf_clear_namespace(b, S.ns_focus, 0, -1)
    end
  end
end

local function diff_close()
  local closed = 0
  for _, tab in ipairs(S.diff_tabs) do
    if valid_tab(tab) then
      for _, w in ipairs(api.nvim_tabpage_list_wins(tab)) do
        pcall(api.nvim_win_call, w, function()
          vim.cmd("diffoff")
        end)
      end
      if #api.nvim_list_tabpages() > 1 then
        local nr = api.nvim_tabpage_get_number(tab)
        if pcall(vim.cmd, "tabclose " .. nr) then
          closed = closed + 1
        end
      end
    end
  end
  S.diff_tabs = {}
  return closed
end

--- Remove everything nvtour created. keep_panel keeps the panel window open.
local function reset(keep_panel)
  for buf in pairs(S.focus) do
    unfocus_buf(buf)
  end
  S.focus = {}
  clear_step_marks()
  diff_close()
  remove_keymaps()
  local qf_id = S.tour.qf_id
  if qf_valid() then
    vim.fn.setqflist({}, "r", { id = qf_id, title = "nvtour (cleared)", items = {} })
    -- clear: when our list is the current one, make the user's previous list current again.
    if not keep_panel and vim.fn.getqflist({ id = 0 }).id == qf_id and vim.fn.getqflist({ nr = 0 }).nr > 1 then
      pcall(vim.cmd, "silent colder")
    end
  else
    qf_id = nil
  end
  -- The list is reused by the next tour, so the 10-deep quickfix stack does not fill up.
  S.tour = { title = "", steps = {}, current = 0, qf_id = qf_id }
  S.panel.text = {}
  if keep_panel then
    render_panel()
  else
    panel_close(false)
    if valid_buf(S.panel.buf) then
      pcall(api.nvim_buf_delete, S.panel.buf, { force = true })
    end
    S.panel.buf = nil
    S.panel.user_closed = false
    S.panel.line_map = {}
  end
  for _, b in ipairs(api.nvim_list_bufs()) do
    local keep = keep_panel and b == S.panel.buf
    if not keep and valid_buf(b) and api.nvim_buf_get_name(b):match("^nvtour://") then
      pcall(api.nvim_buf_delete, b, { force = true })
    end
  end
end

local function ensure_started()
  -- Reuse our list only while it is the current one; a list the user made since then stays on top.
  if not qf_valid() or vim.fn.getqflist({ id = 0 }).id ~= S.tour.qf_id then
    new_qf()
  elseif vim.fn.getqflist({ id = S.tour.qf_id, title = 0 }).title ~= qf_title() then
    update_qf()
  end
  install_keymaps()
end

---------------------------------------------------------------------------
-- Steps
---------------------------------------------------------------------------

local function step_result(step, extra)
  local r = {
    ok = true,
    n = step.n,
    total = #S.tour.steps,
    file = step.file,
    l1 = step.l1,
    l2 = step.l2,
    role = step.role,
    label = step.label,
  }
  for k, v in pairs(extra or {}) do
    r[k] = v
  end
  return r
end

local function jump(step)
  if not valid_buf(step.buf) or not api.nvim_buf_is_loaded(step.buf) then
    step.buf = load_buf(step.file)
    render_step(step, tour_win(step.buf))
  end
  local win = show_buf(tour_win(step.buf, true), step.buf)
  S.tour_win = win
  enter_win(win)
  api.nvim_win_set_cursor(win, { step.l1, 0 })
  api.nvim_win_call(win, function()
    vim.cmd("normal! zv")
    vim.cmd("normal! zz")
  end)
  S.tour.current = step.n
end

local function announce(step)
  local total = #S.tour.steps
  notify(("nvtour %d/%d: %s"):format(step.n, total, step.label or (relpath(step.file) .. ":" .. step.l1)))
end

step_goto = function(n)
  local steps = S.tour.steps
  if #steps == 0 then
    fail("no steps in tour; add one with 'nvtour step'", 6)
  end
  if n < 1 or n > #steps then
    fail(("no such step %d (tour has %d)"):format(n, #steps), 6)
  end
  local step = steps[n]
  jump(step)
  update_qf(n)
  render_panel()
  announce(step)
  return step_result(step)
end

local H = {}

H.start = function(a)
  reset(true)
  S.tour.title = a.title or ""
  S.workspace = a.workspace or S.workspace
  ensure_started()
  render_panel()
  return { ok = true, title = S.tour.title }
end

H.step = function(a)
  S.workspace = a.workspace or S.workspace
  local role = a.role or "info"
  if not ROLES[role] then
    fail("unknown role: " .. tostring(role), 2)
  end
  local buf = load_buf(a.file)
  local count = api.nvim_buf_line_count(buf)
  local l1, l2 = a.l1, a.l2 or a.l1
  if l1 < 1 or l2 < l1 then
    fail(("bad range %d-%d"):format(l1, l2), 6)
  end
  if l2 > count then
    fail(("range %d-%d is beyond end of file (%d lines): %s"):format(l1, l2, count, a.file), 6)
  end
  ensure_started()
  local steps = S.tour.steps
  -- Only the first step of a tour moves the view, so a finished tour starts at step 1.
  local do_jump = a.jump or (#steps == 0 and not a.no_jump)
  if #steps == 0 and vim.g.nvtour_auto_panel ~= false and not S.panel.user_closed then
    panel_open() -- before rendering, so the first note is wrapped to the final window width
  end
  local step = {
    n = #steps + 1,
    file = a.file,
    buf = buf,
    l1 = l1,
    l2 = l2,
    role = role,
    label = a.label,
    note = a.note,
    extmark_ids = {},
  }
  -- The step is added only after it rendered (and jumped); a failure leaves no half step behind.
  local prev_current = S.tour.current
  local ok, err = pcall(function()
    render_step(step, tour_win(buf))
    if do_jump then
      jump(step)
    end
  end)
  if not ok then
    for _, id in ipairs(step.extmark_ids) do
      pcall(api.nvim_buf_del_extmark, step.buf, S.ns_steps, id)
    end
    S.tour.current = prev_current
    error(err, 0)
  end
  steps[#steps + 1] = step
  if do_jump then
    update_qf(step.n)
  else
    update_qf()
  end
  render_panel()
  if do_jump then
    announce(step)
  end
  return step_result(step, { jumped = do_jump and true or false })
end

H["goto"] = function(a)
  return step_goto(a.n)
end

H["next"] = function()
  local n, total = S.tour.current, #S.tour.steps
  if total > 0 and n >= total and n > 0 then
    return step_result(S.tour.steps[n], { message = "already at last step" })
  end
  return step_goto(n + 1)
end

H.prev = function()
  local n = S.tour.current
  if n == 0 then
    return step_goto(1)
  end
  if n == 1 and #S.tour.steps > 0 then
    return step_result(S.tour.steps[1], { message = "already at first step" })
  end
  return step_goto(n - 1)
end

H.first = function()
  return step_goto(1)
end

H.last = function()
  return step_goto(#S.tour.steps)
end

---------------------------------------------------------------------------
-- Focus handlers
---------------------------------------------------------------------------

H.focus = function(a)
  S.workspace = a.workspace or S.workspace
  local buf = load_buf(a.file)
  local count = api.nvim_buf_line_count(buf)
  local ranges = {}
  for _, r in ipairs(a.ranges or {}) do
    if r[1] < 1 or r[2] < r[1] or r[2] > count then
      fail(("bad range %d-%d for %s (%d lines)"):format(r[1], r[2], a.file, count), 6)
    end
    ranges[#ranges + 1] = r
  end
  if #ranges == 0 then
    fail("focus needs at least one range", 2)
  end
  unfocus_buf(buf)
  local merged = merge_ranges(ranges, a.context or 2, count)
  S.focus[buf] = { ranges = merged, dim = a.dim and true or false, file = a.file }
  -- Before the first step focus shows the file; during a tour the view stays on the current step
  -- and folds are applied when the file is shown (by a step or by the user).
  local move = S.tour.current == 0
  local win = tour_win(buf, move)
  if move then
    win = show_buf(win, buf)
    S.tour_win = win
    enter_win(win)
  end
  local shown = win ~= nil and api.nvim_win_get_buf(win) == buf
  if a.dim then
    for _, g in ipairs(gaps_of(merged, count)) do
      for l = g[1], g[2] do
        api.nvim_buf_set_extmark(buf, S.ns_focus, l - 1, 0, { end_row = l, end_col = 0, hl_group = "NvtourDim", hl_eol = true, priority = 200 })
      end
    end
  elseif shown then
    apply_folds(buf, win)
  end
  if move then
    api.nvim_win_set_cursor(win, { merged[1][1], 0 })
    api.nvim_win_call(win, function()
      vim.cmd("normal! zv")
    end)
  end
  return {
    ok = true,
    file = a.file,
    ranges = merged,
    mode = a.dim and "dim" or "fold",
    deferred = (not a.dim and not shown) or nil,
  }
end

H.unfocus = function(a)
  local n = 0
  if a.file then
    for buf, f in pairs(S.focus) do
      if f.file == a.file then
        unfocus_buf(buf)
        n = n + 1
      end
    end
  else
    for buf in pairs(S.focus) do
      unfocus_buf(buf)
      n = n + 1
    end
  end
  return { ok = true, count = n }
end

---------------------------------------------------------------------------
-- Diff
---------------------------------------------------------------------------

local function unique_name(base, buf)
  local name = base
  for i = 2, 1000 do
    if vim.fn.bufexists(name) == 0 then
      api.nvim_buf_set_name(buf, name)
      return name
    end
    name = base .. "#" .. i
  end
  fail("too many nvtour diff buffers named " .. base, 5)
end

H.diff = function(a)
  local real = load_buf(a.file)
  local base = vim.fn.fnamemodify(a.file, ":t")
  local title = a.title or base
  vim.cmd("tabnew")
  local tab = api.nvim_get_current_tabpage()
  S.diff_tabs[#S.diff_tabs + 1] = tab
  local lwin = api.nvim_get_current_win()
  local blank = api.nvim_win_get_buf(lwin)
  local scratch = api.nvim_create_buf(false, true)
  unique_name("nvtour://diff/" .. title .. "/" .. base, scratch)
  vim.bo[scratch].buftype = "nofile"
  vim.bo[scratch].bufhidden = "wipe"
  vim.bo[scratch].swapfile = false
  api.nvim_buf_set_lines(scratch, 0, -1, false, a.lines or {})
  local ft = vim.filetype.match({ filename = a.file })
  if ft then
    vim.bo[scratch].filetype = ft
  end
  vim.bo[scratch].modifiable = false
  vim.bo[scratch].modified = false
  api.nvim_win_set_buf(lwin, scratch)
  if
    valid_buf(blank)
    and blank ~= scratch
    and api.nvim_buf_get_name(blank) == ""
    and not vim.bo[blank].modified
    and #vim.fn.win_findbuf(blank) == 0
  then
    pcall(api.nvim_buf_delete, blank, {})
  end
  api.nvim_win_call(lwin, function()
    vim.cmd("diffthis")
  end)
  local rwin = api.nvim_open_win(real, true, { split = "right", win = lwin })
  api.nvim_win_call(rwin, function()
    vim.cmd("diffthis")
  end)
  pcall(api.nvim_win_call, rwin, function()
    vim.cmd("normal! ]c")
  end)
  notify(("nvtour diff: %s ↔ %s"):format(title, base))
  return { ok = true, title = title, file = a.file, base = base }
end

H.diff_close = function()
  return { ok = true, closed = diff_close() }
end

---------------------------------------------------------------------------
-- Panel / clear / where
---------------------------------------------------------------------------

H.panel = function(a)
  S.workspace = a.workspace or S.workspace
  if a.clear then
    S.panel.text = {}
  end
  if a.text ~= nil then
    S.panel.text = split_lines(a.text)
  end
  ensure_panel_buf()
  local status
  if a.toggle then
    if panel_shown() then
      panel_close(true)
      status = "hidden"
    else
      S.panel.user_closed = false
      panel_open()
      status = "shown"
    end
  else
    S.panel.user_closed = false
    panel_open()
    status = a.clear and "cleared" or (a.text ~= nil and "updated" or "shown")
  end
  render_panel()
  return { ok = true, status = status }
end

H.clear = function(a)
  reset(false)
  local unlisted = 0
  for buf in pairs(S.added_bufs) do
    if not a.keep_buffers and valid_buf(buf) and not vim.bo[buf].modified and #vim.fn.win_findbuf(buf) == 0 then
      vim.bo[buf].buflisted = false
      unlisted = unlisted + 1
    end
  end
  S.added_bufs = {}
  return { ok = true, unlisted = unlisted }
end

local MAX_SEL_LINES, MAX_SEL_BYTES = 200, 65536
local SEL_KIND = { v = "char", V = "line", ["\22"] = "block" }

H.where = function()
  local cur = api.nvim_get_current_win()
  local win = cur
  if not file_win(cur) then
    -- The user is in a terminal, the panel or a special window: report the file window instead.
    local prev = vim.fn.win_getid(vim.fn.winnr("#"))
    win = (prev ~= 0 and file_win(prev) and prev) or tour_win() or cur
  end
  local buf = api.nvim_win_get_buf(win)
  local mode = api.nvim_get_mode().mode
  local live = win == cur and SEL_KIND[mode] ~= nil
  local res = {
    ok = true,
    file = api.nvim_buf_get_name(buf),
    bufnr = buf,
    winid = win,
    current_window = win == cur,
    buftype = vim.bo[buf].buftype,
    filetype = vim.bo[buf].filetype,
    modified = vim.bo[buf].modified,
    changedtick = vim.b[buf].changedtick,
    mode = mode,
  }
  api.nvim_win_call(win, function()
    local pos = api.nvim_win_get_cursor(win)
    res.line, res.col = pos[1], pos[2] + 1
    res.top, res.bottom = vim.fn.line("w0"), vim.fn.line("w$")
    res.cwd = vim.fn.getcwd(0)
    local p1, p2, kind
    if live then
      p1, p2, kind = vim.fn.getpos("v"), vim.fn.getpos("."), mode
    else
      p1, p2, kind = vim.fn.getpos("'<"), vim.fn.getpos("'>"), vim.fn.visualmode()
    end
    if not SEL_KIND[kind] or p1[2] < 1 or p2[2] < 1 then
      return
    end
    local l1, l2 = math.min(p1[2], p2[2]), math.max(p1[2], p2[2])
    local c1, c2
    if kind == "\22" then
      c1, c2 = math.min(p1[3], p2[3]), math.max(p1[3], p2[3])
    elseif kind == "v" then
      local a, b = p1, p2
      if p1[2] > p2[2] or (p1[2] == p2[2] and p1[3] > p2[3]) then
        a, b = p2, p1
      end
      c1, c2 = a[3], b[3]
    end
    local text
    if kind ~= "V" then
      local ok, region = pcall(vim.fn.getregion, p1, p2, { type = kind })
      text = ok and region or nil
    end
    text = text or api.nvim_buf_get_lines(buf, l1 - 1, math.min(l2, l1 + MAX_SEL_LINES), false)
    local out, bytes, truncated = {}, 0, false
    for i, line in ipairs(text) do
      if i > MAX_SEL_LINES or bytes + #line > MAX_SEL_BYTES then
        truncated = true
        break
      end
      out[#out + 1] = line
      bytes = bytes + #line + 1
    end
    res.selection = {
      live = live,
      kind = SEL_KIND[kind],
      l1 = l1,
      l2 = l2,
      c1 = c1,
      c2 = c2,
      text = out,
      truncated = truncated or (#text < l2 - l1 + 1),
    }
  end)
  return res
end

---------------------------------------------------------------------------
-- Dispatch
---------------------------------------------------------------------------

dispatch = function(cmd, args)
  local h = H[(cmd:gsub("-", "_"))]
  if not h then
    return { ok = false, error = "unknown command: " .. tostring(cmd), code = 2, pid = vim.fn.getpid() }
  end
  local outer = W
  W = {}
  local ok, res = pcall(h, args or {})
  local warnings = W
  W = outer
  if not ok then
    if type(res) == "table" and res.nvtour then
      res = { ok = false, error = res.msg, code = res.code }
    else
      res = { ok = false, error = tostring(res), code = 5 }
    end
  end
  if #warnings > 0 then
    res.warnings = warnings
  end
  res.pid = vim.fn.getpid()
  return res
end
M.dispatch = dispatch

-- Notes are wrapped to the window width: re-wrap them when a window that shows them is resized.
api.nvim_create_autocmd("WinResized", {
  group = aug,
  callback = function()
    if #S.tour.steps == 0 then
      return
    end
    local shown = {}
    for _, w in ipairs(vim.v.event.windows or {}) do
      if valid_win(w) then
        shown[api.nvim_win_get_buf(w)] = w
      end
    end
    for _, s in ipairs(S.tour.steps) do
      local w = shown[s.buf]
      if w and s.note and s.note ~= "" and valid_buf(s.buf) then
        pcall(render_step, s, w)
      end
    end
  end,
})

---------------------------------------------------------------------------
-- User commands
---------------------------------------------------------------------------

local function ucmd(name, fn, opts)
  api.nvim_create_user_command(name, function(o)
    local res = fn(o)
    if not res.ok then
      notify(res.error, vim.log.levels.WARN)
    elseif res.message then
      notify(res.message)
    end
  end, vim.tbl_extend("force", { force = true }, opts or {}))
end

ucmd("NvtourNext", function()
  return dispatch("next", {})
end)
ucmd("NvtourPrev", function()
  return dispatch("prev", {})
end)
ucmd("NvtourFirst", function()
  return dispatch("first", {})
end)
ucmd("NvtourLast", function()
  return dispatch("last", {})
end)
ucmd("NvtourGoto", function(o)
  return dispatch("goto", { n = tonumber(o.args) or 0 })
end, { nargs = 1 })
ucmd("NvtourPanel", function()
  return dispatch("panel", { toggle = true })
end)
ucmd("NvtourClear", function()
  return dispatch("clear", {})
end)

-- After an upgrade during a tour, re-install the keys so new or renamed actions appear.
if PREV_VERSION ~= nil and PREV_VERSION ~= M.VERSION then
  if S.keymaps_installed then
    remove_keymaps()
    install_keymaps()
  end
  if valid_buf(S.panel.buf) then
    map_panel_keys(S.panel.buf)
  end
end

_G.nvtour = M
return M

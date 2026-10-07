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
    ns_flash = api.nvim_create_namespace("nvtour_flash"),
    flash = { buf = nil, seq = 0 },
    winbars = {}, -- [winid] = the window's own local 'winbar', restored by clear
    panel = { buf = nil, win = nil, text = {}, user_closed = false, line_map = {} },
    focus = {},
    diff_tabs = {},
    keymaps = {},
    keys = {},
    keymaps_installed = false,
    workspace = nil,
    warned = {},
    tour_win = nil,
    pending_folds = {},
    added_bufs = {},
    refs = {}, -- [sha .. ":" .. file] = { buf, file, rel, ref, sha, lines }: buffers of steps at a git ref
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
-- fallback). The range is never given a background: the accent colours the bar in the sign column,
-- the line numbers, the label and the note number, so the code keeps its syntax colours.
local ROLES = {
  fault = { accent = "DiagnosticError", fallback = 0xfb4934 },
  flow = { accent = "DiagnosticInfo", fallback = 0x83a598 },
  fix = { accent = "DiagnosticOk", fallback = 0xb8bb26 },
  context = { accent = "DiagnosticWarn", fallback = 0xfabd2f },
  info = { accent = "DiagnosticHint", fallback = 0x8ec07c },
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
    set("NvtourNumber" .. R, { fg = accent, bold = true })
    set("NvtourSign" .. R, { fg = accent, bold = true })
    set("NvtourLabel" .. R, { fg = accent, bold = true, italic = true })
    set("NvtourFrame" .. R, { fg = blend(accent, bg, 0.75) })
    -- No fg, so the syntax colour of the --expect text stays.
    set("NvtourMark" .. R, { bold = true, underline = true, sp = accent })
  end
  local code = hl_attr("@markup.raw", "fg") or hl_attr("String", "fg") or fg
  set("NvtourNote", { fg = blend(fg, bg, 0.80), italic = true })
  set("NvtourNoteBg", { bg = blend(fg, bg, 0.07) }) -- the band behind all virtual lines of a step
  set("NvtourNoteCode", { fg = code, bg = blend(fg, bg, 0.16) })
  set("NvtourNoteBold", { fg = fg, bold = true, italic = true })
  set("NvtourNoteCollapsed", { fg = blend(fg, bg, 0.50), italic = true })
  set("NvtourNoteBorder", { fg = blend(fg, bg, 0.40) })
  set("NvtourVia", { fg = blend(fg, bg, 0.65) })
  set("NvtourViaLoc", { fg = blend(hl_attr("Directory", "fg") or fg, bg, 0.85) })
  set("NvtourVersion", { fg = hl_attr("Special", "fg") or fg, bold = true })
  set("NvtourSuggest", { fg = fg }) -- suggested code without a syntax group
  set("NvtourStrike", { strikethrough = true }) -- the lines that a suggestion replaces (no fg: syntax stays)
  set("NvtourDim", { fg = blend(fg, bg, 0.35) })
  set("NvtourFlash", { bg = blend(fg, bg, 0.25) })
  set("NvtourPanelCurrent", { bg = blend(fg, bg, 0.12), bold = true })
  set("NvtourPanelFile", { link = "Directory" })
  set("NvtourPanelProgress", { link = "Comment" })
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

--- True for the read-only buffer of a step at a git ref.
local function is_ref_buf(b)
  return vim.b[b].nvtour_ref ~= nil
end

--- A normal window that shows a file buffer (not a terminal, quickfix, help, or the panel). The
--- buffer of a step at a git ref counts as a file buffer.
local function file_win(w)
  if not is_normal_win(w) or w == S.panel.win then
    return false
  end
  local b = api.nvim_win_get_buf(w)
  return vim.bo[b].buftype == "" or is_ref_buf(b)
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

--- Split `s` into { text, kind } segments (kind nil, "code" or "bold"), without the markers.
--- A marker without a closing partner is plain text.
local function parse_inline(s)
  local segs, plain, i = {}, {}, 1
  local function flush()
    if #plain > 0 then
      segs[#segs + 1] = { table.concat(plain) }
      plain = {}
    end
  end
  while i <= #s do
    local close, kind, len
    if s:sub(i, i) == "`" then
      close, kind, len = s:find("`", i + 1, true), "code", 1
    elseif s:sub(i, i + 1) == "**" then
      close, kind, len = s:find("**", i + 2, true), "bold", 2
    end
    if close and close > i + len then
      flush()
      segs[#segs + 1] = { s:sub(i + len, close - 1), kind }
      i = close + len
    else
      plain[#plain + 1] = s:sub(i, i)
      i = i + 1
    end
  end
  flush()
  return segs
end

--- Plain text of `s` without the inline markers.
local function strip_inline(s)
  local out = {}
  for _, seg in ipairs(parse_inline(s)) do
    out[#out + 1] = seg[1]
  end
  return table.concat(out)
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

--- Name a scratch buffer `base`, or `base#2`, `base#3`, ... when that name exists.
local function unique_name(base, buf)
  local name = base
  for i = 2, 1000 do
    if vim.fn.bufexists(name) == 0 then
      api.nvim_buf_set_name(buf, name)
      return name
    end
    name = base .. "#" .. i
  end
  fail("too many nvtour buffers named " .. base, 5)
end

--- "path" for a step on the working tree, "path @ref" for a step at a git ref.
local function doc_name(s)
  return relpath(s.file) .. (s.ref and (" @" .. s.ref) or "")
end

--- True when two steps are in the same buffer: the same file, at the same commit (or both on the
--- working tree).
local function same_doc(a, b)
  return a.file == b.file and a.sha == b.sha
end

--- The step that the link of `step` comes from: the step given with --from (while it is still in
--- the tour), else the step before it.
local function source_of(step)
  local f = step.from
  if f and f ~= step and S.tour.steps[f.n] == f then
    return f
  end
  return S.tour.steps[step.n - 1]
end

--- True when `step` has a --from link to a step that is still in the tour.
local function explicit_from(step)
  local f = step.from
  return f ~= nil and f ~= step and S.tour.steps[f.n] == f
end

--- The --expect texts as a list without empty texts, or nil when there are none. Takes a list (from
--- the CLI) or one string (a step made by an older runtime).
local function expect_list(v)
  if v == nil then
    return nil
  end
  local out = {}
  for _, t in ipairs(type(v) == "table" and v or { v }) do
    if type(t) == "string" and t ~= "" then
      out[#out + 1] = t
    end
  end
  return #out > 0 and out or nil
end

local render_step -- forward declaration

--- The read-only buffer with the lines of `r` (an entry of S.refs), created when it does not exist.
--- A nofile buffer loses its lines when it is unloaded, so an unloaded one is made again; all steps
--- on it then point to the new buffer and are drawn again.
local function ensure_ref_buf(r)
  if valid_buf(r.buf) and api.nvim_buf_is_loaded(r.buf) then
    return r.buf
  end
  if valid_buf(r.buf) then
    pcall(api.nvim_buf_delete, r.buf, { force = true })
  end
  local buf = api.nvim_create_buf(true, true)
  unique_name(("nvtour://%s/%s"):format(r.ref, r.rel), buf)
  local bo = vim.bo[buf]
  bo.buftype = "nofile"
  bo.bufhidden = "hide" -- the extmarks of its steps must stay when it is not shown
  bo.swapfile = false
  api.nvim_buf_set_lines(buf, 0, -1, false, r.lines)
  -- Set before the filetype, so FileType autocmds (for example an LSP setup) can see it.
  vim.b[buf].nvtour_ref = { ref = r.ref, sha = r.sha, file = r.file }
  local ft = vim.filetype.match({ filename = r.file, contents = r.lines })
  if ft then
    bo.filetype = ft
  end
  bo.modifiable = false
  bo.readonly = true
  bo.modified = false
  r.buf = buf
  for _, s in ipairs(S.tour.steps) do
    if s.sha == r.sha and s.file == r.file and s.buf ~= buf then
      s.buf = buf
      s.extmark_ids = {}
      render_step(s, tour_win(buf))
    end
  end
  return buf
end

--- Buffer for a location `a` ({ file, ref, sha, rel, lines }): the file, or its version at a commit.
local function loc_buf(a)
  if not a.sha then
    return load_buf(a.file)
  end
  local key = a.sha .. ":" .. a.file
  local r = S.refs[key]
  if not r then
    r = { file = a.file, rel = a.rel or relpath(a.file), ref = a.ref or a.sha:sub(1, 12), sha = a.sha, lines = a.lines or {} }
    S.refs[key] = r
  end
  return ensure_ref_buf(r)
end

--- The buffer of `step`, loaded (and made again if the user wiped it).
local function step_buf(step)
  if step.sha then
    step.buf = loc_buf(step)
  elseif not valid_buf(step.buf) or not api.nvim_buf_is_loaded(step.buf) then
    step.buf = load_buf(step.file)
  end
  return step.buf
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

local KEY_ORDER = { "next", "prev", "first", "last", "panel", "clear" }

--- "]w next · [w prev · ..." for the keys that are installed.
local function keys_line()
  local parts = {}
  for _, name in ipairs(KEY_ORDER) do
    if S.keys[name] then
      parts[#parts + 1] = "`" .. S.keys[name] .. "` " .. name
    end
  end
  return table.concat(parts, " · ")
end

local ROLE_MARK = { fault = "✗", flow = "→", fix = "✓", context = "○", info = "•" }

local function fmt_range(s)
  return s.l1 == s.l2 and tostring(s.l1) or (s.l1 .. "-" .. s.l2)
end

--- Label of a step for lists: the label, else the first line of the note (cut to 60 cells).
local function step_title(s)
  if s.label and s.label ~= "" then
    return s.label
  end
  local first = strip_inline((s.note or ""):match("[^\n]*%S[^\n]*") or "")
  first = vim.trim(first)
  if vim.fn.strdisplaywidth(first) > 60 then
    first = vim.fn.strcharpart(first, 0, 59) .. "…"
  end
  return first
end

local function render_panel()
  if not valid_buf(S.panel.buf) then
    return
  end
  local buf = S.panel.buf
  local steps = S.tour.steps
  local lines = { "# " .. ((S.tour.title ~= "" and S.tour.title) or "Walkthrough"), "" }
  local map, hls = {}, {} -- hls: { line, start byte, end byte (-1 = eol), group }
  local cur_line
  local rw = 0
  for _, s in ipairs(steps) do
    rw = math.max(rw, #fmt_range(s))
  end
  -- Steps keep the tour order; a file name line starts each run of steps in the same file (for a
  -- step at a git ref: the same file at the same commit, "path @ref").
  -- A step with a --via or --from link gets a line "↓ [from N: ]via" before it (and before its
  -- file name line, so a link to another file is shown before the file changes).
  local prev
  for _, s in ipairs(steps) do
    local via = s.via and s.via ~= "" and s.via:gsub("%s+", " ") or nil
    if prev and (via or explicit_from(s)) then
      local src = source_of(s)
      local lead = "    ↓ "
      local from = explicit_from(s) and ("from " .. src.n) or ""
      lines[#lines + 1] = lead .. from .. ((from ~= "" and via) and ": " or "") .. (via or "")
      map[#lines] = s.n
      hls[#hls + 1] = { #lines, 0, #lead, "NvtourNoteBorder" }
      if from ~= "" then
        hls[#hls + 1] = { #lines, #lead, #lead + #from, "NvtourSign" .. cap(src.role) }
      end
      hls[#hls + 1] = { #lines, #lead + #from, -1, "NvtourVia" }
    end
    if not (prev and same_doc(s, prev)) then
      lines[#lines + 1] = doc_name(s)
      map[#lines] = s.n -- <CR> on the file name jumps to its first step
      hls[#hls + 1] = { #lines, 0, -1, "NvtourPanelFile" }
    end
    prev = s
    local mark = (s.n == S.tour.current) and "▶ " or "  "
    local head = ("%d. %s"):format(s.n, ROLE_MARK[s.role] or "•")
    local range = fmt_range(s)
    local line = mark .. head .. " " .. range
    local title = step_title(s)
    if title ~= "" then
      line = line .. (" "):rep(rw - #range + 2) .. title
    end
    lines[#lines + 1] = line
    map[#lines] = s.n
    hls[#hls + 1] = { #lines, #mark, #mark + #head, "NvtourSign" .. cap(s.role) }
    if s.n == S.tour.current then
      cur_line = #lines
    end
  end
  if #steps > 0 then
    local keys = keys_line()
    lines[#lines + 1] = ""
    lines[#lines + 1] = (keys ~= "" and (keys .. " · ") or "") .. "`<CR>` jump · `q` close"
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
  for _, h in ipairs(hls) do
    local e = h[3] >= 0 and h[3] or #lines[h[1]]
    api.nvim_buf_set_extmark(buf, S.ns_panel, h[1] - 1, h[2], { end_col = e, hl_group = h[4], priority = 150 })
  end
  if #steps > 0 then
    local progress = S.tour.current > 0 and ("%d/%d"):format(S.tour.current, #steps) or (#steps .. " step(s)")
    api.nvim_buf_set_extmark(buf, S.ns_panel, 0, 0, {
      virt_text = { { "  " .. progress, "NvtourPanelProgress" } },
      virt_text_pos = "eol",
    })
  end
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
  wo.breakindent = true
  wo.conceallevel = 2 -- hides the markdown markers (`code`, **bold**) of the summary and the footer
  wo.concealcursor = "nc"
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

-- Notes support two inline markdown forms: `code` and **bold**. The markers are not shown.
local INLINE_HL = { code = "NvtourNoteCode", bold = "NvtourNoteBold" }

--- Words of the { text, kind } segments `segs`. A word is a list of { text, kind } pieces
--- (`erase`() is one word of two pieces); `sep` is the kind of the space before it.
local function inline_words(segs)
  local words, cur, gap, gap_kind = {}, nil, true, nil
  for _, seg in ipairs(segs) do
    for space, word in seg[1]:gmatch("(%s*)(%S*)") do
      if space ~= "" then
        gap, gap_kind = true, seg[2]
      end
      if word ~= "" then
        if gap then
          cur = { sep = gap_kind }
          words[#words + 1] = cur
          gap = false
        end
        cur[#cur + 1] = { word, seg[2] }
      end
    end
  end
  return words
end

local function word_width(w)
  local n = 0
  for _, p in ipairs(w) do
    n = n + vim.fn.strdisplaywidth(p[1])
  end
  return n
end

--- Append text to a list of chunks; merges with the last chunk of the same kind.
local function add_chunk(line, text, kind)
  local last = line[#line]
  if last and last[2] == kind then
    last[1] = last[1] .. text
  else
    line[#line + 1] = { text, kind }
  end
end

--- Split a word that is wider than `width` display cells. The parts start new lines.
local function split_word(w, width)
  local parts, cur, cw = {}, { sep = w.sep }, 0
  for _, p in ipairs(w) do
    for i = 0, vim.fn.strchars(p[1]) - 1 do
      local ch = vim.fn.strcharpart(p[1], i, 1)
      local chw = vim.fn.strdisplaywidth(ch)
      if cw > 0 and cw + chw > width then
        parts[#parts + 1] = cur
        cur, cw = { newline = true }, 0
      end
      add_chunk(cur, ch, p[2])
      cw = cw + chw
    end
  end
  parts[#parts + 1] = cur
  parts[1].newline = true
  return parts
end

--- Wrap the { text, kind } segments of one paragraph to `width` display cells. Each line is a
--- list of { text, kind } chunks; an empty paragraph gives one empty line.
local function wrap_segs(segs, width)
  local out, cur, cw = {}, nil, 0
  local function push(w, ww)
    if cur and not w.newline and cw + 1 + ww <= width then
      add_chunk(cur, " ", w.sep)
      cw = cw + 1 + ww
    else
      if cur then
        out[#out + 1] = cur
      end
      cur, cw = {}, ww
    end
    for _, p in ipairs(w) do
      add_chunk(cur, p[1], p[2])
    end
  end
  for _, w in ipairs(inline_words(segs)) do
    local ww = word_width(w)
    if ww > width then
      for _, part in ipairs(split_word(w, width)) do
        push(part, word_width(part))
      end
    else
      push(w, ww)
    end
  end
  out[#out + 1] = cur or {}
  return out
end

local function wrap_text(s, width)
  return wrap_segs(parse_inline(s), width)
end

--- Lines of `note` wrapped to `width`, without leading and trailing blank lines.
local function note_lines(note, width)
  local lines = {}
  for _, para in ipairs(split_lines((note or ""):gsub("\t", "  "))) do
    for _, l in ipairs(wrap_text(para, width)) do
      lines[#lines + 1] = l
    end
  end
  while #lines > 0 and #lines[1] == 0 do
    table.remove(lines, 1)
  end
  while #lines > 0 and #lines[#lines] == 0 do
    table.remove(lines)
  end
  return lines
end

--- virt_lines for the note of the current step: a bordered block (`plain`: no border prefixes,
--- for the frame style, which draws its own border).
local function note_chunks(note, width, plain)
  local texts = note_lines(note, width)
  local lines = {}
  for i, t in ipairs(texts) do
    local prefix
    if plain then
      prefix = ""
    elseif #texts == 1 then
      prefix = "▸ "
    elseif i == 1 then
      prefix = "╭ "
    elseif i == #texts then
      prefix = "╰ "
    else
      prefix = "│ "
    end
    if #t == 0 then
      lines[#lines + 1] = { { plain and "" or "│", "NvtourNoteBorder" } }
    else
      local line = { { prefix, "NvtourNoteBorder" } }
      for _, c in ipairs(t) do
        line[#line + 1] = { c[1], INLINE_HL[c[2]] or "NvtourNote" }
      end
      lines[#lines + 1] = line
    end
  end
  return lines
end

--- One virt_line for the note of a step that is not current: the first line, cut with "…".
local function collapsed_chunks(note, width)
  local texts = note_lines(note, width - 2)
  if #texts == 0 then
    return {}
  end
  local line = { { "╶ ", "NvtourNoteBorder" } }
  for _, c in ipairs(texts[1]) do
    line[#line + 1] = { c[1], "NvtourNoteCollapsed" }
  end
  if #texts > 1 then
    line[#line + 1] = { " …", "NvtourNoteCollapsed" }
  end
  return { line }
end

---------------------------------------------------------------------------
-- Links between steps
---------------------------------------------------------------------------

--- Location of step `s` as seen from step `here`: "line 412" in the same buffer, else "a.cpp:412"
--- ("a.cpp:412 @ref" at a git ref, "a.cpp:412 · working tree" when `here` is the same file at a ref).
local function link_loc(s, here)
  if here and same_doc(s, here) then
    return "line " .. s.l1
  end
  local loc = relpath(s.file) .. ":" .. s.l1
  if s.ref then
    return loc .. " @" .. s.ref
  end
  return (here and here.sha and here.file == s.file) and (loc .. " · working tree") or loc
end

--- Segments of the "◇" line of `step`: its file and version. The commit is added when the ref is not
--- itself the commit ("@origin/master (1a2b3c4d5e6f)"); "working tree" when the step before was at
--- a git ref.
local function version_segs(step, prev)
  local segs = { { relpath(step.file), "NvtourViaLoc" } }
  if step.ref then
    segs[#segs + 1] = { " @" .. step.ref, "NvtourVersion" }
    if step.sha:sub(1, #step.ref) ~= step.ref then
      segs[#segs + 1] = { " (" .. step.sha:sub(1, 12) .. ")", "NvtourVia" }
    end
  elseif prev.sha then
    segs[#segs + 1] = { " · ", "NvtourNoteBorder" }
    segs[#segs + 1] = { "working tree", "NvtourVersion" }
  end
  return segs
end

--- Segments of a --via text: `code` and **bold** as in notes, the rest in NvtourVia.
local function via_segs(via)
  local out = {}
  for _, seg in ipairs(parse_inline((via or ""):gsub("%s+", " "))) do
    out[#out + 1] = { seg[1], seg[2] or "NvtourVia" }
  end
  return out
end

--- virt_lines of one link line: `prefix` on the first line, the segments wrapped to `width`.
local function link_lines(prefix, segs, width)
  local out = {}
  for i, t in ipairs(wrap_segs(segs, width - 2)) do
    local line = { { i == 1 and prefix or "  ", "NvtourNoteBorder" } }
    for _, c in ipairs(t) do
      line[#line + 1] = { c[1], INLINE_HL[c[2]] or c[2] }
    end
    out[#out + 1] = line
  end
  return out
end

--- Line above the note of the current step, when the file or the version is different from the
--- step before it in the tour (the code the user saw last):
---   ◇ a.cpp @origin/master (1a2b3c4d5e6f)
--- The --via text is not repeated here: the "next" line of the step before shows it.
local function arrival_lines(step, width)
  local prev = S.tour.steps[step.n - 1]
  if not prev or prev == step or same_doc(prev, step) then
    return {}
  end
  return link_lines("◇ ", version_segs(step, prev), width)
end

--- Line below the range of the current step, about the next step: shown when the next step is in
--- another file or version, or has a --via link. "(from N)" when its link comes from another step.
---   → next 4 · b.cpp:88: <via of step 4>
local function next_lines(step, width)
  local nxt = S.tour.steps[step.n + 1]
  if not nxt or nxt == step then
    return {}
  end
  local via = nxt.via and nxt.via ~= "" and nxt.via or nil
  if not via and same_doc(nxt, step) then
    return {}
  end
  local segs = {
    { "next " .. nxt.n, "NvtourSign" .. cap(nxt.role) },
    { " · ", "NvtourNoteBorder" },
    { link_loc(nxt, step), "NvtourViaLoc" },
  }
  local src = source_of(nxt)
  if via and src ~= step then
    segs[#segs + 1] = { " (from ", "NvtourVia" }
    segs[#segs + 1] = { tostring(src.n), "NvtourSign" .. cap(src.role) }
    segs[#segs + 1] = { ")", "NvtourVia" }
  end
  if via then
    segs[#segs + 1] = { ": ", "NvtourVia" }
    vim.list_extend(segs, via_segs(via))
  elseif nxt.label and nxt.label ~= "" then
    segs[#segs + 1] = { " " .. nxt.label, "NvtourVia" }
  end
  return link_lines("→ ", segs, width)
end

-- The bar of the current step, in the second cell of the sign column (next to the line numbers).
-- The step number is not put in the sign column: "10" would run into the line number ("10348").
local BAR_SIGN = " ▎"

--- Window column (0-based) of the bar of BAR_SIGN: the second cell of a 2-cell sign column just
--- before the line numbers. nil when it cannot be known ('statuscolumn', signs in the number
--- column or no sign column).
local function bar_col(win, textoff)
  local wo = vim.wo[win]
  if wo.statuscolumn ~= "" or wo.signcolumn == "no" or wo.signcolumn == "number" then
    return nil
  end
  local numw = 0
  if wo.number or wo.relativenumber then
    -- As number_width() in nvim: only 'relativenumber' counts the window height, else the lines.
    local n = (wo.relativenumber and not wo.number) and api.nvim_win_get_height(win)
      or api.nvim_buf_line_count(api.nvim_win_get_buf(win))
    numw = math.max(wo.numberwidth, #tostring(n) + 1)
  end
  local col = textoff - numw - 1
  return col >= 1 and col or nil
end

---------------------------------------------------------------------------
-- Suggested code (--suggest)
---------------------------------------------------------------------------

--- `line` with its tabs expanded to spaces for 'tabstop' `ts`.
local function expand_tabs(line, ts)
  if not line:find("\t", 1, true) then
    return line
  end
  local out, col = {}, 0
  for ch in line:gmatch("[%z\1-\127\194-\244][\128-\191]*") do
    if ch == "\t" then
      local n = ts - col % ts
      out[#out + 1] = (" "):rep(n)
      col = col + n
    else
      out[#out + 1] = ch
      col = col + vim.fn.strdisplaywidth(ch)
    end
  end
  return table.concat(out)
end

--- `lines` of code as virt_text chunks with the treesitter highlight groups of the language of
--- `buf`, one chunk list per line. Without a parser or a highlights query the text is NvtourSuggest.
local function code_chunks(lines, buf)
  local groups = {} -- [row (0-based)][byte (0-based)] = highlight group
  local text = table.concat(lines, "\n")
  local ok, lang = pcall(vim.treesitter.language.get_lang, vim.bo[buf].filetype)
  if ok and lang and text ~= "" then
    pcall(function()
      local query = vim.treesitter.query.get(lang, "highlights")
      if not query then
        return
      end
      local tree = vim.treesitter.get_string_parser(text, lang):parse()[1]
      for id, node in query:iter_captures(tree:root(), text, 0, -1) do
        local name = query.captures[id]
        if not name:match("^_") and name ~= "spell" and name ~= "nospell" and name ~= "conceal" then
          local group = "@" .. name .. "." .. lang -- falls back to "@" .. name, as in the buffer
          local sr, sc, er, ec = node:range()
          for r = sr, er do
            local row = groups[r] or {}
            groups[r] = row
            for b = (r == sr and sc or 0), (r == er and ec or #(lines[r + 1] or "")) - 1 do
              row[b] = group -- inner nodes come later and win, as in the treesitter highlighter
            end
          end
        end
      end
    end)
  end
  local out = {}
  for i, line in ipairs(lines) do
    local row, chunks, from = groups[i - 1] or {}, {}, 1
    for b = 1, #line do
      local g = row[b - 1] or "NvtourSuggest"
      if b == #line or (row[b] or "NvtourSuggest") ~= g then
        chunks[#chunks + 1] = { line:sub(from, b), g }
        from = b + 1
      end
    end
    out[i] = chunks
  end
  return out
end

--- Cut `chunks` to `width` display cells; a cut line ends with "…". Returns the chunks and their width.
local function clip_chunks(chunks, width)
  local out, w = {}, 0
  for _, c in ipairs(chunks) do
    local cw = vim.fn.strdisplaywidth(c[1])
    if w + cw > width then
      local room, text = width - w - 1, ""
      for i = 0, vim.fn.strchars(c[1]) - 1 do
        local ch = vim.fn.strcharpart(c[1], i, 1)
        local chw = vim.fn.strdisplaywidth(ch)
        if chw > room then
          break
        end
        text, room = text .. ch, room - chw
      end
      out[#out + 1] = { text .. "…", c[2] }
      return out, width - room
    end
    out[#out + 1] = c
    w = w + cw
  end
  return out, w
end

--- The suggested lines of `step` as chunk lines: tabs expanded, syntax colours, "+ " in front.
--- `shift` cells of the common indentation are removed (at most): the cells that the frame and the
--- "+ " take before the code, so the new code is in the column of the code that it replaces.
local function suggest_lines(step, shift)
  local ts = vim.bo[step.buf].tabstop
  local lines, common = {}, shift
  for _, l in ipairs(step.suggest) do
    lines[#lines + 1] = expand_tabs(l, ts)
    if lines[#lines]:find("%S") then
      common = math.min(common, #lines[#lines]:match("^ *"))
    end
  end
  for i, l in ipairs(lines) do
    lines[i] = l:sub(math.min(common, #l:match("^ *")) + 1)
  end
  local out = {}
  for i, chunks in ipairs(code_chunks(lines, step.buf)) do
    out[i] = vim.list_extend({ { "+ ", "NvtourSign" .. cap(step.role) } }, chunks)
  end
  return out
end

--- The style of the virtual lines of a step: "frame" (the default) or "band" (vim.g.nvtour_note_style).
local function note_style()
  return vim.g.nvtour_note_style == "band" and "band" or "frame"
end

--- Chunks of the gutter in front of a virtual line: blank cells up to the code column (`off`), with
--- the role bar in the column of the range bar when `col` is set. `hl` wraps a group (for the band).
local function gutter_chunks(off, col, role, hl)
  if col then
    local g = { { (" "):rep(col), hl("NvtourNoteBorder") }, { "▎", hl("NvtourSign" .. cap(role)) } }
    if off - col - 1 > 0 then
      g[3] = { (" "):rep(off - col - 1), hl("NvtourNoteBorder") }
    end
    return g
  end
  return off > 0 and { { (" "):rep(off), hl("NvtourNoteBorder") } } or {}
end

local function plain_hl(g)
  return g
end

--- Make the virtual lines of a step stand out from the code with a border, without a background.
--- `kind` is "block" (the links and the note of the current step: a frame ╭─╮ │ │ ╰─╯ in the role
--- colour), "suggest" (the suggested code: the same frame with the title "suggested"), "next" (the
--- line below the range: a "╶─" lead) or "collapsed" (a one-line note or the suggestion line of
--- another step: a grey "╶─" lead). Like the band, the lines start in the gutter.
local function frame(lines, win, role, current, kind)
  if not valid_win(win) then
    return { virt_lines = lines } -- drawn again in the window when the buffer is shown (BufWinEnter)
  end
  local info = vim.fn.getwininfo(win)[1]
  local off, full = info.textoff, info.width
  local col = current and bar_col(win, off) or nil
  local B = current and ("NvtourFrame" .. cap(role)) or "NvtourNoteBorder"
  local inner = math.max(10, full - off) -- cells from the code column to the right edge
  local function row(chunks)
    return vim.list_extend(gutter_chunks(off, col, role, plain_hl), chunks)
  end
  local out = {}
  if kind == "block" or kind == "suggest" then
    -- The suggestion has a title in its top border: "╭ suggested ───╮".
    local title = kind == "suggest" and " suggested " or ""
    out[1] = row({
      { "╭", B },
      { title, "NvtourLabel" .. cap(role) },
      { ("─"):rep(math.max(0, inner - 2 - vim.fn.strdisplaywidth(title))) .. "╮", B },
    })
    for _, line in ipairs(lines) do
      local chunks, w = clip_chunks(line, inner - 4) -- code does not wrap: a long line is cut
      local cells = { { "│ ", B } }
      vim.list_extend(cells, chunks)
      cells[#cells + 1] = { (" "):rep(math.max(0, inner - 4 - w)) .. " │", B }
      out[#out + 1] = row(cells)
    end
    out[#out + 1] = row({ { "╰" .. ("─"):rep(inner - 2) .. "╯", B } })
  else
    for i, line in ipairs(lines) do
      if kind == "collapsed" and line[1] and line[1][1] == "╶ " then
        table.remove(line, 1) -- the frame lead replaces the "╶ " prefix
      end
      local chunks = { { i == 1 and "╶─ " or "   ", B } }
      out[#out + 1] = row(vim.list_extend(chunks, line))
    end
  end
  return { virt_lines = out, virt_lines_leftcol = true }
end

--- Make the virtual lines of a step stand out from the code: a band in NvtourNoteBg over the full
--- window width. The lines start in the gutter (virt_lines_leftcol) with blank cells up to the code
--- column; the current step has its role bar in the column of the range bar, so one bar goes from
--- the note through the range. Returns the extmark options for the lines.
local function band(lines, win, role, current)
  local opts = { virt_lines = lines }
  if not valid_win(win) then
    -- Drawn again in the window when the buffer is shown (BufWinEnter).
    for _, line in ipairs(lines) do
      for _, c in ipairs(line) do
        c[2] = { "NvtourNoteBg", c[2] }
      end
    end
    return opts
  end
  local info = vim.fn.getwininfo(win)[1]
  local off, full = info.textoff, info.width
  local col = current and bar_col(win, off) or nil
  opts.virt_lines_leftcol = true
  for i, line in ipairs(lines) do
    local w = off
    for _, c in ipairs(line) do
      c[2] = { "NvtourNoteBg", c[2] }
      w = w + vim.fn.strdisplaywidth(c[1])
    end
    if w < full then
      line[#line + 1] = { (" "):rep(full - w), "NvtourNoteBg" }
    end
    lines[i] = vim.list_extend(gutter_chunks(off, col, role, function(g)
      return g == "NvtourNoteBorder" and "NvtourNoteBg" or { "NvtourNoteBg", g }
    end), line)
  end
  return opts
end

--- Draw step marks. Only the current step gets the bar, the full note and the --expect marks; the
--- other steps keep the line numbers, the "← N label" marker and a one-line note.
render_step = function(step, win)
  local buf = step.buf
  local ns = S.ns_steps
  local R = cap(step.role)
  local current = step.n == S.tour.current
  for _, id in ipairs(step.extmark_ids or {}) do
    pcall(api.nvim_buf_del_extmark, buf, ns, id)
  end
  step.extmark_ids = {}
  local ids = step.extmark_ids
  local function add(row, opts, col)
    ids[#ids + 1] = api.nvim_buf_set_extmark(buf, ns, row, col or 0, opts)
  end
  for l = step.l1, step.l2 do
    add(l - 1, {
      number_hl_group = "NvtourNumber" .. R,
      sign_text = current and BAR_SIGN or nil,
      sign_hl_group = "NvtourSign" .. R,
      priority = 100,
    })
  end
  local width = 80
  step.drawn_win = valid_win(win) and win or nil
  if valid_win(win) then
    local info = vim.fn.getwininfo(win)[1]
    width = info.width - info.textoff
  end
  width = math.max(30, width - 4)
  local framed = note_style() == "frame"
  local above = current and arrival_lines(step, width) or {}
  if step.note and step.note ~= "" then
    vim.list_extend(above, current and note_chunks(step.note, width, framed) or collapsed_chunks(step.note, width))
  end
  if #above > 0 then
    local opts = framed and frame(above, win, step.role, current, current and "block" or "collapsed")
      or band(above, win, step.role, current)
    add(step.l1 - 1, vim.tbl_extend("force", opts, { virt_lines_above = true }))
  end
  -- Below the range: the suggested code, then the "next" line. Other steps: one suggestion line.
  local below = { virt_lines = {} }
  local function put(opts)
    vim.list_extend(below.virt_lines, opts.virt_lines)
    below.virt_lines_leftcol = below.virt_lines_leftcol or opts.virt_lines_leftcol
  end
  if step.suggest and current then
    if framed then
      put(frame(suggest_lines(step, 4), win, step.role, current, "suggest")) -- "│ " and "+ "
    else
      put(band(vim.list_extend({ { { "suggested", "NvtourLabel" .. R } } }, suggest_lines(step, 2)), win, step.role, current))
    end
  elseif step.suggest then
    local n = #step.suggest
    local line = { { ("suggested: %d line%s"):format(n, n == 1 and "" or "s"), "NvtourNoteCollapsed" } }
    put(framed and frame({ line }, win, step.role, current, "collapsed") or band({ line }, win, step.role, current))
  end
  local nxt = current and next_lines(step, width) or {}
  if #nxt > 0 then
    put(framed and frame(nxt, win, step.role, current, "next") or band(nxt, win, step.role, current))
  end
  -- nvim_win_text_height counts the lines below l2 with the next row: scroll_to adds them.
  step.below = current and #below.virt_lines or 0
  if #below.virt_lines > 0 then
    add(step.l2 - 1, below)
  end
  if step.suggest and current then
    -- The lines that the suggestion replaces are struck through (from the first non-blank character).
    for i, line in ipairs(api.nvim_buf_get_lines(buf, step.l1 - 1, step.l2, false)) do
      local first = line:find("%S")
      if first then
        add(step.l1 + i - 2, { end_col = #line, hl_group = "NvtourStrike", priority = 140 }, first - 1)
      end
    end
  end
  local expects = current and expect_list(step.expect) or {}
  if #expects > 0 then
    local lines = api.nvim_buf_get_lines(buf, step.l1 - 1, step.l2, false)
    for _, text in ipairs(expects) do
      for i, line in ipairs(lines) do
        local from = 1
        while true do
          local s, e = line:find(text, from, true)
          if not s then
            break
          end
          add(step.l1 + i - 2, { end_col = e, hl_group = "NvtourMark" .. R, priority = 150 }, s - 1)
          from = e + 1
        end
      end
    end
  end
  local label = (step.label and step.label ~= "") and (" " .. step.label) or ""
  add(step.l1 - 1, {
    virt_text = { { "  ← ", "NvtourLabel" .. R }, { tostring(step.n), "NvtourSign" .. R }, { label, "NvtourLabel" .. R } },
    virt_text_pos = "eol",
  })
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
    local text = step_title(s)
    if text == "" then
      text = relpath(s.file) .. ":" .. s.l1
    end
    local item = { lnum = s.l1, end_lnum = s.l2, text = ("[%s] %s"):format(s.role, text) }
    if s.sha then
      item.bufnr = step_buf(s) -- the read-only version, not the file on disk
    else
      item.filename = s.file
    end
    items[#items + 1] = item
  end
  local what = { id = S.tour.qf_id, title = qf_title(), items = items }
  if idx then
    what.idx = idx
  end
  vim.fn.setqflist({}, "r", what)
end

---------------------------------------------------------------------------
-- Winbar, flash, scrolling
---------------------------------------------------------------------------

-- The tour window shows the position in its winbar. The expression is evaluated on redraw, so
-- it is set once per window and buffer ('winbar' is reset when the window shows another buffer).
local WINBAR = "%{%v:lua.nvtour.winbar()%}"

local function sl_escape(s)
  return (s:gsub("%%", "%%%%"))
end

--- Winbar text: "nvtour 2/5 fault · label" and, on the right, where the next key goes.
function M.winbar()
  local steps, n = S.tour.steps, S.tour.current
  local step = steps[n]
  if not step then
    return #steps > 0 and (" nvtour · %d step(s)"):format(#steps) or ""
  end
  local pos = ("%d/%d %s"):format(n, #steps, step.role)
  local at = step.ref and (" @" .. step.ref) or ""
  local label = (step.label and step.label ~= "") and (" · " .. step.label) or ""
  -- In a %{} item the window of the bar is the current window (g:statusline_winid is not set).
  local width = api.nvim_win_get_width(api.nvim_get_current_win())
  local max_label = width - vim.fn.strdisplaywidth(" nvtour " .. pos .. at) - 1
  if vim.fn.strdisplaywidth(label) > max_label then
    label = max_label > 4 and (vim.fn.strcharpart(label, 0, max_label - 1) .. "…") or ""
  end
  -- Right side, longest first: the first one that fits is shown.
  local nxt = steps[n + 1]
  local rights
  if nxt then
    local key = "next" .. (S.keys.next and (" " .. S.keys.next) or "") .. ": "
    local nat = nxt.ref and (" @" .. nxt.ref) or ""
    local same = same_doc(nxt, step)
    local loc = same and ("line " .. nxt.l1) or (relpath(nxt.file) .. ":" .. nxt.l1 .. nat)
    local short = same and loc or (vim.fn.fnamemodify(nxt.file, ":t") .. ":" .. nxt.l1 .. nat)
    rights = { key .. loc, key .. short, "" }
    if nxt.label and nxt.label ~= "" then
      table.insert(rights, 1, key .. short .. " " .. nxt.label)
      table.insert(rights, 1, key .. loc .. " " .. nxt.label)
    end
  else
    rights = { "last step" .. (S.keys.first and (" · " .. S.keys.first .. " first") or ""), "last step", "" }
  end
  local room = width - vim.fn.strdisplaywidth(" nvtour " .. pos .. at .. label) - 3
  local right = ""
  for _, r in ipairs(rights) do
    if vim.fn.strdisplaywidth(r) <= room then
      right = r
      break
    end
  end
  local out = (" nvtour %%#NvtourSign%s#%s%%*%s%s"):format(cap(step.role), pos, sl_escape(at), sl_escape(label))
  return out .. "%=" .. sl_escape(right) .. (right ~= "" and " " or "")
end

--- Show the tour winbar in `win`, unless it has a winbar of its own (from the user or a plugin)
--- or vim.g.nvtour_winbar is false.
local function set_winbar(win)
  if vim.g.nvtour_winbar == false or not valid_win(win) then
    return
  end
  local own = api.nvim_get_option_value("winbar", { win = win })
  if own ~= "" and own ~= WINBAR then
    return
  end
  if S.winbars[win] == nil then
    local loc = api.nvim_get_option_value("winbar", { scope = "local", win = win })
    S.winbars[win] = loc == WINBAR and "" or loc
  end
  api.nvim_set_option_value("winbar", WINBAR, { scope = "local", win = win })
end

local function clear_winbars()
  for win, saved in pairs(S.winbars) do
    if valid_win(win) and api.nvim_get_option_value("winbar", { scope = "local", win = win }) == WINBAR then
      api.nvim_set_option_value("winbar", saved, { scope = "local", win = win })
    end
  end
  S.winbars = {}
end

-- A buffer that comes back into a window gets the window options it had there, so a stale tour
-- winbar can return after clear (or in a window that shows no tour any more).
api.nvim_create_autocmd("BufWinEnter", {
  group = aug,
  callback = function()
    local win = api.nvim_get_current_win()
    if S.winbars[win] == nil and api.nvim_get_option_value("winbar", { scope = "local", win = win }) == WINBAR then
      api.nvim_set_option_value("winbar", "", { scope = "local", win = win })
    end
  end,
})

--- Highlight the range of `step` for vim.g.nvtour_flash ms (default 300, 0 = off), so the eye
--- finds it after a jump.
local function flash(step)
  local ms = tonumber(vim.g.nvtour_flash) or 300
  if valid_buf(S.flash.buf) then
    api.nvim_buf_clear_namespace(S.flash.buf, S.ns_flash, 0, -1)
  end
  if ms <= 0 then
    return
  end
  local buf = step.buf
  local last = api.nvim_buf_get_lines(buf, step.l2 - 1, step.l2, false)[1] or ""
  api.nvim_buf_set_extmark(buf, S.ns_flash, step.l1 - 1, 0, {
    end_row = step.l2 - 1,
    end_col = #last,
    hl_group = "NvtourFlash",
    hl_eol = true,
    priority = 250,
  })
  S.flash.seq = S.flash.seq + 1
  S.flash.buf = buf
  local seq = S.flash.seq
  vim.defer_fn(function()
    if S.flash.seq == seq and valid_buf(buf) then
      api.nvim_buf_clear_namespace(buf, S.ns_flash, 0, -1)
    end
  end, ms)
end

--- Screen rows of buffer row `row` (0-based) with the virtual lines above it.
local function rows_of(win, row)
  local ok, h = pcall(api.nvim_win_text_height, win, { start_row = row, end_row = row })
  if ok then
    return h.all, h.fill
  end
  return 1, 0
end

--- Put the cursor on l1 and scroll so the note and the range are in view: the block is centred
--- when it fits in the window, else the note starts at the top. Unlike `zz`, this keeps the note
--- of a step on line 1 (virtual lines above the top line need 'topfill') and the end of a long range
--- in view.
local function scroll_to(step, win)
  api.nvim_win_call(win, function()
    api.nvim_win_set_cursor(win, { step.l1, 0 })
    vim.cmd("normal! zv")
    local height = vim.fn.getwininfo(win)[1].height
    local ok, th = pcall(api.nvim_win_text_height, win, { start_row = step.l1 - 1, end_row = step.l2 - 1 })
    local block = (ok and th.all or (step.l2 - step.l1 + 1)) + (step.below or 0)
    local top, used = step.l1, 0
    local want = block < height and math.floor((height - block) / 2) or 0
    while top > 1 do
      local prev, h = top - 1, nil
      local fold = vim.fn.foldclosed(prev)
      if fold ~= -1 then
        prev, h = fold, 1
      else
        h = rows_of(win, prev - 1)
      end
      if used + h > want then
        break
      end
      top, used = prev, used + h
    end
    -- Do not scroll past the end of the file: the last line stays at the bottom of the window.
    local count = api.nvim_buf_line_count(step.buf)
    local ok2, rest = pcall(api.nvim_win_text_height, win, { start_row = top - 1, end_row = count - 1 })
    local below = ok2 and rest.all or height
    while top > 1 and below < height do
      local prev = top - 1
      local fold = vim.fn.foldclosed(prev)
      local h = fold ~= -1 and 1 or rows_of(win, prev - 1)
      if below + h > height then
        break
      end
      top, below = fold ~= -1 and fold or prev, below + h
    end
    local _, fill = rows_of(win, top - 1)
    vim.fn.winrestview({ topline = top, topfill = fill })
  end)
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
        S.keys[name] = lhs
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
  S.keys = {}
  S.keymaps_installed = false
end

local function clear_step_marks()
  for _, b in ipairs(api.nvim_list_bufs()) do
    if valid_buf(b) then
      api.nvim_buf_clear_namespace(b, S.ns_steps, 0, -1)
      api.nvim_buf_clear_namespace(b, S.ns_focus, 0, -1)
      api.nvim_buf_clear_namespace(b, S.ns_flash, 0, -1)
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
  clear_winbars()
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
  S.refs = {} -- their buffers are deleted below with the other nvtour:// buffers
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

--- Installed keys by action; an empty map is sent as {} (not []).
local function installed_keys()
  return next(S.keys) and S.keys or vim.empty_dict()
end

local function step_text(step)
  if not valid_buf(step.buf) or not api.nvim_buf_is_loaded(step.buf) then
    return nil
  end
  local line = api.nvim_buf_get_lines(step.buf, step.l1 - 1, step.l1, false)[1] or ""
  return vim.fn.strcharpart(line, 0, 100)
end

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
    via = step.via,
    from = explicit_from(step) and step.from.n or nil,
    ref = step.ref,
    sha = step.sha,
    text = step_text(step),
    keys = installed_keys(),
  }
  for k, v in pairs(extra or {}) do
    r[k] = v
  end
  return r
end

local function jump(step)
  step_buf(step)
  local win = tour_win(step.buf, true)
  -- The position before the jump goes into the jumplist of the window, so <C-o> goes back to it.
  pcall(api.nvim_win_call, win, function()
    vim.cmd("normal! m'")
  end)
  win = show_buf(win, step.buf)
  S.tour_win = win
  enter_win(win)
  local old = S.tour.steps[S.tour.current]
  S.tour.current = step.n
  if old and old ~= step and valid_buf(old.buf) and api.nvim_buf_is_loaded(old.buf) then
    render_step(old, tour_win(old.buf))
  end
  set_winbar(win) -- before the note is wrapped and the view is computed: it takes a row
  render_step(step, win)
  scroll_to(step, win)
  flash(step)
end

local function announce(step)
  local total = #S.tour.steps
  local at = step.ref and (" @" .. step.ref) or ""
  notify(("nvtour %d/%d: %s"):format(step.n, total, step.label or (relpath(step.file) .. ":" .. step.l1 .. at)))
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
  return { ok = true, title = S.tour.title, keys = installed_keys() }
end

--- Renumber the steps and render all of them again (after an insert, edit or remove).
local function rerender_all()
  for _, b in ipairs(api.nvim_list_bufs()) do
    if valid_buf(b) then
      api.nvim_buf_clear_namespace(b, S.ns_steps, 0, -1)
    end
  end
  for i, s in ipairs(S.tour.steps) do
    s.n = i
  end
  for _, s in ipairs(S.tour.steps) do
    s.extmark_ids = {}
    if valid_buf(s.buf) and api.nvim_buf_is_loaded(s.buf) then
      render_step(s, tour_win(s.buf))
    end
  end
end

--- Load the buffer of location `a` and check that l1-l2 exists and (with `expect`, a list) contains
--- each expected text.
local function checked_range(a, l1, l2, expect)
  local buf = loc_buf(a)
  local count = api.nvim_buf_line_count(buf)
  if l1 < 1 or l2 < l1 then
    fail(("bad range %d-%d"):format(l1, l2), 6)
  end
  if l2 > count then
    fail(("range %d-%d is beyond end of file (%d lines): %s"):format(l1, l2, count, doc_name(a)), 6)
  end
  local lines = api.nvim_buf_get_lines(buf, l1 - 1, l2, false)
  for _, expect in ipairs(expect_list(expect) or {}) do
    local found = false
    for _, line in ipairs(lines) do
      if line:find(expect, 1, true) then
        found = true
        break
      end
    end
    if not found then
      local first = api.nvim_buf_get_lines(buf, l1 - 1, l1, false)[1] or ""
      local range = l1 == l2 and tostring(l1) or (l1 .. "-" .. l2)
      local at = a.ref and (" @" .. a.ref) or ""
      fail(("--expect %q not found in %s:%s%s; line %d is %q"):format(expect, relpath(a.file), range, at, l1, first), 6)
    end
  end
  return buf
end

H.step = function(a)
  S.workspace = a.workspace or S.workspace
  local role = a.role or "info"
  if not ROLES[role] then
    fail("unknown role: " .. tostring(role), 2)
  end
  local l1, l2 = a.l1, a.l2 or a.l1
  local buf = checked_range(a, l1, l2, a.expect)
  local steps = S.tour.steps
  local at = a.at or (#steps + 1)
  if at < 1 or at > #steps + 1 then
    fail(("--at %d is out of range (tour has %d steps)"):format(at, #steps), 6)
  end
  local from
  if a.from then
    from = steps[a.from]
    if not from then
      fail(("--from %d: no such step (tour has %d)"):format(a.from, #steps), 6)
    end
  end
  ensure_started()
  -- Only the first step of a tour moves the view, so a finished tour starts at step 1.
  local do_jump = a.jump or (#steps == 0 and not a.no_jump)
  if #steps == 0 and vim.g.nvtour_auto_panel ~= false and not S.panel.user_closed then
    panel_open() -- before rendering, so the first note is wrapped to the final window width
  end
  local step = {
    n = at,
    file = a.file,
    ref = a.sha and a.ref or nil,
    sha = a.sha,
    buf = buf,
    l1 = l1,
    l2 = l2,
    role = role,
    label = a.label,
    note = a.note,
    via = (a.via ~= "" and a.via) or nil,
    suggest = (a.suggest and a.suggest ~= "") and split_lines(a.suggest) or nil,
    from = from,
    expect = expect_list(a.expect),
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
    pcall(rerender_all) -- the jump may have drawn the previous current step as not current
    error(err, 0)
  end
  table.insert(steps, at, step)
  if at < #steps and not do_jump and S.tour.current >= at then
    S.tour.current = S.tour.current + 1
  end
  rerender_all() -- the steps next to the new one show links to it
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

local function step_at(n)
  local steps = S.tour.steps
  if not n or n < 1 or n > #steps then
    fail(("no such step %s (tour has %d)"):format(tostring(n), #steps), 6)
  end
  return steps[n]
end

H.edit = function(a)
  S.workspace = a.workspace or S.workspace
  local step = step_at(a.n)
  if a.role and not ROLES[a.role] then
    fail("unknown role: " .. tostring(a.role), 2)
  end
  -- A new location replaces the old one completely: without a ref it is on the working tree.
  local loc = a.file and { file = a.file, ref = a.sha and a.ref or nil, sha = a.sha, rel = a.rel, lines = a.lines }
    or { file = step.file, ref = step.ref, sha = step.sha }
  local l1, l2 = a.l1 or step.l1, a.l2 or a.l1 or step.l2
  local buf = checked_range(loc, l1, l2, a.expect)
  step.file, step.ref, step.sha, step.buf, step.l1, step.l2 = loc.file, loc.ref, loc.sha, buf, l1, l2
  if a.role then
    step.role = a.role
  end
  if a.label ~= nil then
    step.label = a.label ~= "" and a.label or nil
  end
  if a.note ~= nil then
    step.note = a.note ~= "" and a.note or nil
  end
  if a.expect ~= nil then
    step.expect = expect_list(a.expect) -- the new list replaces the old one; '' removes it
  end
  if a.via ~= nil then
    step.via = a.via ~= "" and a.via or nil
  end
  if a.suggest ~= nil then
    step.suggest = a.suggest ~= "" and split_lines(a.suggest) or nil
  end
  if a.from == 0 then
    step.from = nil
  elseif a.from then
    local from = S.tour.steps[a.from]
    if not from then
      fail(("--from %d: no such step (tour has %d)"):format(a.from, #S.tour.steps), 6)
    elseif from == step then
      fail(("--from %d: a step cannot link from itself"):format(a.from), 6)
    end
    step.from = from
  end
  rerender_all()
  if a.jump then
    return step_goto(step.n)
  end
  update_qf()
  render_panel()
  return step_result(step)
end

H.remove = function(a)
  local step = step_at(a.n)
  table.remove(S.tour.steps, step.n)
  local cur = S.tour.current
  if cur == step.n then
    S.tour.current = 0 -- nothing is shown as current until the next navigation
  elseif cur > step.n then
    S.tour.current = cur - 1
  end
  rerender_all()
  update_qf()
  render_panel()
  return { ok = true, removed = step.n, total = #S.tour.steps, current = S.tour.current }
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

H.status = function()
  local steps = {}
  for _, s in ipairs(S.tour.steps) do
    steps[#steps + 1] = {
      n = s.n,
      file = s.file,
      ref = s.ref,
      sha = s.sha,
      l1 = s.l1,
      l2 = s.l2,
      role = s.role,
      label = s.label,
      note = s.note,
      via = s.via,
      from = explicit_from(s) and s.from.n or nil,
      expect = expect_list(s.expect),
      suggest = s.suggest,
    }
  end
  local focus = {}
  for _, f in pairs(S.focus) do
    focus[#focus + 1] = { file = f.file, ranges = f.ranges, mode = f.dim and "dim" or "fold" }
  end
  local diffs = 0
  for _, t in ipairs(S.diff_tabs) do
    if valid_tab(t) then
      diffs = diffs + 1
    end
  end
  return {
    ok = true,
    title = S.tour.title,
    current = S.tour.current,
    total = #S.tour.steps,
    steps = steps,
    focus = focus,
    diff_tabs = diffs,
    panel = { open = panel_shown(), user_closed = S.panel.user_closed },
    keys = installed_keys(),
    workspace = S.workspace,
    version = M.VERSION,
  }
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
  local ref = vim.b[buf].nvtour_ref
  if ref then
    res.file, res.ref, res.sha = ref.file, ref.ref, ref.sha -- the real path, not nvtour://
  end
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

-- The band of a step depends on the gutter of its window: draw the steps of a buffer again when
-- it is shown in a window that they were not drawn for.
api.nvim_create_autocmd("BufWinEnter", {
  group = aug,
  callback = function(ev)
    local win = api.nvim_get_current_win()
    for _, s in ipairs(S.tour.steps) do
      if s.buf == ev.buf and s.drawn_win ~= win and valid_buf(s.buf) and api.nvim_buf_is_loaded(s.buf) then
        pcall(render_step, s, win)
      end
    end
  end,
})

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
      if w and valid_buf(s.buf) and ((s.note and s.note ~= "") or s.n == S.tour.current) then
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
  pcall(rerender_all) -- marks drawn by the old version may differ (for example the old line tint)
  pcall(render_panel)
end

_G.nvtour = M
return M

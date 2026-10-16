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
    tour = { title = "", steps = {}, current = 0, qf_id = nil, chapters = {} },
    ns_steps = api.nvim_create_namespace("nvtour_steps"),
    ns_focus = api.nvim_create_namespace("nvtour_focus"),
    ns_panel = api.nvim_create_namespace("nvtour_panel"),
    ns_flash = api.nvim_create_namespace("nvtour_flash"),
    ns_diagram = api.nvim_create_namespace("nvtour_diagram"),
    ns_anchor = api.nvim_create_namespace("nvtour_anchor"), -- one extmark per step: where its lines are now
    ns_marks = api.nvim_create_namespace("nvtour_marks"), -- one extmark (and sign) per mark of the user
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
    diagrams = {}, -- [name] = { name, file, buf, lines, header, links = { { text, file, l1, l2 } } }
    diagram_split = nil, -- the side of a new diagram window from the last 'diagram --split' (default "above")
    sheet_pending = {}, -- { timer, step }: the debounced 'sheet goto' that waits to start (see "Sheet link")
    attached = {}, -- [buf] = true: buffers with an nvim_buf_attach of ours (see "Steps follow edits")
    sync_timers = {}, -- [buf] = the debounce timer of the sync after an edit
    reloaded = {}, -- [buf] = true: the content was replaced (:e!); the steps are anchored again
    follow_timers = {}, -- [win] = the debounce timer of the follow mode (see "Follow the cursor")
    follow_jump = {}, -- { win, buf, line }: where the last jump put the cursor; follow ignores it
    peek = {}, -- { win, buf, step, ... } while a peek float is open (see "Peek a step"); {} when closed
    marks = {}, -- the marks of the user (see "Marks"); they belong to the user, not to the tour
    mark_seq = 0, -- the id of the next mark
    mark_keys = {}, -- [name] = lhs: the KEYS.always that we installed
    mark_keymaps_installed = false,
    ns_card = api.nvim_create_namespace("nvtour_card"),
    card = { buf = nil, win = nil, line_map = {}, user_closed = false }, -- the step card of the side view (see "Side view")
    view = nil, -- "inline" or "side"; nil: vim.g.nvtour_view
    range_style = nil, -- the range style of the side view from the cycle key; nil: vim.g.nvtour_range_style
    git = {}, -- [dir] = { at, top, sha, ref, linked }: the git state of a directory, for the card
    anims = {}, -- [win] = { finish }: the animation that runs in the window (see "Animation")
    animate = nil, -- true or false from the animate key; nil: vim.g.nvtour_animate
  }
end

local S = (_G.nvtour and _G.nvtour.state) or new_state()
for k, v in pairs(new_state()) do
  if S[k] == nil then
    S[k] = v
  end
end
M.state = S

local DEFAULT_KEYS = {
  next = "]w",
  prev = "[w",
  first = "[W",
  last = "]W",
  panel = "<leader>wp",
  clear = "<leader>wc",
  next_chapter = "]c",
  prev_chapter = "[c",
  peek = "<leader>wk",
  view = "<leader>wv",
  card = "<leader>ws",
  range_style = "<leader>wr",
  animate = "<leader>wa",
  mark = "<leader>wm",
  mark_note = "<leader>wM",
}

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
  set("NvtourDiagramLine", { fg = blend(fg, bg, 0.55) }) -- the box and arrow characters of a diagram
  set("NvtourDiagramLink", { fg = hl_attr("Directory", "fg") or fg, underline = true })
  set("NvtourCardTitle", { fg = fg, bold = true }) -- the label of the step in the card of the side view
  set("NvtourPanelFile", { link = "Directory" })
  set("NvtourPanelChapter", { link = "Title" }) -- the "## Name" line of a chapter (also in the winbar)
  set("NvtourPanelProgress", { link = "Comment" })
  set("NvtourLost", { link = "DiagnosticError" }) -- the "✗ lost" of a step whose lines are gone
  set("NvtourUserMark", { link = "DiagnosticHint" }) -- the "◆" sign of a mark of the user
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

--- True for the read-only buffer of a diagram.
local function is_diagram_buf(b)
  return vim.b[b].nvtour_diagram ~= nil
end

local diagram_win -- forward declaration

--- Window where files are shown (see DESIGN.md section 5). With `buf`, a usable window of the
--- current tab that already shows it wins. Returns nil when no window is usable, unless `create`
--- is set: then a split is opened for `buf` next to the first normal window of the current tab.
--- A diagram buffer goes to the diagram window instead.
local function tour_win(buf, create)
  if buf and valid_buf(buf) and is_diagram_buf(buf) then
    return diagram_win(buf, create)
  end
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
    if is_normal_win(w) and w ~= S.panel.win and w ~= S.card.win then
      anchor = w
      break
    end
  end
  return api.nvim_open_win(buf, false, { split = "left", win = anchor })
end

local DIAGRAM_SPLITS = { above = true, below = true, left = true, right = true, full = true }

--- The window of a full diagram (split "full"): the only normal window of its own tab page.
local function full_diagram_win()
  for _, w in ipairs(api.nvim_list_wins()) do
    if is_normal_win(w) and vim.w[w].nvtour_diagram_split == "full" and is_diagram_buf(api.nvim_win_get_buf(w)) then
      return w
    end
  end
end

--- Open `buf` in a new tab page after the current one and go back: the focus does not move.
local function open_full_diagram_win(buf)
  local cur = api.nvim_get_current_win()
  local switchbuf = vim.o.switchbuf
  vim.o.switchbuf = "" -- 'useopen' or 'usetab' would jump to a window that already shows buf
  local ok, err = pcall(vim.cmd, ("tab sbuffer %d"):format(buf))
  vim.o.switchbuf = switchbuf
  if not ok then
    fail(("could not open a tab page for the diagram: %s"):format(err), 5)
  end
  local win = api.nvim_get_current_win()
  api.nvim_set_current_win(cur)
  return win
end

--- Window for the diagram buffer `buf`: a window of the current tab that shows it, else one that
--- shows another diagram, else (with `create`) a split next to the tour window. `split` is the side
--- of the tour window ("above" by default, "below", "left", "right"), or "full": a tab page of its
--- own (any tab is searched for it); a diagram window on another side is closed first. Above or
--- below it is as high as the diagram (at most 60 % of that window),
--- left or right it takes half of the width of that window. Code steps never use it. Without `create`, a window of
--- another tab that shows `buf` (for the note width), or nil.
diagram_win = function(buf, create, split)
  local tab = api.nvim_get_current_tabpage()
  local full = full_diagram_win()
  if full and split and split ~= "full" then
    pcall(api.nvim_win_close, full, false) -- the user asked for a side: the full tab closes
    full = nil
  end
  local other, elsewhere
  for _, w in ipairs(api.nvim_tabpage_list_wins(tab)) do
    if is_normal_win(w) and w ~= S.panel.win and is_diagram_buf(api.nvim_win_get_buf(w)) then
      if split and (vim.w[w].nvtour_diagram_split or "above") ~= split then
        pcall(api.nvim_win_close, w, false) -- the user asked for another side
      elseif api.nvim_win_get_buf(w) == buf then
        return w
      else
        other = other or w
      end
    end
  end
  if other then
    return other
  end
  if full and valid_win(full) and (split or S.diagram_split) == "full" then
    return full
  end
  if not create then
    for _, w in ipairs(vim.fn.win_findbuf(buf)) do
      if is_normal_win(w) then
        elsewhere = elsewhere or w
      end
    end
    return elsewhere
  end
  split = split or S.diagram_split or "above"
  if split == "full" then
    local win = open_full_diagram_win(buf)
    api.nvim_set_option_value("wrap", false, { scope = "local", win = win })
    vim.w[win].nvtour_diagram_split = split
    return win
  end
  local anchor = tour_win()
  if anchor and api.nvim_win_get_tabpage(anchor) ~= tab then
    anchor = nil
  end
  if not anchor then
    for _, w in ipairs(api.nvim_tabpage_list_wins(tab)) do
      if is_normal_win(w) and w ~= S.panel.win and w ~= S.card.win then
        anchor = w
        break
      end
    end
  end
  local win
  if split == "left" or split == "right" then
    -- No 'winfixwidth': when the panel opens or closes, the code and the diagram share the width.
    local room = anchor and api.nvim_win_get_width(anchor) or vim.o.columns
    win = api.nvim_open_win(buf, false, { split = split, win = anchor or -1, width = math.floor(room / 2) })
  else
    local room = anchor and api.nvim_win_get_height(anchor) or vim.o.lines
    local height = math.max(3, math.min(api.nvim_buf_line_count(buf) + 1, math.floor(room * 0.6)))
    win = api.nvim_open_win(buf, false, { split = split, win = anchor or -1, height = height })
    api.nvim_set_option_value("winfixheight", true, { scope = "local", win = win })
  end
  api.nvim_set_option_value("wrap", false, { scope = "local", win = win }) -- a wrapped diagram falls apart
  vim.w[win].nvtour_diagram_split = split
  return win
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

--- `path` from the workspace, else from the current directory of nvim, else as it is.
local function relpath(path)
  local w = S.workspace
  if w and path:sub(1, #w + 1) == w .. "/" then
    return path:sub(#w + 2)
  end
  return vim.fn.fnamemodify(path, ":.")
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

--- "path" of the file of step `s`, or "diagram NAME" for a step on a diagram.
local function doc_path(s)
  return s.diagram and ("diagram " .. s.diagram) or relpath(s.file)
end

--- "path" for a step on the working tree, "path @ref" for a step at a git ref.
local function doc_name(s)
  return doc_path(s) .. (s.ref and (" @" .. s.ref) or "")
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
local attach_buf -- forward declaration (see "Steps follow edits")
local sync_schedule -- the same

---------------------------------------------------------------------------
-- Anchors: where the lines of a step are now
---------------------------------------------------------------------------

-- Every step on a file (or a git ref) has one extmark in ns_anchor over its lines. The marks move
-- with the user's edits; sync_step reads them back into step.l1 and step.l2. The marks of the user
-- (see "Marks") use the same anchors in ns_marks.

--- Make an anchor over lines l1-l2 of `buf`. Returns its id and `tail`, true when the end could not
--- be put on a column: an empty last line at the end of the buffer.
--- Gravity: the start moves with a line inserted above it (O on the first line); the end stays
--- (o on the last line does not grow the range). A range that ends on an empty line ends at the
--- start of the next line, so that the end is not mistaken for an end that moved there.
local function set_anchor(buf, l1, l2, ns, extra)
  local count = api.nvim_buf_line_count(buf)
  l1 = math.min(math.max(l1, 1), count)
  l2 = math.min(math.max(l2, l1), count)
  local last = api.nvim_buf_get_lines(buf, l2 - 1, l2, false)[1] or ""
  local row, col, tail = l2 - 1, #last, false
  if last == "" and l2 > l1 then
    if l2 < count then
      row, col = l2, 0
    else
      tail = true
    end
  end
  local id = api.nvim_buf_set_extmark(
    buf,
    ns or S.ns_anchor,
    l1 - 1,
    0,
    vim.tbl_extend("force", {
      end_row = row,
      end_col = col,
      right_gravity = true,
      end_right_gravity = false,
      invalidate = true, -- deleting all lines of the range marks it invalid; an undo restores it
      undo_restore = true,
    }, extra or {})
  )
  return id, tail
end

--- Lines l1, l2 of the anchor `id` and whether it is invalid (all its lines were deleted). nil when
--- the mark does not exist. A deleted last line moves the end to column 0 of the next line: that
--- row is then the last line of the range. `ns` is the namespace of the anchor (default ns_anchor).
local function read_anchor(buf, id, tail, ns)
  local ok, m = pcall(api.nvim_buf_get_extmark_by_id, buf, ns or S.ns_anchor, id, { details = true })
  if not ok or not m or #m == 0 then
    return nil
  end
  local d = m[3]
  local count = api.nvim_buf_line_count(buf)
  local l1, l2 = m[1] + 1, d.end_row + 1
  if d.end_col == 0 and d.end_row > m[1] and not (tail and d.end_row == count - 1) then
    l2 = d.end_row
  end
  l1 = math.min(math.max(l1, 1), count)
  l2 = math.min(math.max(l2, l1), count)
  return l1, l2, d.invalid and true or false
end

--- The first problem of lines l1-l2 of `buf` as a text: the range is beyond the end, or a text of
--- `expect` is not in it. nil when the range is fine.
local function range_problem(buf, l1, l2, expect)
  local count = api.nvim_buf_line_count(buf)
  if l1 < 1 or l2 > count then
    return ("range %d-%d is beyond the end of the file (%d lines)"):format(l1, l2, count)
  end
  local lines = api.nvim_buf_get_lines(buf, l1 - 1, l2, false)
  for _, text in ipairs(expect_list(expect) or {}) do
    local found = false
    for _, line in ipairs(lines) do
      if line:find(text, 1, true) then
        found = true
        break
      end
    end
    if not found then
      return ("--expect %q not found in the lines"):format(text)
    end
  end
  return nil
end

--- True when `step` has an anchor in its buffer.
local function anchored(step)
  local buf = step.buf
  if not step.anchor or step.anchor_buf ~= buf or not valid_buf(buf) or not api.nvim_buf_is_loaded(buf) then
    return false
  end
  local ok, m = pcall(api.nvim_buf_get_extmark_by_id, buf, S.ns_anchor, step.anchor, {})
  return ok and m ~= nil and #m > 0
end

--- Delete the anchor of `step` (the step keeps its lines).
local function del_anchor(step)
  if step.anchor and valid_buf(step.anchor_buf) then
    pcall(api.nvim_buf_del_extmark, step.anchor_buf, S.ns_anchor, step.anchor)
  end
  step.anchor, step.anchor_buf, step.tail = nil, nil, nil
end

--- Anchor `step` on its lines in its buffer. A step on a diagram has no anchor: a diagram is
--- replaced as a whole.
local function anchor_step(step)
  local buf = step.buf
  if step.diagram or not valid_buf(buf) or not api.nvim_buf_is_loaded(buf) then
    return
  end
  del_anchor(step)
  step.anchor, step.tail = set_anchor(buf, step.l1, step.l2)
  step.anchor_buf = buf
  step.orig_l1 = step.orig_l1 or step.l1 -- a step of an older runtime has no original range yet
  step.orig_l2 = step.orig_l2 or step.l2
  attach_buf(buf)
end

--- Anchor `step` again from its last known lines, after its buffer was reloaded or made again. The
--- step is lost when the lines are beyond the end of the file now or an --expect text is gone.
local function reanchor_step(step)
  local buf = step.buf
  if step.diagram or not valid_buf(buf) or not api.nvim_buf_is_loaded(buf) then
    return
  end
  del_anchor(step)
  local why = range_problem(buf, step.l1, step.l2, step.expect)
  if why then
    step.lost, step.lost_reason = true, why
  else
    step.lost, step.lost_reason = nil, nil
    anchor_step(step)
  end
  step.stale = true
end

--- Anchor again all steps (and the focus ranges) of `buf`, whose content was replaced.
local function reanchor_buf(buf)
  S.reloaded[buf] = nil
  if not valid_buf(buf) or not api.nvim_buf_is_loaded(buf) then
    return
  end
  for _, s in ipairs(S.tour.steps) do
    if s.buf == buf and not s.diagram then
      reanchor_step(s)
    end
  end
  local f = S.focus[buf]
  if f and f.anchors then
    local count = api.nvim_buf_line_count(buf)
    f.anchors = {}
    for i, r in ipairs(f.ranges) do
      r[1], r[2] = math.min(r[1], count), math.min(r[2], count)
      local id, tail = set_anchor(buf, r[1], r[2])
      f.anchors[i] = { id = id, tail = tail }
    end
    f.stale = true
  end
  attach_buf(buf)
end

--- Read the anchor of `step` into step.l1, step.l2 and step.lost. Returns true when something
--- changed; such a step is `stale` until it is drawn again.
local function sync_step(step)
  local buf = step.buf
  if step.diagram or not valid_buf(buf) or not api.nvim_buf_is_loaded(buf) then
    return false
  end
  if S.reloaded[buf] then
    reanchor_buf(buf)
  end
  if not step.anchor then
    return false -- no anchor yet, or lost by a reload: only edit gives it lines again
  end
  local l1, l2, invalid = read_anchor(buf, step.anchor, step.tail)
  if not l1 then
    reanchor_step(step) -- the mark is gone (the buffer was unloaded and loaded again)
    return true
  end
  local changed = false
  if invalid then
    if not step.lost then
      step.lost, step.lost_reason, changed = true, "lines deleted", true
    end
  else
    if step.lost then
      step.lost, step.lost_reason, changed = nil, nil, true
    end
    if l1 ~= step.l1 or l2 ~= step.l2 then
      step.l1, step.l2, changed = l1, l2, true
    end
  end
  if changed then
    step.stale = true
  end
  return changed
end

--- sync_step for all steps of `buf`. Returns true when a step of it is stale: changed since it was
--- drawn.
local function sync_buf(buf)
  local stale = false
  for _, s in ipairs(S.tour.steps) do
    if s.buf == buf then
      sync_step(s)
      stale = stale or s.stale == true
    end
  end
  return stale
end

--- sync_step for all steps. A buffer with a changed step is drawn again by the debounced sync.
local function sync_all()
  local seen = {}
  for _, s in ipairs(S.tour.steps) do
    if sync_step(s) or s.stale then
      seen[s.buf] = true
    end
  end
  for buf in pairs(seen) do
    if valid_buf(buf) and api.nvim_buf_is_loaded(buf) then
      sync_schedule(buf)
    end
  end
end

--- Make sure `step` has an anchor and its buffer is watched; after its buffer was loaded again.
local function ensure_track(step)
  if not step.diagram and not step.lost and not anchored(step) then
    reanchor_step(step)
  end
end

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
      ensure_track(s)
      render_step(s, tour_win(buf))
    end
  end
  return buf
end

--- True for the first byte sequence of a box drawing, block, shape or arrow character (U+2190-21FF,
--- U+2500-25FF), or the "∧" and "∨" that graph-easy draws as arrow heads.
local function is_line_char(ch)
  local b1, b2 = ch:byte(1, 2)
  if b1 ~= 0xE2 then
    return false
  end
  return (b2 >= 0x94 and b2 <= 0x97) or b2 == 0x86 or b2 == 0x87 or ch == "∧" or ch == "∨"
end

--- Colour the lines and arrows of diagram `d` in NvtourDiagramLine and its link texts in
--- NvtourDiagramLink.
local function highlight_diagram(d)
  local buf, ns = d.buf, S.ns_diagram
  api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  for row, line in ipairs(d.lines) do
    local run_start, run_end
    for pos, ch in line:gmatch("()([%z\1-\127\194-\244][\128-\191]*)") do
      if is_line_char(ch) then
        run_start = run_start or pos
        run_end = pos + #ch
      elseif run_start then
        api.nvim_buf_set_extmark(buf, ns, row - 1, run_start - 1, { end_col = run_end - 1, hl_group = "NvtourDiagramLine", priority = 90 })
        run_start = nil
      end
    end
    if run_start then
      api.nvim_buf_set_extmark(buf, ns, row - 1, run_start - 1, { end_col = run_end - 1, hl_group = "NvtourDiagramLine", priority = 90 })
    end
    for _, link in ipairs(d.links) do
      local from = 1
      while true do
        local s, e = line:find(link.text, from, true)
        if not s then
          break
        end
        api.nvim_buf_set_extmark(buf, ns, row - 1, s - 1, { end_col = e, hl_group = "NvtourDiagramLink", priority = 95 })
        from = e + 1
      end
    end
  end
end

--- Buffer-local diagram keys; they call through _G so they survive a runtime upgrade.
local function map_diagram_keys(buf)
  vim.keymap.set("n", "<CR>", function()
    local res = _G.nvtour.dispatch("diagram_link", {})
    if not res.ok then
      notify(res.error, vim.log.levels.WARN)
    elseif not res.followed then
      vim.cmd("normal! +") -- no link under the cursor: the usual <CR>
    end
  end, { buffer = buf, nowait = true, silent = true, desc = "nvtour: open the link under the cursor" })
end

--- The read-only buffer of diagram `d` (an entry of S.diagrams), created when it does not exist.
--- With `reload`, the lines are set again from d.lines. A buffer made again (the user wiped or
--- unloaded it) takes the steps on the diagram, which are drawn again.
local function ensure_diagram_buf(d, reload)
  local fresh = not (valid_buf(d.buf) and api.nvim_buf_is_loaded(d.buf))
  if fresh then
    if valid_buf(d.buf) then
      pcall(api.nvim_buf_delete, d.buf, { force = true })
    end
    local buf = api.nvim_create_buf(true, true)
    unique_name(d.file, buf)
    local bo = vim.bo[buf]
    bo.buftype = "nofile"
    bo.bufhidden = "hide" -- the extmarks of its steps must stay when it is not shown
    bo.swapfile = false
    vim.b[buf].nvtour_diagram = d.name
    bo.filetype = "nvtourdiagram"
    map_diagram_keys(buf)
    d.buf = buf
  end
  if fresh or reload then
    local bo = vim.bo[d.buf]
    bo.modifiable = true
    bo.readonly = false
    api.nvim_buf_set_lines(d.buf, 0, -1, false, d.lines)
    bo.modifiable = false
    bo.readonly = true
    bo.modified = false
    highlight_diagram(d)
  end
  if fresh then
    for _, s in ipairs(S.tour.steps) do
      if s.diagram == d.name and s.buf ~= d.buf then
        s.buf = d.buf
        s.extmark_ids = {}
        render_step(s, tour_win(d.buf))
      end
    end
  end
  return d.buf
end

--- Buffer for a location `a` ({ file, ref, sha, rel, lines } or { diagram }): the file, its version
--- at a commit, or a diagram.
local function loc_buf(a)
  if a.diagram then
    local d = S.diagrams[a.diagram]
    if not d then
      fail(("no diagram %q; make it with 'nvtour diagram %s'"):format(a.diagram, a.diagram), 6)
    end
    return ensure_diagram_buf(d)
  end
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
  if step.sha or step.diagram then
    step.buf = loc_buf(step)
  elseif not valid_buf(step.buf) or not api.nvim_buf_is_loaded(step.buf) then
    step.buf = load_buf(step.file)
    -- The other steps on the file are in the old buffer: they take the new one, with new anchors.
    for _, s in ipairs(S.tour.steps) do
      if s ~= step and s.file == step.file and not s.sha and not s.diagram and s.buf ~= step.buf then
        s.buf, s.extmark_ids = step.buf, {}
        ensure_track(s)
        render_step(s, tour_win(step.buf))
      end
    end
  end
  ensure_track(step)
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

--- Dim the lines outside `ranges` (the gaps) of `buf`; the old dim marks go.
local function draw_dim(buf, ranges)
  api.nvim_buf_clear_namespace(buf, S.ns_focus, 0, -1)
  for _, g in ipairs(gaps_of(ranges, api.nvim_buf_line_count(buf))) do
    for l = g[1], g[2] do
      api.nvim_buf_set_extmark(buf, S.ns_focus, l - 1, 0, { end_row = l, end_col = 0, hl_group = "NvtourDim", hl_eol = true, priority = 200 })
    end
  end
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
    for _, a in ipairs(f.anchors or {}) do
      pcall(api.nvim_buf_del_extmark, buf, S.ns_anchor, a.id)
    end
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

--- Give the code window and a diagram window beside it (split left or right) the same width, after
--- the panel took or gave back columns.
local function balance_side_diagram()
  local code = tour_win()
  if not code then
    return
  end
  for _, w in ipairs(api.nvim_tabpage_list_wins(api.nvim_win_get_tabpage(code))) do
    local side = vim.w[w].nvtour_diagram_split
    if (side == "left" or side == "right") and is_diagram_buf(api.nvim_win_get_buf(w)) then
      local total = api.nvim_win_get_width(w) + api.nvim_win_get_width(code)
      pcall(api.nvim_win_set_width, w, math.floor(total / 2))
      return
    end
  end
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
      balance_side_diagram()
    end
  end
  S.panel.win = nil
  if user then
    S.panel.user_closed = true
  end
end

local step_goto -- forward declaration

-- The side view (see "Side view") keeps its functions in one table: a Lua chunk has at most 200
-- local variables.
local side = {
  FLASH_NS = api.nvim_create_namespace("nvtour_card_flash"), -- side.flash: the changed lines of the card
  BLINK_MS = 200, -- side.flash: the light is on for this long, then off for this long, and again
}

-- The two sets of keys (one table: a Lua chunk has at most 200 local variables). The keys of a tour
-- (KEYS.tour) are installed when a tour starts and removed by clear. The keys of the marks
-- (KEYS.always) stay while the runtime is loaded (see "Marks").
local KEYS = {
  tour = { "next", "prev", "first", "last", "panel", "clear", "next_chapter", "prev_chapter", "peek", "view", "card", "range_style", "animate" },
  always = { "mark", "mark_note" },
}

--- "]w next · [w prev · ..." for the keys that are installed (the chapter keys: when there are chapters).
local function keys_line()
  local parts = {}
  local chaptered = S.tour.chapters and #S.tour.chapters > 0
  for _, name in ipairs(KEYS.tour) do
    if S.keys[name] and (chaptered or not name:find("chapter", 1, true)) then
      parts[#parts + 1] = "`" .. S.keys[name] .. "` " .. name:gsub("_", " ")
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

--- A sheet link as the user writes it: SHEET#STEP.
local function sheet_label(link)
  return link.sheet .. "#" .. link.step
end

---------------------------------------------------------------------------
-- Chapters
---------------------------------------------------------------------------

-- A chapter is a heading in front of a step: { name, step = <step table> | nil }. It points at the
-- step table, so inserts and removes need no renumbering. A chapter without a step is pending: it
-- takes the next step that is added. The list is kept in tour order (pending last), and at most one
-- chapter starts at a step (and one is pending).

--- The list of chapters; a tour made by an older runtime has none yet.
local function chapters()
  if not S.tour.chapters then
    S.tour.chapters = {}
  end
  return S.tour.chapters
end

--- The chapter that starts at `step` (nil: none); with no step, the pending chapter.
local function chapter_at(step)
  for _, c in ipairs(chapters()) do
    if c.step == step then
      return c
    end
  end
end

--- Put the chapters in tour order again; a chapter whose step is not in the tour is dropped.
local function sort_chapters()
  local by, out = {}, {}
  for _, c in ipairs(chapters()) do
    if c.step then
      by[c.step] = c
    end
  end
  for _, s in ipairs(S.tour.steps) do
    out[#out + 1] = by[s]
  end
  out[#out + 1] = chapter_at(nil)
  S.tour.chapters = out
end

--- Start a chapter `name` at `step` (nil: pending). A chapter that already starts there gets the
--- new name. Returns the chapter and the name it replaced.
local function set_chapter(name, step)
  local c = chapter_at(step)
  if c then
    local old = c.name
    c.name = name
    return c, old
  end
  c = { name = name, step = step }
  table.insert(chapters(), c)
  sort_chapters()
  return c
end
M.set_chapter = set_chapter

local function drop_chapter(chapter)
  for i, c in ipairs(chapters()) do
    if c == chapter then
      table.remove(chapters(), i)
      return
    end
  end
end

--- The chapter that `step` is in: the last one that starts at or before it (nil: none).
local function chapter_of(step)
  local by = {}
  for _, c in ipairs(chapters()) do
    if c.step then
      by[c.step] = c
    end
  end
  local found
  for _, s in ipairs(S.tour.steps) do
    found = by[s] or found
    if s == step then
      return found
    end
  end
end

--- Where each chapter can be entered, in tour order: { { chapter, n } }, n being the first step of the
--- chapter that is not lost. A chapter of lost steps only is left out.
local function chapter_starts()
  local starts, steps, list = {}, S.tour.steps, chapters()
  for i, c in ipairs(list) do
    if c.step then
      local nxt = list[i + 1]
      local last = (nxt and nxt.step) and nxt.step.n - 1 or #steps
      for n = c.step.n, last do
        if not steps[n].lost then
          starts[#starts + 1] = { chapter = c, n = n }
          break
        end
      end
    end
  end
  return starts
end

local function render_panel()
  side.render()
  if not valid_buf(S.panel.buf) then
    return
  end
  sync_all()
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
  local prev -- the step before, for the link line
  local prev_doc -- the step before in the same run of a file, for the file name line (a heading ends the run)
  local starts = {}
  for _, c in ipairs(chapters()) do
    if c.step then
      starts[c.step] = c
    end
  end
  for _, s in ipairs(steps) do
    local chapter = starts[s]
    if chapter then
      -- The heading comes first, then the link line of the step (if any), then the file name line again.
      if #lines > 2 then
        lines[#lines + 1] = ""
      end
      lines[#lines + 1] = "## " .. chapter.name
      map[#lines] = s.n -- <CR> on the heading jumps to the first step of the chapter
      hls[#hls + 1] = { #lines, 0, -1, "NvtourPanelChapter" }
      prev_doc = nil
    end
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
    if not (prev_doc and same_doc(s, prev_doc)) then
      lines[#lines + 1] = doc_name(s)
      map[#lines] = s.n -- <CR> on the file name jumps to its first step
      hls[#hls + 1] = { #lines, 0, -1, "NvtourPanelFile" }
    end
    prev, prev_doc = s, s
    local mark = (s.n == S.tour.current) and "▶ " or "  "
    local head = ("%d. %s"):format(s.n, ROLE_MARK[s.role] or "•")
    local range = fmt_range(s)
    local line = mark .. head .. " " .. range
    local lost_at
    if s.lost then
      lost_at = #line
      line = line .. " ✗ lost"
    end
    local title = step_title(s)
    if title ~= "" then
      line = line .. (" "):rep(s.lost and 2 or (rw - #range + 2)) .. title
    end
    local link_at
    if s.sheet then
      link_at = #line
      line = line .. "  ↔ " .. sheet_label(s.sheet)
    end
    lines[#lines + 1] = line
    map[#lines] = s.n
    hls[#hls + 1] = { #lines, #mark, #mark + #head, "NvtourSign" .. cap(s.role) }
    if lost_at then
      hls[#hls + 1] = { #lines, lost_at, lost_at + #" ✗ lost", "NvtourLost" }
    end
    if link_at then
      hls[#hls + 1] = { #lines, link_at, -1, "NvtourVia" }
    end
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
  map("K", function()
    local res = _G.nvtour.dispatch("peek", {}) -- the step of the line, anchored at the panel cursor
    if not res.ok then
      notify(res.error, vim.log.levels.WARN)
    elseif res.message then
      notify(res.message)
    end
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
  balance_side_diagram()
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
  local loc = doc_path(s) .. ":" .. s.l1
  if s.ref then
    return loc .. " @" .. s.ref
  end
  return (here and here.sha and here.file == s.file) and (loc .. " · working tree") or loc
end

--- Segments of the "◇" line of `step`: its file and version. The commit is added when the ref is not
--- itself the commit ("@origin/master (1a2b3c4d5e6f)"); "working tree" when the step before was at
--- a git ref.
local function version_segs(step, prev)
  local segs = { { doc_path(step), "NvtourViaLoc" } }
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

--- Line above the note of the current step, when it is the first step of the tour with a sheet link,
--- or its link names another sheet than the last linked step before it (the sheet the user saw last):
---   ↔ sheet other
local function sheet_arrival_lines(step, width)
  if not step.sheet then
    return {}
  end
  for i = step.n - 1, 1, -1 do
    local s = S.tour.steps[i]
    if s and s ~= step and s.sheet then
      if s.sheet.sheet == step.sheet.sheet then
        return {}
      end
      break
    end
  end
  return link_lines("↔ ", { { "sheet ", "NvtourVia" }, { step.sheet.sheet, "NvtourViaLoc" } }, width)
end

--- Lines above the note of a step. The chapter heading comes first, when a chapter starts at the step
--- (`chapter_only`: a step that is not current gets only this line). For the current step the "◇"
--- line follows, when the file or the version is different from the step before it in the tour (the
--- code the user saw last), then the sheet line:
---   ## Why it fails
---   ◇ a.cpp @origin/master (1a2b3c4d5e6f)
---   ↔ sheet other
--- The heading is one line: a long name is cut with "…". Its "## " is a chunk of its own (the peek
--- float looks for it). The --via text is not repeated here: the "next" line of the step before shows it.
local function arrival_lines(step, width, chapter_only)
  local out = {}
  local chapter = chapter_at(step)
  if chapter and not step.lost then
    out[1] = clip_chunks({ { "## ", "NvtourPanelChapter" }, { chapter.name, "NvtourPanelChapter" } }, width)
  end
  if chapter_only then
    return out
  end
  local prev = S.tour.steps[step.n - 1]
  if prev and prev ~= step and not same_doc(prev, step) then
    vim.list_extend(out, link_lines("◇ ", version_segs(step, prev), width))
  end
  return vim.list_extend(out, sheet_arrival_lines(step, width))
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
--- The cells a note may take (vim.g.nvtour_note_width), or nil for the window width.
local function note_width_limit()
  local limit = tonumber(vim.g.nvtour_note_width)
  if limit and limit > 0 then
    return math.max(30, limit)
  end
end

-- The peek float (see "Peek a step") keeps its functions in one table: a Lua chunk has at most 200
-- local variables. Its frame of the suggested code is the one of the step, so `frame` shares it.
local peek = {}

--- The rows of the frame around `lines` (chunk lists): "╭ suggested ───╮", "│ code │", "╰───╯", `inner`
--- cells wide, in the border group `B`. `kind` is "suggest" (with the title) or "block" (without).
function peek.box(lines, B, role, kind, inner)
  -- The suggestion has a title in its top border: "╭ suggested ───╮".
  local title = kind == "suggest" and " suggested " or ""
  local out = {
    {
      { "╭", B },
      { title, "NvtourLabel" .. cap(role) },
      { ("─"):rep(math.max(0, inner - 2 - vim.fn.strdisplaywidth(title))) .. "╮", B },
    },
  }
  for _, line in ipairs(lines) do
    local chunks, w = clip_chunks(line, inner - 4) -- code does not wrap: a long line is cut
    local cells = { { "│ ", B } }
    vim.list_extend(cells, chunks)
    cells[#cells + 1] = { (" "):rep(math.max(0, inner - 4 - w)) .. " │", B }
    out[#out + 1] = cells
  end
  out[#out + 1] = { { "╰" .. ("─"):rep(inner - 2) .. "╯", B } }
  return out
end

local function frame(lines, win, role, current, kind, nobar)
  if not valid_win(win) then
    return { virt_lines = lines } -- drawn again in the window when the buffer is shown (BufWinEnter)
  end
  local info = vim.fn.getwininfo(win)[1]
  local off, full = info.textoff, info.width
  local col = (current and not nobar) and bar_col(win, off) or nil
  local B = current and ("NvtourFrame" .. cap(role)) or "NvtourNoteBorder"
  local inner = math.max(10, full - off) -- cells from the code column to the right edge
  local limit = note_width_limit()
  if limit then
    inner = math.min(inner, limit + 4) -- the frame ends where the wrapped note ends
  end
  local function row(chunks)
    return vim.list_extend(gutter_chunks(off, col, role, plain_hl), chunks)
  end
  local out = {}
  if kind == "block" or kind == "suggest" then
    for _, cells in ipairs(peek.box(lines, B, role, kind, inner)) do
      out[#out + 1] = row(cells)
    end
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
--- other steps keep the line numbers, the "← N label" marker and a one-line note. In the side view
--- (see "Side view") the steps keep the line numbers only; the current step gets its range style,
--- the suggested code and the --expect marks.
render_step = function(step, win)
  local buf = step.buf
  local ns = S.ns_steps
  local R = cap(step.role)
  local current = step.n == S.tour.current
  for _, id in ipairs(step.extmark_ids or {}) do
    pcall(api.nvim_buf_del_extmark, buf, ns, id)
  end
  step.extmark_ids = {}
  step.stale = nil
  if step.lost then
    return -- its lines are gone: nothing to mark
  end
  local ids = step.extmark_ids
  local function add(row, opts, col)
    ids[#ids + 1] = api.nvim_buf_set_extmark(buf, ns, row, col or 0, opts)
  end
  local sided = side.on()
  for l = step.l1, step.l2 do
    local sign = (current and not sided) and BAR_SIGN or nil
    if current and sided then
      sign = side.sign(step, l)
    end
    add(l - 1, {
      number_hl_group = "NvtourNumber" .. R,
      sign_text = sign,
      sign_hl_group = "NvtourSign" .. R,
      priority = 100,
    })
  end
  step.drawn_win = valid_win(win) and win or nil
  -- The lines that the suggestion replaces are struck through (from the first non-blank character).
  local function strike()
    for i, line in ipairs(api.nvim_buf_get_lines(buf, step.l1 - 1, step.l2, false)) do
      local first = line:find("%S")
      if first then
        add(step.l1 + i - 2, { end_col = #line, hl_group = "NvtourStrike", priority = 140 }, first - 1)
      end
    end
  end
  -- Every text of --expect in the range is underlined in the role colour (the current step only).
  local function marks()
    local lines = api.nvim_buf_get_lines(buf, step.l1 - 1, step.l2, false)
    for _, text in ipairs(expect_list(step.expect) or {}) do
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
  if sided then
    side.draw(step, win, add, current)
    if current then
      if step.suggest then
        strike()
      end
      marks()
    end
    return
  end
  local width = 80
  if valid_win(win) then
    local info = vim.fn.getwininfo(win)[1]
    width = info.width - info.textoff
  end
  width = math.max(30, width - 4)
  local limit = note_width_limit()
  if limit then
    width = math.min(width, limit)
  end
  local framed = note_style() == "frame"
  local above = arrival_lines(step, width, not current)
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
    strike()
  end
  if current then
    marks()
  end
  local label = (step.label and step.label ~= "") and (" " .. step.label) or ""
  add(step.l1 - 1, {
    virt_text = { { "  ← ", "NvtourLabel" .. R }, { tostring(step.n), "NvtourSign" .. R }, { label, "NvtourLabel" .. R } },
    virt_text_pos = "eol",
  })
end

---------------------------------------------------------------------------
-- Side view
---------------------------------------------------------------------------

-- In the side view the code keeps only the coloured line numbers of the steps. The current step
-- gets a range style and its suggested code; everything else about it (the note, the links, the
-- file and version, the next and the previous step) is in the step card, a window on the left.

-- The parts of a range style, and the order of the cycle key.
side.PARTS = { rules = true, bracket = true, spotlight = true }
side.STYLES = { "rules", "bracket", "spotlight", "rules+spotlight" }
side.MARGIN = 2 -- blank cells on the left of the card (and at the right end of its rules)

--- "inline" or "side": the view from the view key, else vim.g.nvtour_view.
function side.view()
  return S.view or (vim.g.nvtour_view == "side" and "side" or "inline")
end

function side.on()
  return side.view() == "side"
end

--- Nil when `name` is a valid range style ("rules", "rules+spotlight", ...), else the reason.
function side.bad_style(name)
  if type(name) ~= "string" or name == "" then
    return "empty range style"
  end
  for part in name:gmatch("[^+]+") do
    if not side.PARTS[part] then
      return ("unknown range style '%s' (rules, bracket, spotlight, joined with +)"):format(part)
    end
  end
end

--- The name of the range style: from the cycle key, else vim.g.nvtour_range_style, else "rules".
function side.style_name()
  for _, name in ipairs({ S.range_style, vim.g.nvtour_range_style }) do
    if not side.bad_style(name) then
      return name
    end
  end
  return "rules"
end

--- The range style as a set of its parts: { rules = true, spotlight = true }.
function side.style()
  local set = {}
  for part in side.style_name():gmatch("[^+]+") do
    set[part] = true
  end
  return set
end

--- The sign of line `l` of the current step: a bracket ┌ │ └ beside the line numbers (style "bracket").
function side.sign(step, l)
  if not side.style().bracket then
    return nil
  end
  if step.l1 == step.l2 then
    return " ▸"
  end
  return l == step.l1 and " ┌" or (l == step.l2 and " └" or " │")
end

--- "1 ✗ label" of a step, as chunks.
function side.title_chunks(step)
  local R = cap(step.role)
  local out = { { step.n .. " " .. (ROLE_MARK[step.role] or "•"), "NvtourSign" .. R } }
  local title = step_title(step)
  if title ~= "" then
    out[2] = { " " .. title, "NvtourLabel" .. R }
  end
  return out
end

--- Draw the current step of the side view with `add(row, opts, col)` (see render_step): the range
--- style, then below it the suggested code. Other steps get nothing more than their line numbers.
function side.draw(step, win, add, current)
  step.below = 0
  if not current then
    return
  end
  local buf, R = step.buf, cap(step.role)
  local B = "NvtourFrame" .. R
  local style = side.style()
  local info = valid_win(win) and vim.fn.getwininfo(win)[1] or nil
  local off = info and info.textoff or 0
  local room = info and (info.width - info.textoff) or 80
  local limit = note_width_limit()
  local inner = math.max(10, limit and math.min(room, limit + 4) or room)
  -- A virtual line that starts in the gutter (as the frame does), so the text is in the code column.
  local function row(chunks)
    return vim.list_extend(info and gutter_chunks(off, nil, step.role, plain_hl) or {}, chunks)
  end
  local above, below = {}, {}
  local title = side.title_chunks(step)
  if style.rules then
    local head, w = clip_chunks(title, inner - 8)
    local top = { { "── ", B } }
    vim.list_extend(top, head)
    top[#top + 1] = { " " .. ("─"):rep(math.max(1, inner - 4 - w)), B }
    above[1] = row(top)
    below[1] = row({ { ("─"):rep(inner), B } })
  end
  if style.spotlight then
    local count = api.nvim_buf_line_count(buf)
    local dim = { hl_group = "NvtourDim", hl_eol = true, priority = 200 }
    if step.l1 > 1 then
      add(0, vim.tbl_extend("force", dim, { end_row = step.l1 - 1, end_col = 0 }))
    end
    if step.l2 < count then
      local last = api.nvim_buf_get_lines(buf, count - 1, count, false)[1] or ""
      add(step.l2, vim.tbl_extend("force", dim, { end_row = count - 1, end_col = #last }))
    end
  end
  if step.suggest then
    vim.list_extend(below, frame(suggest_lines(step, 4), win, step.role, true, "suggest", true).virt_lines)
  end
  if #above > 0 then
    add(step.l1 - 1, { virt_lines = above, virt_lines_above = true, virt_lines_leftcol = info ~= nil })
  end
  if #below > 0 then
    add(step.l2 - 1, { virt_lines = below, virt_lines_leftcol = info ~= nil })
  end
  step.below = #below -- scroll_to counts the lines below l2
end

--- The version of the code of `step` and its repository, as lines of chunks:
---   working tree · master @ 0c1af94a27e3     (a detached HEAD: "detached @ ...")
---   repo nvtour                              (a linked worktree: "worktree nvtour-fix")
--- A step at a git ref: "git ref v1 · 1a2b3c4d5e6f", or "commit 1a2b3c4d5e6f" when the ref is the
--- commit. {} for a diagram; no repository line outside git. The state of a directory is read at most
--- every 2 s.
function side.git(step)
  if step.diagram then
    return {}
  end
  local dir = vim.fn.fnamemodify(step.file, ":h")
  local c, now = S.git[dir], uv.now()
  if not c or now - c.at > 2000 then
    c = { at = now }
    S.git[dir] = c
    local ok, res = pcall(function()
      return vim
        .system({ "git", "-C", dir, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir", "HEAD", "--symbolic-full-name", "HEAD" }, { text = true })
        :wait(1000)
    end)
    local out = (ok and res.code == 0) and vim.split(vim.trim(res.stdout or ""), "\n") or {}
    if #out >= 4 then
      local common = out[3]
      if common:sub(1, 1) ~= "/" then
        common = dir .. "/" .. common
      end
      local function norm(path)
        return (vim.fn.resolve(vim.fn.simplify(path)):gsub("/$", ""))
      end
      c.top, c.sha, c.ref = out[1], out[4], out[5]
      c.linked = norm(common) ~= norm(out[2])
    end
  end
  local lines = {}
  if step.ref then
    local sha = step.sha:sub(1, 12)
    if step.sha:sub(1, #step.ref) == step.ref then
      lines[1] = { { "commit ", "NvtourVia" }, { sha, "NvtourVersion" } }
    else
      lines[1] = { { "git ref ", "NvtourVia" }, { step.ref, "NvtourVersion" }, { " · " .. sha, "NvtourVia" } }
    end
  elseif c.sha then
    local branch = c.ref and c.ref:match("^refs/heads/(.+)$")
    lines[1] = {
      { "working tree", "NvtourVersion" },
      { " · ", "NvtourNoteBorder" },
      { branch or "detached", "NvtourVia" },
      { " @ " .. c.sha:sub(1, 12), "NvtourVia" },
    }
  end
  if c.top then
    lines[#lines + 1] = { { c.linked and "worktree " or "repo ", "NvtourVia" }, { vim.fn.fnamemodify(c.top, ":t"), "NvtourViaLoc" } }
  end
  return lines
end

function side.shown()
  local w = S.card.win
  return valid_win(w) and valid_buf(S.card.buf) and api.nvim_win_get_buf(w) == S.card.buf
end

--- Buffer-local keys of the card; they call through _G so they survive a runtime upgrade.
function side.map_keys(buf)
  local function map(lhs, fn)
    vim.keymap.set("n", lhs, fn, { buffer = buf, nowait = true, silent = true })
  end
  map("<CR>", function()
    local n = S.card.line_map[api.nvim_win_get_cursor(0)[1]]
    if n then
      local res = _G.nvtour.dispatch("goto", { n = n })
      if not res.ok then
        notify(res.error, vim.log.levels.WARN)
      end
    end
  end)
  map("q", function()
    _G.nvtour.dispatch("card", {}) -- hides the card; the view stays the side view
  end)
end

function side.buf()
  if valid_buf(S.card.buf) then
    return S.card.buf
  end
  local buf = api.nvim_create_buf(false, true)
  api.nvim_buf_set_name(buf, "nvtour://step")
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "hide"
  vim.bo[buf].swapfile = false
  vim.bo[buf].filetype = "markdown"
  vim.bo[buf].modifiable = false
  if not vim.treesitter.highlighter.active[buf] then
    pcall(vim.treesitter.start, buf, "markdown") -- `code` in the note, conceal of the markers
  end
  side.map_keys(buf)
  S.card.buf = buf
  return buf
end

function side.close()
  if side.shown() then
    local w = S.card.win
    if #api.nvim_tabpage_list_wins(api.nvim_win_get_tabpage(w)) > 1 or #api.nvim_list_tabpages() > 1 then
      pcall(api.nvim_win_close, w, true)
      balance_side_diagram()
    end
  end
  S.card.win = nil
end

--- Open the card on the left of the tab of the tour window (a card in another tab is closed first).
function side.open()
  local target = tour_win() or api.nvim_get_current_win()
  local tab = api.nvim_win_get_tabpage(target)
  if side.shown() then
    if api.nvim_win_get_tabpage(S.card.win) == tab then
      return
    end
    side.close()
  end
  local buf = side.buf()
  vim.bo[buf].formatlistpat = [[^\s*\([-*+•]\|\d\+[.)]\)\s\+]] -- the bullets of the card and of markdown lists
  local width = math.max(36, math.min(64, math.floor(vim.o.columns * 0.28)))
  local win
  api.nvim_win_call(target, function()
    win = api.nvim_open_win(buf, false, { split = "left", win = -1, width = width })
  end)
  S.card.win = win
  local wo = vim.wo[win]
  wo.winfixwidth = true
  wo.wrap = true
  wo.linebreak = true
  wo.breakindent = true
  wo.conceallevel = 2
  wo.concealcursor = "nc"
  wo.number = false
  wo.relativenumber = false
  wo.signcolumn = "no"
  wo.foldcolumn = "0"
  wo.cursorline = false
  wo.foldenable = false
  wo.list = false
  wo.spell = false
  wo.statuscolumn = (" "):rep(side.MARGIN) -- a left margin, also on wrapped lines
  wo.fillchars = "eob: " -- no "~" below the last line
  wo.breakindentopt = "list:-1" -- a wrapped item of a list goes on under its text, not under the bullet
  balance_side_diagram()
end

--- Draw the card of the current step; opens it in the side view and closes it otherwise (or when
--- there is no current step). Lines:
---   1/3 ✗ fault · Why it fails          position, role, chapter
---   dangling iterator                   label
---   src/a.cpp:412-415                   file and range
---   working tree · master @ 0c1af94a27e3   version (a ref step: "git ref v1 · <sha>")
---   repo nvtour                         repository (a linked worktree: "worktree NAME")
---   ↔ arch#3                            sheet link
---   ↑ from 2: <via>                     how the tour came here
---   ────────
---   <note>
---   Watch                               --expect, one "• text" line each
---   ────────
---   Next  ]w                          then the next step: number and title, place, --via
---   Previous  [w                      the same for the step before ("first step" at the ends)
function side.render()
  local step = S.tour.steps[S.tour.current]
  if not side.on() or not step or S.card.user_closed then
    side.close()
    return
  end
  sync_step(step)
  side.open()
  local buf, R = S.card.buf, cap(step.role)
  local lines, hls, map, rules = {}, {}, {}, {}
  -- One line of { text, group } segments (group nil: no highlight of ours, markdown shows it).
  local function put(segs, n)
    local text = ""
    for _, sg in ipairs(segs) do
      if sg[2] then
        hls[#hls + 1] = { #lines + 1, #text, #text + #sg[1], sg[2] }
      end
      text = text .. sg[1]
    end
    lines[#lines + 1] = text
    map[#lines] = n
  end
  local function oneline(text)
    return (text or ""):gsub("%s+", " ")
  end
  local chapter = chapter_of(step)
  local head = { { ("%d/%d %s %s"):format(step.n, #S.tour.steps, ROLE_MARK[step.role] or "•", step.role), "NvtourSign" .. R } }
  -- Where side.flash lights up a change: the chapter name, and the lines of the file and its version.
  local spots = {}
  if chapter then
    head[2] = { " · ", "NvtourNoteBorder" }
    head[3] = { chapter.name, "NvtourPanelChapter" }
    local col = #head[1][1] + #head[2][1]
    spots.chapter = { row = 0, col = col, end_row = 0, end_col = col + #chapter.name }
  end
  put(head)
  if step.label and step.label ~= "" then
    put({ { step.label, "NvtourCardTitle" } })
  end
  put({})
  put({ { doc_path(step) .. ":" .. fmt_range(step), "NvtourPanelFile" } })
  local file_row = #lines - 1
  for _, line in ipairs(side.git(step)) do
    put(line)
  end
  spots.file = { row = file_row, col = 0, end_row = #lines - 1, end_col = #lines[#lines] }
  if step.sheet then
    put({ { "↔ ", "NvtourNoteBorder" }, { sheet_label(step.sheet), "NvtourVia" } })
  end
  if step.lost then
    put({ { "✗ lost", "NvtourLost" }, { " · its lines were deleted", "NvtourVia" } })
  end
  local via = step.via and step.via ~= "" and oneline(step.via) or nil
  if step.n > 1 and (via or explicit_from(step)) then
    local src = source_of(step)
    put({ { "↑ from " .. src.n, "NvtourSign" .. cap(src.role) }, { via and ": " or "", "NvtourVia" }, { via or "" } })
  end
  put({})
  rules[#rules + 1] = #lines
  if step.note and step.note ~= "" then
    put({})
    for _, l in ipairs(split_lines(step.note)) do
      put({ { l } })
    end
  end
  local expects = expect_list(step.expect) or {}
  if #expects > 0 then
    -- One item per line: a long text wraps under its own item (breakindentopt "list").
    put({})
    put({ { "Watch", "NvtourCardTitle" } })
    for _, e in ipairs(expects) do
      put({ { "• ", "NvtourSign" .. R }, { e, "NvtourNoteCode" } })
    end
  end
  put({})
  put({})
  rules[#rules + 1] = #lines
  put({})
  -- The next and the previous step, each a block:
  --   Next  ]w
  --   2 → extract + insert          number, role mark, title
  --   line 52                       where it is
  --   ↓ get() calls refresh() …     the --via of the next step (why the tour goes there)
  local function link(key, name, s, none, via)
    put({ { name, "NvtourCardTitle" }, { key and ("  " .. key) or "", "NvtourPanelProgress" } }, s and s.n)
    if not s then
      put({ { none, "NvtourVia" } })
      return
    end
    local title = step_title(s)
    put({ { s.n .. " " .. (ROLE_MARK[s.role] or "•"), "NvtourSign" .. cap(s.role) }, { title ~= "" and (" " .. title) or "", "NvtourLabel" .. cap(s.role) } }, s.n)
    put({ { link_loc(s, step), "NvtourViaLoc" } }, s.n)
    if via then
      put({ { "↓ ", "NvtourNoteBorder" }, { via } }, s.n)
    end
  end
  local nxt, prv = S.tour.steps[step.n + 1], S.tour.steps[step.n - 1]
  link(S.keys.next, "Next", nxt, "last step", nxt and nxt.via and nxt.via ~= "" and oneline(nxt.via) or nil)
  put({})
  link(S.keys.prev, "Previous", prv, "first step")
  S.card.line_map, S.card.spots = map, spots
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  api.nvim_buf_clear_namespace(buf, S.ns_card, 0, -1)
  for _, h in ipairs(hls) do
    api.nvim_buf_set_extmark(buf, S.ns_card, h[1] - 1, h[2], { end_col = h[3], hl_group = h[4], priority = 150 })
  end
  local width = math.max(1, api.nvim_win_get_width(S.card.win) - 2 * side.MARGIN)
  for _, r in ipairs(rules) do
    api.nvim_buf_set_extmark(buf, S.ns_card, r - 1, 0, { virt_text = { { ("─"):rep(width), "NvtourNoteBorder" } }, virt_text_pos = "overlay" })
  end
  side.light()
  pcall(api.nvim_win_set_cursor, S.card.win, { 1, 0 })
end

--- Put the lights of side.flash (S.card.lit, a list of { spot = "chapter" | "file", group }) on the
--- card as it is drawn now: a redraw of the card (the panel redraws it after a jump) keeps them.
function side.light()
  local buf = S.card.buf
  if not valid_buf(buf) then
    return
  end
  api.nvim_buf_clear_namespace(buf, side.FLASH_NS, 0, -1)
  for _, p in ipairs(S.card.lit or {}) do
    local sp = (S.card.spots or {})[p.spot]
    if sp then
      pcall(api.nvim_buf_set_extmark, buf, side.FLASH_NS, sp.row, sp.col,
        { end_row = sp.end_row, end_col = sp.end_col, hl_group = p.group, hl_eol = sp.col == 0, priority = 140 })
    end
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
  sync_all()
  if not qf_valid() then
    new_qf()
  end
  local items = {}
  for _, s in ipairs(S.tour.steps) do
    local text = step_title(s)
    if text == "" then
      text = doc_path(s) .. ":" .. s.l1
    end
    local item = { lnum = s.l1, end_lnum = s.l2, text = ("[%s] %s%s"):format(s.role, text, s.lost and " (lost)" or "") }
    if s.sha or s.diagram then
      item.bufnr = step_buf(s) -- the read-only version or the diagram, not a file on disk
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
-- Steps follow edits
---------------------------------------------------------------------------

-- The anchors move with every edit; the step fields and the marks follow after a short pause. The
-- on_lines callback of nvim_buf_attach runs with the text locked, so it only starts a timer (the
-- same way as the sheet link): one burst of keys is one sync, and only the edited buffer is drawn
-- again. The callbacks call through _G, so they survive a runtime upgrade.
local SYNC_DEBOUNCE_MS = 150

local function sync_cancel(buf)
  local timer = S.sync_timers[buf]
  S.sync_timers[buf] = nil
  if timer then
    pcall(function()
      timer:stop()
      timer:close()
    end)
  end
end

--- True when a step that is not on a diagram is in `buf`.
local function has_steps(buf)
  for _, s in ipairs(S.tour.steps) do
    if s.buf == buf and not s.diagram then
      return true
    end
  end
  return false
end

--- Draw the steps of `buf` again. The current step in another buffer shows the line of the step
--- after it, so it is drawn again when that step is in `buf`.
local function rerender_buf(buf)
  if not valid_buf(buf) then
    return
  end
  api.nvim_buf_clear_namespace(buf, S.ns_steps, 0, -1)
  local win = tour_win(buf)
  for _, s in ipairs(S.tour.steps) do
    if s.buf == buf then
      s.extmark_ids = {}
      if api.nvim_buf_is_loaded(buf) then
        render_step(s, win)
      end
      s.stale = nil
    end
  end
  local cur = S.tour.steps[S.tour.current]
  local nxt = cur and S.tour.steps[cur.n + 1]
  if cur and cur.buf ~= buf and nxt and nxt.buf == buf and valid_buf(cur.buf) and api.nvim_buf_is_loaded(cur.buf) then
    render_step(cur, tour_win(cur.buf))
  end
end

--- The focus ranges of `buf` from their anchors. A range whose lines were all deleted is dropped.
--- Returns true when the ranges changed since the folds were made.
local function sync_focus(buf)
  local f = S.focus[buf]
  if not f or not f.anchors or not valid_buf(buf) or not api.nvim_buf_is_loaded(buf) then
    return false
  end
  if S.reloaded[buf] then
    reanchor_buf(buf)
  end
  local ranges, anchors = {}, {}
  for i, r in ipairs(f.ranges) do
    local a = f.anchors[i]
    local l1, l2, invalid
    if a then
      l1, l2, invalid = read_anchor(buf, a.id, a.tail)
    end
    if l1 and not invalid then
      if l1 ~= r[1] or l2 ~= r[2] then
        f.stale = true
      end
      ranges[#ranges + 1], anchors[#anchors + 1] = { l1, l2 }, a
    else
      f.stale = true
      if a then
        pcall(api.nvim_buf_del_extmark, buf, S.ns_anchor, a.id)
      end
    end
  end
  f.ranges, f.anchors = ranges, anchors
  return f.stale == true
end

--- Make the folds of the focus of `buf` again from its (moved) ranges, in the windows that have them.
--- While the user types in insert mode the folds wait for InsertLeave.
local function refold(buf)
  local f = S.focus[buf]
  if not f then
    return
  end
  if #f.ranges == 0 then
    unfocus_buf(buf)
    return
  end
  f.stale = nil
  if f.dim then
    if f.from_steps then
      draw_dim(buf, f.ranges) -- a dim of anchored ranges moves with its marks; this one is made from the steps
    end
    return
  end
  if vim.fn.mode():find("^[iR]") then
    f.refold = true
    return
  end
  f.refold = nil
  for win in pairs(f.saved or {}) do
    if valid_win(win) and api.nvim_win_get_buf(win) == buf then
      apply_folds(buf, win)
    end
  end
end

--- The ranges `{ l1, l2 }` of the steps in `buf` that a focus from the steps shows: not those of
--- diagrams and lost steps.
local function step_ranges(buf)
  local ranges = {}
  for _, s in ipairs(S.tour.steps) do
    if s.buf == buf and not s.diagram and not s.lost then
      ranges[#ranges + 1] = { s.l1, s.l2 }
    end
  end
  return ranges
end

--- Make the focus of `buf` again from the steps in it, when it is a focus from the steps (`focus
--- --steps`). The ranges are those of the steps now (their anchors are read by sync_step), so a step
--- that was added, edited, removed or moved by an edit changes the folds. With no step left in the
--- buffer the focus ends. Nothing is done when the ranges are the same: the folds stay as they are.
local function refocus_steps(buf)
  local f = S.focus[buf]
  if not f or not f.from_steps then
    return
  end
  local ranges = valid_buf(buf) and api.nvim_buf_is_loaded(buf) and step_ranges(buf) or {}
  if #ranges == 0 then
    unfocus_buf(buf)
    return
  end
  local merged = merge_ranges(ranges, f.from_steps.context, api.nvim_buf_line_count(buf))
  if vim.deep_equal(merged, f.ranges) and not f.refold then
    return
  end
  f.ranges = merged
  refold(buf)
end

--- refocus_steps for all buffers that have a focus from the steps.
local function refocus_all_steps()
  for buf, f in pairs(S.focus) do
    if f.from_steps then
      refocus_steps(buf)
    end
  end
end

api.nvim_create_autocmd("InsertLeave", {
  group = aug,
  callback = function()
    for buf, f in pairs(S.focus) do
      if f.refold then
        pcall(refold, buf)
      end
    end
  end,
})

--- The debounced sync of `buf`: read the anchors; when a step moved or was lost, draw its buffer
--- again and update the quickfix list and the panel.
local function sync_fire(buf)
  if not valid_buf(buf) then
    return
  end
  local stale = sync_buf(buf)
  local focus = sync_focus(buf)
  if stale then
    rerender_buf(buf)
    if qf_valid() then
      update_qf()
    end
    render_panel()
    refocus_steps(buf) -- a focus from the steps follows the moved or lost steps
  end
  if focus then
    refold(buf)
  end
end

--- Run sync_fire for `buf` after 150 ms without an edit.
sync_schedule = function(buf)
  local timer = S.sync_timers[buf]
  if timer then
    timer:stop()
  else
    timer = uv.new_timer()
    S.sync_timers[buf] = timer
  end
  timer:start(
    SYNC_DEBOUNCE_MS,
    0,
    vim.schedule_wrap(function()
      if S.sync_timers[buf] ~= timer then
        return -- cancelled or replaced
      end
      sync_cancel(buf)
      pcall(sync_fire, buf)
    end)
  )
end

--- Called by the on_lines callback of an attached buffer. Returns true to detach: the buffer has
--- no steps (the tour was cleared or the steps were moved away).
function M._buf_changed(buf)
  if not has_steps(buf) and not (S.focus[buf] and S.focus[buf].anchors) then
    S.attached[buf] = nil
    sync_cancel(buf)
    return true
  end
  sync_schedule(buf)
  return nil
end

--- Called by the on_reload callback: the content of the buffer was replaced (:edit, a reload after
--- a change on disk). The extmarks have collapsed, so the steps are anchored again from their last
--- lines at the next sync.
function M._buf_reloaded(buf)
  S.reloaded[buf] = true
  sync_schedule(buf)
end

function M._buf_detached(buf)
  S.attached[buf] = nil
  sync_cancel(buf)
end

--- Watch `buf` for edits (once). Detaches by itself when the buffer has no more steps.
attach_buf = function(buf)
  if S.attached[buf] or not valid_buf(buf) or not api.nvim_buf_is_loaded(buf) then
    return
  end
  local function call(name)
    return function(_, b)
      local m = _G.nvtour
      if not m then
        return true
      end
      return m[name](b)
    end
  end
  local ok = pcall(api.nvim_buf_attach, buf, false, {
    on_lines = call("_buf_changed"),
    on_reload = call("_buf_reloaded"),
    on_detach = call("_buf_detached"),
  })
  if ok then
    S.attached[buf] = true
  end
end

-- Fallback for on_reload: a buffer that is read again (the file was reloaded or the user wiped it
-- and came back) gets its steps anchored again.
api.nvim_create_autocmd("BufReadPost", {
  group = aug,
  callback = function(ev)
    if has_steps(ev.buf) or S.focus[ev.buf] then
      M._buf_reloaded(ev.buf)
    end
  end,
})

---------------------------------------------------------------------------
-- Winbar, flash, scrolling
---------------------------------------------------------------------------

-- The tour window shows the position in its winbar. The expression is evaluated on redraw, so
-- it is set once per window and buffer ('winbar' is reset when the window shows another buffer).
local WINBAR = "%{%v:lua.nvtour.winbar()%}"

local function sl_escape(s)
  return (s:gsub("%%", "%%%%"))
end

--- The diagram shown in `win` when it has a header line (the participant names), else nil.
local function header_diagram(win)
  local name = vim.b[api.nvim_win_get_buf(win)].nvtour_diagram
  local d = name and S.diagrams[name]
  return d and (d.header or 0) > 0 and d.lines[d.header] and d or nil
end

--- Header line of the diagram in the current window, when it is scrolled out of view: cut at
--- 'leftcol' and moved right by the gutter, so the names stay above their lifelines.
local function diagram_header()
  local win = api.nvim_get_current_win()
  local d = header_diagram(win)
  if not d then
    return nil
  end
  local view = vim.fn.winsaveview()
  if view.topline <= d.header then
    return nil
  end
  local textoff = vim.fn.getwininfo(win)[1].textoff
  local room = api.nvim_win_get_width(win) - textoff
  local out, col, run = { string.rep(" ", textoff) }, 0, nil
  for ch in d.lines[d.header]:gmatch("[%z\1-\127\194-\244][\128-\191]*") do
    local w = vim.fn.strdisplaywidth(ch)
    local from, to = col, col + w
    col = to
    if to > view.leftcol and from - view.leftcol < room then
      if from < view.leftcol or to - view.leftcol > room then
        ch = string.rep(" ", math.min(to, view.leftcol + room) - math.max(from, view.leftcol)) -- a wide character cut in two
      end
      local line = is_line_char(ch)
      if line ~= run then
        out[#out + 1] = line and "%#NvtourDiagramLine#" or "%*"
        run = line
      end
      out[#out + 1] = sl_escape(ch)
    end
  end
  return table.concat(out) .. "%*"
end

--- Winbar text: "nvtour 2/5 fault · label" and, on the right, where the next key goes. In a
--- diagram window whose header line is scrolled out of view: that line.
function M.winbar()
  local header = diagram_header()
  if header then
    return header
  end
  if vim.g.nvtour_winbar == false then
    return "" -- a diagram window with a header line, which has the winbar for the header only
  end
  local steps, n = S.tour.steps, S.tour.current
  local step = steps[n]
  if not step then
    return #steps > 0 and (" nvtour · %d step(s)"):format(#steps) or ""
  end
  local pos = ("%d/%d %s"):format(n, #steps, step.role)
  local at = step.ref and (" @" .. step.ref) or (step.diagram and (" · " .. doc_path(step))) or ""
  local label = (step.label and step.label ~= "") and (" · " .. step.label) or ""
  -- In a %{} item the window of the bar is the current window (g:statusline_winid is not set).
  local width = api.nvim_win_get_width(api.nvim_get_current_win())
  -- The chapter ("Name · 3/12") is the first part to go: it is shown only when the whole left
  -- side fits with it, before the label is cut.
  local chapter = chapter_of(step)
  chapter = chapter and (chapter.name .. " · ") or ""
  if vim.fn.strdisplaywidth(" nvtour " .. chapter .. pos .. at .. label) > width - 1 then
    chapter = ""
  end
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
    local loc = same and ("line " .. nxt.l1) or (doc_path(nxt) .. ":" .. nxt.l1 .. nat)
    local base = nxt.diagram and doc_path(nxt) or vim.fn.fnamemodify(nxt.file, ":t")
    local short = same and loc or (base .. ":" .. nxt.l1 .. nat)
    rights = { key .. loc, key .. short, "" }
    if nxt.label and nxt.label ~= "" then
      table.insert(rights, 1, key .. short .. " " .. nxt.label)
      table.insert(rights, 1, key .. loc .. " " .. nxt.label)
    end
  else
    rights = { "last step" .. (S.keys.first and (" · " .. S.keys.first .. " first") or ""), "last step", "" }
  end
  local room = width - vim.fn.strdisplaywidth(" nvtour " .. chapter .. pos .. at .. label) - 3
  local right = ""
  for _, r in ipairs(rights) do
    if vim.fn.strdisplaywidth(r) <= room then
      right = r
      break
    end
  end
  -- While side.flash lights up a new chapter, its name lights up here too (NvtourWinbarFlash).
  local group = S.winbar_lit and "NvtourWinbarFlash" or "NvtourPanelChapter"
  local head = chapter ~= "" and ("%#" .. group .. "#" .. sl_escape(chapter) .. "%*") or ""
  local out = (" nvtour %s%%#NvtourSign%s#%s%%*%s%s"):format(head, cap(step.role), pos, sl_escape(at), sl_escape(label))
  return out .. "%=" .. sl_escape(right) .. (right ~= "" and " " or "")
end

--- Show the tour winbar in `win`, unless it has a winbar of its own (from the user or a plugin)
--- or vim.g.nvtour_winbar is false. A diagram with a header line gets it also when it is false.
local function set_winbar(win)
  if not valid_win(win) or (vim.g.nvtour_winbar == false and not header_diagram(win)) then
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

--- Draw the winbars again: their text can change without a jump (a chapter was added or removed).
local function redraw_winbars()
  pcall(vim.cmd, "redrawstatus!")
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

---------------------------------------------------------------------------
-- Animation
---------------------------------------------------------------------------

-- A jump to a step animates when it stays in the same window. In the same buffer the view scrolls
-- to the step (vim.g.nvtour_scroll_ms, default 500). To another buffer (another file, or the same
-- file at another commit) the old text fades out and the new text fades in
-- (vim.g.nvtour_fade_ms, default 500). The step, the lists and the card change at once; only the
-- view moves over the frames, and the last frame is the view of a jump without animation. The next
-- jump, a command (dispatch) or a key ends an animation at once, so they always see that view. The
-- flash comes after the animation.
--
-- vim.g.nvtour_animate turns it on (true) or off (false); not set, it is on when a UI is attached
-- (a headless nvim, as in the tests, does not animate). The animate key or :NvtourAnimate toggles it.
local anim = {
  FRAME_MS = 16,
  KEY_NS = api.nvim_create_namespace("nvtour_anim_keys"),
  NS = api.nvim_create_namespace("nvtour_fade"), -- the rows of the curtain and its NvtourFade
}

--- Highlight the range of `step` for vim.g.nvtour_flash ms (default 300, 0 = off), so the eye
--- finds it after a jump.
function anim.flash(step)
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

function anim.on()
  if S.animate ~= nil then
    return S.animate
  end
  local v = vim.g.nvtour_animate
  if v == nil then
    return #api.nvim_list_uis() > 0
  end
  return v == true or v == 1
end

--- The duration of an animation: vim.g.nvtour_<name>_ms, else `default`; 0 turns it off.
function anim.ms(name, default)
  return math.max(tonumber(vim.g["nvtour_" .. name .. "_ms"]) or default, 0)
end

--- Call `frame(t)` on each frame, with t (the time) from 0 to 1, for `ms`; then `done()`. A window
--- has at most one animation: a new one ends the old one first.
function anim.run(win, ms, frame, done)
  anim.finish(win)
  local timer = uv.new_timer()
  local start = uv.hrtime()
  local run = {}
  run.finish = function()
    if S.anims[win] ~= run then
      return
    end
    S.anims[win] = nil
    pcall(function()
      timer:stop()
      timer:close()
    end)
    pcall(done)
  end
  S.anims[win] = run
  timer:start(
    anim.FRAME_MS,
    anim.FRAME_MS,
    vim.schedule_wrap(function()
      if S.anims[win] ~= run then
        return
      end
      local t = (uv.hrtime() - start) / 1e6 / ms
      if t >= 1 or not valid_win(win) or not pcall(frame, t) then
        run.finish()
      end
    end)
  )
end

function anim.finish(win)
  local run = S.anims[win]
  if run then
    run.finish()
  end
end

function anim.finish_all()
  for win in pairs(S.anims) do
    anim.finish(win)
  end
end

--- What a jump needs to animate from: the buffer and the view of `win` before the jump, and the
--- current step; nil when animation is off.
function anim.capture(win)
  if not anim.on() or not valid_win(win) then
    return nil
  end
  local view = api.nvim_win_call(win, vim.fn.winsaveview)
  return { win = win, buf = api.nvim_win_get_buf(win), view = view, step = S.tour.steps[S.tour.current] }
end

--- Start the animation of the jump to `step` in `win` (the view is already the final one) from
--- `from` (anim.capture). `done` runs when it ends. False when there is nothing to animate. A new
--- chapter in the same buffer fades too, so its title card has a place.
function anim.start(step, win, from, done)
  if not from then
    return false
  end
  -- What changed (a chapter, a file or version) is shown where it is not on the screen yet. The
  -- inline view has it in the step (the chapter heading, the change of file), so it gets no card;
  -- the side view lights it up in the card on the left. Only a hidden card leaves it to the curtain.
  local card = anim.card(step, from.step)
  if not side.on() then
    card = { hold = 0 }
  elseif side.shown() then
    side.flash(card)
    card = { hold = 0 }
  end
  if from.win ~= win or not valid_buf(from.buf) or not api.nvim_buf_is_loaded(from.buf) then
    return false
  end
  local ok, started
  if from.buf == step.buf and not card.chapter then
    ok, started = pcall(anim.scroll, win, from, done)
  else
    ok, started = pcall(anim.fade, win, from, step, done, card)
  end
  return ok and started
end

--- The lines of the title card of the jump from `prev` (nil: no current step) to `step`, as chunks
--- { text, hl }: the chapter when it changes (`card.chapter` is then true), and the file and its
--- version when they change (`card.file`). `card.hold` is the time the card stays: vim.g.nvtour_title_ms
--- for the chapter plus vim.g.nvtour_file_ms for the file (each default 1500; 0 leaves that line out);
--- `card.title_ms` and `card.file_ms` are the two (side.flash lights each part up for its time).
function anim.card(step, prev)
  local title_ms, file_ms = anim.ms("title", 1500), anim.ms("file", 1500)
  local card = { hold = 0, title_ms = title_ms, file_ms = file_ms }
  local chapter = chapter_of(step)
  if title_ms > 0 and chapter and (not prev or chapter_of(prev) ~= chapter) then
    card[#card + 1] = { { chapter.name, "NvtourPanelChapter" } } -- no "## ": the highlight shows the chapter
    card.chapter, card.hold = true, card.hold + title_ms
  end
  if file_ms > 0 and (not prev or not same_doc(prev, step)) then
    card[#card + 1] = vim.list_extend({ { "◇ ", "NvtourNoteBorder" } }, version_segs(step, prev or {}))
    card.file, card.hold = true, card.hold + file_ms
  end
  return card
end

--- Light up the parts of the side card that the jump changed (`card` of anim.card: `chapter`, `file`):
--- the chapter name for card.title_ms, the file and version lines for card.file_ms. The light is the
--- colour of the range flash, and it blinks: on and off every side.BLINK_MS, until its time ends.
--- A new chapter lights up in the winbar too (S.winbar_lit, NvtourWinbarFlash: the colour of the
--- chapter on the light). No curtain and no wait: the code is there at once, and the card says what
--- changed.
function side.flash(card)
  local parts = {}
  if card.chapter then
    parts[#parts + 1] = { spot = "chapter", ms = card.title_ms, group = "NvtourCardFlashChapter" }
  end
  if card.file then
    parts[#parts + 1] = { spot = "file", ms = card.file_ms, group = "NvtourCardFlashFile" }
  end
  if #parts == 0 then
    return
  end
  local bg = hl_attr("Normal", "bg") or (vim.o.background ~= "light" and 0x1d2021 or 0xfbf1c7)
  local light = hl_attr("NvtourFlash", "bg") or bg
  local bar_bg = hl_attr("WinBar", "bg") or bg
  local title = api.nvim_get_hl(0, { name = "NvtourPanelChapter", link = false })
  local function paint(p, on)
    api.nvim_set_hl(0, p.group, { bg = on and light or bg })
    if p.spot == "chapter" then
      api.nvim_set_hl(0, "NvtourWinbarFlash", { fg = title.fg, bold = title.bold, italic = title.italic, bg = on and light or bar_bg })
    end
  end
  local longest = 0
  for _, p in ipairs(parts) do
    p.on = true
    paint(p, true)
    longest = math.max(longest, p.ms)
  end
  S.card.lit = parts
  S.winbar_lit = card.chapter or nil
  side.light()
  redraw_winbars()
  anim.run(S.card.win, longest, function(t)
    local e = t * longest
    for _, p in ipairs(parts) do
      local on = e < p.ms and math.floor(e / side.BLINK_MS) % 2 == 0
      if on ~= p.on then -- a highlight is set only when it changes: each change redraws the screen
        p.on = on
        paint(p, on)
      end
    end
  end, function()
    if S.card.lit == parts then
      S.card.lit, S.winbar_lit = nil, nil
      side.light()
      redraw_winbars()
    end
  end)
end

--- The layout of the title card `card` (anim.card) in a box in the middle of a curtain of `width` x
--- `height` blank rows: { rows = { { row, col, text, marks = { { from, to, group } } } }, groups,
--- top, count }, with `col` and the marks in bytes; nil when it does not fit.
function anim.card_layout(card, width, height)
  local inner, lines = 0, {}
  for i, line in ipairs(card) do
    lines[i] = clip_chunks(line, width - 6)
    local w = 0
    for _, c in ipairs(lines[i]) do
      w = w + vim.fn.strdisplaywidth(c[1])
    end
    lines[i].width, inner = w, math.max(inner, w)
  end
  if inner == 0 or height < #lines + 2 or width < 10 then
    return nil
  end
  local top, col = math.floor((height - #lines - 2) / 2), math.floor((width - inner - 4) / 2)
  local layout = { rows = {}, groups = { NvtourNoteBorder = true }, top = top, count = #lines + 2, width = inner + 4 }
  local function row(r, chunks)
    local text, marks = "", {}
    for _, c in ipairs(chunks) do
      marks[#marks + 1] = { col + #text, col + #text + #c[1], c[2] }
      text = text .. c[1]
      layout.groups[c[2]] = true
    end
    layout.rows[#layout.rows + 1] = { row = r, col = col, text = text, marks = marks }
  end
  local bar = string.rep("─", inner + 2)
  row(top, { { "╭" .. bar .. "╮", "NvtourNoteBorder" } })
  for i, line in ipairs(lines) do
    local chunks = { { "│ ", "NvtourNoteBorder" } }
    vim.list_extend(chunks, line)
    chunks[#chunks + 1] = { string.rep(" ", inner - line.width) .. " │", "NvtourNoteBorder" }
    row(top + i, chunks)
  end
  row(top + #lines + 1, { { "╰" .. bar .. "╯", "NvtourNoteBorder" } })
  return layout
end

--- Write the card of `layout` into the blank rows of the curtain buffer `cbuf` (`on`), or blank them
--- again. Each chunk gets the group "NvtourFade" .. its group, of anim.NS. A blended float fades
--- its characters only half way, so the card is in the curtain only while the curtain is opaque.
function anim.card_put(cbuf, layout, on)
  for _, r in ipairs(layout.rows) do
    if on then
      api.nvim_buf_set_text(cbuf, r.row, r.col, r.row, r.col + layout.width, { r.text })
      r.ids = {}
      for _, m in ipairs(r.marks) do
        r.ids[#r.ids + 1] = api.nvim_buf_set_extmark(cbuf, anim.NS, r.row, m[1], { end_col = m[2], hl_group = "NvtourFade" .. m[3], priority = 5000 })
      end
    else
      for _, id in ipairs(r.ids or {}) do
        pcall(api.nvim_buf_del_extmark, cbuf, anim.NS, id)
      end
      api.nvim_buf_set_text(cbuf, r.row, r.col, r.row, r.col + #r.text, { string.rep(" ", layout.width) })
    end
  end
end

--- Run `count` times the key `key` (<C-e> or <C-y>) with :normal! in `win`. vim.on_key sees the keys
--- of :normal! too: while `anim.busy` is set, it does not take them for keys of the user.
function anim.keys(win, count, key)
  anim.busy = true
  local ok, err = pcall(api.nvim_win_call, win, function()
    vim.cmd(("normal! %d%s"):format(count, key))
  end)
  anim.busy = false
  if not ok then
    error(err, 0)
  end
end

--- The lines from `a` to `b` (exclusive) in `win` as <C-e> counts them (a closed fold is one), at
--- most `cap`.
function anim.scroll_lines(win, a, b, cap)
  return api.nvim_win_call(win, function()
    local n, l = 0, a
    while l < b and n < cap do
      local fold = vim.fn.foldclosedend(l)
      l = (fold ~= -1 and fold or l) + 1
      n = n + 1
    end
    return n
  end)
end

--- Scroll from the old view to the final one. A long way starts one window height before the step,
--- on the side of the old view, so the scroll is never slow.
function anim.scroll(win, from, done)
  local ms = anim.ms("scroll", 500)
  local to = api.nvim_win_call(win, vim.fn.winsaveview)
  if ms == 0 or from.view.topline == to.topline then
    return false
  end
  local down = to.topline > from.view.topline
  local a, b = math.min(from.view.topline, to.topline), math.max(from.view.topline, to.topline)
  local n = anim.scroll_lines(win, a, b, api.nvim_win_get_height(win))
  -- <C-e> scrolls the text up (the view moves down the file), <C-y> the other way.
  local back, fwd = down and "\25" or "\5", down and "\5" or "\25"
  anim.keys(win, n, back)
  local moved = 0
  anim.run(win, ms, function(t)
    local k = math.floor(n * (1 - (1 - t) ^ 3) + 0.5) - moved -- eased out: fast, then slow
    if k > 0 then
      anim.keys(win, k, fwd)
      moved = moved + k
    end
  end, function()
    if valid_win(win) then
      api.nvim_win_call(win, function()
        vim.fn.winrestview(to)
      end)
    end
    done()
  end)
  return true
end

--- The file of `buf`: the file of a step at a git ref, else the name of the buffer.
function anim.buf_file(buf)
  local r = vim.b[buf].nvtour_ref
  return vim.fs.normalize(r and r.file or api.nvim_buf_get_name(buf))
end

--- Line `l` of the lines `new` mapped to the lines `old` of another version of the file: the same
--- line when it did not change, else the first line of the change.
function anim.map_line(old, new, l)
  local ok, hunks = pcall(vim.diff, table.concat(old, "\n") .. "\n", table.concat(new, "\n") .. "\n", { result_type = "indices" })
  if not ok then
    return math.min(l, #old)
  end
  local offset = 0
  for _, h in ipairs(hunks) do
    local sa, ca, sb, cb = h[1], h[2], h[3], h[4]
    -- A count of 0 gives the line before the change as its start.
    local astart, bstart = ca == 0 and sa + 1 or sa, cb == 0 and sb + 1 or sb
    if l < bstart then
      break
    end
    if l < bstart + cb then
      return math.max(1, math.min(astart, #old))
    end
    offset = (astart + ca) - (bstart + cb)
  end
  return math.max(1, math.min(l + offset, #old))
end

--- Keys of the rows of the text area of `win` (1 = the first row) that show buffer text: two rows
--- with the same key look the same (the text of the line, the part of a wrapped line, the extmarks
--- on it). Rows of virtual lines and closed folds have no key. The rows are counted from the view
--- (nvim_win_text_height), not from the screen: the window was not drawn since its view changed.
function anim.row_keys(win)
  local buf = api.nvim_win_get_buf(win)
  local height = vim.fn.getwininfo(win)[1].height
  local view = api.nvim_win_call(win, vim.fn.winsaveview)
  local count = api.nvim_buf_line_count(buf)
  local function rows(a, b) -- the rows of lines a..b, with the virtual lines above each of them
    local ok, h = pcall(api.nvim_win_text_height, win, { start_row = a - 1, end_row = b - 1 })
    return ok and h.all or (b - a + 1), ok and h.fill or 0
  end
  local _, top_fill = rows(view.topline, view.topline)
  local hidden = top_fill - (view.topfill or 0) -- the virtual lines above the top line that are not shown
  local keys = {}
  local l = view.topline
  while l <= count do
    local last = api.nvim_win_call(win, function()
      return vim.fn.foldclosedend(l)
    end)
    local all, fill = rows(l, l)
    local row = rows(view.topline, l) - hidden - (all - fill) + 1 -- the first row of line l
    if row > height then
      break
    end
    if last ~= -1 then
      l = last + 1
    else
      local key = { api.nvim_buf_get_lines(buf, l - 1, l, false)[1] or "" }
      for _, m in ipairs(api.nvim_buf_get_extmarks(buf, -1, { l - 1, 0 }, { l - 1, -1 }, { details = true })) do
        local d = m[4]
        if d.ns_id ~= S.ns_flash then -- the flash of the step before ends anyway
          local virt = {}
          for _, c in ipairs(d.virt_text or {}) do
            virt[#virt + 1] = c[1]
          end
          key[#key + 1] = table.concat({ m[3], d.end_col or "", d.hl_group or "", d.line_hl_group or "", table.concat(virt) }, ":")
        end
      end
      key = table.concat(key, "\0")
      for i = 0, all - fill - 1 do
        if row + i >= 1 then
          keys[row + i] = key .. "\0" .. i
        end
      end
      l = l + 1
    end
  end
  return keys
end

--- Show `buf` in `win` at `view` for the fade: no autocmds, no change of the jumplist or of the
--- alternate file.
function anim.show(win, buf, view)
  api.nvim_win_call(win, function()
    vim.cmd(("noautocmd keepalt keepjumps buffer %d"):format(buf))
    vim.fn.winrestview(view)
  end)
end

--- Fade from the old buffer to the new one, through the background: the old buffer is shown again
--- under a blank float, the curtain, which fades in; then the new buffer comes back and the curtain
--- fades out. (A float that blends shows the windows under it, never another float, so the old text
--- cannot be a float.) The gutter always fades; a row whose text looks the same before and after
--- stays. For the same file at another version, the old lines are put where the same lines of the
--- new version are (when that is near the old view), so only the lines that changed fade. With a
--- title card (`card`, anim.card), the curtain stays opaque for `card.hold` and shows the card in the
--- middle; its text fades with the curtain.
function anim.fade(win, from, step, done, card)
  local ms = anim.ms("fade", 500)
  if ms == 0 or not vim.o.termguicolors then
    return false -- without RGB colours a float cannot blend
  end
  local info = vim.fn.getwininfo(win)[1]
  if not info or info.height < 1 or info.width < 1 or api.nvim_win_get_buf(win) ~= step.buf then
    return false
  end
  for _, b in ipairs({ from.buf, step.buf }) do
    local bh = vim.bo[b].bufhidden
    if bh ~= "" and bh ~= "hide" then
      return false -- leaving the window would unload or delete it
    end
  end
  local to = api.nvim_win_call(win, vim.fn.winsaveview)
  local after = anim.row_keys(win)
  local view, align = from.view, false
  if from.buf ~= step.buf and anim.buf_file(from.buf) == anim.buf_file(step.buf) then
    -- Not when the old text would jump far first: the step is at other code, nothing stays.
    local a, b = api.nvim_buf_get_lines(from.buf, 0, -1, false), api.nvim_buf_get_lines(step.buf, 0, -1, false)
    local top = anim.map_line(a, b, to.topline)
    if math.abs(top - from.view.topline) < info.height then
      view = { topline = top, topfill = to.topfill, lnum = anim.map_line(a, b, to.lnum), col = 0, leftcol = to.leftcol }
      align = true
    end
  end
  -- The old buffer is read-only while it is shown again: a key typed then must not change it.
  local modifiable = vim.bo[from.buf].modifiable
  -- The step before keeps the look of the current step while its buffer is shown again.
  local prev = from.step
  if not (prev and prev ~= step and prev.buf == from.buf and not prev.lost) then
    prev = nil
  end
  local cbuf, back
  local function restore()
    if not back then
      back = true
      pcall(function()
        vim.bo[from.buf].modifiable = modifiable
      end)
      if prev and valid_buf(prev.buf) then
        pcall(render_step, prev, tour_win(prev.buf))
      end
      if from.buf == step.buf then
        pcall(render_step, step, win) -- the current step again (the old view drew it as not current)
      end
      if valid_win(win) and valid_buf(step.buf) and api.nvim_win_get_buf(win) == from.buf then
        pcall(anim.show, win, step.buf, to)
      end
    end
  end
  local function close()
    restore()
    if cbuf then
      pcall(api.nvim_buf_delete, cbuf, { force = true })
    end
  end
  local ok, err = pcall(function()
    vim.bo[from.buf].modifiable = false
    if prev then
      local current = S.tour.current
      S.tour.current = prev.n
      pcall(render_step, prev, win)
      if from.buf == step.buf then
        pcall(render_step, step, win) -- in the same buffer (a new chapter): drawn as before the jump
      end
      S.tour.current = current
    end
    anim.show(win, from.buf, view)
    local before = anim.row_keys(win)
    if align then
      -- The notes of the two steps differ in height: shift the old text by the rows that make the
      -- most rows look the same.
      local best, most = 0, -1
      for d = -math.floor(info.height / 2), math.floor(info.height / 2) do
        local n = 0
        for r = 1, info.height do
          if after[r] ~= nil and before[r + d] == after[r] then
            n = n + 1
          end
        end
        if n > most or (n == most and math.abs(d) < math.abs(best)) then
          best, most = d, n
        end
      end
      if best ~= 0 then
        anim.keys(win, math.abs(best), best > 0 and "\5" or "\25")
        before = anim.row_keys(win)
      end
    end
    -- The curtain: blank rows (written: its name is nvtour://fade), transparent (winblend 100)
    -- except where NvtourFade is, whose blend goes from 100 to 0 and back.
    cbuf = api.nvim_create_buf(false, true)
    vim.bo[cbuf].bufhidden = "wipe"
    unique_name("nvtour://fade", cbuf)
    local blank, rows = string.rep(" ", info.width), {}
    for r = 1, info.height do
      rows[r] = blank
    end
    api.nvim_buf_set_lines(cbuf, 0, -1, false, rows)
    local gutter = math.max(info.textoff, vim.fn.getwininfo(win)[1].textoff) -- the signs of the two can differ
    local layout = card and #card > 0 and anim.card_layout(card, info.width, info.height) or nil
    local hold = layout and card.hold or 0
    local card_top, card_rows = layout and layout.top or -1, layout and layout.count or 0
    for r = 1, info.height do
      -- The rows of the card always fade: the blank cells in the box must cover the text.
      local same = before[r] ~= nil and before[r] == after[r] and not (r > card_top and r <= card_top + card_rows)
      local mark = same and { end_col = gutter, hl_group = "NvtourFade" } or { line_hl_group = "NvtourFade", priority = 100 }
      api.nvim_buf_set_extmark(cbuf, anim.NS, r - 1, 0, mark)
    end
    local curtain = api.nvim_open_win(cbuf, false, {
      relative = "editor",
      row = info.winrow - 1 + (info.winbar or 0),
      col = info.wincol - 1,
      width = info.width,
      height = info.height,
      focusable = false,
      style = "minimal",
      zindex = 1, -- under every other float (the peek, messages)
      noautocmd = true,
    })
    vim.wo[curtain].winbar = "" -- a new window takes the winbar of the current window
    vim.wo[curtain].wrap = false
    vim.wo[curtain].winblend = 100
    vim.wo[curtain].winhighlight = "NormalFloat:Normal"
    local bg = hl_attr("Normal", "bg") or (vim.o.background ~= "light" and 0x1d2021 or 0xfbf1c7)
    local fg = hl_attr("Normal", "fg") or (vim.o.background ~= "light" and 0xebdbb2 or 0x3c3836)
    -- `o`: the opacity of the curtain; `a`: of the card, which fades by its colours (blend 0).
    local function opacity(o, a)
      api.nvim_set_hl(anim.NS, "NvtourFade", { bg = bg, blend = 100 - math.floor(100 * o + 0.5) })
      for g in pairs(a and layout.groups or {}) do
        local h = api.nvim_get_hl(0, { name = g, link = false })
        api.nvim_set_hl(anim.NS, "NvtourFade" .. g, { fg = blend(h.fg or fg, bg, a), bg = bg, bold = h.bold, italic = h.italic, blend = 0 })
      end
      api.nvim_win_set_hl_ns(curtain, anim.NS) -- again: a change of the group alone redraws nothing
    end
    opacity(0)
    -- In: half the fade; opaque for `hold`, with the card; out: the other half. The card fades in and
    -- out over `ramp` ms of the hold.
    local ramp, shown = math.min(150, hold / 3), false
    anim.run(win, ms + hold, function(t)
      local e = t * (ms + hold)
      local on = layout ~= nil and e >= ms / 2 and e < ms / 2 + hold
      if on ~= shown then
        anim.card_put(cbuf, layout, on)
        shown = on
      end
      local o = e < ms / 2 and math.sin(math.pi * e / ms) or e < ms / 2 + hold and 1 or math.sin(math.pi * (e - hold) / ms)
      opacity(o, on and math.max(0, math.min(1, (e - ms / 2) / ramp, (ms / 2 + hold - e) / ramp)) or nil)
      if e >= ms / 2 then
        restore() -- the curtain covers the old text now
      end
    end, function()
      close()
      done()
    end)
  end)
  if not ok then
    close()
    error(err, 0)
  end
  return true
end

-- A key ends the animations that run when it is typed. Scheduled (no window changes in an on_key
-- callback), so only these: an animation that the key itself starts (]w) stays.
vim.on_key(function()
  if next(S.anims) == nil or anim.busy then
    return
  end
  local runs = vim.tbl_values(S.anims)
  vim.schedule(function()
    for _, run in ipairs(runs) do
      run.finish()
    end
  end)
end, anim.KEY_NS)
M._anim = anim -- for the tests

---------------------------------------------------------------------------
-- Scroll to a step
---------------------------------------------------------------------------

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
    return res
  end
  -- Marks the cursor line, or the selection in visual mode (which ends). With `note`, then asks for
  -- a comment: an empty or cancelled input keeps the mark without one.
  local function mark_key(note)
    local res = run("mark")
    local mode = api.nvim_get_mode().mode
    if mode == "v" or mode == "V" or mode == "\22" then
      pcall(vim.cmd, "normal! \27")
    end
    if note and res.ok then
      vim.ui.input({ prompt = "Note for the agent: " }, function(text)
        text = text and vim.trim(text) or ""
        if text ~= "" then
          run("mark_comment", { id = res.id, comment = text })
        end
      end)
    end
  end
  return {
    mark = function()
      mark_key(false)
    end,
    mark_note = function()
      mark_key(true)
    end,
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
    -- ]c and [c are also the keys of vim to move between changes: in a window in diff mode they
    -- keep that meaning.
    next_chapter = function()
      if vim.wo.diff then
        pcall(vim.cmd, "normal! " .. vim.v.count1 .. "]c")
      else
        run("next_chapter")
      end
    end,
    prev_chapter = function()
      if vim.wo.diff then
        pcall(vim.cmd, "normal! " .. vim.v.count1 .. "[c")
      else
        run("prev_chapter")
      end
    end,
    panel = function()
      run("panel", { toggle = true })
    end,
    peek = function()
      run("peek")
    end,
    view = function()
      run("view")
    end,
    card = function()
      run("card")
    end,
    range_style = function()
      run("range_style")
    end,
    animate = function()
      run("animate")
    end,
    clear = function()
      run("clear")
    end,
  }
end

--- Install the keys of `set` (KEYS.tour or KEYS.always) that are free. A key that is taken is skipped
--- with one warning. The keys of the marks also work in visual mode.
local function install_keymaps(set)
  local always = set == KEYS.always
  if (always and S.mark_keymaps_installed) or (not always and S.keymaps_installed) then
    return
  end
  local cfg = vim.g.nvtour_keys
  if type(cfg) ~= "table" then
    cfg = {}
  end
  local actions = key_actions()
  local reg = always and S.mark_keys or S.keys
  for _, name in ipairs(set) do
    local lhs = cfg[name]
    if lhs == nil then
      lhs = DEFAULT_KEYS[name]
    end
    if type(lhs) == "string" and lhs ~= "" then
      if vim.fn.maparg(lhs, "n") == "" and (not always or vim.fn.maparg(lhs, "x") == "") then
        vim.keymap.set(always and { "n", "x" } or "n", lhs, actions[name], { desc = "nvtour " .. name })
        S.keymaps[lhs] = true
        reg[name] = lhs
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
  if always then
    S.mark_keymaps_installed = true
  else
    S.keymaps_installed = true
  end
end

--- Remove the keys of `set` (KEYS.tour or KEYS.always) that we installed.
local function remove_keymaps(set)
  local always = set == KEYS.always
  local reg = always and S.mark_keys or S.keys
  for _, name in ipairs(set) do
    local lhs = reg[name]
    if lhs then
      pcall(vim.keymap.del, "n", lhs)
      if always then
        pcall(vim.keymap.del, "x", lhs)
      end
      S.keymaps[lhs] = nil
      reg[name] = nil
    end
  end
  if always then
    S.mark_keymaps_installed = false
  else
    S.keymaps_installed = false
  end
end

local function clear_step_marks()
  for _, b in ipairs(api.nvim_list_bufs()) do
    if valid_buf(b) then
      api.nvim_buf_clear_namespace(b, S.ns_steps, 0, -1)
      api.nvim_buf_clear_namespace(b, S.ns_focus, 0, -1)
      api.nvim_buf_clear_namespace(b, S.ns_flash, 0, -1)
      api.nvim_buf_clear_namespace(b, S.ns_anchor, 0, -1)
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

---------------------------------------------------------------------------
-- Sheet link
---------------------------------------------------------------------------

-- A step with a sheet link ({ sheet = ID, step = "3" or "step-id" }) shows that step of the sheet
-- in the viewer of the `sheet` tool when it becomes current. Only `sheet goto -q --no-autostart ID STEP`
-- is ever run, detached, without waiting; any failure is ignored (safety rule 7 in DESIGN.md).
local SHEET_DEBOUNCE_MS = 150

local function sheet_cmd()
  local c = vim.g.nvtour_sheet_cmd
  return (type(c) == "string" and c ~= "") and c or "sheet"
end

--- Drop the call that waits to start.
local function sheet_cancel()
  local timer = S.sheet_pending.timer
  S.sheet_pending = {}
  if timer then
    pcall(function()
      timer:stop()
      timer:close()
    end)
  end
end

local function sheet_fire(step)
  local link = step.sheet
  if not link or S.tour.steps[step.n] ~= step then
    return -- the link was removed or the step is gone
  end
  local cmd = sheet_cmd()
  if vim.fn.executable(cmd) ~= 1 then
    return
  end
  -- link.env (SHEET_SOCKET, SHEET_HOME from the agent) is merged over the environment of nvim.
  pcall(vim.system, { cmd, "goto", "-q", "--no-autostart", link.sheet, tostring(link.step) }, {
    detach = true,
    stdout = false,
    stderr = false,
    env = link.env,
  })
end

--- `step` became the current step. The last step wins: a step without a link cancels the call of the
--- step before it, so holding a key starts one process, not one per step.
local function sheet_schedule(step)
  sheet_cancel()
  if not step.sheet then
    return
  end
  local timer = uv.new_timer()
  S.sheet_pending = { timer = timer, step = step }
  timer:start(
    SHEET_DEBOUNCE_MS,
    0,
    vim.schedule_wrap(function()
      if S.sheet_pending.timer ~= timer then
        return -- cancelled or replaced
      end
      sheet_cancel()
      pcall(sheet_fire, step)
    end)
  )
end

--- The link of a `--sheet` argument. Returns (link, given): given is false when the argument is
--- absent (keep the old link); link is nil when the argument removes the link. `#STEP` takes the
--- sheet of the tour and fails (exit 6) when the tour has none.
--- env is the SHEET_SOCKET/SHEET_HOME of the CLI of this call (only the two names, only strings).
local function sheet_env_of(env)
  local out
  if type(env) == "table" then
    for _, name in ipairs({ "SHEET_SOCKET", "SHEET_HOME" }) do
      if type(env[name]) == "string" and env[name] ~= "" then
        out = out or {}
        out[name] = env[name]
      end
    end
  end
  return out
end

--- The link of a --sheet value (see parse_sheet_link in cli.py). `tour` (default: the tour) gives the
--- sheet and the variables that '#STEP' uses; `apply` passes the ones of the tour it is making.
local function resolve_sheet(v, env, tour)
  tour = tour or S.tour
  if v == nil then
    return nil, false
  end
  if type(v) ~= "table" then
    return nil, true
  end
  local from_tour = v.sheet == nil or v.sheet == ""
  local id = v.sheet
  if id == nil or id == "" then
    id = tour.sheet
    if not id then
      fail(
        ("--sheet #%s: the tour has no sheet; start it with 'nvtour start --sheet SHEET' or use SHEET#STEP"):format(
          tostring(v.step)
        ),
        6
      )
    end
  end
  local link_env = sheet_env_of(env)
  if not link_env and from_tour then
    link_env = tour.sheet_env -- '#STEP' of a tour whose 'start --sheet' had the variables
  end
  return { sheet = id, step = tostring(v.step), env = link_env }, true
end

local function same_link(a, b)
  if a == nil or b == nil then
    return a == b
  end
  return a.sheet == b.sheet and a.step == b.step and vim.deep_equal(a.env, b.env)
end

-- The follow mode (see "Follow the cursor") keeps its functions in one table: a Lua chunk has at most
-- 200 local variables.
local follow = {}

--- Stop the pending follow checks of all windows.
function follow.cancel_all()
  for win, timer in pairs(S.follow_timers) do
    S.follow_timers[win] = nil
    pcall(function()
      timer:stop()
      timer:close()
    end)
  end
  S.follow_jump = {}
end

--- Remove everything nvtour created. keep_panel keeps the panel window open.
local function reset(keep_panel)
  anim.finish_all()
  sheet_cancel()
  follow.cancel_all()
  peek.close()
  for buf in pairs(S.sync_timers) do
    sync_cancel(buf)
  end
  S.reloaded = {}
  for buf in pairs(S.focus) do
    unfocus_buf(buf)
  end
  S.focus = {}
  clear_step_marks()
  clear_winbars()
  diff_close()
  remove_keymaps(KEYS.tour) -- the keys of the marks stay: the marks belong to the user
  side.close()
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
  S.tour = { title = "", steps = {}, current = 0, qf_id = qf_id, chapters = {} }
  S.refs = {} -- their buffers are deleted below with the other nvtour:// buffers
  S.diagrams = {} -- the same
  S.diagram_split = nil
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
    S.card.user_closed = false
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
  install_keymaps(KEYS.tour)
end

---------------------------------------------------------------------------
-- Steps
---------------------------------------------------------------------------

--- Installed keys by action; an empty map is sent as {} (not []).
local function installed_keys()
  return next(S.keys) and S.keys or vim.empty_dict()
end

local function step_text(step)
  if step.lost or not valid_buf(step.buf) or not api.nvim_buf_is_loaded(step.buf) then
    return nil
  end
  local line = api.nvim_buf_get_lines(step.buf, step.l1 - 1, step.l1, false)[1] or ""
  return vim.fn.strcharpart(line, 0, 100)
end

local function step_result(step, extra)
  sync_step(step)
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
    diagram = step.diagram,
    sheet = step.sheet and sheet_label(step.sheet) or nil,
    chapter = (chapter_of(step) or {}).name,
    resolved_from = step.resolved_from,
    lost = step.lost or nil,
    text = step_text(step),
    keys = installed_keys(),
  }
  for k, v in pairs(extra or {}) do
    r[k] = v
  end
  return r
end

--- `step` becomes the current step: draw the old and the new step, show the winbar in `win`, start the
--- sheet link. `opts.scroll` puts the cursor on the step and scrolls to it, then flashes the range
--- (a jump; follow mode moves nothing), animated from `opts.from` (anim.capture). `opts.lists` also
--- updates the quickfix list and the panel (the caller of a jump does that after it, when the step is
--- in the tour).
local function make_current(step, win, opts)
  local old = S.tour.steps[S.tour.current]
  S.tour.current = step.n
  if old and old ~= step and valid_buf(old.buf) and api.nvim_buf_is_loaded(old.buf) then
    render_step(old, tour_win(old.buf))
  end
  set_winbar(win) -- before the note is wrapped and the view is computed: it takes a row
  side.render() -- the card opens before the step is drawn, so the rules have the final width
  render_step(step, win)
  if opts.scroll then
    scroll_to(step, win)
    S.follow_jump = { win = win, buf = step.buf, line = step.l1 }
    local function done()
      anim.flash(step)
    end
    if not anim.start(step, win, opts.from, done) then
      done()
    end
  end
  if opts.lists then
    update_qf(step.n)
    render_panel()
  end
  sheet_schedule(step)
end

local function jump(step)
  anim.finish_all() -- before the view is read: the end of an animation sets the view
  step_buf(step)
  sync_step(step)
  local win = tour_win(step.buf, true)
  local from = anim.capture(win)
  -- The position before the jump goes into the jumplist of the window, so <C-o> goes back to it.
  pcall(api.nvim_win_call, win, function()
    vim.cmd("normal! m'")
  end)
  win = show_buf(win, step.buf)
  if not step.diagram then
    S.tour_win = win -- the diagram window is never a window for code
  end
  enter_win(win)
  make_current(step, win, { scroll = true, from = from })
end

local function announce(step)
  local total = #S.tour.steps
  local at = step.ref and (" @" .. step.ref) or ""
  notify(("nvtour %d/%d: %s"):format(step.n, total, step.label or (doc_path(step) .. ":" .. step.l1 .. at)))
end

step_goto = function(n)
  local steps = S.tour.steps
  if #steps == 0 then
    fail("no steps in tour; add one with 'nvtour step'", 6)
  end
  if n < 1 or n > #steps then
    fail(("no such step %d (tour has %d)"):format(n, #steps), 6)
  end
  sync_all()
  local step = steps[n]
  if step.lost then
    fail(("step %d: its lines were deleted; re-point it with: nvtour edit %d FILE:L1-L2"):format(n, n), 6)
  end
  jump(step)
  update_qf(n)
  render_panel()
  announce(step)
  return step_result(step)
end

---------------------------------------------------------------------------
-- Follow the cursor
---------------------------------------------------------------------------

-- With vim.g.nvtour_follow_cursor on, the step that holds the cursor becomes the current step. The
-- one CursorMoved autocmd always exists and returns at once when the mode is off. A move starts a
-- timer per window (like the sheet link): a burst of moves is one check. The check changes no
-- cursor, view or focus, and gives no announcement.
follow.DEBOUNCE_MS = 100

function follow.on()
  local v = vim.g.nvtour_follow_cursor
  return v == true or v == 1
end

--- True for a window whose cursor can follow: a file window or a diagram window (not the panel, not a
--- window of a diff tab) that shows a buffer with a step.
function follow.win(win)
  if not is_normal_win(win) or win == S.panel.win or is_diff_tab(api.nvim_win_get_tabpage(win)) then
    return false
  end
  local buf = api.nvim_win_get_buf(win)
  if not (file_win(win) or is_diagram_buf(buf)) then
    return false
  end
  for _, s in ipairs(S.tour.steps) do
    if s.buf == buf then
      return true
    end
  end
  return false
end

--- The step that holds the cursor line `line` of `buf`: the innermost (the greatest l1, then the
--- smallest l2; the current step wins a tie). Lost steps do not count.
function follow.step_at(buf, line)
  local best
  for _, s in ipairs(S.tour.steps) do
    if s.buf == buf and not s.lost and s.l1 <= line and line <= s.l2 then
      if
        not best
        or s.l1 > best.l1
        or (s.l1 == best.l1 and s.l2 < best.l2)
        or (s.l1 == best.l1 and s.l2 == best.l2 and s.n == S.tour.current)
      then
        best = s
      end
    end
  end
  return best
end

function follow.fire(win, buf)
  if not follow.on() or not follow.win(win) or api.nvim_win_get_buf(win) ~= buf or S.anims[win] then
    return -- an animation moves the cursor of its window
  end
  local line = api.nvim_win_get_cursor(win)[1]
  local at = S.follow_jump
  if at.win == win and at.buf == buf and at.line == line then
    return -- the cursor is where a jump put it: the jump chose the step
  end
  at.win = nil
  sync_buf(buf)
  local step = follow.step_at(buf, line)
  if step and step.n ~= S.tour.current then
    make_current(step, win, { lists = true })
    redraw_winbars()
  end
end

function follow.schedule(win)
  local timer = S.follow_timers[win]
  if timer then
    timer:stop()
  else
    timer = uv.new_timer()
    S.follow_timers[win] = timer
  end
  local buf = api.nvim_win_get_buf(win)
  timer:start(
    follow.DEBOUNCE_MS,
    0,
    vim.schedule_wrap(function()
      if S.follow_timers[win] ~= timer then
        return -- cancelled or replaced
      end
      S.follow_timers[win] = nil
      pcall(function()
        timer:stop()
        timer:close()
      end)
      pcall(follow.fire, win, buf)
    end)
  )
end

api.nvim_create_autocmd("CursorMoved", {
  group = aug,
  callback = function()
    if not follow.on() or #S.tour.steps == 0 then
      return
    end
    local win = api.nvim_get_current_win()
    if follow.win(win) then
      follow.schedule(win)
    end
  end,
})

---------------------------------------------------------------------------
-- Peek a step
---------------------------------------------------------------------------

-- `peek` (a key, `K` in the panel, :NvtourPeek [N]) shows the full content of a step in a float at
-- the cursor: the "◇" line, the note, the suggested code and the "next" line, made by the same
-- functions as the lines under the code. The step does not become the current step: nothing in the
-- tour changes. The float shows a scratch buffer nvtour://peek, which is the only buffer written.
-- S.peek = { win, buf, step, group, maps } exists while the float is open.

--- Close the peek float: the float and its buffer, the autocmds and the keys of the source buffer.
function peek.close()
  local p = S.peek
  S.peek = {}
  if p.group then
    pcall(api.nvim_del_augroup_by_id, p.group)
  end
  for _, m in ipairs(p.maps or {}) do
    pcall(vim.keymap.del, "n", m[2], { buffer = m[1] })
  end
  if valid_win(p.win) then
    pcall(api.nvim_win_close, p.win, true)
  end
  if valid_buf(p.buf) then
    pcall(api.nvim_buf_delete, p.buf, { force = true })
  end
end

--- Close the float `win` after the current event (a window cannot always be closed inside an autocmd).
function peek.close_later(win)
  vim.schedule(function()
    if S.peek.win == win then
      peek.close()
    end
  end)
end

--- The cells of the text of the float shown from window `win`: the width of the window without the
--- gutter and the border, at most 100, or vim.g.nvtour_note_width. The panel is narrow: it takes the
--- width of the editor.
function peek.width(win)
  local limit = note_width_limit()
  if limit then
    return math.min(limit, 100)
  end
  local info = vim.fn.getwininfo(win)[1]
  local avail = win == S.panel.win and vim.o.columns - 2 or info.width - info.textoff
  return math.max(30, math.min(100, avail - 2))
end

--- The lines of the float for `step`, as chunk lists. Without a "◇" line from the arrival lines
--- (the file is the one of the step before) the file is shown anyway: the peek is about one step.
function peek.lines(step, width)
  local R = cap(step.role)
  local out = arrival_lines(step, width)
  local head = (out[1] and out[1][1][1] == "## ") and 1 or 0 -- the chapter heading stays first
  if not (out[head + 1] and out[head + 1][1][1] == "◇ ") then
    local arrival = link_lines("◇ ", version_segs(step, S.tour.steps[step.n - 1] or {}), width)
    for i, line in ipairs(arrival) do
      table.insert(out, head + i, line)
    end
  end
  if step.lost then
    out[#out + 1] = { { "✗ lost", "NvtourLost" }, { " · its lines were deleted", "NvtourVia" } }
  end
  if step.note and step.note ~= "" then
    vim.list_extend(out, note_chunks(step.note, width, true))
  end
  if step.suggest and valid_buf(step.buf) then
    vim.list_extend(out, peek.box(suggest_lines(step, 4), "NvtourFrame" .. R, step.role, "suggest", width))
  end
  return vim.list_extend(out, next_lines(step, width))
end

--- Open the float of `step` at the cursor of window `win` (the current window).
function peek.open(step, win)
  peek.close()
  local R = cap(step.role)
  local width = peek.width(win)
  local lines, text = peek.lines(step, width), {}
  for i, chunks in ipairs(lines) do
    local parts = {}
    for _, c in ipairs(chunks) do
      parts[#parts + 1] = (c[1]:gsub("\n", " "))
    end
    text[i] = table.concat(parts)
  end
  for _, b in ipairs(api.nvim_list_bufs()) do
    if api.nvim_buf_get_name(b) == "nvtour://peek" then
      pcall(api.nvim_buf_delete, b, { force = true }) -- left by an older runtime
    end
  end
  local buf = api.nvim_create_buf(false, true)
  api.nvim_buf_set_name(buf, "nvtour://peek")
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].swapfile = false
  api.nvim_buf_set_lines(buf, 0, -1, false, text)
  vim.bo[buf].modifiable = false
  local ns = api.nvim_create_namespace("nvtour_peek")
  for i, chunks in ipairs(lines) do
    local col = 0
    for _, c in ipairs(chunks) do
      if type(c[2]) == "string" and #c[1] > 0 then
        api.nvim_buf_set_extmark(buf, ns, i - 1, col, { end_col = col + #c[1], hl_group = c[2], priority = 150 })
      end
      col = col + #c[1]
    end
  end
  local label = (step.label and step.label ~= "") and (" " .. step.label) or ""
  local float = api.nvim_open_win(buf, false, {
    relative = "cursor",
    row = 1,
    col = 0,
    width = width,
    height = math.max(1, math.min(#text, math.floor(api.nvim_win_get_height(win) / 2))),
    style = "minimal",
    border = "rounded",
    title = { { (" %d/%d%s "):format(step.n, #S.tour.steps, label), "NvtourLabel" .. R } },
    focusable = false,
    zindex = 60,
  })
  vim.wo[float].wrap = false
  vim.wo[float].winhighlight = "FloatBorder:NvtourFrame" .. R
  local src = api.nvim_win_get_buf(win)
  local cursor = api.nvim_win_get_cursor(win)
  local group = api.nvim_create_augroup("nvtour_peek", { clear = true })
  S.peek = { win = float, buf = buf, step = step, group = group, maps = {} }
  -- Opening the float moves nothing. A CursorMoved that is still queued from the keys before
  -- (the user pressed `j`, then the peek key) finds the cursor where it was: only a move closes.
  api.nvim_create_autocmd({ "CursorMoved", "CursorMovedI" }, {
    group = group,
    callback = function()
      local w = api.nvim_get_current_win()
      local pos = api.nvim_win_get_cursor(w)
      if w ~= win or pos[1] ~= cursor[1] or pos[2] ~= cursor[2] then
        peek.close_later(float)
      end
    end,
  })
  api.nvim_create_autocmd({ "BufLeave", "WinLeave", "WinClosed" }, {
    group = group,
    callback = function(ev)
      if ev.event ~= "WinClosed" or tonumber(ev.match) == win then
        peek.close_later(float)
      end
    end,
  })
  -- <Esc> and q close it, for the time it is open; a buffer-local key of the user stays as it is.
  for _, lhs in ipairs({ "<Esc>", "q" }) do
    local old = api.nvim_buf_call(src, function()
      return vim.fn.maparg(lhs, "n", false, true)
    end)
    if type(old) ~= "table" or old.buffer ~= 1 then
      vim.keymap.set("n", lhs, function()
        peek.close()
      end, { buffer = src, nowait = true, silent = true, desc = "nvtour: close the peek" })
      table.insert(S.peek.maps, { src, lhs })
    end
  end
end

--- The step that holds the cursor of window `win`; in the panel, the step of the line.
function peek.step_at_cursor(win)
  local line = api.nvim_win_get_cursor(win)[1]
  if win == S.panel.win then
    return S.tour.steps[S.panel.line_map[line] or 0]
  end
  local buf = api.nvim_win_get_buf(win)
  sync_buf(buf)
  return follow.step_at(buf, line)
end

local H = {}

H.start = function(a)
  reset(true)
  S.tour.title = a.title or ""
  S.tour.sheet = (a.sheet and a.sheet ~= "") and a.sheet or nil
  S.tour.sheet_env = S.tour.sheet and sheet_env_of(a.sheet_env) or nil
  S.workspace = a.workspace or S.workspace
  ensure_started()
  render_panel()
  return { ok = true, title = S.tour.title, sheet = S.tour.sheet, keys = installed_keys() }
end

--- Peek a step: step `a.n`, or the step under the cursor. The same step again closes the float.
H.peek = function(a)
  local steps = S.tour.steps
  local win = api.nvim_get_current_win()
  local step
  if a.n ~= nil then
    if a.n < 1 or a.n > #steps then
      fail(("no such step %s (tour has %d)"):format(tostring(a.n), #steps), 6)
    end
    step = steps[a.n]
  else
    step = peek.step_at_cursor(win)
  end
  local was = S.peek.step
  if valid_win(S.peek.win) then
    peek.close()
    if step == nil or step == was then
      return { ok = true, closed = true }
    end
  end
  if not step then
    return { ok = true, message = "nvtour: no step here" }
  end
  sync_step(step)
  peek.open(step, win)
  return { ok = true, n = step.n }
end

--- Renumber the steps and render all of them again (after an insert, edit or remove).
local function rerender_all()
  sync_all()
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
      ensure_track(s) -- also creates the anchor of a step made by an older runtime
      render_step(s, tour_win(s.buf))
    end
  end
  refocus_all_steps() -- the steps of a file changed: so do the folds of a focus from the steps
end

--- Draw the code and the card again after the view or the range style changed, and scroll the
--- current step back into view (its block above and below the range is not the same).
function side.refresh()
  side.render()
  rerender_all()
  local step = S.tour.steps[S.tour.current]
  if step and not step.lost and valid_buf(step.buf) then
    local win = tour_win(step.buf)
    if win and api.nvim_win_get_buf(win) == step.buf then
      scroll_to(step, win)
    end
  end
  render_panel()
end

--- Set the view: `a.mode` is "inline", "side" or "toggle" (the default).
H.view = function(a)
  local mode = a.mode
  if mode == nil or mode == "" or mode == "toggle" then
    mode = side.on() and "inline" or "side"
  end
  if mode ~= "inline" and mode ~= "side" then
    fail(("unknown view '%s' (inline, side or toggle)"):format(tostring(mode)), 2)
  end
  S.view = mode
  if mode == "side" then
    S.card.user_closed = false -- the side view comes back with its card
  end
  side.refresh()
  return { ok = true, view = mode, message = "nvtour: " .. mode .. " view" }
end

--- Hide or show the step card (like the panel). Hiding keeps the side view, so the code stays clean;
--- showing it also turns the side view on.
H.card = function()
  local hide = side.on() and side.shown()
  S.card.user_closed = hide
  if not hide then
    S.view = "side"
  end
  side.refresh()
  local state = hide and "hidden" or "shown"
  return { ok = true, card = state, view = side.view(), message = "nvtour: step card " .. state }
end

--- Set the range style of the side view (`a.style`), or take the next one of side.STYLES. The view
--- becomes the side view: the style is only shown there.
H.range_style = function(a)
  local style = a.style
  if style == nil or style == "" then
    local cur, i = side.style_name(), 0
    for k, name in ipairs(side.STYLES) do
      if name == cur then
        i = k
      end
    end
    style = side.STYLES[i % #side.STYLES + 1]
  end
  local bad = side.bad_style(style)
  if bad then
    fail(bad, 2)
  end
  S.range_style = style
  S.view = "side"
  side.refresh()
  return { ok = true, range_style = style, view = "side", message = "nvtour: range style " .. style }
end

--- Turn the animation of jumps on or off (`a.mode`: "on", "off" or "toggle", the default).
H.animate = function(a)
  local mode, on = a.mode, nil
  if mode == nil or mode == "" or mode == "toggle" then
    on = not anim.on()
  elseif mode == "on" or mode == "off" then
    on = mode == "on"
  else
    fail(("unknown mode '%s' (on, off or toggle)"):format(tostring(mode)), 2)
  end
  S.animate = on
  return { ok = true, animate = on, message = "nvtour: animation " .. (on and "on" or "off") }
end

--- Lines (l1, l2) that the text range `q` selects in the list `lines`; `name` is for the messages.
--- q = { start, end, minus, plus, nth, near } (see ranges.py, which has the same function in Python).
--- `minus` stops at line 1.
local function resolve_query(lines, q, name)
  local hits = {}
  for n, line in ipairs(lines) do
    if line:find(q.start, 1, true) then
      hits[#hits + 1] = n
    end
  end
  if #hits == 0 then
    fail(("text '%s' not found in %s (%d lines)"):format(q.start, name, #lines), 6)
  end
  local l1
  if q.nth then
    if q.nth > #hits then
      fail(("text '%s' has %d match(es) in %s, but @%d was asked"):format(q.start, #hits, name, q.nth), 6)
    end
    l1 = hits[q.nth]
  elseif q.near then
    l1 = hits[1]
    for _, n in ipairs(hits) do
      if math.abs(n - q.near) < math.abs(l1 - q.near) then
        l1 = n
      end
    end
  elseif #hits > 1 then
    local shown = {}
    for i = 1, math.min(#hits, 10) do
      shown[i] = ("  %d| %s"):format(hits[i], vim.fn.strcharpart(lines[hits[i]], 0, 100))
    end
    if #hits > 10 then
      shown[#shown + 1] = ("  ... and %d more"):format(#hits - 10)
    end
    fail(("text '%s' matches %d lines in %s; choose one with @N or ~L:\n%s"):format(q.start, #hits, name, table.concat(shown, "\n")), 6)
  else
    l1 = hits[1]
  end
  local l2 = l1
  if q["end"] then
    l2 = nil
    for n = l1, #lines do
      if lines[n]:find(q["end"], 1, true) then
        l2 = n
        break
      end
    end
    if not l2 then
      fail(("end text '%s' not found in %s after line %d (%d lines)"):format(q["end"], name, l1, #lines), 6)
    end
  end
  return math.max(1, l1 - (q.minus or 0)), l2 + (q.plus or 0)
end
M._resolve_query = resolve_query

--- Load the buffer of location `a` and check that l1-l2 exists and (with `expect`, a list) contains
--- each expected text. A text range `a.query` gives l1 and l2 (the arguments are not used then).
--- With `lines` (a list), those lines are checked and no buffer is loaded (`apply` checks the lines
--- of a git blob or of a diagram that is not in nvim yet). Returns the buffer and the checked l1 and l2.
local function checked_range(a, l1, l2, expect, lines)
  -- Where the --expect text `text` is in `all` (the lines of the file) when it is not in the range
  -- that starts at `l1`: "; it is on line(s) ..." with at most 3 lines, the nearest to l1 first, or
  -- "; it is not in the file". Lets the agent fix the range without reading the file again.
  local function expect_hint(all, text, l1)
    local hits = {}
    for n, line in ipairs(all) do
      if line:find(text, 1, true) then
        hits[#hits + 1] = n
      end
    end
    if #hits == 0 then
      return "; it is not in the file"
    end
    table.sort(hits, function(x, y)
      local dx, dy = math.abs(x - l1), math.abs(y - l1)
      return dx < dy or (dx == dy and x < y)
    end)
    local shown = {}
    for i = 1, math.min(#hits, 3) do
      shown[i] = tostring(hits[i])
    end
    local more = #hits > 3 and (" and %d more"):format(#hits - 3) or ""
    return ("; it is on line%s %s%s"):format(#hits > 1 and "s" or "", table.concat(shown, ", "), more)
  end

  local buf, count, get
  if lines then
    count = #lines
    get = function(first, last)
      return vim.list_slice(lines, first, last)
    end
  else
    buf = loc_buf(a)
    count = api.nvim_buf_line_count(buf)
    get = function(first, last)
      return api.nvim_buf_get_lines(buf, first - 1, last, false)
    end
  end
  if a.query then
    l1, l2 = resolve_query(get(1, count), a.query, doc_name(a))
  end
  if l1 < 1 or l2 < l1 then
    fail(("bad range %d-%d"):format(l1, l2), 6)
  end
  if l2 > count then
    fail(("range %d-%d is beyond end of file (%d lines): %s"):format(l1, l2, count, doc_name(a)), 6)
  end
  local checked = get(l1, l2)
  for _, expect in ipairs(expect_list(expect) or {}) do
    local found = false
    for _, line in ipairs(checked) do
      if line:find(expect, 1, true) then
        found = true
        break
      end
    end
    if not found then
      local first = checked[1] or ""
      local range = l1 == l2 and tostring(l1) or (l1 .. "-" .. l2)
      local at = a.ref and (" @" .. a.ref) or ""
      fail(("--expect %q not found in %s:%s%s; line %d is %q%s"):format(expect, doc_path(a), range, at, l1, first,
        expect_hint(get(1, count), expect, l1)), 6)
    end
  end
  return buf, l1, l2
end

--- The step table for location `a` ({ file, ref, sha, diagram, note, label, ... } as H.step gets it) at
--- index `n`, with its checked `buf`, `l1` and `l2`, its sheet `link` and its `from` step.
local function make_step(a, n, buf, l1, l2, link, from)
  return {
    n = n,
    file = a.file,
    ref = a.sha and a.ref or nil,
    sha = a.sha,
    diagram = a.diagram,
    buf = buf,
    l1 = l1,
    l2 = l2,
    role = a.role or "info",
    label = a.label,
    note = a.note,
    via = (a.via ~= "" and a.via) or nil,
    suggest = (a.suggest and a.suggest ~= "") and split_lines(a.suggest) or nil,
    from = from,
    sheet = link,
    expect = expect_list(a.expect),
    query = a.query, -- the text range the lines came from (nil: the agent gave line numbers)
    resolved_from = a.resolved_from,
    orig_l1 = l1, -- the range given by the agent, for the report when the step moves
    orig_l2 = l2,
    extmark_ids = {},
  }
end

--- Put `step` into the tour at `step.n` (1 to #steps + 1). The step is added only after it rendered
--- (and jumped, with `opts.jump`); a failure leaves no half step behind. `opts.render = false` does
--- not draw it: the caller draws all steps with rerender_all (`apply` does it once at the end). A
--- pending chapter starts at the step. The caller also updates the quickfix list and the panel.
local function add_step(step, opts)
  local steps = S.tour.steps
  local prev_current = S.tour.current
  local ok, err = pcall(function()
    anchor_step(step)
    if opts.render ~= false then
      render_step(step, tour_win(step.buf))
    end
    if opts.jump then
      jump(step)
    end
  end)
  if not ok then
    for _, id in ipairs(step.extmark_ids) do
      pcall(api.nvim_buf_del_extmark, step.buf, S.ns_steps, id)
    end
    del_anchor(step)
    S.tour.current = prev_current
    pcall(rerender_all) -- the jump may have drawn the previous current step as not current
    error(err, 0)
  end
  table.insert(steps, step.n, step)
  local pending = chapter_at(nil)
  if pending then
    pending.step = step -- the next step added, wherever it is inserted
    sort_chapters()
  end
  if step.n < #steps and not opts.jump and S.tour.current >= step.n then
    S.tour.current = S.tour.current + 1
  end
end

H.step = function(a)
  S.workspace = a.workspace or S.workspace
  local role = a.role or "info"
  if not ROLES[role] then
    fail("unknown role: " .. tostring(role), 2)
  end
  local buf, l1, l2 = checked_range(a, a.l1 or 0, a.l2 or a.l1 or 0, a.expect)
  local link = resolve_sheet(a.sheet, a.sheet_env)
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
  local step = make_step(a, at, buf, l1, l2, link, from)
  add_step(step, { jump = do_jump })
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
  local link, link_given = resolve_sheet(a.sheet, a.sheet_env)
  -- A new location replaces the old one completely: without a ref it is on the working tree.
  local moved = a.file ~= nil
  sync_step(step) -- without a new location the step keeps the lines it has now, not the old ones
  local loc = a.file
      and { file = a.file, ref = a.sha and a.ref or nil, sha = a.sha, rel = a.rel, lines = a.lines, diagram = a.diagram, query = a.query }
    or { file = step.file, ref = step.ref, sha = step.sha, diagram = step.diagram }
  local l1, l2 = a.l1 or step.l1, a.l2 or a.l1 or step.l2
  local buf = step.buf
  if moved or not step.lost then -- a lost step has no lines to check; only a new location gives it some
    buf, l1, l2 = checked_range(loc, l1, l2, a.expect)
  end
  if moved then
    step.query, step.resolved_from = a.query, a.resolved_from
    del_anchor(step)
    step.lost, step.lost_reason = nil, nil
  end
  step.file, step.ref, step.sha, step.diagram, step.buf = loc.file, loc.ref, loc.sha, loc.diagram, buf
  step.l1, step.l2 = l1, l2
  if moved then
    step.orig_l1, step.orig_l2 = l1, l2
    anchor_step(step)
  end
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
  local link_changed = link_given and not same_link(step.sheet, link)
  if link_given then
    step.sheet = link
  end
  rerender_all()
  if a.jump then
    return step_goto(step.n)
  end
  if link_changed and step.n == S.tour.current then
    sheet_schedule(step) -- the current step has another link now
  end
  update_qf()
  render_panel()
  return step_result(step)
end

H.remove = function(a)
  sync_all()
  local step = step_at(a.n)
  del_anchor(step)
  table.remove(S.tour.steps, step.n)
  local chapter = chapter_at(step)
  if chapter then
    -- The heading moves to the step that is now at this place; with none (or one with a chapter
    -- of its own) it is dropped.
    local nxt = S.tour.steps[step.n]
    if nxt and not chapter_at(nxt) then
      chapter.step = nxt
    else
      drop_chapter(chapter)
    end
  end
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

--- The chapters as the CLI shows them, in tour order: { { name, step = n | nil } } (nil: pending).
local function chapter_list()
  local list = {}
  for _, c in ipairs(chapters()) do
    list[#list + 1] = { name = c.name, step = c.step and c.step.n or vim.NIL }
  end
  return list
end

H.chapter = function(a)
  local steps = S.tour.steps
  if a.list then
    return { ok = true, chapters = chapter_list() }
  end
  if a.remove then
    local step = steps[a.remove] -- #steps + 1 is the pending chapter
    local chapter = (a.remove >= 1 and a.remove <= #steps + 1) and chapter_at(step) or nil
    if not chapter then
      fail(
        a.remove == #steps + 1 and "no pending chapter"
          or ("no chapter starts at step %d (tour has %d steps)"):format(a.remove, #steps),
        6
      )
    end
    drop_chapter(chapter)
    rerender_all()
    render_panel()
    redraw_winbars()
    return { ok = true, removed = a.remove, name = chapter.name, pending = step == nil, chapters = chapter_list() }
  end
  local name = vim.trim(((a.name or ""):gsub("%s+", " "))) -- a heading is one line
  if name == "" then
    fail("the chapter needs a name", 2)
  end
  local at = a.at
  if at and (at < 1 or at > #steps + 1) then
    fail(("--at %d is out of range (tour has %d steps)"):format(at, #steps), 6)
  end
  local chapter, old = set_chapter(name, at and steps[at] or nil)
  rerender_all() -- the heading above the first step of the chapter
  render_panel()
  redraw_winbars()
  return {
    ok = true,
    name = name,
    step = chapter.step and chapter.step.n or nil,
    pending = chapter.step == nil,
    replaced = old,
    chapters = chapter_list(),
  }
end

--- The number of the first step that is not lost, from step `n` (not included) in direction `dir`
--- (1 or -1); nil when there is none. A skipped lost step is announced.
local function live_step(n, dir)
  local steps = S.tour.steps
  local i = n + dir
  while steps[i] and steps[i].lost do
    local msg = ("step %d is lost (its lines were deleted); skipped"):format(i)
    notify("nvtour: " .. msg, vim.log.levels.WARN)
    if W then
      W[#W + 1] = msg
    end
    i = i + dir
  end
  return steps[i] and i or nil
end

H["next"] = function()
  sync_all()
  local n, total = S.tour.current, #S.tour.steps
  if total > 0 and n >= total and n > 0 then
    return step_result(S.tour.steps[n], { message = "already at last step" })
  end
  local i = live_step(n, 1)
  if not i and n > 0 then
    return step_result(S.tour.steps[n], { message = "already at last step" })
  end
  return step_goto(i or n + 1)
end

H.prev = function()
  sync_all()
  local n = S.tour.current
  if n == 0 then
    return step_goto(live_step(0, 1) or 1)
  end
  if n == 1 and #S.tour.steps > 0 then
    return step_result(S.tour.steps[1], { message = "already at first step" })
  end
  local i = live_step(n, -1)
  if not i then
    return step_result(S.tour.steps[n], { message = "already at first step" })
  end
  return step_goto(i)
end

H.first = function()
  sync_all()
  return step_goto(live_step(0, 1) or 1)
end

H.last = function()
  sync_all()
  return step_goto(live_step(#S.tour.steps + 1, -1) or #S.tour.steps)
end

--- Go to the first step of the next chapter. Where `[c` goes: the start of the chapter the current
--- step is in, when it is not at that start, else the start of the chapter before.
local function chapter_jump(dir)
  sync_all()
  local starts = chapter_starts()
  if #starts == 0 then
    for _, c in ipairs(chapters()) do
      if c.step then
        fail("every step of the chapters is lost", 6)
      end
    end
    fail("no chapters", 6)
  end
  local n = S.tour.current
  if dir > 0 then
    for _, st in ipairs(starts) do
      if st.n > n then
        return step_goto(st.n)
      end
    end
  elseif n == 0 then
    return step_goto(starts[1].n)
  else
    for i = #starts, 1, -1 do
      if starts[i].n < n then
        return step_goto(starts[i].n)
      end
    end
  end
  local msg = dir > 0 and "already in the last chapter" or "already at the first chapter"
  if S.tour.steps[n] then
    return step_result(S.tour.steps[n], { message = msg })
  end
  fail(msg, 6)
end

H.next_chapter = function()
  return chapter_jump(1)
end

H.prev_chapter = function()
  return chapter_jump(-1)
end

---------------------------------------------------------------------------
-- Focus handlers
---------------------------------------------------------------------------

--- The ranges `{ l1, l2 }` of focus args `a` in `buf` (a text range gives the lines it selects now),
--- checked against the buffer; fails (exit 6) on a bad range and (exit 2) when there is none.
local function focus_ranges(a, buf)
  local count = api.nvim_buf_line_count(buf)
  local ranges = {}
  for _, r in ipairs(a.ranges or {}) do
    if r.query then -- a text range: the lines it selects now
      local l1, l2 = resolve_query(api.nvim_buf_get_lines(buf, 0, -1, false), r.query, relpath(a.file))
      r = { l1, l2 }
    end
    if r[1] < 1 or r[2] < r[1] or r[2] > count then
      fail(("bad range %d-%d for %s (%d lines)"):format(r[1], r[2], a.file, count), 6)
    end
    ranges[#ranges + 1] = r
  end
  if #ranges == 0 then
    fail("focus needs at least one range", 2)
  end
  return ranges
end

--- Fold or dim `ranges` (plus `ctx` lines around each) of `buf`, which holds `file`. With `move`, the
--- file is shown and the cursor goes to the first range; without it the view stays and the folds are
--- applied when the file is shown. `from_steps` (`{ context, dim }`) makes the focus follow the steps
--- of the buffer (see refocus_steps): the ranges are then not given by a command, so they have no
--- anchors of their own; the anchors of the steps are the only ones.
local function focus_buf(buf, file, ranges, ctx, dim, move, from_steps)
  local count = api.nvim_buf_line_count(buf)
  unfocus_buf(buf)
  local merged = merge_ranges(ranges, ctx, count)
  -- The ranges follow the edits of the user like the steps do (see "Steps follow edits").
  local anchors
  if not from_steps then
    anchors = {}
    for i, r in ipairs(merged) do
      local id, tail = set_anchor(buf, r[1], r[2])
      anchors[i] = { id = id, tail = tail }
    end
  end
  S.focus[buf] = { ranges = merged, dim = dim and true or false, file = file, anchors = anchors, from_steps = from_steps }
  attach_buf(buf)
  local win = tour_win(buf, move)
  if move then
    win = show_buf(win, buf)
    S.tour_win = win
    enter_win(win)
  end
  local shown = win ~= nil and api.nvim_win_get_buf(win) == buf
  if dim then
    draw_dim(buf, merged)
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
    file = file,
    ranges = merged,
    mode = dim and "dim" or "fold",
    deferred = (not dim and not shown) or nil,
  }
end

--- Fold or dim the ranges of `a` in its file.
local function do_focus(a, move)
  local buf = load_buf(a.file)
  local ranges = focus_ranges(a, buf)
  return focus_buf(buf, a.file, ranges, a.context or 2, a.dim, move)
end

H.focus = function(a)
  S.workspace = a.workspace or S.workspace
  -- Before the first step focus shows the file; during a tour the view stays on the current step
  -- and folds are applied when the file is shown (by a step or by the user).
  return do_focus(a, S.tour.current == 0)
end

--- Fold or dim every toured file (with `a.file`, only that file) to the lines of its steps, and keep
--- it so: the folds follow the steps (see refocus_steps). One result per buffer, in tour order. Diagram
--- steps and lost steps do not count; a buffer of a git ref does. The view does not move.
H.focus_steps = function(a)
  S.workspace = a.workspace or S.workspace
  sync_all()
  local order, ranges = {}, {}
  for _, st in ipairs(S.tour.steps) do
    if not st.diagram and not st.lost and (not a.file or st.file == a.file) then
      local buf = step_buf(st)
      if not ranges[buf] then
        ranges[buf] = { file = st.file, ref = st.ref, ranges = {} }
        order[#order + 1] = buf
      end
      local list = ranges[buf].ranges
      list[#list + 1] = { st.l1, st.l2 }
    end
  end
  if #order == 0 then
    fail(a.file and ("focus --steps: no step is in " .. a.file) or "focus --steps: the tour has no steps", 6)
  end
  local ctx = a.context or 2
  local results = {}
  for _, buf in ipairs(order) do
    local g = ranges[buf]
    local res = focus_buf(buf, g.file, g.ranges, ctx, a.dim, false, { context = ctx, dim = a.dim and true or false })
    S.focus[buf].ref = g.ref
    res.ref = g.ref
    results[#results + 1] = res
  end
  return { ok = true, focus = results }
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
-- Diagrams
---------------------------------------------------------------------------

local DIAGRAM_PREFIX = "nvtour://diagram/" -- the CLI names the file of a diagram step the same way

--- Make diagram `a.name` from the rendered `a.lines`, or replace its lines. Steps on it stay;
--- a step beyond the new last line is an error, a step whose --expect text is gone is lost.
H.diagram = function(a)
  S.workspace = a.workspace or S.workspace
  local lines = a.lines or {}
  if #lines == 0 then
    fail("the diagram is empty", 6)
  end
  if a.split and not DIAGRAM_SPLITS[a.split] then
    fail(("bad split %q: use above, below, left, right or full"):format(a.split), 2)
  end
  local d = S.diagrams[a.name]
  for _, s in ipairs(S.tour.steps) do
    if s.diagram == a.name and s.l2 > #lines then
      fail(("step %d (lines %s) is beyond the new diagram (%d lines); edit or remove it first"):format(s.n, fmt_range(s), #lines), 6)
    end
  end
  if not d then
    d = { name = a.name, file = DIAGRAM_PREFIX .. a.name }
    S.diagrams[a.name] = d
  end
  S.diagram_split = a.split or S.diagram_split
  d.lines, d.links, d.header = lines, a.links or {}, a.header or 0
  local buf = ensure_diagram_buf(d, true)
  local shown = false
  if a.show then
    local win = show_buf(diagram_win(buf, true, a.split), buf)
    set_winbar(win) -- the header line of the diagram is shown there when it scrolls out of view
    if vim.w[win].nvtour_diagram_split == "full" then
      enter_win(win) -- a tab page of its own is seen only when it is current
    end
    local current = S.tour.steps[S.tour.current]
    if not (current and current.diagram == d.name) then
      api.nvim_win_call(win, function()
        api.nvim_win_set_cursor(win, { 1, 0 })
        vim.fn.winrestview({ topline = 1, topfill = 0 })
      end)
    end
    shown = true
  end
  -- The new lines may not have the --expect text of a step on it any more: such a step is lost until
  -- a later version of the diagram has the text again. (A step beyond the end is refused above.)
  for _, s in ipairs(S.tour.steps) do
    if s.diagram == d.name then
      local why = range_problem(buf, s.l1, s.l2, s.expect)
      s.lost, s.lost_reason = why and true or nil, why
    end
  end
  rerender_all() -- set_lines moved the marks of the steps on it
  return { ok = true, name = d.name, count = #lines, links = #d.links, header = d.header, shown = shown }
end

--- Open the link under the cursor of a diagram window: the range in the tour window, which
--- becomes the current window. followed = false when there is no link under the cursor.
H.diagram_link = function()
  local win = api.nvim_get_current_win()
  local name = vim.b[api.nvim_win_get_buf(win)].nvtour_diagram
  local d = name and S.diagrams[name]
  if not d then
    return { ok = true, followed = false }
  end
  local row, col = unpack(api.nvim_win_get_cursor(win))
  local line = d.lines[row] or ""
  local target
  for _, link in ipairs(d.links) do
    local from = 1
    while not target do
      local s, e = line:find(link.text, from, true)
      if not s then
        break
      end
      if col >= s - 1 and col < e then
        target = link
      end
      from = e + 1
    end
  end
  if not target then
    return { ok = true, followed = false }
  end
  local buf = load_buf(target.file)
  local count = api.nvim_buf_line_count(buf)
  if target.l2 > count then
    fail(("link %q: range %d-%d is beyond end of file (%d lines): %s"):format(target.text, target.l1, target.l2, count, relpath(target.file)), 6)
  end
  local code = tour_win(buf, true)
  pcall(api.nvim_win_call, code, function()
    vim.cmd("normal! m'")
  end)
  code = show_buf(code, buf)
  S.tour_win = code
  api.nvim_set_current_win(code)
  api.nvim_win_set_cursor(code, { target.l1, 0 })
  vim.cmd("normal! zvzz")
  anim.flash({ buf = buf, l1 = target.l1, l2 = target.l2 })
  return { ok = true, followed = true, file = target.file, l1 = target.l1, l2 = target.l2 }
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

---------------------------------------------------------------------------
-- Apply: a whole tour from one call
---------------------------------------------------------------------------

--- Run `fn` and return what it returns; an error of nvtour from it gets `prefix` in front of its
--- message (`step[3]: ...`), the code stays.
local function scoped(prefix, fn)
  local res = vim.F.pack_len(pcall(fn))
  if res[1] then
    return unpack(res, 2, res.n) -- the values after `true`
  end
  local err = res[2]
  if type(err) == "table" and err.nvtour then
    err = { nvtour = true, msg = prefix .. err.msg, code = err.code }
  elseif type(err) == "string" then
    err = prefix .. err
  end
  error(err, 0)
end

--- The first 100 characters of line `l1` of `lines` (a list) or of `buf`, as the step result shows it.
local function first_text(buf, lines, l1)
  local line = lines and lines[l1] or (api.nvim_buf_get_lines(buf, l1 - 1, l1, false)[1] or "")
  return vim.fn.strcharpart(line, 0, 100)
end

--- A whole tour in one call (see DESIGN.md section 15). The arguments are `title`, `sheet`,
--- `sheet_env`, `panel`, `focus`, `diagrams` and `steps` (entries as the commands of the same names
--- get them, with `lines` of a git blob only in the first step that has it), and the flags `append`,
--- `check` and `no_jump`. Phase 1 checks everything and changes nothing of the tour (files stay
--- loaded, as after a failed `step`); a failure names the entry (`step[4]: ...`). It checks all
--- entries and fails once with all errors, also those of Python (`errors`, with the entries that
--- failed there as { skip = true }). With `check` it ends there. Phase 2 builds the tour; the steps are drawn, and the quickfix list and the panel are
--- updated, once at the end.
--- The steps are numbered from 1 in the file. With `append` a `from` is the number in the final tour:
--- the existing steps come first, then the steps of the file.
local function apply_tour(a)
  S.workspace = a.workspace or S.workspace
  local append = a.append and true or false
  local base = append and #S.tour.steps or 0
  -- The tour that '#STEP' links of the steps go with: the one that is there, or the one that is made.
  local tour = S.tour
  if not append then
    local sheet = (a.sheet and a.sheet ~= "") and a.sheet or nil
    tour = { sheet = sheet, sheet_env = sheet and sheet_env_of(a.sheet_env) or nil }
  end

  -- Phase 1: check. Each entry is checked, also after an error, so that one call reports all errors.
  local errors = {}
  for _, e in ipairs(a.errors or {}) do
    errors[#errors + 1] = e
  end
  local RANK = { tour = 0, sheet = 0, diagram = 1, focus = 2, step = 3 } -- as ENTRY_RANK in cli.py
  local function check(kind, i, fn)
    local ok, err = pcall(scoped, ("%s[%d]: "):format(kind, i), fn)
    if not ok then
      if type(err) ~= "table" or not err.nvtour then
        error(err, 0) -- a bug, not a problem of the tour file
      end
      errors[#errors + 1] = { kind = kind, i = i, msg = err.msg, code = err.code }
    end
  end
  local diagram_lines = {} -- [name] = the lines of a diagram of the file
  local diagram_failed = {} -- [name] = true: its error is reported, and the steps on it are not checked
  for i, d in ipairs(a.diagrams or {}) do
    if d.skip then
      if d.name then
        diagram_failed[d.name] = true
      end
    else
      check("diagram", i, function()
        local lines = d.lines or {}
        if #lines == 0 then
          fail("the diagram is empty", 6)
        end
        if d.split and not DIAGRAM_SPLITS[d.split] then
          fail(("bad split %q: use above, below, left, right or full"):format(d.split), 2)
        end
        for _, s in ipairs(append and S.tour.steps or {}) do -- as 'diagram' does for a diagram that is replaced
          if s.diagram == d.name and s.l2 > #lines then
            fail(("step %d (lines %s) is beyond the new diagram (%d lines); edit or remove it first"):format(s.n, fmt_range(s), #lines), 6)
          end
        end
        diagram_lines[d.name] = lines
      end)
      if not diagram_lines[d.name] then
        diagram_failed[d.name] = true
      end
    end
  end
  for i, f in ipairs(a.focus or {}) do
    if not f.skip then
      check("focus", i, function()
        focus_ranges(f, load_buf(f.file))
      end)
    end
  end
  local plan, blobs = {}, {}
  local running, pending_name -- the chapter that the next step is in, and the pending one it starts
  if append then
    local last = S.tour.steps[#S.tour.steps]
    running = last and (chapter_of(last) or {}).name
    pending_name = (chapter_at(nil) or {}).name
  end
  for i, st in ipairs(a.steps or {}) do
    if st.sha then -- first, so the next step on the blob has its lines when this step fails
      local key = st.sha .. ":" .. st.file
      if st.lines then
        blobs[key] = st.lines
      else
        st.lines = blobs[key]
      end
    end
    if not st.skip and not (st.diagram and diagram_failed[st.diagram]) then
      check("step", i, function()
        if not ROLES[st.role or "info"] then
          fail("unknown role: " .. tostring(st.role), 2)
        end
        local lines = st.lines -- the lines to check, when they are not in a buffer yet
        if st.diagram then
          local d = S.diagrams[st.diagram]
          lines = diagram_lines[st.diagram] or (append and d and d.lines) or nil
          if not lines then
            fail(("no diagram %q in the file%s"):format(st.diagram, append and " or in the tour" or ""), 6)
          end
        end
        local buf, l1, l2 = checked_range(st, st.l1 or 0, st.l2 or st.l1 or 0, st.expect, lines)
        if st.from and (st.from < 1 or st.from > base + i - 1) then
          fail(("from %d: no such step before this one (%d step(s) come before it)"):format(st.from, base + i - 1), 6)
        end
        local link = resolve_sheet(st.sheet, a.sheet_env, tour)
        local chapter
        if st.chapter ~= nil then
          chapter = vim.trim((st.chapter:gsub("%s+", " "))) -- a heading is one line
          if chapter == "" then
            fail("the chapter needs a name", 2)
          end
        end
        if i == 1 and pending_name then
          running = pending_name
        end
        running = chapter or running
        plan[i] = { st = st, l1 = l1, l2 = l2, link = link, chapter = chapter, in_chapter = running, text = first_text(buf, lines, l1) }
      end)
    end
  end
  if #errors > 0 then
    for n, e in ipairs(errors) do
      e.seq = n
    end
    table.sort(errors, function(x, y)
      if RANK[x.kind] ~= RANK[y.kind] then
        return RANK[x.kind] < RANK[y.kind]
      end
      return x.i < y.i or (x.i == y.i and x.seq < y.seq)
    end)
    if #errors == 1 then
      fail(errors[1].msg, errors[1].code)
    end
    local msgs = {}
    for n, e in ipairs(errors) do
      msgs[n] = e.msg
    end
    fail(("%d errors, nothing was changed:\n%s"):format(#errors, table.concat(msgs, "\n")), errors[1].code)
  end

  if a.check then
    local out = {}
    for i, p in ipairs(plan) do
      local st = p.st
      out[i] = {
        n = base + i,
        total = base + #plan,
        file = st.file,
        l1 = p.l1,
        l2 = p.l2,
        role = st.role or "info",
        label = st.label,
        via = (st.via ~= "" and st.via) or nil,
        from = st.from,
        ref = st.sha and st.ref or nil,
        sha = st.sha,
        diagram = st.diagram,
        sheet = p.link and sheet_label(p.link) or nil,
        chapter = p.in_chapter,
        resolved_from = st.resolved_from,
        text = p.text,
      }
    end
    return { ok = true, check = true, steps = out, total = base + #plan }
  end

  -- Phase 2: build.
  if not append then
    reset(true)
    S.tour.title = a.title or ""
    S.tour.sheet, S.tour.sheet_env = tour.sheet, tour.sheet_env
  end
  ensure_started()
  for i, d in ipairs(a.diagrams or {}) do
    scoped(("diagram[%d]: "):format(i), function()
      H.diagram(d)
    end)
  end
  for i, f in ipairs(a.focus or {}) do
    scoped(("focus[%d]: "):format(i), function()
      do_focus(f, false) -- the view moves with the jump to the first step
    end)
  end
  if a.panel ~= nil then
    S.panel.text = split_lines(a.panel)
  end
  local first_tour_step = #S.tour.steps == 0
  if ((first_tour_step and #plan > 0) or a.panel ~= nil) and vim.g.nvtour_auto_panel ~= false and not S.panel.user_closed then
    panel_open() -- before rendering, so the notes are wrapped to the final window width
  end
  local added = {}
  for i, p in ipairs(plan) do
    scoped(("step[%d]: "):format(i), function()
      local st = p.st
      local buf = loc_buf(st)
      local from = st.from and S.tour.steps[st.from] or nil
      local step = make_step(st, #S.tour.steps + 1, buf, p.l1, p.l2, p.link, from)
      add_step(step, { jump = false, render = false })
      if p.chapter then
        set_chapter(p.chapter, step)
      end
      added[#added + 1] = step
    end)
  end
  rerender_all()
  local jumped_to
  if #added > 0 and not a.no_jump then
    jumped_to = added[1].n
    step_goto(jumped_to) -- also updates the quickfix list and the panel
  else
    update_qf()
    render_panel()
  end
  local results = {}
  for i, step in ipairs(added) do
    local r = step_result(step, { jumped = step.n == jumped_to })
    r.keys = nil
    results[i] = r
  end
  return {
    ok = true,
    steps = results,
    total = #S.tour.steps,
    panel_lines = #S.panel.text,
    chapters = #chapter_list(),
    jumped_to = jumped_to,
    current = S.tour.current,
    keys = installed_keys(),
  }
end

H.apply = function(a)
  if not a.check then
    return apply_tour(a)
  end
  local workspace = S.workspace -- a check changes nothing, also not the directory that paths are shown from
  local ok, res = pcall(apply_tour, a)
  S.workspace = workspace
  if not ok then
    error(res, 0)
  end
  return res
end

H.status = function()
  sync_all()
  for buf in pairs(S.focus) do
    sync_focus(buf)
  end
  local steps, modified, seen = {}, {}, {}
  for _, s in ipairs(S.tour.steps) do
    local changed = valid_buf(s.buf) and vim.bo[s.buf].modified or false
    if changed and not seen[s.file] then
      seen[s.file] = true
      modified[#modified + 1] = s.file
    end
    steps[#steps + 1] = {
      orig_l1 = s.orig_l1,
      orig_l2 = s.orig_l2,
      lost = s.lost or nil,
      lost_reason = s.lost and s.lost_reason or nil,
      modified = changed,
      n = s.n,
      file = s.file,
      ref = s.ref,
      sha = s.sha,
      diagram = s.diagram,
      l1 = s.l1,
      l2 = s.l2,
      role = s.role,
      label = s.label,
      note = s.note,
      via = s.via,
      from = explicit_from(s) and s.from.n or nil,
      expect = expect_list(s.expect),
      suggest = s.suggest,
      sheet = s.sheet and sheet_label(s.sheet) or nil,
    }
  end
  local focus = {}
  for _, f in pairs(S.focus) do
    focus[#focus + 1] = { file = f.file, ref = f.ref, ranges = f.ranges, mode = f.dim and "dim" or "fold", from_steps = f.from_steps and true or nil }
  end
  local diffs = 0
  for _, t in ipairs(S.diff_tabs) do
    if valid_tab(t) then
      diffs = diffs + 1
    end
  end
  local diagrams = {}
  for _, d in pairs(S.diagrams) do
    diagrams[#diagrams + 1] = { name = d.name, count = #d.lines, links = #d.links }
  end
  table.sort(diagrams, function(left, right)
    return left.name < right.name
  end)
  return {
    ok = true,
    title = S.tour.title,
    tour_sheet = S.tour.sheet,
    current = S.tour.current,
    total = #S.tour.steps,
    steps = steps,
    chapters = chapter_list(),
    modified_files = #modified > 0 and modified or nil,
    focus = focus,
    diagrams = diagrams,
    diff_tabs = diffs,
    panel = { open = panel_shown(), user_closed = S.panel.user_closed },
    view = side.view(),
    range_style = side.style_name(),
    card = side.shown(),
    follow = follow.on(),
    animate = anim.on(),
    keys = installed_keys(),
    mark_keys = next(S.mark_keys) and S.mark_keys or vim.empty_dict(),
    marks = #S.marks,
    workspace = S.workspace,
    version = M.VERSION,
  }
end

H.clear = function(a)
  reset(false)
  local unlisted = 0
  for buf in pairs(S.added_bufs) do
    if not a.keep_buffers and valid_buf(buf) and not vim.bo[buf].modified and #vim.fn.win_findbuf(buf) == 0 then
      -- Unlisting fires BufDelete; plugins such as treesitter-context then drop the still-loaded buffer.
      api.nvim_buf_call(buf, function()
        vim.cmd("noautocmd setlocal nobuflisted")
      end)
      unlisted = unlisted + 1
    end
  end
  S.added_bufs = {}
  return { ok = true, unlisted = unlisted }
end

-- The limits of a selection that is reported (where) or saved (marks), and the names of its kinds.
local SEL = { lines = 200, bytes = 65536, kind = { v = "char", V = "line", ["\22"] = "block" } }

--- The selection in window `win`, which shows `buf`: the live one when `win` is the current window
--- and `mode` is a visual mode, else the previous one (the '< and '> marks). nil when there is none.
--- The text is cut to SEL.lines lines and SEL.bytes bytes.
local function selection_of(win, buf, mode)
  local live = win == api.nvim_get_current_win() and SEL.kind[mode] ~= nil
  return api.nvim_win_call(win, function()
    local p1, p2, kind
    if live then
      p1, p2, kind = vim.fn.getpos("v"), vim.fn.getpos("."), mode
    else
      p1, p2, kind = vim.fn.getpos("'<"), vim.fn.getpos("'>"), vim.fn.visualmode()
    end
    if not SEL.kind[kind] or p1[2] < 1 or p2[2] < 1 then
      return nil
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
    text = text or api.nvim_buf_get_lines(buf, l1 - 1, math.min(l2, l1 + SEL.lines), false)
    local out, bytes, truncated = {}, 0, false
    for i, line in ipairs(text) do
      if i > SEL.lines or bytes + #line > SEL.bytes then
        truncated = true
        break
      end
      out[#out + 1] = line
      bytes = bytes + #line + 1
    end
    return {
      live = live,
      kind = SEL.kind[kind],
      l1 = l1,
      l2 = l2,
      c1 = c1,
      c2 = c2,
      text = out,
      truncated = truncated or (#text < l2 - l1 + 1),
    }
  end)
end

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
  end)
  res.selection = selection_of(win, buf, mode)
  return res
end

---------------------------------------------------------------------------
-- Marks
---------------------------------------------------------------------------

-- The user marks places for the agent with <leader>wm (or <leader>wM, with a comment). A mark has an
-- extmark in ns_marks (an anchor like the ones of the steps, so it follows edits) with a "◆" sign on
-- the first line of its range; a long range has the sign on its first line only. The list is in S.marks
-- (Lua state only: it is gone with nvim). Marks belong to the user: start and clear keep them; only
-- `nvtour marks --clear` and :NvtourMarksClear remove them.
local marks = {}

--- The range of mark `m` now: reads the anchor back. A mark whose lines were all deleted keeps its
--- last range and gets `deleted`; an undo restores the lines and clears the flag.
function marks.sync(m)
  if not (valid_buf(m.buf) and api.nvim_buf_is_loaded(m.buf)) then
    return
  end
  local l1, l2, invalid = read_anchor(m.buf, m.extmark, m.tail, S.ns_marks)
  if not l1 then
    return
  end
  m.deleted = invalid or nil
  if not invalid then
    m.l1, m.l2 = l1, l2
  end
end

--- Remove the sign of mark `m`.
function marks.del(m)
  if valid_buf(m.buf) then
    pcall(api.nvim_buf_del_extmark, m.buf, S.ns_marks, m.extmark)
  end
end

--- Add a mark on the cursor line, or on the selection in visual mode, of the current window.
H.mark = function()
  local win = api.nvim_get_current_win()
  local buf = api.nvim_win_get_buf(win)
  local diagram = vim.b[buf].nvtour_diagram
  if not (file_win(win) or (diagram and is_normal_win(win))) then
    fail("mark: this window shows no file")
  end
  local file = api.nvim_buf_get_name(buf)
  if file == "" then
    fail("mark: this buffer has no name")
  end
  local ref = vim.b[buf].nvtour_ref
  if ref then
    file = ref.file -- the real path, not nvtour://
  end
  local mode = api.nvim_get_mode().mode
  local m = { buf = buf, file = file, ref = ref and ref.ref or nil, sha = ref and ref.sha or nil, diagram = diagram, at = os.time() }
  local sel = SEL.kind[mode] and selection_of(win, buf, mode)
  if sel then
    m.l1, m.l2, m.kind, m.text, m.truncated = sel.l1, sel.l2, sel.kind, sel.text, sel.truncated
  else
    local line = api.nvim_win_get_cursor(win)[1]
    m.l1, m.l2, m.kind = line, line, "line"
    m.text = api.nvim_buf_get_lines(buf, line - 1, line, false)
    m.truncated = false
    if #m.text[1] > SEL.bytes then
      m.text, m.truncated = {}, true
    end
  end
  m.extmark, m.tail = set_anchor(buf, m.l1, m.l2, S.ns_marks, {
    sign_text = "◆",
    sign_hl_group = "NvtourUserMark",
    priority = 200, -- over the bar of a step
  })
  S.mark_seq = S.mark_seq + 1
  m.id = S.mark_seq
  S.marks[#S.marks + 1] = m
  local where = diagram and ("diagram " .. diagram) or relpath(file)
  return {
    ok = true,
    id = m.id,
    n = #S.marks,
    message = ("nvtour: mark %d added (%s:%s)"):format(#S.marks, where, m.l1 == m.l2 and m.l1 or (m.l1 .. "-" .. m.l2)),
  }
end

--- Set the comment of the mark with id `a.id` (the number of a mark changes when marks are cleared).
H.mark_comment = function(a)
  for _, m in ipairs(S.marks) do
    if m.id == a.id then
      m.comment = a.comment
      return { ok = true }
    end
  end
  return { ok = true } -- the mark was cleared while the prompt was open
end

--- The marks, in the order they were made. With `clear`, the signs and the list are removed after the
--- list is built, so what is returned is what is removed.
H.marks = function(a)
  local now = os.time()
  local list = {}
  for i, m in ipairs(S.marks) do
    marks.sync(m)
    list[i] = {
      n = i,
      file = m.file,
      ref = m.ref,
      sha = m.sha,
      diagram = m.diagram,
      l1 = m.l1,
      l2 = m.l2,
      kind = m.kind,
      text = m.text,
      truncated = m.truncated or nil,
      comment = m.comment,
      at = m.at,
      age = now - m.at,
      deleted = m.deleted,
    }
  end
  if a.clear then
    for _, m in ipairs(S.marks) do
      marks.del(m)
    end
    S.marks = {}
  end
  return { ok = true, marks = list, count = #list, now = now }
end

H.marks_count = function()
  return { ok = true, count = #S.marks }
end

---------------------------------------------------------------------------
-- Dispatch
---------------------------------------------------------------------------

dispatch = function(cmd, args)
  local h = H[(cmd:gsub("-", "_"))]
  if not h then
    return { ok = false, error = "unknown command: " .. tostring(cmd), code = 2, pid = vim.fn.getpid() }
  end
  pcall(anim.finish_all) -- a command sees, and starts from, the final view of a jump
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

-- The header line of a diagram comes and goes in the winbar as the diagram scrolls. Scrolling a
-- window that is not the current one (with the mouse) does not redraw its winbar by itself.
api.nvim_create_autocmd("WinScrolled", {
  group = aug,
  callback = function()
    for id in pairs(vim.v.event) do
      local w = tonumber(id)
      if w and valid_win(w) and header_diagram(w) then
        if not pcall(api.nvim__redraw, { win = w, winbar = true }) then
          vim.cmd("redrawstatus!")
        end
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
    if side.shown() and shown[S.card.buf] then
      pcall(side.render) -- the rules of the card are as wide as its window
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
ucmd("NvtourNextChapter", function()
  return dispatch("next_chapter", {})
end)
ucmd("NvtourPrevChapter", function()
  return dispatch("prev_chapter", {})
end)
ucmd("NvtourPanel", function()
  return dispatch("panel", { toggle = true })
end)
ucmd("NvtourClear", function()
  return dispatch("clear", {})
end)
ucmd("NvtourPeek", function(o)
  return dispatch("peek", { n = o.args ~= "" and (tonumber(o.args) or 0) or nil })
end, { nargs = "?" })
ucmd("NvtourMarksClear", function()
  local res = dispatch("marks", { clear = true })
  res.message = ("nvtour: %d mark(s) cleared"):format(res.count or 0)
  return res
end)
ucmd("NvtourView", function(o)
  return dispatch("view", { mode = o.args })
end, { nargs = "?", complete = function()
  return { "inline", "side", "toggle" }
end })
ucmd("NvtourCard", function()
  return dispatch("card", {})
end)
ucmd("NvtourRangeStyle", function(o)
  return dispatch("range_style", { style = o.args })
end, { nargs = "?", complete = function()
  return side.STYLES
end })
ucmd("NvtourAnimate", function(o)
  return dispatch("animate", { mode = o.args })
end, { nargs = "?", complete = function()
  return { "on", "off", "toggle" }
end })
ucmd("NvtourFollow", function()
  local on = not follow.on()
  vim.g.nvtour_follow_cursor = on
  if not on then
    follow.cancel_all()
  end
  return { ok = true, message = "nvtour: follow " .. (on and "on" or "off") }
end)

-- After an upgrade during a tour, re-install the keys so new or renamed actions appear.
if PREV_VERSION ~= nil and PREV_VERSION ~= M.VERSION then
  -- The keys of the marks are ours too: remove and install them again, once.
  remove_keymaps(KEYS.always)
  if S.keymaps_installed then
    remove_keymaps(KEYS.tour)
    install_keymaps(KEYS.tour)
  end
  if valid_buf(S.panel.buf) then
    map_panel_keys(S.panel.buf)
  end
  if valid_buf(S.card.buf) then
    side.map_keys(S.card.buf)
  end
  for _, d in pairs(S.diagrams) do
    if valid_buf(d.buf) then
      map_diagram_keys(d.buf)
    end
  end
  pcall(rerender_all) -- marks drawn by the old version may differ (for example the old line tint)
  pcall(render_panel)
end

-- The keys of the marks are installed when the runtime loads, tour or not (a no-op when they are).
install_keymaps(KEYS.always)

_G.nvtour = M
return M

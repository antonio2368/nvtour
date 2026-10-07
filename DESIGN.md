# nvtour — design

`nvtour` lets an AI agent (Claude Code, Codex, houseofagents agents) give a **visual, read-only
walkthrough** of code inside the user's already-running Neovim. The agent explains a bug or a
concept in chat and, in parallel, drives the editor: jump to a range, highlight it, attach a short
comment as virtual text, fold away irrelevant code, show a read-only diff, and keep a side panel
with the step list and a longer markdown explanation. The user steps through with keys.

It is **not** a code editing or review tool. It never writes to a file buffer or to a file the user edits
(its own pin cache and `instances --prune` are the only file system writes).

## 1. Non-goals

- No edits, no accept/reject diffs, no diagnostics, no LSP.
- No spawning of nvim. If there is no suitable instance, the CLI says so and the agent asks the
  user to open one.
- No images in v1 (the user's terminal is alacritty, no kitty graphics protocol). See §13.
- No MCP server in v1. The CLI is the product; MCP is a thin wrapper later.

## 2. Architecture

```
agent (Bash) ──► nvtour CLI (Python, pynvim) ──msgpack-RPC over unix socket──► running nvim
                     │                                                          │
                     │ loads once per nvim process                              ▼
                     └──────────────────────────────────────────────► nvtour.lua runtime (_G.nvtour)
                                                                      state, extmarks, panel, folds, diff tabs
```

- **Python CLI** (`nvtour/`): argument parsing, instance discovery and selection, path
  resolution, git access for diffs and ref steps, printing. Stateless except a small per-workspace pin cache.
- **Lua runtime** (`nvtour/lua/nvtour.lua`, shipped as package data): all editor state and all
  rendering. Every CLI command is **one** `nvim_exec_lua` round trip calling
  `_G.nvtour.dispatch(cmd, args)` so each operation is atomic from nvim's point of view.
- **Loading**: on every CLI run the client evaluates `return _G.nvtour and _G.nvtour.VERSION`.
  If it is missing or differs from the packaged version (the first 12 hex digits of the SHA-1 of the
  Lua source, passed to the chunk as its first vararg), the client sends the whole Lua source with
  `nvim_exec_lua`. Reloading must preserve existing state
  (`M.state = (_G.nvtour and _G.nvtour.state) or new_state()`). Keymaps and panel maps call
  `_G.nvtour.dispatch` at call time; after an upgrade during a tour the keys are re-installed.
- **Blocked nvim**: before the first request the client calls `nvim_get_mode()` (answered at once).
  If `blocking` is true (hit-enter or other prompt), the command is refused with exit 5 and "the
  command was not run"; otherwise nvim would queue the request and run it after the user answers.
- **Timeouts**: one budget (`--timeout`) for the whole command: connect, mode check, version check,
  runtime load and dispatch share it. A timeout error names the phase.
- **Return protocol**: `dispatch` always returns a table `{ ok = true, ... }` or
  `{ ok = false, error = "message" }`. Lua errors are caught with `pcall` and converted to the
  error form. Python maps `ok = false` to exit code 5 and prints the message to stderr.

Dependencies: Python ≥ 3.11, `pynvim` (already installed, 0.6.0), Neovim ≥ 0.10 (user has 0.12.4).

## 3. Instance discovery and selection

### Socket enumeration

Neovim ≥ 0.7 starts an RPC server per instance at `stdpath('run')/nvim.<pid>.0`. Scan, in order:

1. `$XDG_RUNTIME_DIR/nvim.*.0`
2. `/tmp/nvim.$USER/*/nvim.*.0`
3. `/tmp/nvim.*.0`

For each socket: parse `<pid>` from the filename. If `/proc/<pid>` does not exist, or
`/proc/<pid>/comm` is not `nvim*` (a reused pid), mark **stale** and skip (do not delete;
`nvtour instances --prune` may unlink stale sockets, nothing else does, and only Unix sockets owned
by the user). Otherwise **probe** with an RPC call, 2 s timeout, all sockets in parallel. The probe
first calls `nvim_get_mode()`: an nvim that waits for input is **blocked**. Otherwise one `exec_lua`
returns:

```lua
{ pid = vim.fn.getpid(), cwd = vim.fn.getcwd(), version = vim.version(),
  buffers = <listed, named buffers, realpath'd, max 200>, tabpages = #vim.api.nvim_list_tabpages() }
```

A socket that fails the probe (connection refused or timeout) is reported as `unresponsive`.

### Workspace

`W` = `--workspace DIR` if given, else `git rev-parse --show-toplevel` of the CLI's cwd, else the
cwd. Always `os.path.realpath`. Git worktrees resolve naturally to their own toplevel.

### Score (per live instance)

| condition | score |
|---|---|
| `realpath(cwd) == W` | 100 |
| `W` is an ancestor of `cwd` (nvim opened in a subdir) | 80 |
| `cwd` is an ancestor of `W` (nvim opened in `~/projects`) | 60 |
| otherwise, any listed buffer path under `W` | 40 |
| none (or empty cwd) | 0 |

Pick the unique highest score > 0. If exactly one live instance exists, pick it even with score 0
(`kind = "only instance"`, with a note on stderr). Ties, or several instances that all score 0 →
not selectable. If no instance is live but some are blocked → exit 5 (ask the user to answer the
prompt).

### Precedence of socket choice

1. `--socket PATH`
2. `$NVTOUR_SOCKET`
3. `$NVIM` (set automatically inside an nvim terminal; if the agent runs inside that nvim, that is
   the instance, regardless of score)
4. pinned socket for `W` from the cache, if still alive
5. discovery + score

### Pin cache

`$XDG_RUNTIME_DIR/nvtour/<sha256(W)[:16]>` (fallback `~/.cache/nvtour/`) containing
`{"socket": PATH, "pid": PID or null}` (plain-text pins of 0.1 are still read). The pid is stored only
when `/proc/<pid>` exists, so a custom socket name (`--listen`) or an nvim in another pid namespace
(socket mounted into a container) is validated by existence alone. Written atomically by
`nvtour attach` and by a successful automatic selection. `nvtour attach --clear` removes it. A dead
pinned socket is ignored and removed. `attach SOCKET` probes that socket directly and only pins a
live instance.

### Exit codes

| code | meaning | stderr content |
|---|---|---|
| 0 | ok | |
| 2 | usage error | argparse message (or invalid `--timeout`/`--context`, stdin is a terminal) |
| 3 | no unique match | table of live instances (pid, cwd, score) + hint: `open nvim in <W>` or `nvtour attach <pid>` |
| 4 | no live nvim at all, or `--socket` path missing | `no running nvim found; open nvim in <W> and retry` / `socket not found: P` |
| 5 | RPC failure or timeout, nvim waiting for input | message with the phase; `waiting for input ... not run` |
| 6 | bad file / range (file missing, L1 > L2, beyond EOF, `--expect` mismatch) | message |

The agent relays 3/4 messages to the user verbatim. With `--json` every failure (also usage errors)
prints `{"ok": false, "code": N, "error": "..."}` on stdout. Unexpected exceptions become one line
(`OSError` → 6, others → 5), never a traceback.

## 4. CLI reference

Global flags (before the subcommand): `--socket PATH`, `--workspace DIR`, `--json`,
`--timeout SECONDS` (default 5). Paths in arguments are resolved relative to the CLI's cwd, then
`realpath`'d before being sent to nvim.

```
nvtour instances [--prune]
nvtour attach [PID|SOCKET] [--clear]
nvtour where
nvtour start [TITLE]
nvtour step FILE:L1[-L2] [--ref GITREF] [--note TEXT | --note -] [--label TEXT] [--role ROLE]
            [--expect TEXT]... [--via TEXT] [--from N] [--suggest TEXT | --suggest -] [--at N]
            [--jump | --no-jump]
nvtour edit N [FILE:L1[-L2] [--ref GITREF]] [--note TEXT | --note -] [--label TEXT] [--role ROLE]
            [--expect TEXT]... [--via TEXT] [--from N] [--suggest TEXT | --suggest -] [--jump]
nvtour remove N
nvtour goto N | nvtour next | nvtour prev | nvtour first | nvtour last
nvtour status
nvtour focus FILE:L1-L2 [L3-L4 ...] [--context N] [--dim]
nvtour unfocus [FILE]
nvtour diff FILE (--ref GITREF | --file PATH | --stdin) [--title TEXT]
nvtour diff-close
nvtour panel [TEXT | --file PATH | -] [--toggle] [--clear]
nvtour clear [--keep-buffers]
nvtour doctor
```

Range syntax: `path:L1` (single line), `path:L1-L2` or `path:L1,L2`; `path:L:COL` is accepted and the
column is ignored (unless a file literally named `path:L` exists). `focus` takes one file and one or
more ranges; extra bare `L3-L4` arguments apply to the same file.

### Commands

- **instances** — prints one line per socket: `pid  state(live|stale|unresponsive|blocked)  score
  cwd (#buffers)`. With `--prune` unlinks stale sockets (Unix sockets owned by the user only).
- **attach** — selects and pins an instance. With no argument runs selection; prints
  `attached: pid P  socket S  cwd C  (exact match|subdir|parent|buffers)`.
- **where** — context for "point and ask", read from the user's file window (the current window,
  or the previous one when the current window is a terminal, the panel or another special window):
  `file:line:col` (with ` @REF` when the window shows a ref step buffer; `file` is then the real path
  and `ref`/`sha` are set), mode, buffer flags (`modified`, `buftype`, `changedtick`), visible range
  `top-bottom`, window cwd, and the selection `{live, kind = char|line|block, l1, c1, l2, c2, text,
  truncated}`: live in visual mode, else the previous `'<`/`'>` selection. Char and block selections
  give the exact text (`getregion()`); at most 200 lines and 64 KiB.
- **start** — resets any existing tour (same as `clear` but keeps the panel if open), sets the
  title, creates a fresh quickfix list, installs keymaps. `step` without a prior `start` implicitly
  starts an untitled tour.
- **step** — appends a step (see §6), or inserts it with `--at N`. Only the first step of a tour
  jumps (so a finished tour is at step 1); `--jump` forces a jump, `--no-jump` suppresses it for the
  first step. Prints `step N/N: file:L1-L2 [role] label` and the first highlighted line
  (`  L1| text`). `--expect TEXT` fails with exit 6 unless `TEXT` occurs in the range; it can be
  given more than one time, then each text must occur (the error names the first one that does not;
  in `edit` the new list replaces the old one, `''` removes it). `--note -`
  reads the note from stdin (lets agents pass multi-line text with a heredoc). A step is added only
  after it rendered (and jumped); on an error nothing of it remains. `--ref GITREF` puts the step on
  the file as it is at that git ref, not on the working tree (§6, "Steps at a git ref"); the output
  then shows the location as `file:L1-L2 @GITREF`. `--via TEXT` is the link to the step: why the
  tour goes there from the step before it, or from step N with `--from N` (§6, "Links between
  steps"). `--from N` with a step number that does not exist is exit 6. `--suggest TEXT|-` is the
  code that would replace the range (§6, "Suggested code"); `--note -` and `--suggest -` together
  is exit 2 (only one can read stdin).
- **edit / remove** — change or delete step N; all steps are renumbered and rendered again. A new
  location replaces the old one completely: `FILE:L1[-L2]` alone puts the step on the working tree,
  `FILE:L1[-L2] --ref GITREF` on that ref. `--ref` without a location is a usage error (exit 2).
- **goto / next / prev / first / last** — move the current step (wraps at ends is **not** desired;
  clamp and say `already at last step`). `prev` before any jump goes to step 1. Prints the same
  lines as `step`.
- **status** — the tour (title, steps, current), focus, panel, diff tabs and installed keys. Read only.
- **focus / unfocus** — §8.
- **diff / diff-close** — §9.
- **panel** — §7. `TEXT`, `--file`, or `-` replaces the free markdown section. `--toggle`
  shows/hides the window. `--clear` empties the free section.
- **clear** — removes everything nvtour created: extmarks, signs, folds (restoring fold options),
  dim, diff tabs, panel window and buffer, keymaps, the nvtour quickfix items. Buffers that were
  loaded stay loaded; the ones nvtour added to the buffer list are unlisted again unless shown,
  modified, or `--keep-buffers`. Prints `cleared`.
- **doctor** — checks: nvim on PATH (optional), pynvim importable, runtime dirs scanned,
  live/stale sockets, selected instance, version of that nvim ≥ 0.10, Lua runtime version loaded in
  it. The exit code is the code of the first failed required check (pynvim, selected instance,
  nvim version, connection).

`--json` makes every command print a single JSON object instead of text (the `dispatch` return
table plus `socket`, `pid`, and `warnings` when there are any). Without `--json`, warnings are
printed on stderr as `warning: ...`.

## 5. Lua runtime: state

```lua
M.VERSION = (...) or "dev" -- source hash passed by the client
M.state = {
  tour = { title = "", steps = {}, current = 0, qf_id = nil },
  ns_steps = nvim_create_namespace("nvtour_steps"),
  ns_focus = nvim_create_namespace("nvtour_focus"),
  panel = { buf = nil, win = nil, text = {}, user_closed = false },
  focus = { [bufnr] = { ranges = {...}, dim = bool, file = path,
                        saved = { [winid] = { foldmethod=, foldenable=, foldlevel=, foldminlines= } } } },
  pending_folds = { [winid] = { [bufnr] = saved fold options } }, -- restored on BufWinEnter
  diff_tabs = { tabpage handles },
  keys = { [action] = lhs },  -- the keys that were installed
  tour_win = winid,           -- last window used for the tour
  added_bufs = { [bufnr] = true }, -- buffers nvtour added to the buffer list
  ns_flash = nvim_create_namespace("nvtour_flash"),
  flash = { buf = bufnr, seq = n },  -- the last flash; a newer one cancels its timer
  winbars = { [winid] = the window's own local 'winbar' },  -- restored by clear
  refs = { [sha .. ":" .. file] = { buf, file, rel, ref, sha, lines } },  -- ref step buffers
}
```

Each step: `{ n, file, buf, l1, l2, role, label, note, expect, via, from, suggest, ref, sha, extmark_ids = {} }`.
`suggest` is a list of lines (nil when there is no suggestion).
`ref` and `sha` are nil for a step on the working tree. `from` is the step table of a `--from N`
link (not a number, so it stays correct when steps are inserted or removed); a link to a removed
step falls back to the step before. `below` is the number of link lines below the range, and
`drawn_win` the window that the band was computed for.

### Tour window

The window where files are shown. A window is usable when it is a normal (non-floating) window
that is not the panel, shows a buffer with `buftype == ""` or an nvtour ref step buffer (never a
terminal, quickfix, help or plugin window), and is not in an nvtour diff tab. Order: (1) a usable window of the current tab that
already shows the step's buffer, (2) the current window, (3) the previous window (`wincmd p`
target), (4) the last tour window, (5) the first usable window of the current tab, then of the other
tabs, (6) a new split next to the first normal window of the current tab.

Entering the tour window: when the current window is a terminal, it keeps the focus; the file
window still moves (`vim.g.nvtour_steal_focus = "unless_terminal"` default, `"always"`, `"never"`).

### Showing a buffer, safely

`local buf = vim.fn.bufadd(path); vim.fn.bufload(buf); vim.bo[buf].buflisted = true;
nvim_win_set_buf(tourwin, buf)`. Never `:edit`, never `:edit!`. Switching the buffer of a window
with `hidden` set keeps the previous buffer's modifications; the runtime sets `vim.o.hidden = true`
on load because of this. A modified buffer that cannot be hidden (`'bufhidden'` is
`unload`/`delete`/`wipe`, or `'hidden'` is off) and is shown in only this window is never replaced:
`nvim_win_set_buf` would run `'autowrite'` (a write to disk) or fail with E37. A split is opened
next to it instead, with a warning.

`bufload()` shows no swap-file dialog. Inside `pcall`, its ATTENTION message (E325) becomes an error
instead of a hit-enter prompt; the buffer is loaded anyway and nvtour continues with a warning.

## 6. Step rendering

Roles and highlight groups (all defined with `default = true` so the user can override in
`init.lua`):

| role | accent from |
|---|---|
| `fault` | `DiagnosticError` |
| `flow` | `DiagnosticInfo` |
| `fix` | `DiagnosticOk` |
| `context` | `DiagnosticWarn` |
| `info` (default) | `DiagnosticHint` |

The range never gets a background colour, so syntax highlighting stays as it is (a background over
many lines is heavy, and `Diff*` groups are defined with `reverse` in many colorschemes).
`NvtourNumber<Role>`, `NvtourSign<Role>` and `NvtourLabel<Role>` use the accent as foreground and `NvtourMark<Role>` (bold, underlined in the accent; no `fg`, so the syntax colour
stays) marks the `--expect` text. Also `NvtourNote`, `NvtourNoteCode` (fg of `@markup.raw` or
`String`), `NvtourNoteBold`, `NvtourNoteCollapsed`, `NvtourNoteBorder`, `NvtourDim` (focus `--dim`),
`NvtourFlash`, `NvtourPanelCurrent`, `NvtourVia` and `NvtourViaLoc` (blended from `Directory`), all
blended from `Normal`, `NvtourVersion` (fg of `Special`, bold), `NvtourNoteBg` (the band), `NvtourFrame<Role>` (the frame), and `NvtourPanelFile` (→
`Directory`), `NvtourPanelProgress` (→ `Comment`). A `User NvtourHighlights` autocmd runs after they
are defined.

Only the **current** step (`n == tour.current`) is drawn in full. The other steps keep the
line-number highlight, the end-of-line marker and a one-line note; the bar, the full note and the
`--expect` marks move with the current step. Every change of `tour.current` re-renders the old and the new
current step.

For a step at `l1..l2` in buffer `buf` (0-based rows internally):

1. **Range** — one extmark per line with `number_hl_group = NvtourNumber<Role>`; for the current
   step also `sign_text = " ▎"`, `sign_hl_group = NvtourSign<Role>`, `priority = 100`: a bar in the
   second cell of the sign column, next to the line numbers. The step number is not a sign: a
   2-digit number would touch the line number (`10` + `348` reads `10348`).
2. **Note** — virtual lines **above** `l1` (`virt_lines_above = true`). Current step: one virtual
   line per wrapped line, `{ {prefix, "NvtourNoteBorder"}, text chunks }`. Prefix `╭ ` for the first
   line, `│ ` for middle lines, `╰ ` for the last line; a single-line note uses `▸ `.
   Text chunks: `` `code` `` → `NvtourNoteCode`, `**bold**` → `NvtourNoteBold` (markers removed; a
   marker without a closing partner stays as text), the rest `NvtourNote`. Wrap width = tour window
   width − `textoff` (from `vim.fn.getwininfo`) − 4, minimum 30; words wider than that are split.
   Blank lines in the note are kept as `│`. Other steps: one virtual line `╶ <first line>`, in
   `NvtourNoteCollapsed`, with ` …` when the note is longer. Notes are wrapped again on
   `WinResized`. For the current step, the link lines ("Links between steps" below) come before
   the note in the same block, and the "next" line is a virtual line **below** `l2`.
3. **Expect marks** (current step only) — every occurrence of each text of `expect` (a list; a
   single string from an older runtime is read as a list of one) in `l1..l2`:
   `hl_group = NvtourMark<Role>`, `priority = 150`.
4. **Marker** — on `l1`, for every step: `virt_text = { {"  ← ", NvtourLabel<Role>}, {n,
   NvtourSign<Role>}, {" " .. label, NvtourLabel<Role>} }` (no label: `← n`), `virt_text_pos = "eol"`.
5. **Jump** (first step of a tour, or `--jump`; never with `--no-jump`) — show the buffer in the
   tour window, set `tour.current = n`, re-render the old and the new current step, set the winbar,
   then scroll (in that window via `nvim_win_call`): cursor on `l1`, `normal! zv`, and
   `winrestview({ topline, topfill })`. The block (the note, `l1..l2` and the "next" line, measured
   with `nvim_win_text_height`, so virtual lines and wrapped lines count; it counts the lines below
   `l2` with the next row, so they are added) is centred when it fits in the
   window, else the note starts at the top line. `topfill` shows the virtual lines above the top
   line, so a note on line 1 is visible (`zz` hides it). The view never goes past the end of the
   file. Closed folds count as one row. Then the range flashes: one extmark in `nvtour_flash`
   (`hl_group = NvtourFlash`, `hl_eol`, `priority = 250`) removed after `vim.g.nvtour_flash` ms
   (default 300; `0` = off). Before the buffer is shown, `normal! m'` in the tour window adds the
   position before the jump to its jumplist, so `<C-o>` goes back to the previous step.
6. **Winbar** — the tour window gets `setlocal winbar=%{%v:lua.nvtour.winbar()%}`, unless the window
   already has a winbar (the user's or a plugin's) or `vim.g.nvtour_winbar == false`. Text:
   ` nvtour 2/5 fault · <label>` and on the right `next ]w: <file>:<line> <label>` (only the line when
   the next step is in the same file) or `last step · [W first`. The right side takes the longest form
   that fits the window width (without the label, with the file name only, or nothing). `'winbar'`
   is reset when the window shows another buffer, so it is set again on every jump. `clear`
   restores the saved value; a `BufWinEnter` autocmd removes a tour winbar that a buffer brings back
   into an untracked window.
7. **Quickfix** — `vim.fn.setqflist({}, "r", { id = tour.qf_id, title = "nvtour: " .. title,
   items = <one item per step: filename, lnum, end_lnum, text = "[role] " .. (label or first note
   line)> })`. The index moves only when the step jumped. The list is created with
   `setqflist({}, " ", {...})` and its `id` read back with `getqflist({ id = 0 }).id`, so the
   user's other quickfix lists are untouched. The next tour reuses the list while it is the current
   one.
8. **Panel** — re-render (§7); on the first step the panel is opened automatically (before the
   note is rendered, so the note is wrapped to the final width) unless
   `vim.g.nvtour_auto_panel == false` or `panel.user_closed`.
9. `vim.notify(("nvtour %d/%d: %s"):format(n, total, label or file:l1), vim.log.levels.INFO)`.

`goto/next/prev` perform (5), (6), (8), (9) for an existing step and move the quickfix index with
`setqflist({}, "r", { id = qf_id, idx = n })`.

Validation (exit 6 from Python after the Lua check): file must exist and be readable (for `--ref`:
must exist at that ref); `1 ≤ l1 ≤ l2 ≤ line count`.

### Steps at a git ref

`step FILE:L1[-L2] --ref GITREF` shows code as it is at a git ref: code that a change removed or
moved, or the old version of a range next to a step on the new version. The step behaves like any
other step (notes, roles, `--expect`, panel, quickfix, keys); only its buffer is different.

**Python.** `FILE` does not have to exist in the working tree (a deleted file is fine).
1. The git toplevel is found from the nearest existing directory of `FILE`; `FILE` must be inside it
   (else exit 6). `rel` = `FILE` relative to the toplevel.
2. `git rev-parse --verify --quiet --end-of-options GITREF^{commit}` gives the commit `sha` (else
   exit 6, `unknown git ref`). The step is bound to this commit: a ref that moves later (`HEAD`
   after a commit, a fetched branch) does not change a step that exists.
3. `git cat-file blob <sha>:<rel>` gives the content (else exit 6 with git's message; a directory
   is not a blob). Decoded as UTF-8 with replacement, split into lines (a final newline does not
   add a line).
4. Sent to Lua: `file` (realpath'd), `rel`, `ref` (as typed, for display), `sha`, `lines`.

**Lua: the ref buffer.** One scratch buffer per (`sha`, `file`), kept in `state.refs` and reused by
all steps on it:
- created with `nvim_create_buf(true, true)` (listed, so buffer lists show it), named
  `nvtour://<ref>/<rel>` (`#2`, `#3`, ... appended if the name exists, for example `HEAD` at two
  commits);
- `buftype = nofile`, `bufhidden = hide` (the extmarks of its steps must survive when it is not
  shown; `wipe` as in the diff tab would remove them), `swapfile = false`, lines set once, then
  `modifiable = false`, `readonly = true`, `modified = false`;
- `filetype` from `vim.filetype.match({ filename = file, contents = lines })`, so syntax and
  treesitter highlighting work as in the real file;
- `vim.b.nvtour_ref = { ref, sha, file }` marks it.

The lines stay in `state.refs`. If the user wipes or unloads the buffer (a `nofile` buffer loses its
lines when unloaded), the next use (jump, quickfix) creates it again from these lines and points
all steps of that key to the new buffer.

**Display.** The range, the note, the marker and the `--expect` marks are drawn exactly as on a
real file.
- Winbar: ` nvtour 2/5 fault @origin/master · <label>`; the right side names the next step as
  `file:line @ref` when it is in another document (the same file at another ref counts as another
  document).
- Panel: a ref step starts a file name line `path @ref`; steps on the same file and commit share it.
- Quickfix: the item uses `bufnr` of the ref buffer, not `filename`.
- `step`, `goto`, ... and `status` print the location as `path:L1-L2 @ref`; `--json` adds `ref` and
  `sha`. The `vim.notify` message uses `path:l1 @ref` when the step has no label.

**Lifetime.** `clear` and `start` delete the ref buffers (they delete every `nvtour://` buffer) and
empty `state.refs`. A ref buffer that no step uses after `edit`/`remove` stays until then.

**Not supported for refs:** `focus` (it takes a working tree file) and `diff` (it compares the
working tree buffer). Language servers: most configurations do not attach to `nofile` buffers; one
that does can show diagnostics for the old code.

### Links between steps

A jump to another file, or to another version of the same file, loses the "why" and the "where".
The current step shows the "where" above its note, and the "why" of the next jump below its range:

```
     ╭──────────────────────────────────────────────╮
   ▎ │ ◇ b.cpp @origin/master (1a2b3c4d5e6f)         │  ← the file and version, when they changed
   ▎ │ The cleanup thread erases the entry ...       │  ← the note
     ╰──────────────────────────────────────────────╯
88 ▎   cache.erase(key);
   ▎ ╶─ → next 4 · c.cpp:10: the reader uses `it` again  ← below l2: where ]w goes, and why (--via)
```

- **Source.** The source of a step is the step given with `--from N`, else the step before it.
- **Where.** The `◇` line is shown when the buffer is different from the step before it in the tour
  (the code the user saw last, also when `--from` names another step): the file
  name (`NvtourViaLoc`) and the version (`NvtourVersion`): ` @ref` for a step at a git ref, with the
  first 12 characters of the commit in `()` when the ref is not itself that commit, or
  ` · working tree` when the step before was at a git ref. The first step has no `◇` line.
- **Next.** The "next" line is shown when the next step is in another buffer or has a `--via` link.
  The location is `line N` in the same buffer, else `path:N` (with ` @ref`, or ` · working tree` when
  this step is the same file at a ref), then ` (from N)` when the link of the next step comes from
  another step, then the `--via` text (`NvtourVia`, with `` `code` `` and `**bold**` as in notes), else
  the label of the next step. `next N` has the colour `NvtourSign<Role>` of the next step. The
  `--via` text is not shown again on the arrival at the step: the same text twice is noise.
- Each link line is wrapped to the note width; continuation lines are indented by 2 cells.
- Only the current step shows link lines. Adding, editing or removing a step renders all steps
  again, so the steps next to it show the new links.

### Frame and band

All virtual lines of a step (link lines, the note, the collapsed note) must not look like code. Both
styles start the lines in the gutter (`virt_lines_leftcol`) with blank cells up to the code column
(`textoff`), so the text is in the column of the code, and in the current step the gutter has `▎` in
`NvtourSign<Role>` in the column of the range bar, so one bar goes from the note through the range.
The column is `textoff - number width - 1` (the second cell of a 2-cell sign column before the
numbers; the number width as in nvim: `max('numberwidth', digits + 1)`); there is no bar with a
`'statuscolumn'`, `signcolumn=no` or `signcolumn=number`. Both need a window: a step drawn without
one gets plain lines, and is drawn again on `BufWinEnter` in the window that shows its buffer.

**Frame** (the default). No background. The block above `l1` of the current step (the `◇` line and the
note) is in a frame `╭─╮ │ │ ╰─╯` in `NvtourFrame<Role>` (the role accent blended 75 % over `Normal`
bg), from the code column to the right edge of the window; the note has no `╭ │ ╰ ▸` prefixes in it.
The "next" line gets a lead `╶─ ` in `NvtourFrame<Role>`, a collapsed note a lead `╶─ ` in
`NvtourNoteBorder` (instead of `╶ `); continuation lines are indented by 3 cells. The frame costs 2
screen lines per block; `scroll_to` counts them.

**Band** (`vim.g.nvtour_note_style = "band"`). Each line has `NvtourNoteBg` (a light tint, blended from `Normal`) over the full window
width: the chunks get `{ "NvtourNoteBg", group }` (so `NvtourNoteCode` keeps its own background), and
blank padding goes to the window width. The lines start in the gutter (`virt_lines_leftcol`) with
blank cells up to the code column (`textoff`), so the text is in the column of the code. In the
current step the gutter has `▎` in `NvtourSign<Role>` in the column of the range bar, so one bar goes
from the note through the range.

### Suggested code

`--suggest` gives the lines that would replace `l1..l2`. They are only virtual lines: the buffer and
the file do not change.

- **Current step.** Below `l2`, before the "next" line: a frame like the note block, with the title
  ` suggested ` in `NvtourLabel<Role>` in its top border; each line is `+ ` in `NvtourSign<Role>` and
  the code. The lines `l1..l2` get `hl_group = NvtourStrike` (strikethrough only, so the syntax colour
  stays) from the first non-blank character to the end of the line, `priority = 140`. Band style: a
  `suggested` line and the `+ ` lines in the band.
- **Code.** Tabs are expanded with the `'tabstop'` of the buffer. Up to 4 cells (band: 2) of the
  common indentation are removed, the cells of `│ ` and `+ `, so the new code is in the column of the
  code that it replaces. Colours: the language of the buffer's filetype
  (`vim.treesitter.language.get_lang`), `vim.treesitter.get_string_parser` on the text, and the
  captures of its `highlights` query as `@capture.lang` (inner captures win; `_*`, `spell`, `nospell`
  and `conceal` are skipped). Without a parser or a query the code is `NvtourSuggest`. A line wider
  than the frame is cut with `…` (code does not wrap).
- **Other steps.** One line `suggested: N lines` in `NvtourNoteCollapsed`, with the grey `╶─ ` lead.
- `scroll_to` counts the suggestion with the other lines below `l2`.

## 7. Panel

A scratch buffer `nvtour://panel` (`buftype = nofile`, `bufhidden = hide`, `swapfile = false`,
`filetype = markdown`, `modifiable = false` outside rendering) shown in a `botright vertical`
split. Window options: width `clamp(floor(columns * 0.3), 40, 70)`, `winfixwidth`, `wrap`,
`linebreak`, `nonumber`, `norelativenumber`, `signcolumn = no`, `foldcolumn = 0`,
`cursorline`, `breakindent`, `conceallevel = 2`, `concealcursor = nc` (hides the markdown markers),
`winhighlight = "Normal:NormalFloat"` optional.

Rendered content:

```
# <title or "Walkthrough">                    2/3      ← progress: virt_text, NvtourPanelProgress

src/a.cpp                                               ← NvtourPanelFile
▶ 1. ✗ 412-415  dangling iterator
    ↓ the cleanup thread erases the entry               ← --via of step 2: NvtourVia
src/b.cpp
  2. → 88       erase on cleanup thread
src/a.cpp
  3. ✓ 430      the fix

`]w` next · `[w` prev · `[W` first · `]W` last · `<leader>wp` panel · `<leader>wc` clear · `<CR>` jump · `q` close

---
<free markdown text set via `nvtour panel`>
```

Steps keep the tour order; a file name line starts each run of steps in the same file (for a ref
step: in the same file at the same commit, shown as `path @ref`). `N. <mark>`
is highlighted with `NvtourSign<Role>`; marks: `✗` fault, `→` flow, `✓` fix, `○` context, `•` info.
A step without a label shows the first line of its note (markers removed, cut to 60 cells); the
quickfix item text uses the same. Before the first jump the progress is `N step(s)`. The footer line
lists only the keys that were installed; the keys are inline code so markdown does not read
`[W first · ]W` as a link. A step with a `--via` or `--from` link gets a line `↓ [from N: ]<via>`
before it, and before its file name line, so a link to another file shows before the file changes.
`<CR>` on a file name line or a link line jumps to the step after it. The panel opens in the tab of the tour
window; a panel window left in another tab is closed first.

Paths shown relative to `W` (passed from Python as `workspace`). The current step line gets an
extmark `line_hl_group = NvtourPanelCurrent`. Buffer-local normal-mode maps in the panel: `<CR>`
jumps to the step under the cursor (in the tour window, not the panel), `q` closes the panel and
sets `panel.user_closed = true`. `panel --toggle` or `panel TEXT` reopen it and reset
`user_closed`.

If the user closes the panel window by other means, `panel.win` becomes invalid; treat it as
closed (do not reopen automatically for steps if `user_closed`).

## 8. Focus

`focus FILE:L1-L2 [L3-L4 ...] [--context N] [--dim]` — show only the given ranges of one file.

- Ranges are extended by `N` context lines (default 2), merged if overlapping, clamped to the
  buffer.
- Before the first step of a tour, `focus` shows the buffer in the tour window and moves the cursor
  to the first range. During a tour it does not move the view: folds are applied now if the tour
  window shows the buffer, else when the buffer is shown (by a step or by the user).
- Default (fold mode): save the window's local `foldmethod`, `foldenable`, `foldlevel`,
  `foldminlines` into `state.focus[buf].saved[win]` (one entry per window); set them with
  `:setlocal` semantics (`foldmethod = manual`, `foldenable = true`, `foldminlines = 0`);
  `normal! zE` (pre-existing manual folds in that window are lost); then for every gap between
  kept ranges `vim.cmd(("%d,%dfold"):format(a, b))`; close them (`zM` would also close kept
  regions, so instead create folds already closed: after creating, `normal! zM` then `zv` at the
  cursor is wrong; use `vim.cmd(("%d,%dfoldclose"):format(a, b))` per gap).
- `--dim` mode: no folds; one extmark per line outside the ranges with `line_hl_group =
  NvtourDim` in `ns_focus`. Buffer-local, works regardless of window.
- Focus state is per buffer. When `step` later shows that buffer in the tour window, fold-mode
  focus is re-applied (folds are window-local and may be gone). Fold the step's own range open
  (`zv` after the jump already does this).
- `unfocus [FILE]` — for each saved window: if it still shows the buffer, `normal! zE` and restore
  the options; if it shows another buffer now, keep the saved options in `pending_folds` and restore
  them on the next `BufWinEnter` of that buffer in that window (another buffer's options are never
  touched). Clear `ns_focus` extmarks, drop the state. FILE is compared with the stored path (no
  `bufnr()` pattern). With no FILE, unfocus all.

## 9. Read-only diff

`diff FILE (--ref GITREF | --file PATH | --stdin) [--title TEXT]`

Python obtains the "other" content: `--ref` → `git -C <toplevel> show <GITREF>:<path relative to
toplevel>` (error 6 if git fails, with git's stderr); `--file` → file contents; `--stdin` → stdin.
Sent to Lua as a list of lines plus `title` (default `GITREF` or basename).

Lua: `tabnew`; in the new tab's window create a scratch buffer named
`nvtour://diff/<title>/<basename>` (`buftype = nofile`, `bufhidden = wipe`, `swapfile = false`),
set lines, set `filetype` from `vim.filetype.match({ filename = path })`, `modifiable = false`,
`diffthis`. Then `vsplit`, show the real buffer (bufadd/bufload) in the right window,
`diffthis`, cursor in the right window, `pcall(vim.cmd, "normal! ]c")`. Push the tabpage handle to
`state.diff_tabs`. Notify `nvtour diff: <title> ↔ <basename>`.

`diff-close` — for each recorded tabpage still valid: `diffoff!` in it, then `tabclose`. Also
done by `clear`. The real buffer is never written; `diffthis` is window-local and disappears with
the tab.

## 10. Keymaps and user commands

Installed by `start`/first `step`, removed by `clear`. Global normal-mode maps, each set **only if
`vim.fn.maparg(lhs, "n") == ""`**; otherwise `vim.notify` a warning once, return it in `warnings`,
and skip that key. `vim.g.nvtour_keys` is read (missing entries use the defaults), never written.

```lua
DEFAULT_KEYS = {
  next = "]w", prev = "[w", first = "[W", last = "]W", panel = "<leader>wp", clear = "<leader>wc",
}
```

User commands (defined on load, always available): `:NvtourNext`, `:NvtourPrev`, `:NvtourFirst`,
`:NvtourLast`, `:NvtourGoto N`, `:NvtourPanel`, `:NvtourClear`.

The quickfix list also works (`:cnext`, `:cprev`, `:copen`); it moves the cursor only, not the
current step.

## 11. Safety rules (enforce in code and tests)

1. Never call `nvim_buf_set_lines`/`nvim_buf_set_text` on a buffer that is not an `nvtour://`
   scratch buffer. Never `:w`, `:edit!`, `:bd!`, `:qa`.
2. Never change global options except `hidden = true`. Window-local fold options and the tour
   `winbar` are set locally, saved and restored per window (the panel's own window options are not
   restored: nvtour closes that window). Highlight groups use `default = true`.
3a. Never replace a modified buffer that cannot be hidden (that would run `'autowrite'`).
3. Never close windows/tabs we did not create (panel window, diff tabs only).
4. Never touch an nvim instance the user did not select (no broadcasting).
5. Every command has one timeout budget for all its RPC calls; a hang never blocks the agent for
   more than `--timeout` seconds (plus the 2 s discovery probes, which run in parallel). A command
   is not sent to an nvim that waits for input.
6. Tests never connect to sockets under the real `$XDG_RUNTIME_DIR`; they set `XDG_RUNTIME_DIR` to
   a temp dir and start their own `nvim --headless --clean` there (nvim then creates its socket in
   that dir, so discovery is exercised end to end).

## 12. Packaging, install, skill

```
~/projects/nvtour/
  pyproject.toml          # name nvtour, console_scripts nvtour = nvtour.cli:main, dependency pynvim
  README.md               # usage, keys, example session, init.lua options
  DESIGN.md
  nvtour/__init__.py      # __version__
  nvtour/cli.py           # argparse, subcommands, output
  nvtour/discover.py      # socket scan, probe, score, pin cache
  nvtour/client.py        # pynvim attach with timeout, ensure_lua_loaded, call(cmd, args)
  nvtour/ranges.py        # FILE:L1-L2 parsing, path resolution
  nvtour/gitutil.py       # toplevel, git show
  nvtour/lua/nvtour.lua   # the runtime (package data)
  skill/SKILL.md          # symlinked to ~/.claude/skills/nvtour and ~/.codex/skills/nvtour
  tests/                  # pytest: unit + headless integration
  .gitignore
```

Install: `pip install --user --break-system-packages -e ~/projects/nvtour` → `~/.local/bin/nvtour`
(on PATH). Skill: `ln -s ~/projects/nvtour/skill ~/.claude/skills/nvtour` and the same into
`~/.codex/skills/nvtour`. No nvim plugin install is required; the runtime is injected over RPC.

### SKILL.md (same frontmatter format for Claude Code and Codex)

```
---
name: nvtour
description: Give a visual, read-only walkthrough of code inside the user's running Neovim while explaining a bug or a concept in chat — jump, highlight, annotate with virtual-text notes, fold to the relevant parts, show read-only diffs, keep a step panel. Use when the user explicitly asks to explain, walk through, show, or visualize something "in nvim" / "in the editor". Also use it, without asking first, when the user asks about code ("this chunk", "this function", "here", "what does this do"), gives no file, line or pasted code, and "this" does not point to something earlier in the chat: read their nvim cursor or selection to find the location and answer in chat. Never edits files.
---
```

Body (concise, imperative): see `skill/SKILL.md`. It covers point and ask (`attach` and `where`
when no location is given, the previous-selection caveat, the `@REF`, `modified` and no-file
flags, an answer in chat and a tour only on an explicit request in nvim), the workflow (attach,
start, one step per point, panel summary; only the first step moves the view), how to get line
numbers right (`rg -n`, the echoed line, `--expect`), fixing a tour (`edit`, `remove`, `--at`,
`status`), note style and roles, focus and its manual-fold caveat, diffs, and what to do on each
exit code.

## 13. Future

- Images: render a PNG (graphviz/mermaid) and show it with `snacks.image` when the terminal
  supports the kitty graphics protocol. Not for alacritty.
- MCP stdio wrapper exposing the same commands as tools.
- tmux: focus the pane running the selected nvim.
- Step groups / chapters in the panel for long concept tours.

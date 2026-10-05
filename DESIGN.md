# nvtour — design

`nvtour` lets an AI agent (Claude Code, Codex, houseofagents agents) give a **visual, read-only
walkthrough** of code inside the user's already-running Neovim. The agent explains a bug or a
concept in chat and, in parallel, drives the editor: jump to a range, highlight it, attach a short
comment as virtual text, fold away irrelevant code, show a read-only diff, and keep a side panel
with the step list and a longer markdown explanation. The user steps through with keys.

It is **not** a code editing or review tool. It never writes to a file buffer or to disk.

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
  resolution, git access for diffs, printing. Stateless except a small per-workspace pin cache.
- **Lua runtime** (`nvtour/lua/nvtour.lua`, shipped as package data): all editor state and all
  rendering. Every CLI command is **one** `nvim_exec_lua` round trip calling
  `_G.nvtour.dispatch(cmd, args)` so each operation is atomic from nvim's point of view.
- **Loading**: on every CLI run the client evaluates `return _G.nvtour and _G.nvtour.VERSION`.
  If it is missing or differs from the Python package's `LUA_VERSION`, the client sends the whole
  Lua source with `nvim_exec_lua`. Reloading must preserve existing state
  (`M.state = (_G.nvtour and _G.nvtour.state) or new_state()`).
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

For each socket: parse `<pid>` from the filename. If `/proc/<pid>` does not exist, mark **stale**
and skip (do not delete; `nvtour instances --prune` may unlink stale sockets, nothing else does).
Otherwise **probe** with an RPC call, 2 s timeout. The probe is one `exec_lua` returning:

```lua
{ pid = vim.fn.getpid(), cwd = vim.fn.getcwd(), version = vim.version(),
  buffers = <listed, named buffers, absolute paths, max 200>, tabpages = #vim.api.nvim_list_tabpages() }
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
| none | 0 |

Pick the unique highest score > 0. Ties or no score > 0 → not selectable.

### Precedence of socket choice

1. `--socket PATH`
2. `$NVTOUR_SOCKET`
3. `$NVIM` (set automatically inside an nvim terminal; if the agent runs inside that nvim, that is
   the instance, regardless of score)
4. pinned socket for `W` from the cache, if still alive
5. discovery + score

### Pin cache

`$XDG_RUNTIME_DIR/nvtour/<sha256(W)[:16]>` (fallback `~/.cache/nvtour/`) containing the socket
path. Written by `nvtour attach` and by a successful automatic selection. `nvtour attach --clear`
removes it. A dead pinned socket is ignored and removed.

### Exit codes

| code | meaning | stderr content |
|---|---|---|
| 0 | ok | |
| 2 | usage error | argparse message |
| 3 | no unique match | table of live instances (pid, cwd, score) + hint: `open nvim in <W>` or `nvtour attach <pid>` |
| 4 | no live nvim at all | `no running nvim found; open nvim in <W> and retry` |
| 5 | RPC failure or timeout | message + hint `nvim may be waiting at a prompt; press <Enter> in nvim` |
| 6 | bad file / range (file missing, L1 > L2, beyond EOF) | message |

The agent relays 3/4 messages to the user verbatim.

## 4. CLI reference

Global flags (before the subcommand): `--socket PATH`, `--workspace DIR`, `--json`,
`--timeout SECONDS` (default 5). Paths in arguments are resolved relative to the CLI's cwd, then
`realpath`'d before being sent to nvim.

```
nvtour instances [--prune]
nvtour attach [PID|SOCKET] [--clear]
nvtour where
nvtour start [TITLE]
nvtour step FILE:L1[-L2] [--note TEXT | --note -] [--label TEXT] [--role ROLE] [--no-jump]
nvtour goto N | nvtour next | nvtour prev
nvtour focus FILE:L1-L2 [L3-L4 ...] [--context N] [--dim]
nvtour unfocus [FILE]
nvtour diff FILE (--ref GITREF | --file PATH | --stdin) [--title TEXT]
nvtour diff-close
nvtour panel [TEXT | --file PATH | -] [--toggle] [--clear]
nvtour clear
nvtour doctor
```

Range syntax: `path:L1` (single line) or `path:L1-L2`. `focus` takes one file and one or more
ranges; extra bare `L3-L4` arguments apply to the same file.

### Commands

- **instances** — prints one line per socket: `pid  state(live|stale|unresponsive)  score  cwd
  (#buffers)`. With `--prune` unlinks stale sockets.
- **attach** — selects and pins an instance. With no argument runs selection; prints
  `attached: pid P  socket S  cwd C  (exact match|subdir|parent|buffers)`.
- **where** — context for "point and ask": `file:line:col`, mode, last visual selection of the
  current buffer (`'<`/`'>` marks, with the selected text, max 200 lines), visible range
  `top-bottom`, window cwd. Lets the user select code and ask "why is this here?".
- **start** — resets any existing tour (same as `clear` but keeps the panel if open), sets the
  title, creates a fresh quickfix list, installs keymaps. `step` without a prior `start` implicitly
  starts an untitled tour.
- **step** — appends a step (see §6) and jumps to it unless `--no-jump`. Prints
  `step N/N: file:L1-L2 [role] label`. `--note -` reads the note from stdin (lets agents pass
  multi-line text with a heredoc).
- **goto / next / prev** — move the current step (wraps at ends is **not** desired; clamp and say
  `already at last step`). Prints the same line as `step`.
- **focus / unfocus** — §8.
- **diff / diff-close** — §9.
- **panel** — §7. `TEXT`, `--file`, or `-` replaces the free markdown section. `--toggle`
  shows/hides the window. `--clear` empties the free section.
- **clear** — removes everything nvtour created: extmarks, signs, folds (restoring fold options),
  dim, diff tabs, panel window and buffer, keymaps, the nvtour quickfix list. Buffers that were
  loaded stay loaded. Prints `cleared`.
- **doctor** — checks: nvim on PATH and version ≥ 0.10, pynvim importable, runtime dirs scanned,
  live/stale sockets, selected instance, Lua runtime version loaded in it. Exit 0 if a usable
  instance exists.

`--json` makes every command print a single JSON object instead of text (the `dispatch` return
table plus `socket`, `pid`).

## 5. Lua runtime: state

```lua
M.VERSION = "<same string as Python LUA_VERSION>"
M.state = {
  tour = { title = "", steps = {}, current = 0, qf_id = nil },
  ns_steps = nvim_create_namespace("nvtour_steps"),
  ns_focus = nvim_create_namespace("nvtour_focus"),
  panel = { buf = nil, win = nil, text = {}, user_closed = false },
  focus = { [bufnr] = { ranges = {...}, dim = bool, saved = { foldmethod=, foldenable=, foldlevel=, foldminlines= } } },
  diff_tabs = { tabpage handles },
  keymaps_installed = false,
}
```

Each step: `{ n, file, buf, l1, l2, role, label, note, extmark_ids = {} }`.

### Tour window

The window where files are shown. Rule: the current window, unless it is the panel or belongs to
an nvtour diff tab; then the previous window (`wincmd p` target) or the first window in the first
non-diff tabpage that is not the panel. If the current tabpage is an nvtour diff tab, switch to
the previous tabpage first.

### Showing a buffer, safely

`local buf = vim.fn.bufadd(path); vim.fn.bufload(buf); vim.bo[buf].buflisted = true;
nvim_win_set_buf(tourwin, buf)`. Never `:edit`, never `:edit!`. Switching the buffer of a window
with `hidden` set keeps the previous buffer's modifications. (The user has `hidden = true`; also
set `vim.o.hidden = true` defensively on load, it is harmless.)

## 6. Step rendering

Roles and highlight groups (all defined with `default = true` so the user can override in
`init.lua`):

| role | line group | links to | sign/label group |
|---|---|---|---|
| `fault` | `NvtourLineFault` | `DiffDelete` | `NvtourSignFault` → `DiagnosticError` |
| `flow` | `NvtourLineFlow` | `DiffChange` | `NvtourSignFlow` → `DiagnosticInfo` |
| `fix` | `NvtourLineFix` | `DiffAdd` | `NvtourSignFix` → `DiagnosticOk` |
| `context` | `NvtourLineContext` | `CursorLine` | `NvtourSignContext` → `Comment` |
| `info` (default) | none | | `NvtourSignInfo` → `DiagnosticHint` |

Also `NvtourNote` → `Comment` with `italic = true`, `NvtourNoteBorder` → `NonText`,
`NvtourLabel<Role>` → same as the sign group, `NvtourDim` → `Comment` (used by focus `--dim`),
`NvtourPanelCurrent` → `Visual`.

For a step at `l1..l2` in buffer `buf` (0-based rows internally):

1. **Range highlight** — one extmark per line with `line_hl_group = NvtourLine<Role>` (skipped
   for `info`). One per line is deliberate: `line_hl_group` only applies to the mark's own row.
2. **Sign** — on `l1`: `sign_text = tostring(n)` (max 2 cells; `n ≥ 100` → `"++"`),
   `sign_hl_group = NvtourSign<Role>`, `priority = 100`.
3. **Note** — virtual lines **above** `l1` (`virt_lines_above = true`), one chunk per wrapped
   line: `{ {prefix, "NvtourNoteBorder"}, {text, "NvtourNote"} }`. Prefix `╭ ` for the first
   line, `│ ` for middle lines, `╰ ` for the last line; a single-line note uses `▸ `. Wrap width =
   tour window width − `textoff` (from `vim.fn.getwininfo`) − 4, minimum 30. Blank lines in the
   note are kept as `│`.
4. **Label** — on `l1`: `virt_text = { {"  ← " .. label, "NvtourLabel<Role>"} }`,
   `virt_text_pos = "eol"`.
5. **Jump** (unless `no_jump`) — show the buffer in the tour window, `nvim_win_set_cursor(win,
   {l1, 0})`, then `normal! zv` and `normal! zz` executed in that window via
   `nvim_win_call`. Set `tour.current = n`.
6. **Quickfix** — `vim.fn.setqflist({}, "r", { id = tour.qf_id, title = "nvtour: " .. title,
   items = <one item per step: filename, lnum, end_lnum, text = label or first note line, type =
   role initial> })`. The list is created with `setqflist({}, " ", {...})` by `start` and its `id`
   read back with `getqflist({ id = 0 }).id`, so the user's other quickfix lists are untouched.
7. **Panel** — re-render (§7); open it automatically on the first step unless
   `vim.g.nvtour_auto_panel == false` or `panel.user_closed`.
8. `vim.notify(("nvtour %d/%d: %s"):format(n, total, label or file:l1), vim.log.levels.INFO)`.

`goto/next/prev` perform (5), (7), (8) for an existing step and move the quickfix index with
`setqflist({}, "r", { id = qf_id, idx = n })`.

Validation (exit 6 from Python after the Lua check): file must exist and be readable; `1 ≤ l1 ≤
l2 ≤ line count`.

## 7. Panel

A scratch buffer `nvtour://panel` (`buftype = nofile`, `bufhidden = hide`, `swapfile = false`,
`filetype = markdown`, `modifiable = false` outside rendering) shown in a `botright vertical`
split. Window options: width `clamp(floor(columns * 0.3), 40, 70)`, `winfixwidth`, `wrap`,
`linebreak`, `nonumber`, `norelativenumber`, `signcolumn = no`, `foldcolumn = 0`,
`cursorline`, `winhighlight = "Normal:NormalFloat"` optional.

Rendered content:

```
# <title or "Walkthrough">

▶ 1. src/a.cpp:412-415  dangling iterator
  2. src/b.cpp:88       erase on cleanup thread
  3. src/a.cpp:430      the fix

---
<free markdown text set via `nvtour panel`>
```

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
- Default (fold mode): show the buffer in the tour window; save the window's `foldmethod`,
  `foldenable`, `foldlevel`, `foldminlines` into `state.focus[buf].saved`; set `foldmethod =
  manual`, `foldenable = true`, `foldminlines = 0`; `normal! zE` (only if we set manual ourselves
  — document that pre-existing manual folds in that window are lost); then for every gap between
  kept ranges `vim.cmd(("%d,%dfold"):format(a, b))`; close them (`zM` would also close kept
  regions, so instead create folds already closed: after creating, `normal! zM` then `zv` at the
  cursor is wrong; use `vim.cmd(("%d,%dfoldclose"):format(a, b))` per gap).
- `--dim` mode: no folds; one extmark per line outside the ranges with `line_hl_group =
  NvtourDim` in `ns_focus`. Buffer-local, works regardless of window.
- Focus state is per buffer. When `step` later shows that buffer in the tour window, fold-mode
  focus is re-applied (folds are window-local and may be gone). Fold the step's own range open
  (`zv` after the jump already does this).
- `unfocus [FILE]` — restore saved fold options for the tour window, `normal! zE` if we had set
  manual, clear `ns_focus` extmarks, drop the state. With no FILE, unfocus all.

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
`vim.fn.maparg(lhs, "n") == ""`**; otherwise `vim.notify` a warning once and skip that key.

```lua
vim.g.nvtour_keys = vim.g.nvtour_keys or {
  next = "]w", prev = "[w", panel = "<leader>wp", clear = "<leader>wc",
}
```

User commands (defined on load, always available): `:NvtourNext`, `:NvtourPrev`,
`:NvtourGoto N`, `:NvtourPanel`, `:NvtourClear`.

The quickfix list also works (`:cnext`, `:cprev`, `:copen`).

## 11. Safety rules (enforce in code and tests)

1. Never call `nvim_buf_set_lines`/`nvim_buf_set_text` on a buffer that is not an `nvtour://`
   scratch buffer. Never `:w`, `:edit!`, `:bd!`, `:qa`.
2. Never change global options except `hidden = true`. Window-local fold options are saved and
   restored. Highlight groups use `default = true`.
3. Never close windows/tabs we did not create (panel window, diff tabs only).
4. Never touch an nvim instance the user did not select (no broadcasting).
5. Every RPC call has a timeout; a hang never blocks the agent for more than `--timeout` seconds.
6. Tests never connect to sockets under the real `$XDG_RUNTIME_DIR`; they set `XDG_RUNTIME_DIR` to
   a temp dir and start their own `nvim --headless --clean` there (nvim then creates its socket in
   that dir, so discovery is exercised end to end).

## 12. Packaging, install, skill

```
~/projects/nvtour/
  pyproject.toml          # name nvtour, console_scripts nvtour = nvtour.cli:main, dependency pynvim
  README.md               # usage, keys, example session, init.lua options
  DESIGN.md
  nvtour/__init__.py      # __version__, LUA_VERSION
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
description: Give a visual, read-only walkthrough of code inside the user's running Neovim while explaining a bug or a concept in chat — jump, highlight, annotate with virtual-text notes, fold to the relevant parts, show read-only diffs, keep a step panel. Use when the user asks to explain, walk through, show, or visualize something "in nvim" / "in the editor", or asks for a guided tour of a bug, a code path, or a concept. Never edits files.
---
```

Body (concise, imperative):

- **Workflow**: `nvtour attach` first (relay exit 3/4 messages to the user verbatim and stop; do not
  start nvim yourself). Then `nvtour start "<title>"`. Then, interleaved with the chat
  explanation, one `nvtour step` per point. Prefer 3–8 steps, one idea per step, in reading order
  of the explanation (cause → propagation → effect, or entry → core → exit). Finish with `nvtour
  panel -` holding a short markdown summary and tell the user the keys (`]w` / `[w`, `<leader>wp`,
  `<leader>wc`, or `:cnext`).
- **Writing notes**: note ≤ 3 short sentences, label ≤ 6 words, roles: `fault` for the wrong
  line(s), `flow` for how data/control gets there, `fix` for where/how it should change,
  `context` for background, `info` for neutral explanation. Reference identifiers by name in the
  note; the highlight already shows the location.
- **When to use focus**: concept spans a long file → `nvtour focus FILE:a-b c-d` once per file
  before the steps in it. Use `--dim` when surrounding code matters for reading.
- **When to use diff**: before/after a fix (`--stdin` with the proposed version), or compare
  versions (`--ref origin/master`, `--ref v25.8`). It is read-only.
- **Point and ask**: if the user says "this" / "here", run `nvtour where` to read their cursor or
  selection.
- **Multi-line notes** via heredoc: `nvtour step f.cpp:10-12 --role fault --note - <<'EOF' … EOF`.
- **Never** modify files, never run `nvtour clear` unless the user asks or you start a new tour.

## 13. Future

- Images: render a PNG (graphviz/mermaid) and show it with `snacks.image` when the terminal
  supports the kitty graphics protocol. Not for alacritty.
- MCP stdio wrapper exposing the same commands as tools.
- tmux: focus the pane running the selected nvim.
- Step groups / chapters in the panel for long concept tours.

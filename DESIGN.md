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
- No images in v1 (the user's terminal is alacritty, no kitty graphics protocol). Text diagrams are in §10; images are in §14.
- No MCP server in v1. The CLI is the product; MCP is a thin wrapper later.

## 2. Architecture

```
agent (Bash) ──► nvtour CLI (Python, stdlib) ──msgpack-RPC over unix socket──► running nvim
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
- **RPC client** (`nvtour/rpc.py`): a small msgpack-RPC client on the standard library (socket and
  struct), not pynvim. A command is a new process that sends 3 requests, so the start of the
  process is most of its time; the import of pynvim (asyncio, greenlet) took about 30 ms of the
  75 ms of a command. The deadline of the budget is the timeout of the socket, so no thread is
  needed. Strings are UTF-8 with `surrogateescape` both ways (as in pynvim); an ext value (Buffer,
  Window, Tabpage) is read as its handle. A request from nvim to the client (`rpcrequest`) gets an
  error response, and a notification is skipped, so nvim never waits for the client.
- **Return protocol**: `dispatch` always returns a table `{ ok = true, ... }` or
  `{ ok = false, error = "message" }`. Lua errors are caught with `pcall` and converted to the
  error form. Python maps `ok = false` to exit code 5 and prints the message to stderr.

Dependencies: Python ≥ 3.11, Neovim ≥ 0.10 (user has 0.12.4). The tests also need `pytest` and `pynvim`
(the test extra), to look at the state of the test nvim.

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
| 2 | usage error | argparse message (or invalid `--timeout`/`--context`, stdin is a terminal, a bad tour file: §15) |
| 3 | no unique match | table of live instances (pid, cwd, score) + hint: `open nvim in <W>` or `nvtour attach <pid>` |
| 4 | no live nvim at all, or `--socket` path missing | `no running nvim found; open nvim in <W> and retry` / `socket not found: P` |
| 5 | RPC failure or timeout, nvim waiting for input | message with the phase; `waiting for input ... not run` |
| 6 | bad file / range (file missing, L1 > L2, beyond EOF, `--expect` mismatch); `marks --wait` without a mark | message (`no marks after S s`) |

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
nvtour marks [--wait SECONDS] [--clear]
nvtour start [TITLE] [--sheet SHEET]
nvtour step FILE:L1[-L2] | FILE:/TEXT/... [--ref GITREF] [--note TEXT | --note -] [--label TEXT] [--role ROLE]
            [--expect TEXT]... [--via TEXT] [--from N] [--suggest TEXT | --suggest -] [--at N]
            [--sheet SHEET#STEP] [--jump | --no-jump]
nvtour step L1[-L2] | /TEXT/... --diagram NAME [the same options]
nvtour edit N [FILE:L1[-L2] | FILE:/TEXT/... [--ref GITREF] | L1[-L2] | /TEXT/... --diagram NAME] [--note TEXT | --note -]
            [--label TEXT] [--role ROLE] [--expect TEXT]... [--via TEXT] [--from N]
            [--suggest TEXT | --suggest -] [--sheet SHEET#STEP] [--jump]
nvtour remove N
nvtour apply [FILE | -] [--append] [--check] [--no-jump]
nvtour chapter NAME [--at N] | nvtour chapter --remove N | nvtour chapter --list
nvtour goto N | nvtour next | nvtour prev | nvtour first | nvtour last
nvtour status
nvtour focus FILE:L1-L2 [L3-L4 | /TEXT/... ...] [--context N] [--dim]
nvtour focus --steps [FILE] [--context N] [--dim]
nvtour unfocus [FILE]
nvtour diff FILE (--ref GITREF | --file PATH | --stdin) [--title TEXT]
nvtour diff-close
nvtour diagram NAME [FILE|-] [--format mermaid|dot|easy|text] [--link TEXT=FILE:L1[-L2] | TEXT=FILE:/TEXT/...]... [--ascii]
               [--header N] [--split above|below|left|right|full] [--no-show]
nvtour panel [TEXT | --file PATH | -] [--toggle] [--clear]
nvtour clear [--keep-buffers]
nvtour doctor
```

Range syntax: `path:L1` (single line), `path:L1-L2` or `path:L1,L2`; `path:L:COL` is accepted and the
column is ignored (unless a file literally named `path:L` exists). `focus` takes one file and one or
more ranges; extra bare `L3-L4` arguments apply to the same file. With `--steps` the argument is a plain
FILE (or none) and a range is exit 2 (§8, "Focus from the steps").

**Ranges by text.** A range can be named by the text of its lines (plain text, not a regex):

```
FILE:/TEXT/                 the line that contains TEXT
FILE:/TEXT/+N               that line and N lines after it
FILE:/START/../END/         from the START line to the first END line at or after it, inclusive
FILE:/TEXT/@N               the N-th line with TEXT (from 1)
FILE:/TEXT/~L               the line with TEXT nearest to line L (a tie: the earlier line)
FILE:/TEXT/@N+2, FILE:/A/@2../B/+1    combined: @N or ~L after START (not both), +N last
```

Grammar after the file: `/START/ [@N | ~L] [../END/] [+N]`; `+N` goes on the last line, so it is the
end of `END` too. `\/` is a slash in `TEXT`; other backslashes are literal. `TEXT` and `END` are not
empty. The parse rule (`ranges.parse_spec`): a spec is a text range when it has a `:/` and the rest
after it reads as a well formed query. The first such `:/` splits, so `TEXT` can contain `:`
(`a.cpp:/Cache::get/`) and a path can contain `:/` (the candidates are tried from the left). With no
such `:/`, the old rules apply (`FILE:L1[-L2]`, `L1,L2`, `L:COL`, last `:` splits). A bad text range
(for example a missing closing `/`) is exit 6 with the reason. A slash in `TEXT` that is not
escaped can be read as the end of the text, or as another `:/` split: write `\/`.

Errors are exit 6: several lines match and there is no `@N` or `~L` (the message lists the matching
lines, at most 10, as `  N| text`); no match (`text 'T' not found in FILE (N lines)`); `@N` beyond the
number of matches; `END` not found at or after the `START` line. The resolved lines go through the
normal range checks (`+N` beyond the end of the file is exit 6).

Python (`ranges.py`): `parse_spec(spec) -> Spec(path, l1, l2, query)` (numbers or `query`, never both),
`query = { start, end, plus, nth, near }` (`end`, `nth` and `near` are None when absent);
`parse_file_range` is built on it and refuses a query; `parse_range_or_query(text)` is for the extra
`focus` arguments and for `--diagram` steps; `format_query` gives the canonical text (the field
`resolved_from`); `resolve_query(lines, query, name)` is the Python twin of the Lua function. The two
resolvers are small and run on the same table of cases in `tests/test_query.py` (same results, same
messages). `cli.location()` sends `query` (without the absent keys: nested nils do not survive
msgpack) and `resolved_from` instead of `l1` and `l2`.

### Commands

- **instances** — prints one line per socket: `pid  state(live|stale|unresponsive|blocked)  score
  cwd (#buffers)`. With `--prune` unlinks stale sockets (Unix sockets owned by the user only).
- **attach** — selects and pins an instance. With no argument runs selection; prints
  `attached: pid P  socket S  cwd C  (exact match|subdir|parent|buffers)`. It also loads the Lua runtime
  in that nvim (§5, "Marks": the mark keys are installed when the runtime loads); when that fails it prints
  `warning: could not load the nvtour runtime: ...` on stderr and still pins.
- **where** — context for "point and ask", read from the user's file window (the current window,
  or the previous one when the current window is a terminal, the panel or another special window):
  `file:line:col` (with ` @REF` when the window shows a ref step buffer; `file` is then the real path
  and `ref`/`sha` are set), mode, buffer flags (`modified`, `buftype`, `changedtick`), visible range
  `top-bottom`, window cwd, and the selection `{live, kind = char|line|block, l1, c1, l2, c2, text,
  truncated}`: live in visual mode, else the previous `'<`/`'>` selection. Char and block selections
  give the exact text (`getregion()`); at most 200 lines and 64 KiB.
- **marks** `[--wait SECONDS] [--clear]` — the places that the user marked with `<leader>wm` (§5, "Marks"),
  in the order they were made. One entry per mark, in the form of `where`:
  `N. FILE:L1-L2 [@REF] [(lines deleted)] (AGE) ["comment"]` (`diagram NAME:L1-L2` on a diagram), then up to
  5 lines of the saved text with 5 spaces in front, then `...` when there are more lines (or the text was
  cut). AGE is `N s ago`, `N min ago` or `N h ago`. No marks: `no marks`, exit 0. `--json`: `{ok, marks =
  [{n, file, ref, sha, diagram, l1, l2, kind = char|line|block, text (all lines), truncated, comment, at,
  age, deleted}], count, now}`. `--clear` removes the marks in the same Lua call that returns them (so what
  is printed is what is removed; marks made later stay). `--wait S` first polls `marks_count` every 0.5 s,
  each time with a new short RPC (own connection, `--timeout`, the check for a nvim that waits for input),
  and goes on when the count is more than 0; after S seconds it is exit 6 with `no marks after S s`. This is
  `run_marks` in `cli.py`; the call itself is the normal `run_remote`. While a `vim.ui.input` prompt of
  `<leader>wM` is open, nvim waits for input and every call fails with exit 5 (as for any prompt).
- **start** — resets any existing tour (same as `clear` but keeps the panel if open), sets the
  title, creates a fresh quickfix list, installs keymaps. `step` without a prior `start` implicitly
  starts an untitled tour. `--sheet SHEET` sets the sheet of the tour (§6, "Sheet link"); the id is
  checked (exit 2) and the output ends with `  ↔ sheet SHEET`.
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
  is exit 2 (only one can read stdin). `--sheet SHEET#STEP` links the step to a step of a `sheet`
  canvas (§6, "Sheet link"); the output then ends with `  ↔ sheet SHEET#STEP`.
- **edit / remove** — change or delete step N; all steps are renumbered and rendered again. A new
  location replaces the old one completely: `FILE:L1[-L2]` alone puts the step on the working tree,
  `FILE:L1[-L2] --ref GITREF` on that ref. `--ref` without a location is a usage error (exit 2).
  `edit --sheet SHEET#STEP` changes the sheet link, `--sheet ''` removes it. Links stay with their
  steps when the steps are renumbered; `edit` without `--sheet` keeps the link.
- **apply** — a whole tour from a TOML or JSON file (§15): by default a new tour (as `start`), then the
  diagrams, the focus, the steps and the panel text of the file, then one jump to step 1 (`--no-jump`: none).
  `--append` adds the steps to the current tour instead (a `title` or `sheet` in the file is exit 2; `from`
  numbers are the ones of the final tour; it jumps to the first new step). `--check` does all the checks and
  prints the resolved steps with the prefix `ok ` and `check: N steps ok`, and changes nothing. Everything is
  checked before anything changes: a failure is exit 6 (exit 2 for the form of the file) with the entry in
  front of the message (`step[4]: ...`) and the old tour stays. Reads stdin for `-` (the default) and refuses
  a terminal (exit 2).
- **chapter** — a heading in front of a step (§5, "Chapters"; §7; §11). `chapter NAME` is pending: it
  takes the next step that is added, at any place (also with `step --at`). `--at N` puts it in front of
  step N; N equal to steps + 1 is pending and a larger N is exit 6 (like `step --at`). Another chapter at
  that step gets the new name (at most one chapter starts at a step, and one is pending). `--remove N`
  removes the chapter that starts at step N (N = steps + 1: the pending chapter) and is exit 6 when there is
  none. `--list` prints the chapters in tour order. Each action prints one line: `chapter "Name" at step 3`,
  `chapter "Name" pending (next step)` (with `, replacing "Old"` when a name was replaced), `removed chapter
  at step 3`, `removed pending chapter`; `--list` prints one `chapter ...` line per chapter, or `no chapters`.
  `--json` has `name`, `step` (a number, `null` when pending), `pending`, `replaced`, `removed` and the
  list `chapters = [{ name, step }]`. The name is cut to one line (white space becomes one space). Exactly
  one of NAME, `--remove` and `--list` is needed (exit 2), and `--at` goes with NAME only.
- **goto / next / prev / first / last** — move the current step (wraps at ends is **not** desired;
  clamp and say `already at last step`). `prev` before any jump goes to step 1. Prints the same
  lines as `step`. A lost step (§6, "Steps follow edits") makes `goto` fail with exit 6;
  `next`, `prev`, `first` and `last` skip it.
- **status** — the tour (title, steps, current), focus (with `from_steps`), panel, diff tabs and installed keys. Read only.
  Steps follow the edits of the user (§6, "Steps follow edits"): the lines are the current ones, a
  step that moved shows `(was L1-L2)`, a step whose lines were deleted shows `(lost: reason)`, and
  `modified: a.cpp, b.cpp` lists the files whose buffer differs from disk (`modified_files` and
  per step `modified`, `orig_l1`, `orig_l2`, `lost`, `lost_reason` in `--json`).
  A step with a sheet link has `sheet` (`SHEET#STEP`) in `--json` and `↔ sheet SHEET#STEP` in the text;
  `tour_sheet` and the line `sheet: SHEET` show the sheet of the tour. `chapters = [{ name, step }]` is
  in `--json` (`step` is `null` when pending); the text has a line `## Name` before the step that starts the
  chapter, and `## Name (pending: the next step added)` at the end for a pending chapter.
  `follow` (`--json`, true or false) tells if the follow mode is on (§6, "Follow the cursor"); the text has
  the line `follow: on` only then. `marks` (`--json`, a number) is the number of marks of the user (§5, "Marks");
  the text has the line `marks: N` only when N is more than 0. `mark_keys` (`--json`) has the installed keys of
  the marks; `keys` has only the keys of the tour.
- **focus / unfocus** — §8.
- **diff / diff-close** — §9.
- **diagram** — renders a diagram into a read-only buffer that steps can point to (`step --diagram`);
  §10.
- **panel** — §7. `TEXT`, `--file`, or `-` replaces the free markdown section. `--toggle`
  shows/hides the window. `--clear` empties the free section.
- **clear** — removes everything nvtour created: extmarks, signs, folds (restoring fold options),
  dim, diff tabs, panel window and buffer, keymaps, the nvtour quickfix items. Buffers that were
  loaded stay loaded; the ones nvtour added to the buffer list are unlisted again unless shown,
  modified, or `--keep-buffers`. Prints `cleared`.
- **doctor** — checks: nvim on PATH (optional), runtime dirs scanned,
  live/stale sockets, selected instance, version of that nvim ≥ 0.10, Lua runtime version loaded in
  it. The exit code is the code of the first failed required check (selected instance,
  nvim version, connection). The check `sheet command` is informational (never fails the command):
  it says whether `vim.g.nvtour_sheet_cmd` (default `sheet`) is executable in the selected nvim.

`--json` makes every command print a single JSON object instead of text (the `dispatch` return
table plus `socket`, `pid`, and `warnings` when there are any). Without `--json`, warnings are
printed on stderr as `warning: ...`.

## 5. Lua runtime: state

```lua
M.VERSION = (...) or "dev" -- source hash passed by the client
M.state = {
  tour = { title = "", steps = {}, current = 0, qf_id = nil, chapters = {} },
  ns_steps = nvim_create_namespace("nvtour_steps"),
  ns_focus = nvim_create_namespace("nvtour_focus"),
  ns_panel = nvim_create_namespace("nvtour_panel"),
  panel = { buf = nil, win = nil, text = {}, user_closed = false, line_map = {} },  -- line_map: panel line -> step
  focus = { [bufnr] = { ranges = {...}, dim = bool, file = path,
                        saved = { [winid] = { foldmethod=, foldenable=, foldlevel=, foldminlines= } },
                        anchors = { { id, tail } ... },  -- nil for a focus from the steps
                        from_steps = { context = n, dim = bool },  -- nil for a plain focus
                        ref = "v1" } },  -- file at a git ref (a focus from the steps only)
  pending_folds = { [winid] = { [bufnr] = saved fold options } }, -- restored on BufWinEnter
  diff_tabs = { tabpage handles },
  keymaps = { [lhs] = true },  -- the lhs of every key that we mapped (tour keys and mark keys)
  keys = { [action] = lhs },  -- the keys of the tour that were installed (KEYS.tour)
  keymaps_installed = false,  -- the tour keys are installed
  workspace = nil,  -- W, passed from Python (paths in the panel are relative to it)
  warned = {},  -- keys that gave a warning already (one warning per key)
  mark_keys = { [action] = lhs },  -- the keys of the marks that were installed (KEYS.always)
  mark_keymaps_installed = false,  -- the mark keys are installed
  marks = { { id, buf, file, ref, sha, diagram, l1, l2, kind, text, truncated, comment, at, extmark, tail } },
  ns_marks = nvim_create_namespace("nvtour_marks"),  -- the extmark and sign of each mark (§5, "Marks")
  mark_seq = n,  -- the id of the next mark
  tour_win = winid,           -- last window used for the tour
  added_bufs = { [bufnr] = true }, -- buffers nvtour added to the buffer list
  ns_flash = nvim_create_namespace("nvtour_flash"),
  flash = { buf = bufnr, seq = n },  -- the last flash; a newer one cancels its timer
  winbars = { [winid] = the window's own local 'winbar' },  -- restored by clear
  refs = { [sha .. ":" .. file] = { buf, file, rel, ref, sha, lines } },  -- ref step buffers
  ns_diagram = nvim_create_namespace("nvtour_diagram"),  -- the colours of diagram lines and links
  diagrams = { [name] = { name, file, buf, lines, header, links = { { text, file, l1, l2 } } } },  -- §10
  diagram_split = nil,  -- the last `diagram --split` (§10)
  sheet_pending = {},  -- { timer, step }: the debounced `sheet goto` (§6, "Sheet link")
  ns_anchor = nvim_create_namespace("nvtour_anchor"),  -- one extmark per step (§6, "Steps follow edits")
  attached = { [bufnr] = true },  -- buffers with our nvim_buf_attach
  sync_timers = { [bufnr] = uv timer },  -- the debounced sync after an edit
  reloaded = { [bufnr] = true },  -- the content was replaced; the steps are anchored again
  follow_timers = { [winid] = uv timer },  -- the debounced check of the follow mode (§6, "Follow the cursor")
  follow_jump = { win, buf, line },  -- where the last jump put the cursor; the follow check ignores it
  peek = { win, buf, step, group, maps },  -- the open peek float (§6, "Peek a step"); {} when there is none
}
```

Each step: `{ n, file, buf, l1, l2, role, label, note, expect, via, from, suggest, sheet, ref, sha, diagram, extmark_ids = {},
anchor, anchor_buf, lost, lost_reason, orig_l1, orig_l2, query, resolved_from }`.
`anchor` is the id of the anchor extmark (in `ns_anchor`, in buffer `anchor_buf`); a step on a diagram has none.
`l1` and `l2` are the lines that the anchor had at the last sync. `lost` is true when the lines of
the step are gone, and `lost_reason` says why. `orig_l1` and `orig_l2` are the lines that the agent
gave. `query` is the text range that gave the lines (nil when the agent gave numbers) and
`resolved_from` its text (`/TEXT/...`, the field of the same name in the step result); both are kept
but not used after the step was made. A step made by an older runtime has none of these fields; every
use of them accepts `nil`.
`diagram` is the name of the diagram of a step on a diagram (§10), else nil.

### Chapters

`tour.chapters = { { name, step = <step table> | nil } }`. A chapter is a heading in front of a step; it
holds that step and the steps after it up to the next chapter. The steps before the first chapter are in
no chapter. Like `from`, a chapter points at the step table, so an insert or a remove needs no
renumbering. The list is in tour order, pending chapter (no `step`) last; `sort_chapters()` restores the
order when a pending chapter takes a step. At most one chapter starts at a step and one is pending
(`set_chapter(name, step)` replaces the name). When a step is removed, its chapter moves to the step now at
that index; with no such step, or when that step starts a chapter already, it is dropped. `start`
and `clear` set a new list. `set_chapter(name, step)` is also `M.set_chapter` for other code (the
batch `apply`). A tour made by an older runtime has no `chapters` field: `chapters()` creates it on
first use, so a reload during a tour is safe.

**Heading above the step.** `arrival_lines(step, width, chapter_only)` puts the line `## Name` first
(`NvtourPanelChapter` for the `## ` and the name, which are two chunks) when `chapter_at(step)` is a
chapter and the step is not lost. This is for every chapter start, not only when the chapter changes,
because a chapter start is structure, not a transition. The current step gets it before the `◇` and
`↔ sheet` lines, in the same frame or band. A step that is not current gets only this line
(`chapter_only`), above its collapsed note (or alone, when it has no note); the `◇` and sheet lines stay
for the current step. The name is cut with `clip_chunks` to the note width (`…`), so the heading is one
line. `peek.lines` keeps the heading first and adds its `◇` line after it. The line depends on the
chapters, so `H.chapter` calls `rerender_all()` after `set_chapter` and `drop_chapter`; `step`, `remove` and
`apply` do it already (a pending chapter takes its step before that call).

`]c` and `[c` (`next_chapter`, `prev_chapter`) use the first step of each chapter that is not lost
(a chapter of lost steps only is skipped). `]c` goes to the first start after the current step. `[c`
goes to the last start before the current step: inside a chapter, not at its first step, that is the
start of the chapter (as `[[` goes to the start of the section); at the first step of a chapter it is the
chapter before. At the end they answer `already in the last chapter` / `already at the first chapter`;
with no chapter, `no chapters` (exit 6 in the result, a warning for a key).
`suggest` is a list of lines (nil when there is no suggestion).
`ref` and `sha` are nil for a step on the working tree. `from` is the step table of a `--from N`
link (not a number, so it stays correct when steps are inserted or removed); a link to a removed
step falls back to the step before. `below` is the number of link lines below the range, and
`drawn_win` the window that the band was computed for.

### Marks

The user marks places for the agent (`<leader>wm`, `<leader>wM`; `mark` and `mark_note` in `nvtour_keys`).
The marks belong to the user, not to the tour: `start` and `clear` keep them (`reset` does not touch
`S.marks` or `ns_marks`), and only `H.marks({ clear = true })` (`nvtour marks --clear`) and
`:NvtourMarksClear` remove them. They are in `S.marks` only, so they are gone with the nvim process; a
reload of the runtime keeps them (the fill loop after `new_state()`).

`H.mark` (the key calls it through `dispatch`, like the other keys) works on the current window: a file
window, a ref step buffer or a diagram window (else `mark: this window shows no file`). In visual mode
(`SEL.kind[mode]`) it takes the selection from `selection_of(win, buf, mode)`; that function was
factored out of `H.where`, so `where` and the marks have the same limits `SEL.lines` = 200 and `SEL.bytes`
= 64 KiB, and the same text (`getregion`). In normal mode the mark is the cursor line, kind `line`. A mark
has `file` (the real path for a ref buffer, with `ref` and `sha`), `diagram` for a diagram buffer, the
saved `text`, `at = os.time()` and, for `<leader>wM`, a `comment`. The key action then asks with
`vim.ui.input({ prompt = "Note for the agent: " })`; an empty or cancelled input keeps the mark without a
comment (`H.mark_comment` finds the mark by `id`; the number `n` changes when marks are cleared). A visual
selection ends after the key.

The range is an anchor made with `set_anchor` and read with `read_anchor`, both with the namespace `ns_marks`
(§6, "Steps follow edits": gravity, `invalidate`, `undo_restore`). The extmark has `sign_text = "◆"` in
`NvtourUserMark` (links to `DiagnosticHint`), priority 200 (above the bar of a step). The sign is on the
first line of the range only (one extmark). `H.marks` calls `marks.sync` for each mark, which reads the lines
back from the extmark; when all lines were deleted (`invalid`) the mark keeps its last range and has
`deleted = true` (`(lines deleted)` in the output), and an undo clears the flag. The `text` is the text at
the time of the mark.

Keys: `KEYS.tour` (next, prev, first, last, panel, clear, peek, next_chapter, prev_chapter) and
`KEYS.always` (mark, mark_note). `install_keymaps(set)` and `remove_keymaps(set)` take one of them (in one
table `KEYS` because the main chunk is at the limit of 200 local variables). `reset` removes only
`KEYS.tour`. `install_keymaps(KEYS.always)` runs at the end of the runtime chunk, so the mark keys exist after
any command, also `attach`, without a tour. The keys of the marks are set in normal and visual mode, with
the same rule as the others (free in both modes; one warning per key in `S.warned`). A runtime upgrade removes
the keys that we installed and installs them again, so a key is never mapped twice and a key of the user that
we skipped is not warned about again. `status` has `mark_keys` for them.

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
   tour window, then `make_current(step, win, { scroll = true })`: set `tour.current = n`,
   re-render the old and the new current step, set the winbar, and scroll (in that window via
   `nvim_win_call`): cursor on `l1`, `normal! zv`, and
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

Text ranges (§4, "Ranges by text"): `checked_range(a, l1, l2, expect)` loads the buffer of the location
(the file, the blob of `--ref`, or the diagram) and, when `a.query` is set, first calls the pure
function `resolve_query(lines, q, name)` on all the lines of that buffer (also exposed as
`M._resolve_query`, so tests can call it through `exec_lua`). It returns `l1, l2`, which replace
the numbers before the range checks and the `--expect` check; `checked_range` returns them with the
buffer. `H.step` and `H.edit` store `query` and `resolved_from` in the step; `step_result` returns
`resolved_from`. The text is searched in the buffer, so unsaved edits count, and once: after that the
step has an anchor like any other.

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

**Not supported for refs:** `focus FILE:...` (it takes a working tree file; `focus --steps` does focus
the ref buffers of steps) and `diff` (it compares the working tree buffer). Language servers: most configurations do not attach to `nofile` buffers; one
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

### Sheet link

A step can go with a step of a canvas of the `sheet` tool (a live visual canvas that an agent
drives from a CLI). `--sheet SHEET#STEP` on `step` and `edit` stores `{ sheet, step }` in the step.
SHEET is a sheet id (`[A-Za-z0-9_][A-Za-z0-9_-]*`). STEP is a number (0 is the overview) or a step
id (`[A-Za-z_][A-Za-z0-9_.-]*`). The Python CLI checks both (exit 2). A value without `#` is exit 2,
so a step id is never read as a sheet id. `start --sheet SHEET` sets the sheet of the tour; then
`--sheet '#STEP'` uses it, and `#STEP` without a sheet of the tour is exit 6. `--sheet ''` on
`edit` removes the link.

- **When.** Each time a step with a link becomes the current step: `goto`, `next`, `prev`, `first`,
  `last`, the keymaps, `<CR>` in the panel, and the jump to the first step by `step`. The call is
  in `make_current()`, the one place where the current step changes (every navigation goes through
  `jump()`, which calls it; the follow mode calls it too, see "Follow the cursor").
  `edit` and a render do not call it, unless the link of the current step changed. `remove` does
  not call it. `edit` and `remove` keep the links of the other steps. `clear` and `start` drop all
  links and cancel a call that waits.
- **How.** The Lua runtime runs `vim.system({ CMD, "goto", "-q", "--no-autostart", SHEET, STEP },
  { detach = true })`, never waits for it, discards the output and ignores failures. CMD is
  `vim.g.nvtour_sheet_cmd` or `sheet`. When CMD is not executable, nothing happens (no error).
- **Debounce.** The call starts 150 ms after the step became current; a later step replaces it
  (the last step wins). A step without a link also cancels the call of the step before it. Holding
  `]w` starts one process, not one per step.
- **Output.** The step line ends with `  ↔ sheet SHEET#STEP`; `status` and `--json` have `sheet`;
  the panel shows `  ↔ SHEET#STEP` after the step title (`NvtourVia`, muted).
- **Sheet change.** When the current step is the first step of the tour with a link, or links to
  another sheet than the last step before it in the tour that has a link (the sheet the user saw
  last), the block above its note has the line `↔ sheet SHEET` (`NvtourVia`, the id in
  `NvtourViaLoc`), after the `◇` line. Steps without a link between them do not count.
- **Remote and environment.** The nvim process starts `sheet`, so by default its environment
  decides which viewer is used. The CLI of `step`, `edit --sheet` and `start --sheet` also records
  `SHEET_SOCKET` and `SHEET_HOME` from its own environment (only these two, only when set and not
  empty) with the link. The runtime passes them in the `env` of `vim.system`, merged over the
  environment of nvim; without them the call uses the environment of nvim. So a
  `SHEET_SOCKET=tcp://127.0.0.1:PORT` forward works when nvim was started before the variable was
  set. A `#STEP` link without variables in its own call uses the ones of `start --sheet`. When nvim
  runs on a remote host, set the variable in the environment of the agent (or of nvim).

### Steps follow edits

The user can edit a buffer during a tour. The extmarks of a step move with the edit, but the numbers
`l1` and `l2` do not. So each step on a file (or on a git ref) has one **anchor**: an extmark in
`ns_anchor` over its lines. `sync_step` reads the anchor back into `l1` and `l2`.

- **Anchor.** `nvim_buf_set_extmark(buf, ns_anchor, l1 - 1, 0, { end_row, end_col = #line(l2),
  right_gravity = true, end_right_gravity = false, invalidate = true, undo_restore = true })`. A line
  inserted above the first line (`O`) moves the range down. A line inserted after the last line
  (`o`) does not make the range longer. A line inserted inside the range makes it longer. When a
  range ends on an empty line, its end is the start of the next line, so that the end is not read as
  an end that moved there. When the last line of the range is deleted, nvim moves the end to column 0 of
  the next line; that row is then the first line after the range. A step on a diagram has no anchor,
  because `diagram NAME` replaces all its lines.
- **Sync.** `sync_step` sets `l1` and `l2` from the anchor. It is called first in `jump`,
  `step_goto`, `H.edit` (when no new location is given), `H.remove`, `rerender_all`, `update_qf`,
  `render_panel`, `H.status` and `step_result`. This costs one `nvim_buf_get_extmark_by_id` per
  step. A step that changed is `stale` until it is drawn again.
- **Change detection.** Every buffer that gets a step is watched with `nvim_buf_attach(buf, false,
  { on_lines, on_reload, on_detach })`. The `on_lines` callback runs with the text locked, so it only
  starts a timer: 150 ms after the last edit (one timer per buffer, as in "Sheet link") the sync runs
  for that buffer. When a step of it is stale, `rerender_buf` draws the steps of this buffer again
  (not `rerender_all`: an edit in one file of a tour across 10 files does not draw the other 9), then
  the quickfix list and the panel are updated. The current step in another buffer is drawn again when
  the step after it is in this buffer (its "next" line shows the line). The focus ranges of the buffer
  have anchors too (a focus from the steps does not: §8); when they changed, the folds are made again (after `InsertLeave` while the user
  types). The callbacks call through `_G.nvtour`, so they survive a runtime upgrade.
  `on_lines` returns `true` (detach) when the buffer has no more steps and no focus: this is how
  `clear` detaches.
- **Reload.** `:e!`, or a reload after a change on disk (`autoread`), replaces the content, and the
  extmarks collapse. `on_reload` and `BufReadPost` (a fallback, also for a buffer that was wiped and
  loaded again) set `reloaded[buf]`; the next sync anchors each step of the buffer again from its
  last `l1` and `l2`, then checks the range and each `--expect` text. A range beyond the end of the
  file, or a missing text, makes the step lost (`lost_reason`). The file on disk is the source after a
  reload, so the agent runs `status` after files changed.
- **Lost steps.** Deleting all lines of the range makes the anchor invalid (an undo makes it valid
  again, and the step is back). A lost step has no marks in its buffer; the panel shows `✗ lost`
  after the range (`NvtourLost`, linked to `DiagnosticError`); the quickfix text ends with
  `(lost)`. `goto N` fails with exit 6 and `step N: its lines were deleted; re-point it with: nvtour
  edit N FILE:L1-L2`. `next`, `prev`, `first` and `last` skip a lost step with a notification (and a
  warning in the CLI output). `edit N --note ...` without a location changes the other fields and
  keeps the step lost; `edit N FILE:L1-L2` gives it new lines and a new anchor.
- **Report.** `status` has per step `orig_l1`, `orig_l2` (the lines that the agent gave), `lost`,
  `lost_reason` and `modified` (the buffer differs from disk), and the top-level list
  `modified_files`. The text shows `(was 88-90)` after a step that moved, `(lost: reason)` after a lost
  step, and the line `modified: a.cpp, b.cpp` after the steps. `step`/`goto` output ends with
  `(lost)` for a lost step. A line number from `rg` on disk can differ from the buffer when the file is
  in `modified`.
- **Diagram steps.** `diagram NAME` for a name that exists checks the steps on it again: a step beyond
  the new last line is still an error (nothing changes); a step whose `--expect` text is not in its
  lines any more is lost, until a later version of the diagram has the text again.
- **Upgrade.** `rerender_all` makes the anchor of a step that has none (a step of an older runtime, or
  a step whose buffer was loaded again), from its `l1` and `l2`. `reset` clears `ns_anchor` in all
  buffers and cancels the timers. `clear_step_marks` clears `ns_anchor` too. Reading or writing an
  anchor never changes a buffer: anchors are extmarks (§12 rule 1 is the same).

### Follow the cursor

`jump(step)` shows the buffer, enters the window and calls `make_current(step, win, opts)`. That
function is the one place where the current step changes: it sets `tour.current`, draws the old and
the new step, sets the winbar of `win` and starts the sheet link (`sheet_schedule`). With
`opts.scroll` (a jump) it also scrolls, notes the cursor position in `state.follow_jump` and flashes
the range. With `opts.lists` it also runs `update_qf(n)` and `render_panel()`. `jump` does not set
`lists`: its callers do that, because `add_step` jumps before the step is in the tour.

`vim.g.nvtour_follow_cursor` (`true` or `1`; default off) turns the follow mode on. The one
`CursorMoved` autocmd (group `nvtour`, not `CursorMovedI`) always exists and returns at once when the
mode is off or the tour has no step; `:NvtourFollow` only flips the option. A move in a window that
shows a buffer with a step starts a 100 ms timer of that window (`state.follow_timers[win]`, like the
sheet link and the sync after an edit; `reset` and `:NvtourFollow` (off) cancel them). The window is
a file window or a diagram window; the panel and the windows of a diff tab never count.

When the timer fires, the window must still show the same buffer. `sync_buf(buf)` reads the anchors.
The step with the cursor line in `l1..l2` is chosen: the greatest `l1`, then the smallest `l2` (the
innermost; the current step wins a tie); lost steps are skipped. When it is not the current step,
`make_current(step, win, { lists = true })` runs, with `win` the window of the cursor (not
`state.tour_win`: the buffer can be in several windows). Nothing else changes: no scroll, no flash, no
`notify`, no cursor or focus change. `state.tour_win` is not set. No step under the cursor: the
current step stays. A jump moves the cursor to `l1` of its step; the check ignores that one position
(`state.follow_jump`), else a smaller step that starts on the same line would take the current step
from the step of the jump. `status` has `follow`.

### Peek a step

A step that is not current has a one-line note. `peek` shows all of a step in a float at the cursor,
without a change of the current step (`make_current` is not called: no redraw of the steps, no winbar,
panel, quickfix or sheet change). The key (`peek` in `DEFAULT_KEYS`), `K` in the panel and
`:NvtourPeek [N]` all call `H.peek({ n })`. The step is `n`, else the step of the panel line (when the
current window is the panel), else the one under the cursor (`peek.step_at_cursor`: `sync_buf`, then the
same choice as follow mode: greatest `l1`, then smallest `l2`; lost steps do not count). No step:
`nvtour: no step here`. `n` out of range: `fail` (exit code 6; a warning in the user command). When a float
is open, `H.peek` closes it first; the same step again (or no step) only closes: that is the toggle.

`peek.lines(step, width)` builds the lines with the functions of `render_step`, which return chunk lists:
`arrival_lines` (the `◇` line is added, after the chapter heading, when the arrival lines have none,
because the peek is about one step), `note_chunks(note, width, true)` (plain, no prefix), `suggest_lines` in the frame `peek.box`
(`frame` uses the same function, so the frame of the suggestion is the same in both places), and
`next_lines`. A lost step gets a `✗ lost` line first. The chunks become the lines of a scratch buffer
`nvtour://peek` (`bufhidden = wipe`) with one extmark per chunk (`ns` `nvtour_peek`). The window is
`nvim_open_win(buf, false, { relative = "cursor", row = 1, col = 0, border = "rounded", style = "minimal",
focusable = false, zindex = 60, title = "N/T label" })`, `winhighlight = FloatBorder:NvtourFrame<Role>`. The
text width is `vim.g.nvtour_note_width`, else the window width minus the gutter and the border, at most
100 (the panel is narrow: the editor width). The height is the number of lines, at most half the window.

`S.peek = { win, buf, step, group, maps }` exists while it is open. One augroup `nvtour_peek` (created on
open, deleted on close) has `CursorMoved`, `CursorMovedI`, `BufLeave`, `WinLeave` and `WinClosed` of the
source window. They close the float with `vim.schedule` (not all windows can be closed inside an autocmd).
A cursor event closes only when the cursor is not where it was at the open: a `CursorMoved` that was queued
before the key must not close the float that the key opened. `<Esc>` and `q` are buffer-local maps in the
source buffer while it is open; the entry of `maparg` with `buffer = 1` (a map of the user) is not
replaced, and the maps are removed on close. `peek.close` is the one place that undoes all of it; `reset`
(`clear` and `start`) calls it. It reads only `S.peek`, so it also closes a float of an older runtime.

### Frame and band

All virtual lines of a step (link lines, the note, the collapsed note) must not look like code. Both
styles start the lines in the gutter (`virt_lines_leftcol`) with blank cells up to the code column
(`textoff`), so the text is in the column of the code, and in the current step the gutter has `▎` in
`NvtourSign<Role>` in the column of the range bar, so one bar goes from the note through the range.
The column is `textoff - number width - 1` (the second cell of a 2-cell sign column before the
numbers; the number width as in nvim: `max('numberwidth', digits + 1)`); there is no bar with a
`'statuscolumn'`, `signcolumn=no` or `signcolumn=number`. Both need a window: a step drawn without
one gets plain lines, and is drawn again on `BufWinEnter` in the window that shows its buffer.

**Frame** (the default). No background. The block above `l1` of the current step (the chapter heading, the `◇` line and the
note) is in a frame `╭─╮ │ │ ╰─╯` in `NvtourFrame<Role>` (the role accent blended 75 % over `Normal`
bg), from the code column to the right edge of the window, or to `vim.g.nvtour_note_width` + 4 cells when that is set (notes then wrap at that width); the note has no `╭ │ ╰ ▸` prefixes in it.
The "next" line gets a lead `╶─ ` in `NvtourFrame<Role>`, a collapsed note a lead `╶─ ` in
`NvtourNoteBorder` (instead of `╶ `); continuation lines are indented by 3 cells. The `## Name` line of
a chapter start is the first line of that block, also for a step that is not current (the lead is then
before the heading and the collapsed note is indented below it). The frame costs 2
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

### Side view

`side.view()` is `S.view` (set by `view`), else `vim.g.nvtour_view == "side"`, else `"inline"`. All of the
above is the inline view. In the side view (`render_step` → `side.draw`):

- **Every step:** the line numbers (`number_hl_group`) only. No note, link line, chapter line, eol marker
  or collapsed suggestion.
- **Current step:** the range style, then below `l2` the suggested code (the frame of "Suggested code",
  without the gutter bar), `NvtourStrike` on the replaced lines and the `--expect` marks (as inline). `step.below` counts the lines below `l2`
  for `scroll_to`.

The **range style** is `S.range_style` (from the cycle key or `range_style STYLE`), else
`vim.g.nvtour_range_style`, else `rules`; an invalid name falls through. It is parts joined by `+`:

- `rules`: a virtual line above `l1`, `── N <mark> <title> ───`, and one below `l2`, `─────`, in
  `NvtourFrame<Role>`, from the code column to the right edge (or `nvtour_note_width` + 4).
- `bracket`: `sign_text` ` ┌`, ` │`, ` └` (one line: ` ▸`) in `NvtourSign<Role>`.
- `spotlight`: `NvtourDim` (`hl_eol`, priority 200) from line 1 to `l1 − 1` and from `l2 + 1` to the end.

`side.STYLES` is the cycle: `rules`, `bracket`, `spotlight`, `rules+spotlight`.

The **step card** is a scratch buffer `nvtour://step` (`nofile`, `hide`, markdown, treesitter started)
in a `split = "left", win = -1` window of the tab of the tour window, width `clamp(columns * 0.28, 36, 64)`,
`winfixwidth`, `wrap`, `linebreak`, `conceallevel = 2`, `statuscolumn = "  "`. `side.render()` (called by `make_current` before
`render_step`, so the rules get the final width, and by `render_panel`) opens it in the side view when there is
a current step and closes it otherwise. Lines, highlighted with extmarks in `nvtour_card`:

```
1/3 ✗ fault · <chapter>                 NvtourSign<Role>, NvtourPanelChapter
<label>                                 NvtourCardTitle
<path>:<range>                          NvtourPanelFile
working tree · <branch> @ <sha12>       the version, see below
repo <name> | worktree <name>           the repository
↔ SHEET#STEP                            NvtourVia
↑ from N: <via>                         --via or --from of this step
────                                    overlay virt_text, the window width (drawn again on WinResized)
<note lines, raw markdown>
Watch                                   NvtourCardTitle
• <expect>                              one line each: NvtourSign<Role>, NvtourNoteCode
────
Next  <next key>                        NvtourCardTitle, the key in NvtourPanelProgress
N <mark> <title>                        NvtourSign<Role>, NvtourLabel<Role> ("last step" at the end)
<link_loc>                              NvtourViaLoc
↓ <via of the next step>

Previous  <prev key>                    the same block without the via ("first step")
```

The version lines come from one `git -C DIR rev-parse --show-toplevel --absolute-git-dir
--git-common-dir HEAD --symbolic-full-name HEAD` (cached per directory for 2 s, 1 s timeout). A step on a
file: `working tree · <branch> @ <sha12>` (the branch from `refs/heads/...`, else `detached`). A step at a git
ref: `git ref <ref> · <sha12>`, or `commit <sha12>` when the ref is a prefix of the commit. Then `repo <name>`
(the toplevel's name), or `worktree <name>` when the common dir is not the git dir (a linked worktree). A
diagram: none. The window has `statuscolumn = "  "` (`side.MARGIN`): a left margin, also on wrapped lines;
the rules end the same 2 cells before the right edge.
`fillchars = "eob: "`, `breakindentopt = "list:-1"` with a `formatlistpat` for `•`, `-`, `*`, `+` and `1.`
(a wrapped item goes on under its text). Card keys: `<CR>` on a line of the Next or Previous block (`card.line_map`) goes to that step, `q` runs `card`.

`card` hides the card when the side view shows it (`card.user_closed = true`: `side.render` keeps it closed,
also on jumps), else shows it and sets the side view. `view side` resets `user_closed`, so the side view comes
back with its card; so does `clear`. `<leader>ws` and `:NvtourCard` run it.

`view [inline|side|toggle]` and `range_style [STYLE]` (it also sets the side view) render all steps again
(`rerender_all`), draw the card, scroll the current step into view (`scroll_to`) and render the panel. A bad
name is exit 2. Both return a `message` that the keys show. `status` returns `view`, `range_style` and
`card` (open). `clear` closes the card; the view stays for the next tour.

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

## Why does it fail?                                    ← chapter: NvtourPanelChapter (link: Title)
src/a.cpp                                               ← NvtourPanelFile
▶ 1. ✗ 412-415  dangling iterator  ↔ arch#cache      ← sheet link (--sheet): NvtourVia
    ↓ the cleanup thread erases the entry               ← --via of step 2: NvtourVia
src/b.cpp
  2. → 88       erase on cleanup thread
src/a.cpp
  3. ✓ 430      the fix

`]w` next · `[w` prev · `[W` first · `]W` last · `<leader>wp` panel · `<leader>wc` clear · `<leader>wk` peek · `<CR>` jump · `q` close

---
<free markdown text set via `nvtour panel`>
```

Steps keep the tour order; a file name line starts each run of steps in the same file (for a ref
step: in the same file at the same commit, shown as `path @ref`). A chapter is a line `## Name`
before its first step (and after an empty line, unless it is the first line); the file name line
starts again after it. The link line (`↓ [from N: ]via`) of the first step of a chapter stays: it
comes after the heading and before the file name line (a heading ends the run of a file, but not the
link from the step before). `<CR>` on the chapter line goes to the first step of the chapter. The winbar
shows `Name · 3/12 ...` for a step in a chapter; the name is shown only when the left part fits with it
(the label is cut after the name is left out). `N. <mark>`
is highlighted with `NvtourSign<Role>`; marks: `✗` fault, `→` flow, `✓` fix, `○` context, `•` info.
A step without a label shows the first line of its note (markers removed, cut to 60 cells); the
quickfix item text uses the same. Before the first jump the progress is `N step(s)`. The footer line
lists only the keys that were installed (the chapter keys: only when there are chapters); the keys are inline code so markdown does not read
`[W first · ]W` as a link. A step with a `--via` or `--from` link gets a line `↓ [from N: ]<via>`
before it, and before its file name line, so a link to another file shows before the file changes.
`<CR>` on a chapter line, a file name line or a link line jumps to the step after it. The panel opens in the tab of the tour
window; a panel window left in another tab is closed first.

Paths shown relative to `W` (passed from Python as `workspace`). The current step line gets an
extmark `line_hl_group = NvtourPanelCurrent`. Buffer-local normal-mode maps in the panel: `<CR>`
jumps to the step under the cursor (in the tour window, not the panel), `K` peeks at that step (§6,
"Peek a step"; the float is anchored at the panel cursor), `q` closes the panel and
sets `panel.user_closed = true`. `panel --toggle` or `panel TEXT` reopen it and reset
`user_closed`.

If the user closes the panel window by other means, `panel.win` becomes invalid; treat it as
closed (do not reopen automatically for steps if `user_closed`).

## 8. Focus

`focus FILE:L1-L2 [L3-L4 ...] [--context N] [--dim]` — show only the given ranges of one file. Each
range (the first one and each extra one) can be a text range (§4, "Ranges by text"); the CLI sends
`{ query = ... }` instead of `{ l1, l2 }` and `H.focus` resolves it on the lines of the buffer, once.

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
- The ranges follow the edits of the user like the steps do: each range has an anchor in `ns_anchor`
  (§6, "Steps follow edits"). After an edit the ranges are read back and the folds are made again;
  a range whose lines were all deleted is dropped.
- Focus state is per buffer. When `step` later shows that buffer in the tour window, fold-mode
  focus is re-applied (folds are window-local and may be gone). Fold the step's own range open
  (`zv` after the jump already does this).
- `unfocus [FILE]` — for each saved window: if it still shows the buffer, `normal! zE` and restore
  the options; if it shows another buffer now, keep the saved options in `pending_folds` and restore
  them on the next `BufWinEnter` of that buffer in that window (another buffer's options are never
  touched). Clear `ns_focus` extmarks, drop the state. FILE is compared with the stored path (no
  `bufnr()` pattern). With no FILE, unfocus all.

**Focus from the steps.** `focus --steps [FILE] [--context N] [--dim]` (`H.focus_steps`) focuses every
toured file, or only FILE, on the ranges of its steps. It groups `state.tour.steps` by buffer (diagram
steps and lost steps are skipped; ref buffers are included, as they are normal buffers with folds) and
calls `focus_buf(buf, file, ranges, ctx, dim, move, from_steps)` once per buffer. `H.focus` calls the same
function once, so there is one implementation of the folds and the dim. `move` is `false`: the view stays.
The result is `{ ok, focus = { per file: file, ref, ranges, mode, deferred } }`; the CLI prints one
`focus: file ranges (mode)` line per entry (`file @REF` for a ref buffer). No step in the tour or in FILE
is exit 6; a range together with `--steps` is exit 2 (in the CLI).
- **Sticky.** `focus_buf` sets `state.focus[buf].from_steps = { context, dim }`. `refocus_steps(buf)`
  reads the ranges of the steps again (`step_ranges`, `merge_ranges`) and, when they changed, makes the
  folds (`refold`) or the dim marks (`draw_dim`) again. It runs from `rerender_all` for every buffer that
  has such a focus (so `step`, `edit`, `remove` and `apply` work with it, and an `edit` that
  moves a step to another file changes both files) and from `sync_fire` when the steps of a buffer
  moved or were lost (a user edit, §6). When no step is left in the buffer, `refocus_steps` calls
  `unfocus_buf`. When the ranges are the same, nothing is done, so the folds that the user opened stay.
- **No anchors of its own.** A plain focus has anchors for its ranges. A focus from the steps has none
  (`anchors = nil`; `sync_focus` and `reanchor_buf` skip it): its ranges come from `step.l1` and `step.l2`,
  which `sync_step` reads from the anchors of the steps. Two sets of anchors would fight about the same
  lines: after an edit the two sets could give different ranges, and the folds would follow the one that
  was read last.
- **Scope.** Only the buffers that were focused stay in sync. A file that gets its first step after
  `focus --steps` is not focused; run the command again (it is safe to repeat: it restores the fold
  options and takes them again). `focus FILE:...` on the buffer replaces the focus (it is no longer
  from the steps); `unfocus`, `start`, `clear` and `reset` end it. `status` shows `from_steps = true`
  and the text `(fold, from steps)` or `(dim, from steps)`. `apply` has no `focus --steps` key.

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

## 10. Diagrams

`diagram NAME [FILE|-] [--format mermaid|dot|easy|text] [--link TEXT=FILE:L1[-L2]]... [--ascii] [--header N] [--split above|below|left|right|full] [--no-show]`

A concept is often easier to see as a picture: a sequence of messages, a graph of components. The
terminal cannot show images (§1), so a diagram is text: Unicode box drawing in a read-only buffer.
A diagram is a document like a file, so tour steps can point to its lines, and `]w` goes from the
diagram to the code and back.

**Python: rendering** (`nvtour/diagram.py`).
- `NAME`: letters, digits, `.`, `_`, `-` (else exit 2). The source is `FILE` or stdin (default `-`).
- Format: `--format`, else from the extension (`.mmd`/`.mermaid` mermaid, `.dot`/`.gv` dot, `.txt`
  text), else mermaid.
- Renderers, found on `PATH` or given as a command (with arguments, `shlex` split) in an environment
  variable: mermaid → `mermaid-ascii -f -` (`$NVTOUR_MERMAID_ASCII`; flowcharts and sequence
  diagrams); dot → `graph-easy --from=dot --as=boxart`, easy (Graph::Easy syntax) →
  `graph-easy --from=txt --as=boxart` (`$NVTOUR_GRAPH_EASY`); text → the source as it is (a diagram
  drawn by hand). `--ascii` asks the renderer for `+-|` lines (`--ascii`, `--as=ascii`). The source
  goes to the renderer on stdin; timeout 30 s.
- Errors: no renderer, or it cannot run → exit 2 with an install hint; a renderer that fails, prints
  nothing, or times out → exit 6 with its stderr.
- The output is cleaned: colour escape codes removed (they would show as raw text in a buffer), tabs
  expanded, trailing blanks and blank first and last lines removed. An empty diagram is exit 6.
- `--link TEXT=FILE:L1[-L2]` (split at the last `=` that leaves a readable range, so TEXT can contain
  `=`): TEXT must occur in the rendered lines and the range must exist in FILE, else exit 6 (the same
  idea as `--expect`). The range can be a text range, `--link 'TEXT=FILE:/text/'`: the CLI resolves it
  with `ranges.resolve_query` on the lines of FILE (read as nvim shows them), and the message of an
  error starts with `--link 'TEXT':`.
- `--header N`: line N is the header line (§10, header line); 0 is none. Without it, a Mermaid
  `sequenceDiagram` (after `%%` comments and `---` front matter) whose first rendered line starts
  with `┌` or `+` and the second with `│` or `|` has header line 2, the participant names; other
  diagrams have none. N outside 0..lines is exit 6.
- Sent to Lua: `name`, `lines`, `links` (`{ text, file, l1, l2 }`), `header`, `split` (nil when not
  given), `show`. The output is
  `diagram NAME: N lines, W columns, K link(s), [header line H, ]shown|not shown`, then every line numbered (`  7| ...`),
  so the agent can pick the lines of a step. `--json` adds `lines`.
- `doctor` reports both renderers (optional checks).

**Lua: the diagram buffer.** One buffer per name, in `state.diagrams[name] = { name, file, buf,
lines, header, links }`, named `nvtour://diagram/NAME`, created like a ref buffer (§6): listed scratch,
`buftype = nofile`, `bufhidden = hide`, no swap file, then `modifiable = false`, `readonly = true`;
`filetype = nvtourdiagram`; `vim.b.nvtour_diagram = NAME` marks it. Colours in `ns_diagram`: runs
of box drawing, block, shape and arrow characters (U+2190–21FF, U+2500–25FF, `∧`, `∨`) get
`NvtourDiagramLine` (blended from `Normal`), so the labels stand out; each occurrence of a link text
gets `NvtourDiagramLink` (fg of `Directory`, underlined). Like a ref buffer, a wiped or unloaded
diagram buffer is made again from `lines` on the next use, and its steps move to the new buffer.

**`diagram` again with the same name** replaces the lines in the same buffer. A step on the diagram
that is beyond the new last line makes it exit 6, and nothing changes. The steps on it are drawn
again.

**The diagram window.** A split **above** the tour window by default (the code stays below it, the
panel on the right), `height = min(lines + 1, 60 % of that window)` (the `+ 1` is the winbar), at
least 3 rows, `winfixheight` on. `--split above|below|left|right` gives the side of the tour window;
left or right, half of the width of that window and no `winfixwidth`: `panel_open` and `panel_close`
call `balance_side_diagram()`, which gives the tour window and the diagram window the same width
again (the panel has `winfixwidth`, so otherwise the code window alone would give or take the columns,
and a fixed-width diagram could leave it 1 column wide). `wrap` is off (a wrapped diagram falls
apart); the options are window-local. `w:nvtour_diagram_split` marks the side; `diagram --split` closes
a diagram window of the current tab on another side and opens a new one. `--split full` puts the diagram in a tab page of its own, after the current tab (`:tab sbuffer` with `switchbuf` empty for the call), so it fills the screen; the focus goes there through `enter_win` (a tab that is not current is not seen), so the user's terminal keeps the focus by default. Its window is found in any tab; a `--split` to a side closes it (and so its tab). Code steps and links still use the tour window, in its own tab, and `enter_win` switches to it. `state.diagram_split`
keeps the last `--split`, so a window that is made later (a jump to a diagram step after the user
closed it) is on the same side; `start` and `clear` forget it. Without `--split`, an existing diagram
window is used where it is. `diagram`
shows it unless `--no-show`; the focus does not move. A window of the current tab that shows a
diagram is reused (it then shows the new one). `tour_win(buf)` gives a diagram buffer this window;
for a file it never chooses a window that shows a diagram (such a window is not a file window), so a
code step goes below the diagram, never into it. `S.tour_win` is set only by code steps.

**Header line.** A buffer cannot keep lines at the top while it scrolls, so the participant names of
a sequence diagram leave the view, and the lifelines below have no names. `diagram` sets the tour
winbar (§6, Winbar) in the diagram window when it shows it, and `M.winbar()` first checks for a header:
when the window shows a diagram with `header > 0` and `topline > header`, the winbar is that line
instead of the tour position. It starts with `textoff` blanks (sign, number and fold columns) and is
cut at `leftcol` and at the text width by display cells (a wide character cut in two becomes
blanks), so each name stays above its lifeline also when the window scrolls sideways. Line
characters are in `NvtourDiagramLine`, as in the buffer. The code window keeps the tour position.
A diagram with a header gets the winbar also when `vim.g.nvtour_winbar` is false; the winbar is then
empty until the header line scrolls out. Scrolling a window that is not current (the mouse) does
not redraw its winbar, so `WinScrolled` redraws the winbar of each scrolled diagram window that has
a header (`nvim__redraw`, else `redrawstatus!`). One line only: a float with the full box would
cover diagram lines, and needs its own lifetime.

**Steps on a diagram.** `step --diagram NAME L1[-L2]` or `/TEXT/...` (the text is searched in the
rendered lines; `--ref` with it is exit 2; an unknown name is
exit 6). The CLI sends `diagram = NAME` and `file = nvtour://diagram/NAME`. Everything else is as
on a file: range, note, roles, `--expect` (exit 6 names `diagram NAME:L`), `--via`, links, panel,
quickfix (`bufnr`), winbar, jump and scroll. Where a step shows its file, a diagram step shows
`diagram NAME` (`doc_path`): the panel file name line, the `◇` line, the "next" line and the winbar
(` nvtour 2/5 flow · diagram NAME · <label>`). `edit N L1[-L2] --diagram NAME` moves a step onto a
diagram; `edit N FILE:L1[-L2]` moves it back. `step`, `status` print `diagram NAME:L1-L2`; `--json`
adds `diagram`. `status` also lists the diagrams (`diagram: NAME (N lines, K link(s))`).

**Links.** `<CR>` in a diagram buffer (buffer-local map, through `_G.nvtour.dispatch("diagram_link")`
so it survives an upgrade): on a link text, show FILE in the tour window (after `normal! m'`, so
`<C-o>` goes back), make it the current window, cursor on `L1`, `zvzz`, and flash `L1..L2`. Not on a
link: the usual `<CR>` (`normal! +`). A link whose file is now shorter is a warning.

**Lifetime.** `clear` and `start` delete the diagram buffers (every `nvtour://` buffer), which also
closes their windows, and empty `state.diagrams`. So `diagram` comes after `start`.

## 11. Keymaps and user commands

The keys of the tour (`KEYS.tour`) are installed by `start`/first `step` and removed by `clear`. The keys of
the marks (`KEYS.always`) are installed when the runtime loads and stay (§5, "Marks"); they are also set in
visual mode, and are free only when `maparg` is empty in both modes. Global normal-mode maps, each set
**only if `vim.fn.maparg(lhs, "n") == ""`**; otherwise `vim.notify` a warning once, return it in `warnings`,
and skip that key. `vim.g.nvtour_keys` is read (missing entries use the defaults), never written.

The actions are in two sets. `KEYS.tour` is `next`, `prev`, `first`, `last`, `panel`, `clear`, `next_chapter`,
`prev_chapter`, `peek`, `view`, `card` and `range_style`. `KEYS.always` is `mark` and `mark_note`.

```lua
DEFAULT_KEYS = {
  next = "]w", prev = "[w", first = "[W", last = "]W", panel = "<leader>wp", clear = "<leader>wc",
  next_chapter = "]c", prev_chapter = "[c", peek = "<leader>wk",
  view = "<leader>wv", card = "<leader>ws", range_style = "<leader>wr",
  mark = "<leader>wm", mark_note = "<leader>wM",
}
```

`mark` adds a mark on the cursor line or the selection; `mark_note` does the same and asks for a comment.

`peek` shows the full content of the step under the cursor in a float (§6, "Peek a step"). While the float
is open, `<Esc>` and `q` are mapped (buffer-local) to close it; they are removed on close.

`]c` and `[c` are keys of Vim too (next and previous change). They are free when `maparg` is empty, which a
built-in key is, so nvtour takes them; in a window in diff mode they run the built-in key
(`normal! ]c`), so `nvtour diff` tabs keep working.

User commands (defined on load, always available): `:NvtourNext`, `:NvtourPrev`, `:NvtourFirst`,
`:NvtourLast`, `:NvtourNextChapter`, `:NvtourPrevChapter`, `:NvtourGoto N`, `:NvtourPanel`, `:NvtourClear`,
`:NvtourPeek [N]` (peeks step N, default the step under the cursor; see "Peek a step"), `:NvtourFollow`
(flips `vim.g.nvtour_follow_cursor` and says the new state; see "Follow the cursor"), `:NvtourMarksClear`
(removes all marks and says how many; see §5, "Marks"), `:NvtourView [inline|side|toggle]`, `:NvtourCard`
and `:NvtourRangeStyle [STYLE]` (see §6, "Side view").

The quickfix list also works (`:cnext`, `:cprev`, `:copen`); it moves the cursor only, not the
current step.

## 12. Safety rules (enforce in code and tests)

1. Never call `nvim_buf_set_lines`/`nvim_buf_set_text` on a buffer that is not an `nvtour://`
   scratch buffer. Never `:w`, `:edit!`, `:bd!`, `:qa`.
2. Never change global options except `hidden = true`. Window-local fold options and the tour
   `winbar` are set locally, saved and restored per window (the panel's own window options are not
   restored: nvtour closes that window). Highlight groups use `default = true`.
3a. Never replace a modified buffer that cannot be hidden (that would run `'autowrite'`).
3. Never close windows/tabs we did not create (panel window, diff tabs and the peek float only).
4. Never touch an nvim instance the user did not select (no broadcasting).
5. Every command has one timeout budget for all its RPC calls; a hang never blocks the agent for
   more than `--timeout` seconds (plus the 2 s discovery probes, which run in parallel). A command
   is not sent to an nvim that waits for input.
6. Tests never connect to sockets under the real `$XDG_RUNTIME_DIR`; they set `XDG_RUNTIME_DIR` to
   a temp dir and start their own `nvim --headless --clean` there (nvim then creates its socket in
   that dir, so discovery is exercised end to end).
7. The only other program that nvtour runs is `sheet goto -q --no-autostart SHEET STEP`, and only
   for a step with a sheet link (§6, "Sheet link"): detached, without waiting, with `--no-autostart`
   so nvtour never starts a viewer. No other `sheet` command, no shell. Tests use a fake `sheet`
   script (`vim.g.nvtour_sheet_cmd`) that records its arguments.

## 13. Packaging, install, skill

```
~/projects/nvtour/
  pyproject.toml          # name nvtour, console_scripts nvtour = nvtour.cli:main, no dependencies
  README.md               # usage, keys, example session, init.lua options
  DESIGN.md
  nvtour/__init__.py      # __version__
  nvtour/cli.py           # argparse, subcommands, output
  nvtour/discover.py      # socket scan, probe, score, pin cache
  nvtour/client.py        # connect with timeout, ensure_lua_loaded, call(cmd, args)
  nvtour/rpc.py           # msgpack codec and msgpack-RPC connection (stdlib)
  nvtour/ranges.py        # FILE:L1-L2 and FILE:/TEXT/ parsing, resolve_query, path resolution
  nvtour/gitutil.py       # toplevel, git show
  nvtour/diagram.py       # diagram renderers, output cleaning, --link targets
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
description: Give a visual, read-only walkthrough of code inside the user's running Neovim while explaining a bug or a concept in chat — jump, highlight, annotate with virtual-text notes, fold to the relevant parts, show read-only diffs and text diagrams (Mermaid/Graphviz) linked to the code, apply a whole tour from a TOML file, keep a step panel, read the places the user marked. Use when the user explicitly asks to explain, walk through, show, or visualize something "in nvim" / "in the editor". Also use it, without asking first, when the user asks about code ("this chunk", "this function", "here", "what does this do"), gives no file, line or pasted code, and "this" does not point to something earlier in the chat: read their nvim cursor or selection to find the location and answer in chat. Never edits files.
---
```

Body (concise, imperative): see `skill/SKILL.md`. It covers point and ask (`attach` and `where`
when no location is given, the previous-selection caveat, the `@REF`, `modified` and no-file
flags, an answer in chat and a tour only on an explicit request in nvim), the workflow (attach,
start, one step per point or, for 3 steps or more, the whole tour in one `apply` call with a TOML example, panel
summary; only the first step moves the view), how to get line numbers right (`FILE:/text/` first, the echoed
line, `--expect`), fixing a tour (`edit`, `remove`, `--at`, `status`, steps that moved or were lost after edits,
the questions that the user asks during a tour), chapters, marks (`marks --wait --clear`), note style and roles,
links, sheets, focus (`focus --steps`) and its manual-fold caveat, old code, suggestions, diagrams, diffs, and what to
do on each exit code.

## 14. Future

- Images: render a PNG (graphviz/mermaid) and show it with `snacks.image` when the terminal
  supports the kitty graphics protocol. Not for alacritty.
- MCP stdio wrapper exposing the same commands as tools.
- tmux: focus the pane running the selected nvim.
- `nvtour save`: write the tour as a tour file (§15) from `status --json`.

## 15. Tour file format (`apply`)

`nvtour apply [FILE|-] [--append] [--check] [--no-jump]` makes a whole tour from one file, in one RPC
(`apply`). nvtour only reads the format; it never writes it (a later `save` command could write the
same keys from `status --json`).

```toml
title = "Dangling iterator in cleanup"
sheet = "arch"                       # optional, like start --sheet
panel = """
**Cause**: use after erase. **Fix**: copy the value before `erase()`.
"""

[[focus]]                            # optional, like nvtour focus
file = "src/a.cpp"
ranges = ["400-440", "/evict/../}/"]
dim = false
context = 2

[[diagram]]                          # optional, like nvtour diagram
name = "read"
format = "mermaid"                   # or file = "docs/read.mmd"
source = """
sequenceDiagram
  C->>K: get
"""
links = ["get=src/cache.cpp:/Cache::get/+8"]
split = "above"

[[step]]
at = "src/a.cpp:/it->second/"        # FILE:L1[-L2] or any text range
role = "fault"
label = "dangling iterator"
note = "The iterator is used after `erase()` invalidated it."
expect = ["it->second"]              # list or one string
suggest = """
        it->second.expires_at += ttl;
"""                                  # string with new lines, or a list of lines
sheet = "#evict"                     # SHEET#STEP or #STEP
chapter = "The bug"                  # starts a chapter at this step

[[step]]
at = "src/a.cpp:/it->second/"
ref = "origin/master"                # optional: the file at a git ref (not with diagram)
role = "context"
label = "the old code"

[[step]]
at = "2"                             # with diagram: L1[-L2] or a text range of the diagram
diagram = "read"                     # optional: a step on a diagram (not with ref)
label = "the read"
via = "the cleanup thread runs `evict()`"
from = 1                             # optional: the link comes from step 1 (an earlier step)
```

### Schema

| table | keys (type) | required |
|---|---|---|
| top level | `title`, `sheet`, `panel` (strings); the tables `focus`, `diagram`, `step` | none |
| `[[focus]]` | `file` (string), `ranges` (list of `L1[-L2]` or text ranges `/TEXT/...`), `dim` (bool), `context` (int ≥ 0, default 2) | `file`, `ranges` |
| `[[diagram]]` | `name`, `format` (`mermaid`, `dot`, `easy`, `text`), `source` (string) or `file` (path), `links` (list of `TEXT=FILE:L1[-L2]` or `TEXT=FILE:/TEXT/`), `split` (`above`, `below`, `left`, `right`, `full`), `ascii` (bool), `header` (int ≥ 0), `show` (bool, default true) | `name`, and exactly one of `source` and `file` |
| `[[step]]` | `at`, `ref`, `diagram`, `role`, `label`, `note`, `expect` (string or list), `via`, `from` (int ≥ 1), `suggest` (string or list of lines), `sheet`, `chapter` | `at` |

The keys have the meaning of the options of the commands of the same names (`step`, `focus`,
`diagram`, `start`): `at` is the location argument of `step` (`FILE:L1[-L2]` or `FILE:/TEXT/...`; with
`diagram`, `L1[-L2]` or `/TEXT/...`, as `step --diagram NAME`), `show = false` is `--no-show`. A chapter
is a key of the step: there is no `[[chapter]]` table. The steps are added in file order (`at` is a
location, never an insert position). A diagram `file` is read from the cwd of the CLI, with the format
guessed from its extension as for `nvtour diagram`; with `source` the default format is mermaid.
A JSON `null` value is the same as no key. The format is JSON when the first
character that is not white space is `{` (the same keys, arrays for the tables), else TOML (`tomllib`).
`sheet` of the top level is checked as `start --sheet` is; a step `sheet` is `SHEET#STEP` or `#STEP`.
An empty file is a tour with no step.

### Python phase (`cli.py`)

- `parse_tour(text) -> dict` is pure (no file, no git, no nvim). It checks the tables and the types and
  returns the values to use (`expect` is a list, `suggest` one string, a key that is absent stays
  absent). An unknown key, a wrong type, a missing required key or `source` and `file` together is exit 2.
  The message names the entry with a number from 1 in file order: `step[3]: unknown key "lable" (did you
  mean "label"?)` (`difflib.get_close_matches`), `step[1]: "from" must be 1 or more`, `focus[1]: missing
  the key "ranges"`; the top level is `tour:`. A bad TOML or JSON text is `tour file: bad TOML: ...`.
- `apply_request(args, base)` then builds the Lua args with the helpers of the commands. Each entry runs
  in `scoped("step[3]", ...)`, so any error of the helpers (a bad range, an unknown git ref, a
  renderer that fails, a bad sheet link) carries the prefix and keeps its exit code. `location(spec,
  ref, diagram, blobs)` is used per step; with `ref`, git is read once per `(file, ref)` (the `blobs`
  cache), and the lines of a blob go with the first step on a given `(commit, file)` only (the Lua side
  keeps them for the other steps). `diagram_args` is the code of the `diagram` command. `--append` with
  `title` or `sheet` in the file is exit 2. The args are `{ title, sheet, sheet_env, panel, focus,
  diagrams, steps, append, check, no_jump }`; a nested table never has an absent key (nested nils do not
  survive msgpack), so the entries only have the keys that the file had.

### Lua phase (`H.apply`)

1. **Check** (no change to the tour). For each diagram: not empty, the split, and (with `append`) no step
   on a diagram of the same name beyond the new lines. For each focus: the ranges (`focus_ranges`, the same
   code as `focus`). For each step: the role, then `checked_range` (the same code as `step`: it loads the
   file, resolves a text range, checks the range and each `expect`). A step on a git blob is checked on
   the lines that came with it, and a step on a diagram on the lines of the diagram of the file (or, with
   `append`, of a diagram of the tour); neither makes a buffer. A diagram step with no such diagram is exit
   6. `from` must be a step before this one: `1 .. i-1` for entry `i`; with `append` the numbers are the
   ones of the final tour (the `E` steps that are there are `1 .. E`, entry `i` is step `E + i`). The sheet
   link is made by `resolve_sheet` against the tour that will exist (the sheet of the file, not the one
   of the old tour; with `append`, the tour that is there). A chapter name is cut to one line like
   `chapter NAME`, and an empty one is exit 2. A failure is `scoped("step[i]: ", ...)`: the exit code of
   the failure with the entry in front. Buffers that were loaded stay loaded, as after a failed `step`.
2. **Check mode.** With `check` the call ends here and returns the resolved steps (`n`, `total`, `file`,
   `l1`, `l2`, `role`, `label`, `via`, `from`, `ref`, `sha`, `diagram`, `sheet`, `chapter`, `resolved_from`
   and the first line `text`). With `append`, `n` and `total` count the steps that are there.
3. **Build.** Unless `append`: `reset(true)` (as `start`), the title, the sheet and its variables. Then
   `ensure_started`, the diagrams (`H.diagram`), the focus (`do_focus(f, false)`: the view stays and the
   folds are applied when the file is shown), the panel text and, on the first step of a tour, the panel
   window (before the notes are drawn, as `step` does). Each step goes through `make_step` and
   `add_step`, the code that `H.step` uses after its checks, with `render = false`; a chapter of the
   entry is set with `set_chapter` (a pending chapter takes the first new step, and the chapter of the
   file replaces its name). Then once: `rerender_all`, and `step_goto(first new step)` (which updates the
   quickfix list and the panel) unless `no_jump`, else `update_qf` and `render_panel`. A file with no step
   does not jump.
4. **Result.** `{ ok, steps = [step results, without keys], total, panel_lines, chapters, jumped_to,
   current, keys }`; with `check`, `{ ok, check = true, steps, total }`.

`--append` jumps to the first new step (unless `--no-jump`), so that the user sees what was added; `step`
alone jumps only for the first step of a tour.

### Output

One block per step as `step` prints it (`step N/T: file:L1-L2 [role] label` and the first line,
`  L1| text`), then `panel: N lines`, `chapters: N` and `jumped to step N` (`N` is 1 for a new tour; the
line is left out with `--no-jump`). With `--check` each step line starts with `ok ` and the output ends
with `check: N steps ok`. `--json` has the same data as fields (above).

# nvtour

`nvtour` lets an AI agent (Claude Code, Codex, ...) give a visual, read-only walkthrough of code inside
your already-running Neovim. The agent explains a bug or a concept in chat and drives the editor in
parallel: jump to a range, highlight it, attach a note as virtual text, fold away irrelevant code, show a
read-only diff, and keep a side panel with the step list. You step through with keys.

It never writes to a file buffer or to a file you edit. The only files it writes are its own pin cache
(`$XDG_RUNTIME_DIR/nvtour/`) and, with `instances --prune`, it removes stale nvim sockets. Plugins that react
to window or buffer switches (auto-save, session savers) are outside this promise. See `DESIGN.md` for the full
design.

## Install

```
pip install --user --break-system-packages -e ~/projects/nvtour
ln -s ~/projects/nvtour/skill ~/.claude/skills/nvtour
ln -s ~/projects/nvtour/skill ~/.codex/skills/nvtour
```

Needs Python >= 3.11, `pynvim`, Neovim >= 0.10. No nvim plugin is needed: the Lua runtime is sent over RPC
the first time a command runs (and again when its version changes). Run `nvtour doctor` to check the setup.

## Example agent session: a 3-step bug walkthrough

```
nvtour attach
nvtour start "Dangling iterator in cleanup"
nvtour step src/a.cpp:412-415 --role fault --label "dangling iterator" --expect "it->second" \
    --note "The iterator is used after erase() invalidated it."
nvtour step src/b.cpp:88 --role flow --label "erase on cleanup thread" --note - <<'EOF'
The cleanup thread erases the entry while the reader still holds `it`.
EOF
nvtour step src/a.cpp:430 --role fix --label "copy before erase"
nvtour panel - <<'EOF'
**Cause**: use after erase. **Fix**: copy the value before `erase()`.
EOF
```

Only the first step jumps; the others are added without moving the view, so the finished tour is at step 1.
`step --jump` forces a jump, `--no-jump` suppresses it for the first step too. Each step prints the first
highlighted line, and `--expect TEXT` fails with exit 6 when `TEXT` is not in the range.

Commands:

| command | what it does |
|---|---|
| `attach [PID\|SOCKET]`, `attach --clear` | select and pin an instance |
| `instances [--prune]` | list instances (`live`, `stale`, `unresponsive`, `blocked`) |
| `where` | cursor, live or previous selection (exact text), visible range of the user's file window |
| `start [TITLE]` | start a new tour (keeps an open panel) |
| `step FILE:L1[-L2] [--note TEXT\|-] [--label] [--role] [--expect TEXT] [--at N] [--jump\|--no-jump]` | add a step |
| `edit N [FILE:L1[-L2]] [--note] [--label] [--role] [--expect] [--jump]` | change a step (`''` removes a label or note) |
| `remove N` | remove a step |
| `goto N`, `next`, `prev`, `first`, `last` | navigate |
| `status` | the tour, focus, panel, diff tabs and installed keys |
| `focus FILE:L1-L2 [L3-L4 ...] [--context N] [--dim]`, `unfocus [FILE]` | fold or dim the rest of a file |
| `diff FILE (--ref REF\|--file PATH\|--stdin) [--title]`, `diff-close` | read-only diff tab |
| `panel [TEXT\|-\|--file PATH] [--toggle] [--clear]` | side panel text |
| `clear [--keep-buffers]` | remove everything nvtour created |
| `doctor` | check the setup |

Ranges: `FILE:L1`, `FILE:L1-L2`, `FILE:L1,L2`, and `FILE:L:COL` (the column is ignored).

## Keys and options

Default keys (set only if free; otherwise a warning is shown once and the key is skipped):

| key | action |
|---|---|
| `]w` | next step |
| `[w` | previous step |
| `[W` | first step |
| `]W` | last step |
| `<leader>wp` | toggle panel |
| `<leader>wc` | clear everything |

The panel shows the installed keys in a footer line. In the panel: `<CR>` jumps to the step under the cursor,
`q` closes the panel. Commands: `:NvtourNext`, `:NvtourPrev`, `:NvtourFirst`, `:NvtourLast`, `:NvtourGoto N`,
`:NvtourPanel`, `:NvtourClear`. The quickfix list (`nvtour: <title>`) works too (`:cnext`, `:cprev`), but it
only moves the cursor; it does not change the current step of the tour.

Options for `init.lua` (set before the first step):

```lua
vim.g.nvtour_keys = { next = "]w", prev = "[w", first = "[W", last = "]W", panel = "<leader>wp", clear = "<leader>wc" }
vim.g.nvtour_auto_panel = false -- do not open the panel on the first step
vim.g.nvtour_steal_focus = "unless_terminal" -- "always" | "never"; default keeps the focus in a terminal window
```

Files are shown in a normal file window, never in a terminal, quickfix, help or other special window. When you
type in a terminal window (for example a Claude Code split), the tour moves the file window but the terminal
keeps the focus.

Highlight groups. Each role takes its accent colour from the colorscheme's `DiagnosticError` (fault),
`DiagnosticInfo` (flow), `DiagnosticOk` (fix), `Comment` (context) or `DiagnosticHint` (info) and tints
the line background by blending that accent into the `Normal` background, so syntax highlighting stays
intact. Groups: `NvtourLine{Fault,Flow,Fix,Context}` (line tint), `NvtourNumber{...}` (line numbers of
the range), `NvtourSign{...}` (step number in the sign column), `NvtourLabel{...}` (end-of-line label),
`NvtourNote`, `NvtourNoteBorder`, `NvtourDim`, `NvtourPanelCurrent`. They are defined with
`default = true`, so a plain `vim.api.nvim_set_hl(0, "NvtourLineFault", {...})` in `init.lua` wins on
first load. To survive colorscheme changes and runtime upgrades, set overrides in an autocmd:

```lua
vim.api.nvim_create_autocmd("User", {
  pattern = "NvtourHighlights",
  callback = function() vim.api.nvim_set_hl(0, "NvtourLineFault", { bg = "#3c1f1e" }) end,
})
```

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 2 | usage error |
| 3 | no unique matching nvim (table of live instances on stderr) |
| 4 | no live nvim at all, or the `--socket` path does not exist |
| 5 | RPC failure or timeout; `waiting for input` means nvim is at a prompt and nothing ran |
| 6 | bad file, range or `--expect` |

With `--json`, errors are printed on stdout too: `{"ok": false, "code": N, "error": "..."}`. Warnings (for
example a key that is already mapped) are in the `warnings` list, or on stderr as `warning: ...`.

## How discovery picks an instance

Socket choice precedence: `--socket PATH`, `$NVTOUR_SOCKET`, `$NVIM`, the pinned socket for the workspace
(if alive), then discovery. Discovery scans `$XDG_RUNTIME_DIR/nvim.*.0`, `/tmp/nvim.$USER/*/nvim.*.0`,
`/tmp/nvim.*.0` (probed in parallel); sockets whose pid is gone or belongs to another program are `stale`,
sockets that fail a 2 s probe are `unresponsive`, and an nvim at a prompt is `blocked`.
The workspace `W` is `--workspace`, else the git toplevel of the cwd, else the cwd.

| condition | score |
|---|---|
| nvim cwd == `W` | 100 |
| `W` is an ancestor of nvim cwd | 80 |
| nvim cwd is an ancestor of `W` | 60 |
| otherwise, a listed buffer is under `W` | 40 |
| none | 0 |

The unique highest score > 0 wins. If only one nvim runs, it is used even with score 0 (with a note on
stderr). The choice is pinned in `$XDG_RUNTIME_DIR/nvtour/<sha256(W)[:16]>` (fallback `~/.cache/nvtour/`).
Ties, or several instances that all score 0, give exit 3. `nvtour attach [PID|SOCKET]` pins explicitly,
`nvtour attach --clear` removes the pin.

A pin stores the socket and, when it is visible in `/proc`, the pid. A socket with a custom name
(`nvim --listen PATH`) or one that belongs to an nvim in another pid namespace is checked by existence only. So
an agent in a container can use a host nvim whose socket is mounted into the container:
`nvtour attach /mounted/nvim.sock` (or `$NVTOUR_SOCKET`). Note that access to the socket gives full control
of that nvim.

## Choices where the design was open

- `NVTOUR_RUNTIME_ONLY=1` skips the `/tmp` scan patterns (used by the tests so they cannot see real instances).
- A `$NVIM` or `$NVTOUR_SOCKET` value that points to a missing file is ignored (stale env), not an error.
  `--socket` to a missing path is exit 4.
- Global flags (`--json`, `--socket`, ...) are also accepted after the subcommand.
- Quickfix item text is `[role] label`. `clear` empties the nvtour quickfix list (retitled `nvtour (cleared)`)
  because a single list cannot be deleted without dropping the user's others; if it is the current list, the
  previous list becomes current again. The next tour reuses the list while it is still the current one.
- `clear` unlists the buffers nvtour added to the buffer list (not shown and not modified); `--keep-buffers`
  keeps them listed. Buffers are never unloaded or wiped.
- The panel opens automatically only on the first step of a tour, before the first note is drawn. `start`
  keeps an open panel but clears its text.
- `focus` before the first step shows the file; during a tour it does not move the view, and the folds are
  applied when the file is shown. Fold mode runs `zE` in the window (existing manual folds there are lost) and
  on `unfocus`; fold options are set with `:setlocal` and restored per window, and only on the focused buffer.
- A modified buffer that cannot be hidden (`'bufhidden'` is `unload`, `delete` or `wipe`, or `'hidden'` is
  off) is never replaced in its window: nvtour opens a split instead and warns, so `'autowrite'` cannot write it.
- A file that has a swap file (open in another nvim) is shown anyway, with a warning.
- A single-line step prints as `file:L`, a range as `file:L1-L2`. `focus` prints the context-expanded merged ranges.
- Lua errors carry an exit code (`code` in the JSON result) so range errors found in nvim map to exit 6.
- `where` reports the user's file window: when the current window is a terminal, the panel or another special
  window, it uses the previous window. It uses the live visual selection when in visual mode (`live`), else
  the `'<`/`'>` marks (`previous`); characterwise and blockwise selections give the exact text.
- Before each command the CLI calls `nvim_get_mode()`; when nvim waits for input (a prompt), the command is
  refused with exit 5 instead of being queued until the user answers.
- The Lua runtime sets `vim.o.hidden = true` (the only global option changed). It is what lets nvtour show
  another file in a window whose buffer has unsaved changes.
- The runtime version is a hash of the Lua source, so any change to it is loaded into nvim on the next command.

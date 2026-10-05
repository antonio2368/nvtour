# nvtour

`nvtour` lets an AI agent (Claude Code, Codex, ...) give a visual, read-only walkthrough of code inside
your already-running Neovim. The agent explains a bug or a concept in chat and drives the editor in
parallel: jump to a range, highlight it, attach a note as virtual text, fold away irrelevant code, show a
read-only diff, and keep a side panel with the step list. You step through with keys.

It never writes to a file buffer or to disk. See `DESIGN.md` for the full design.

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
nvtour step src/a.cpp:412-415 --role fault --label "dangling iterator" \
    --note "The iterator is used after erase() invalidated it."
nvtour step src/b.cpp:88 --role flow --label "erase on cleanup thread" --note - <<'EOF'
The cleanup thread erases the entry while the reader still holds `it`.
EOF
nvtour step src/a.cpp:430 --role fix --label "copy before erase"
nvtour panel - <<'EOF'
**Cause**: use after erase. **Fix**: copy the value before `erase()`.
EOF
```

## Keys and options

Default keys (set only if free; otherwise a warning is shown once and the key is skipped):

| key | action |
|---|---|
| `]w` | next step |
| `[w` | previous step |
| `<leader>wp` | toggle panel |
| `<leader>wc` | clear everything |

In the panel: `<CR>` jumps to the step under the cursor, `q` closes the panel.
Commands: `:NvtourNext`, `:NvtourPrev`, `:NvtourGoto N`, `:NvtourPanel`, `:NvtourClear`. The quickfix list
(`nvtour: <title>`) works too (`:cnext`, `:cprev`).

Options for `init.lua` (set before the first step):

```lua
vim.g.nvtour_keys = { next = "]w", prev = "[w", panel = "<leader>wp", clear = "<leader>wc" }
vim.g.nvtour_auto_panel = false -- do not open the panel on the first step
```

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
| 4 | no live nvim at all |
| 5 | RPC failure or timeout (nvim may be waiting at a prompt; press `<Enter>`) |
| 6 | bad file or range |

## How discovery picks an instance

Socket choice precedence: `--socket PATH`, `$NVTOUR_SOCKET`, `$NVIM`, the pinned socket for the workspace
(if alive), then discovery. Discovery scans `$XDG_RUNTIME_DIR/nvim.*.0`, `/tmp/nvim.$USER/*/nvim.*.0`,
`/tmp/nvim.*.0`; sockets whose pid is gone are `stale`, sockets that fail a 2 s probe are `unresponsive`.
The workspace `W` is `--workspace`, else the git toplevel of the cwd, else the cwd.

| condition | score |
|---|---|
| nvim cwd == `W` | 100 |
| `W` is an ancestor of nvim cwd | 80 |
| nvim cwd is an ancestor of `W` | 60 |
| otherwise, a listed buffer is under `W` | 40 |
| none | 0 |

The unique highest score > 0 wins and is pinned in `$XDG_RUNTIME_DIR/nvtour/<sha256(W)[:16]>` (fallback
`~/.cache/nvtour/`). Ties or all zero give exit 3. `nvtour attach [PID|SOCKET]` pins explicitly,
`nvtour attach --clear` removes the pin.

## Choices where the design was open

- `NVTOUR_RUNTIME_ONLY=1` skips the `/tmp` scan patterns (used by the tests so they cannot see real instances).
- A `$NVIM` or `$NVTOUR_SOCKET` value that points to a missing file is ignored (stale env), not an error.
  `--socket` to a missing path is exit 5.
- Global flags (`--json`, `--socket`, ...) are also accepted after the subcommand.
- Quickfix item `type` is the first letter of the role. `clear` empties the nvtour quickfix list
  (retitled `nvtour (cleared)`) because a single list cannot be deleted without dropping the user's others.
- The panel opens automatically only on the first step of a tour. `start` keeps an open panel but clears its text.
- `focus` always runs `zE` in the tour window (existing manual folds there are lost) and on `unfocus`.
- A single-line step prints as `file:L`, a range as `file:L1-L2`. `focus` prints the context-expanded merged ranges.
- Lua errors carry an exit code (`code` in the JSON result) so range errors found in nvim map to exit 6.
- `where` uses the live visual selection when in visual mode, else the `'<`/`'>` marks.
- The Lua runtime sets `vim.o.hidden = true` (the only global option changed).

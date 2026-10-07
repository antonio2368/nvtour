---
name: nvtour
description: Give a visual, read-only walkthrough of code inside the user's running Neovim while explaining a bug or a concept in chat — jump, highlight, annotate with virtual-text notes, fold to the relevant parts, show read-only diffs, keep a step panel. Use when the user explicitly asks to explain, walk through, show, or visualize something "in nvim" / "in the editor". Also use it, without asking first, when the user asks about code ("this chunk", "this function", "here", "what does this do"), gives no file, line or pasted code, and "this" does not point to something earlier in the chat: read their nvim cursor or selection to find the location and answer in chat. Never edits files.
---

# nvtour

`nvtour` drives the user's already-running Neovim from the shell. It never edits files and never starts nvim.

## Point and ask (no location given)

Use this when the user asks about code ("this", "here", "this chunk") but gives no file, line or pasted code. Do not use it when "this" points to something earlier in the chat (a snippet, a command, an output).

1. Run `nvtour attach`, then `nvtour where` to read their cursor or selection. Do not ask the user for the location first.
   - Exit 3 or 4: relay the stderr message to the user verbatim and stop. Do not start nvim and do not guess a location.
   - A live selection is the code the user means.
   - A selection marked `previous, may be old` is the last visual selection, not a live one. Use it if the user said they selected something, or if they point to a range ("this chunk", "these lines") and the cursor is in or near it. Otherwise use the cursor line and the code around it.
2. Read the code that the user sees, not only the file on disk. Check the flags on the first line of `where`:
   - `@REF` (`at git ref ...`): the buffer shows the file at a git ref. Read `git show REF:PATH`, not the working tree.
   - `modified (differs from disk)`: the file on disk is not what the user sees. Use the selection text that `where` prints. If there is no selection, tell the user that the line on disk can be different from their buffer, or ask them to save.
   - `[No Name]` or a `buftype` (for example `terminal`): there is no file to read. Use the selection text, or ask the user where the code is.
3. Answer the question in chat. Read what the code calls as needed. Do not create a tour.
4. After the answer, offer a walkthrough in nvim only if it would help (for example the answer goes across several files or functions). Ask in one short line. Do not offer it for a short, local answer.
5. Create a tour only if the user explicitly asks for it in nvim or the editor ("show me in nvim", "walk me through it in the editor"), or says yes to your offer. Then use the Workflow below and skip its step 1 (you already attached).

## Workflow

1. Run `nvtour attach` first.
   - Exit 3 or 4: relay the stderr message to the user verbatim and stop. Do not start nvim yourself.
2. Run `nvtour start "<title>"`.
3. Interleave with your chat explanation: one `nvtour step FILE:L1[-L2] ...` per point.
   - Use 3 to 8 steps. One idea per step.
   - Follow reading order: cause, propagation, effect (or entry, core, exit).
   - Only the first step moves the user's view. Later steps are added without a jump, so the tour opens at step 1.
   - When a step is in a different file from the step before it, give `--via TEXT`: why the tour goes there (see "Links").
4. End with `nvtour panel -` holding a short markdown summary (heredoc on stdin).
5. In chat, only say that the tour is open in nvim. Do not list the steps again and do not list the keys. Then continue with the rest of the reply (for example options or questions that are still open).

## Line numbers

- Get line numbers from `rg -n` or `sed -n 'A,Bp'` output, not from memory.
- Each `step` prints the first highlighted line (`  412| ...`). Check it.
- Add `--expect TEXT` (a substring of the range) to make a wrong range fail with exit 6 instead of highlighting the wrong code. The user sees `TEXT` underlined, so pick the exact expression the note is about (for example `it->second`).
- `FILE:L:COL` (compiler and `rg --column` output) is accepted; the column is ignored.

## Fixing a tour

- `nvtour edit N [FILE:L1-L2] [--note ...] [--label ...] [--role ...]` changes step N. `--label ''` removes the label.
- `nvtour remove N` removes step N. `nvtour step ... --at N` inserts at position N.
- `nvtour status` shows the tour, focus, panel and keys. Use it after an error or when you lost track. Do not `clear` and start again.

## Writing notes

- Note: at most 3 short sentences. Label: at most 6 words.
- Roles: `fault` = wrong line(s); `flow` = how data or control gets there; `fix` = where or how it should change; `context` = background; `info` = neutral explanation (default).
- Name identifiers in the note. The highlight already shows the location.
- Put identifiers in backticks (`` `erase()` ``); `**bold**` also works. Other markdown is shown as typed.
- Only the current step shows its full note; the others show the first line. Make the first sentence of a note stand alone.
- Text that starts with `-`: write `--note=TEXT`, `--label=TEXT` or `--via=TEXT`.
- Multi-line notes:

  ```
  nvtour step f.cpp:10-12 --role fault --note - <<'EOF'
  First sentence.
  Second sentence.
  EOF
  ```

## Links

- `--via TEXT` tells why the tour goes from the step before to this step, for example ``--via 'on a miss `get()` calls `evict()`'``. nvim shows it above the note of the step (`← from 2 · a.cpp:412: ...`), below the range of the step before (`→ next 3 · ...`) and in the panel.
- Give `--via` on each step in another file. Give it also in the same file when the connection is not clear from the code (a call, a callback, a shared variable, the same lock).
- Write the connection, not the content of the new step: what leads there (a call, a return, data that goes there, a thread). The note tells what the code does. At most one short sentence; put identifiers in backticks.
- `--from N` makes the link come from step N, not from the step before (for example back to step 1). Use it only when the tour goes back to an earlier point.
- Do not write the file name, the line or the git ref in a note or `--via`. nvim shows them on each change of file or version.
- `nvtour edit N --via TEXT` changes a link; `--via ''` removes it; `--from 0` links from the step before again.

## Focus

- Concept spans a long file: run `nvtour focus FILE:a-b c-d` once per file.
- During a tour, focus does not move the view; the folds appear when that file is shown.
- Fold mode removes the user's own manual folds in that window. Use `--dim` if the user has manual folds or the surrounding code matters. `nvtour unfocus` undoes it.

## Old code

- `nvtour step FILE:L1-L2 --ref GITREF` shows the range as it is at a git ref, in a read-only buffer. Everything else works as on a normal step.
- Use it for code that a change removed or moved, and for before/after: a step on the old range (`--ref origin/master`), then a step on the new range (no `--ref`).
- Get line numbers of the old version from `git show GITREF:PATH | sed -n 'A,Bp'` (or `| rg -n`), not from the working tree.
- The file does not have to exist in the working tree.
- `nvtour edit N FILE:L1-L2` without `--ref` moves the step to the working tree. Add `--ref` to keep it on the old version.

## Diff

- Before/after a fix that is not applied: `nvtour diff FILE --stdin` with the proposed version.
- Compare full files: `nvtour diff FILE --ref origin/master`.
- Read-only. `nvtour diff-close` closes it.

## Rules

- Never modify files.
- Never create a tour unless the user explicitly asks for it in nvim or the editor ("in nvim", "in the editor", "show me in vim") or says yes to your offer. "Walk me through", "explain" or "tour" alone is not enough: answer in chat.
- Never run `nvtour clear` unless the user asks or you start a new tour.
- Exit codes: 2 usage, 3 no unique nvim match, 4 no nvim (or `--socket` path missing), 5 RPC failure, 6 bad file, range or `--expect`.
- Exit 5 with `waiting for input`: nothing ran. Ask the user to press `<Esc>` or `<Enter>` in nvim, then retry once.
- Exit 5 with a timeout `during dispatch`: the command may still run. Run `nvtour status` before you retry.
- Warnings (for example a key that is already mapped) are printed on stderr as `warning: ...`.
- Global flags: `--socket PATH`, `--workspace DIR`, `--json`, `--timeout S`.

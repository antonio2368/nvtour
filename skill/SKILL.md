---
name: nvtour
description: Give a visual, read-only walkthrough of code inside the user's running Neovim while explaining a bug or a concept in chat — jump, highlight, annotate with virtual-text notes, fold to the relevant parts, show read-only diffs, keep a step panel. Use when the user asks to explain, walk through, show, or visualize something "in nvim" / "in the editor", or asks for a guided tour of a bug, a code path, or a concept. Never edits files.
---

# nvtour

`nvtour` drives the user's already-running Neovim from the shell. It never edits files and never starts nvim.

## Workflow

1. Run `nvtour attach` first.
   - Exit 3 or 4: relay the stderr message to the user verbatim and stop. Do not start nvim yourself.
2. Run `nvtour start "<title>"`.
3. Interleave with your chat explanation: one `nvtour step FILE:L1[-L2] ...` per point.
   - Use 3 to 8 steps. One idea per step.
   - Follow reading order: cause, propagation, effect (or entry, core, exit).
   - Only the first step moves the user's view. Later steps are added without a jump, so the tour opens at step 1.
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
- Text that starts with `-`: write `--note=TEXT` or `--label=TEXT`.
- Multi-line notes:

  ```
  nvtour step f.cpp:10-12 --role fault --note - <<'EOF'
  First sentence.
  Second sentence.
  EOF
  ```

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

## Point and ask

If the user says "this" or "here", run `nvtour where` to read their cursor or selection. A selection marked `previous, may be old` is the last visual selection, not a live one: use it only if the user said they selected something.

## Rules

- Never modify files.
- Never run `nvtour clear` unless the user asks or you start a new tour.
- Exit codes: 2 usage, 3 no unique nvim match, 4 no nvim (or `--socket` path missing), 5 RPC failure, 6 bad file, range or `--expect`.
- Exit 5 with `waiting for input`: nothing ran. Ask the user to press `<Esc>` or `<Enter>` in nvim, then retry once.
- Exit 5 with a timeout `during dispatch`: the command may still run. Run `nvtour status` before you retry.
- Warnings (for example a key that is already mapped) are printed on stderr as `warning: ...`.
- Global flags: `--socket PATH`, `--workspace DIR`, `--json`, `--timeout S`.

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
4. End with `nvtour panel -` holding a short markdown summary (heredoc on stdin).
5. In chat, only say that the tour is open in nvim. Do not list the steps again and do not list the keys. Then continue with the rest of the reply (for example options or questions that are still open).

## Writing notes

- Note: at most 3 short sentences. Label: at most 6 words.
- Roles: `fault` = wrong line(s); `flow` = how data or control gets there; `fix` = where or how it should change; `context` = background; `info` = neutral explanation (default).
- Name identifiers in the note. The highlight already shows the location.
- Multi-line notes: `nvtour step f.cpp:10-12 --role fault --note - <<'EOF' ... EOF`

## Focus

- Concept spans a long file: run `nvtour focus FILE:a-b c-d` once per file before the steps in it.
- Use `--dim` when the surrounding code matters for reading. `nvtour unfocus` undoes it.

## Diff

- Before/after a fix: `nvtour diff FILE --stdin` with the proposed version.
- Compare versions: `nvtour diff FILE --ref origin/master`.
- Read-only. `nvtour diff-close` closes it.

## Point and ask

If the user says "this" or "here", run `nvtour where` to read their cursor or selection.

## Rules

- Never modify files.
- Never run `nvtour clear` unless the user asks or you start a new tour.
- Exit codes: 3 no unique nvim match, 4 no nvim, 5 RPC failure (nvim may wait at a prompt), 6 bad file or range.
- Global flags: `--socket PATH`, `--workspace DIR`, `--json`, `--timeout S`.

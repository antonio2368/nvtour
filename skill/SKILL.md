---
name: nvtour
description: Give a visual, read-only walkthrough of code inside the user's running Neovim while explaining a bug or a concept in chat — jump, highlight, annotate with virtual-text notes, fold to the relevant parts, show read-only diffs and text diagrams (Mermaid/Graphviz) linked to the code, apply a whole tour from a TOML file, keep a step panel, read the places the user marked. Use when the user explicitly asks to explain, walk through, show, or visualize something "in nvim" / "in the editor". Also use it, without asking first, when the user asks about code ("this chunk", "this function", "here", "what does this do"), gives no file, line or pasted code, and "this" does not point to something earlier in the chat: read their nvim cursor or selection to find the location and answer in chat. Never edits files.
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
2. Write the whole tour as TOML and run `nvtour apply - <<'EOF' ... EOF` once, also for a tour of 1 or 2 steps. It starts the tour, adds the steps, sets the panel and jumps to step 1: do not run `start`, `step` or `panel` as well. The quoted heredoc needs no shell quoting, so notes can contain `(`, `'`, `$` and backticks. Do not chain `nvtour start ... && nvtour step ... && ...` in one command.

   ```
   nvtour apply - <<'EOF'
   title = "Dangling iterator in cleanup"
   panel = "**Cause**: use after erase. **Fix**: copy the value before `erase()`."

   [[step]]
   at = "src/a.cpp:/it->second/"            # a text range (see "Line numbers")
   chapter = "Why does it fail?"            # starts a chapter here (see "Chapters")
   role = "fault"
   label = "dangling iterator"
   expect = ["it->second"]
   note = "The iterator is used after `erase()` invalidated it."

   [[step]]
   at = "src/b.cpp:/void evict(/-2+6"         # 2 lines before the text (a comment) to 6 after it
   role = "flow"
   via = "the cleanup thread runs `evict()`"
   note = "The cleanup thread erases the entry while the reader holds `it`."
   EOF
   ```

   - Keys: top level `title`, `sheet`, `panel`, `[[focus]]`, `[[diagram]]`; `[[step]]` `at` (required), `role`, `label`, `note`, `expect`, `parts`, `via`, `from`, `suggest`, `sheet`, `ref`, `chapter`, `diagram`.
   - Check the echoed `step N/T` lines of every step. Run `apply --check` first when the tour is long or uses `ref`: it changes nothing. A failure lists every bad entry at once (`step[4]: ...`, one per line): fix them all in one edit of the tour file, then run it again. Use `step`, `edit` and `remove` only for fixes and for one more step in a tour that is open.
3. Interleave with your chat explanation: one step per point.
   - Use 3 to 8 steps. One idea per step.
   - Follow reading order: cause, propagation, effect (or entry, core, exit).
   - Only the first step moves the user's view. Later steps are added without a jump, so the tour opens at step 1.
   - When a step is in a different file from the step before it, give `via` / `--via TEXT`: why the tour goes there (see "Links").
4. End with a short markdown summary in the panel: the `panel` key, or `nvtour panel -` (heredoc on stdin).
5. In chat, only say that the tour is open in nvim. Do not list the steps again and do not list the keys. Then continue with the rest of the reply (for example options or questions that are still open).

## Line numbers

- Give every range by its text: `FILE:/text/`. Use text that you already read while you studied the code. Do not run `sed -n`, `rg -n`, `nl` or `cat -n` only to get line numbers for a step: nvtour finds the lines.
- The forms: `/text/` is the line that has the text. `/text/+N` adds N lines after it, `/text/-N` adds N lines before it (for example the comment above a function), and `/text/-N+M` does both. `/start/../end/` is a block from the `start` line to the first `end` line after it (`/start/-N../end/+M` with context). `\/` is a slash in the text. The text is plain, not a regex. Quote it when it has spaces.
- On several matches nvtour fails with exit 6 and lists the lines with their numbers. Pick, in this order: longer text that occurs once; `~L` (the match nearest to line L: an approximate L is enough); `@N` (the N-th match: it changes when the file changes).
- `focus`, `edit`, `step --diagram NAME /label/` and `diagram --link 'TEXT=FILE:/text/'` take it too. In a tour file it is the same text in `at`.
- Use numbers only when no text can name the lines, and then take them from tool output that you have already, not from memory. `FILE:L:COL` (compiler and `rg --column` output) is accepted; the column is ignored.
- Each `step` prints the first highlighted line (`  412| ...`). Check it.
- Add `--expect TEXT` (a substring of the range) to make a wrong range fail with exit 6 instead of highlighting the wrong code. A query and an expect together are fine. The error tells where the text is (`; it is on line 231`, or `; it is not in the file`): fix the range from that, without a new lookup. The user sees `TEXT` underlined, so pick the exact expression the note is about (for example `it->second`).
- When the note names more than one part of the range, give `--expect` for each one (at most 3): `--expect 'refresh(key)' --expect 'it->second'`. Each text must be in the range.

## Fixing a tour

- `nvtour edit N [FILE:/text/] [--note ...] [--label ...] [--role ...]` changes step N. `--label ''` removes the label.
- `nvtour remove N` removes step N. `nvtour step ... --at N` inserts at position N.
- `nvtour status` shows the tour, focus, panel and keys. Use it after an error or when you lost track. Do not `clear` and start again.
- **Questions during a tour:** before you answer a question that the user asks while a tour is open, run `nvtour status` and answer relative to the current step. The user may have moved with `]w`, `<CR>` in the panel, by cursor (`follow: on`), or read another step in a peek float (`<leader>wk`, `K` in the panel) that does not change the current step. If `status` shows `marks: N`, or the user mentions "the places I marked", run `nvtour marks` and use them.
- **Edited files:** steps follow the edits of the user. A step `(was 88-90)` moved and is fine. A step `(lost)` needs `nvtour edit N FILE:/text/`. A `modified: a.cpp` line means the buffer differs from disk: read the text from `status` or `where`, not from `sed` or `rg`.

## Chapters

- For a tour of more than 8 steps, group the steps: `chapter = "Why does it fail?"` on the first step of each group in the tour file (or `nvtour chapter "Why does it fail?"` before the first step of each group; the next step you add starts the chapter). Use 3 to 6 steps per chapter, and name each chapter after the question that it answers. The first step of each chapter shows its name above the note, so do not repeat it in the note. The user moves between chapters with `]c` / `[c`. `nvtour chapter --list` shows them; `nvtour chapter "Name" --at N` and `nvtour chapter --remove N` fix them.

## Marks (the user points at places)

- When you need the user to point at several places, ask them to put the cursor on each place (or select it) and press `<leader>wm`, or `<leader>wM` to add a comment. Then run `nvtour marks --wait 110 --clear`: it returns as soon as there is a mark, and prints the file, the lines, the first lines of the text, the git ref (`@REF`) and the comment. Exit 6 (`no marks after 110 s`) means the user has not marked yet: tell them, and run it again when they say they are done.
- To read marks that are there already, run `nvtour marks` (`--json` has the whole text of a mark). A mark with `(lines deleted)` points at lines that the user deleted: use the saved text. A mark with `@REF` is on the file at that git ref: read `git show REF:PATH`.
- The marks stay when you run `start` or `clear`: they belong to the user.

## Writing notes

- Note: readable cold. A one-line lead sentence, then short paragraphs split by blank lines; numbered lists for sequences, bullet lists for cases or outcomes. Explain what the names and values in the range mean, in several sentences when needed. Label: at most 6 words.
- Roles: `fault` = wrong line(s); `flow` = how data or control gets there; `fix` = where or how it should change; `context` = background; `info` = neutral explanation (default).
- Name identifiers in the note. The highlight already shows the location.
- Put identifiers in backticks (`` `erase()` ``); `**bold**` also works. Other markdown is shown as typed, except links to parts (below).
- **Parts:** when a note explains several places of its range, give each place as a part, in the order the note names them: `parts = ["/entries.find/../return {}/", { at = "/return it/", text = "it->second" }]` in a tour file, or `--part AT [--part-text TEXT]` on `step`. The first part is `a`, then `b`, ... A part is lines of the range (`L1[-L2]` or a text range, searched in the lines of the step only); `text` narrows it to one expression in those lines. Then link the words of the note that are about a part: `[It finds the entry](a) and keeps [the iterator](b).` The words can be one word, a sentence or a whole line; `[words](a,c)` links to two parts. Put punctuation after the link, not inside it. Do not link the lead sentence. A link must be on one line of the note.
- Give a part `text` when it shares a line with another part, so that `]r` can show one of them alone. Use at most 4 parts; split a step that needs more. Do not give parts to a step whose note is about the whole range.
- Only the current step shows its full note; the others show the first line. Make the first sentence of a note stand alone.
- Text that starts with `-`: write `--note=TEXT`, `--label=TEXT` or `--via=TEXT`.
- Multi-line notes: in a tour file, `note = """...` with the lines, and `"""` at the end. On a single `step` (a fix), `--note -` with a heredoc:

  ```
  nvtour step f.cpp:/erase(it)/-1+1 --role fault --note - <<'EOF'
  First sentence.
  Second sentence.
  EOF
  ```

## Links

- `--via TEXT` tells why the tour goes from the step before to this step, for example ``--via 'on a miss `get()` calls `evict()`'``. nvim shows it below the range of the step before (`→ next 3 · a.cpp:412: ...`), so the user knows why before the jump, and in the panel.
- Give `--via` on each step in another file. Give it also in the same file when the connection is not clear from the code (a call, a callback, a shared variable, the same lock).
- Write the connection, not the content of the new step: what leads there (a call, a return, data that goes there, a thread). The note tells what the code does. At most one short sentence; put identifiers in backticks.
- `--from N` makes the link come from step N, not from the step before (for example back to step 1). Use it only when the tour goes back to an earlier point.
- Do not write the file name, the line or the git ref in a note or `--via`. nvim shows them on each change of file or version.
- `nvtour edit N --via TEXT` changes a link; `--via ''` removes it; `--from 0` links from the step before again.

## Sheets

- Use this only when you also draw a `sheet` canvas for the same topic. nvtour never starts or changes a sheet by itself.
- `--sheet SHEET#STEP` on `nvtour step` (or `edit`) links a tour step to a step of a sheet. When the user moves to that tour step, the viewer shows the sheet step. Example: ``nvtour step f.cpp:/void evict(/ --sheet arch#evict`` (`sheet = "arch#evict"` in a tour file).
- Prefer a step id for STEP (`arch#evict`): numbers change when the sheet changes. 0 is the overview.
- `nvtour start TITLE --sheet SHEET` sets the sheet of the tour; then `--sheet '#evict'` is enough. `nvtour edit N --sheet ''` removes a link.
- One tour can use several sheets: a full `SHEET#STEP` links a step to another sheet (`#STEP` keeps the tour's sheet), so the tour can go from `arch#overview` to `raft#append` and back. The note of the first linked step and of each step where the sheet changes starts with `↔ sheet raft`, so you do not need to write that the sheet changed.
- A value without `#` is exit 2. `#STEP` without `start --sheet` is exit 6.
- Create the sheet before you link to it. If `sheet` is not installed or the viewer is not open, nothing happens and nvtour prints no error (`nvtour doctor` shows the `sheet command`).

## Focus

- Concept spans a long file: run `nvtour focus FILE:a-b c-d` once per file.
- After a long tour in long files, run `nvtour focus --steps` once. It folds every toured file to the lines of its steps, and it stays in sync when you add, edit or remove steps (or the user edits). Run it again when a new file gets its first step.
- During a tour, focus does not move the view; the folds appear when that file is shown.
- Fold mode removes the user's own manual folds in that window. Use `--dim` if the user has manual folds or the surrounding code matters. `nvtour unfocus` undoes it.

## Old code

- `nvtour step FILE:/text/ --ref GITREF` (or `ref = "GITREF"` in a tour file) shows the range as it is at a git ref, in a read-only buffer. Everything else works as on a normal step.
- Use it for code that a change removed or moved, and for before/after: a step on the old range (`--ref origin/master`), then a step on the new range (no `--ref`).
- With `--ref`, the text is searched in the file at that ref. Use text from the old version (`git show GITREF:PATH`, or the diff you read), not from the working tree.
- The file does not have to exist in the working tree.
- `nvtour edit N FILE:/text/` without `--ref` moves the step to the working tree. Add `--ref` to keep it on the old version.

## Suggested fix

- A `fix` step with a concrete change gives `--suggest`: the code that would replace the range. nvim shows it below the range, with syntax colours, and strikes through the old lines.
- `--suggest` replaces the whole range: give all the new lines for it, not only the changed line. To delete a line, leave it out. Use the indentation of the file.
- In a tour file, write the code in a literal string (`'''`): it keeps backslashes and quotes as they are. On a single `step`, `--suggest -` reads a heredoc; only one of `--note -` and `--suggest -` can read stdin, so give the note inline then.

  ```
  [[step]]
  at = "f.cpp:/expires_at = now/+1"
  role = "fix"
  label = "update in place"
  note = "Change the expiry through `it`."
  suggest = '''
          it->second.expires_at += ttl;
          return it->second.value;
  '''
  ```

- `nvtour edit N --suggest ''` removes it.

## Diagrams

- Use a diagram when the point is a protocol, a sequence of messages, or how components connect, and code alone does not show it. Do not draw a diagram for one function.
- Make it after `nvtour start` (`start` and `clear` delete diagrams): `nvtour diagram NAME --link 'TEXT=FILE:/text/' <<'EOF'` with Mermaid source on stdin, or a `[[diagram]]` entry in the tour file. The terminal shows no images: it is text in a split next to the code.
- Before the first `nvtour diagram` in a conversation, ask the user where to put the diagram: above the code (horizontal split, good for wide diagrams), or beside it (vertical split, good for tall diagrams such as a long `sequenceDiagram`), and on which side, or full screen (a tab page of its own, good when there is no code to look at). Do not ask again in the same conversation unless the user wants to change it. Give the answer as `--split above|below|left|right|full` on every `nvtour diagram`.
- Formats: Mermaid `sequenceDiagram` for messages over time, `graph LR` / `graph TD` for components (default; needs `mermaid-ascii`). `--format dot` for Graphviz DOT (needs `graph-easy`). `--format text` for a small diagram you draw yourself. Keep it to about 6 participants or 20 nodes: wide diagrams do not fit.
- The participant names of a `sequenceDiagram` stay in the winbar when the user scrolls; nothing to do. For a long `--format text` diagram with a row of names or columns at the top, give `--header N` (the line of the names).
- The output prints every rendered line with its number. A step on a diagram is best given by its label: `/label/` is searched in the rendered lines, not in the source.
- `nvtour step --diagram NAME /label/ --note ...` (`diagram = "NAME"` and `at = "/label/"` in a tour file) puts a step on diagram lines (for example one message). Give `--expect` with the label text. Then step into the code with `--via`, and back to the diagram for the next message.
- `--link 'TEXT=FILE:/text/'` lets the user press `<CR>` on TEXT to open that code. Link each label that has one clear code location. TEXT must be in the rendered diagram (exit 6 otherwise).
- Exit 2 with `not found`: the renderer is missing. Tell the user, or draw the diagram by hand with `--format text`.
- Never state a message or an edge that you did not verify in the code or the docs.

## Diff

- Before/after a fix that is larger than one range: `nvtour diff FILE --stdin` with the proposed version.
- Compare full files: `nvtour diff FILE --ref origin/master`.
- Read-only. `nvtour diff-close` closes it.

## Rules

- Never modify files.
- Never create a tour unless the user explicitly asks for it in nvim or the editor ("in nvim", "in the editor", "show me in vim") or says yes to your offer. "Walk me through", "explain" or "tour" alone is not enough: answer in chat.
- Never run `nvtour clear` unless the user asks or you start a new tour.
- Never run `nvtour marks --clear` unless you have read the marks in this turn (`--clear` prints what it removes, so one call is enough).
- Exit codes: 2 usage, 3 no unique nvim match, 4 no nvim (or `--socket` path missing), 5 RPC failure, 6 bad file, range or `--expect` (or `marks --wait` with no mark).
- Exit 5 with `waiting for input`: nothing ran. If you asked the user to mark places, they may be typing a comment (`<leader>wM`): wait, then retry once. Else ask the user to press `<Esc>` or `<Enter>` in nvim, then retry once.
- Exit 5 with a timeout `during dispatch`: the command may still run. Run `nvtour status` before you retry.
- Warnings (for example a key that is already mapped) are printed on stderr as `warning: ...`.
- Global flags: `--socket PATH`, `--workspace DIR`, `--json`, `--timeout S`.

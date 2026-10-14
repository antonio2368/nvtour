# nvtour

## Tests

Always run the tests in parallel with pytest-xdist (in the `test` extra):

```sh
python -m pytest -n auto --basetemp=/tmp/nvt            # the whole suite: about 1 min instead of almost 4
python -m pytest -n auto --basetemp=/tmp/nvt tests/test_side.py   # while iterating: only the files you touch
```

- Each xdist worker gets its own sandbox and its own headless nvim (session fixtures), so the workers do
  not share state.
- `--basetemp` must be short on macOS: the nvim socket lives under it, and the default pytest temp path is
  longer than the 104-byte limit of a Unix socket path, so nvim never starts. Give two concurrent runs
  (for example a baseline in a worktree) different `--basetemp` directories.
- On macOS some tests always fail: instance discovery reads `/proc` (`test_score_table`, `test_doctor`,
  `test_discovery_*`, ...), and the diagram tests need `mermaid-ascii`. Compare against a baseline run of
  `HEAD` in a separate git worktree (not `git stash`) before treating a failure as new.

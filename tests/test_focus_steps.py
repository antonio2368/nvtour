"""Tests of `nvtour focus --steps`: a focus that follows the steps."""

from __future__ import annotations

import json
import time

from test_integration import find_buf, ns_marks, ok

SETTLE = 0.45  # the sync runs 150 ms after the last edit


def settle():
    time.sleep(SETTLE)


def status(cli):
    return json.loads(ok(cli("--json", "status")))


def closed(nv, win, line):
    """foldclosed() of `line` in the window `win`."""
    return nv.exec_lua("local w, l = ...; return vim.api.nvim_win_call(w, function() return vim.fn.foldclosed(l) end)", win.handle, line)


def setup_tour(cli, nv):
    """Two windows, a.txt left and b.txt right; steps a.txt:10-12, a.txt:30, b.txt:5. Returns the windows."""
    ok(cli("start", "Focus"))
    ok(cli("step", "a.txt:10-12", "--label", "one"))
    ok(cli("step", "a.txt:30", "--label", "two", "--no-jump"))
    ok(cli("step", "b.txt:5", "--label", "three", "--no-jump"))
    nv.command("edit a.txt")
    wa = nv.current.window
    nv.command("vsplit b.txt")
    wb = nv.current.window
    return wa, wb


def foci(cli):
    return {f["file"].rsplit("/", 1)[-1]: f for f in status(cli)["focus"]}


def test_focus_steps_folds_both_files(cli, nv):
    wa, wb = setup_tour(cli, nv)
    out = ok(cli("focus", "--steps", "--context", "1"))
    assert out.splitlines() == ["focus: a.txt 9-13 29-31 (fold)", "focus: b.txt 4-6 (fold)"]
    for line in (1, 8, 14, 28, 32, 60):
        assert closed(nv, wa, line) != -1, line
    for line in (9, 10, 12, 13, 29, 30, 31):
        assert closed(nv, wa, line) == -1, line
    assert closed(nv, wb, 1) != -1 and closed(nv, wb, 3) != -1 and closed(nv, wb, 7) != -1 and closed(nv, wb, 30) != -1
    assert closed(nv, wb, 4) == -1 and closed(nv, wb, 5) == -1 and closed(nv, wb, 6) == -1
    assert wb == nv.current.window  # the view does not move


def test_adding_a_step_opens_a_fold_gap_and_remove_closes_it(cli, nv):
    wa, _ = setup_tour(cli, nv)
    ok(cli("focus", "--steps", "--context", "1"))
    assert closed(nv, wa, 20) != -1
    ok(cli("step", "a.txt:20", "--label", "four", "--no-jump"))
    assert closed(nv, wa, 20) == -1 and closed(nv, wa, 19) == -1 and closed(nv, wa, 21) == -1
    assert closed(nv, wa, 16) != -1 and closed(nv, wa, 23) != -1  # the gaps on both sides
    assert foci(cli)["a.txt"]["ranges"] == [[9, 13], [19, 21], [29, 31]]
    ok(cli("remove", "4"))
    assert closed(nv, wa, 20) != -1 and closed(nv, wa, 19) != -1
    assert foci(cli)["a.txt"]["ranges"] == [[9, 13], [29, 31]]


def test_edit_of_a_step_moves_the_folds(cli, nv):
    wa, wb = setup_tour(cli, nv)
    ok(cli("focus", "--steps", "--context", "1"))
    ok(cli("edit", "2", "a.txt:45-46"))
    assert closed(nv, wa, 30) != -1 and closed(nv, wa, 44) == -1 and closed(nv, wa, 47) == -1
    assert foci(cli)["a.txt"]["ranges"] == [[9, 13], [44, 47]]
    # A step that goes to another file: both files change; b.txt has no step left and is not focused.
    ok(cli("edit", "3", "a.txt:20"))
    assert "b.txt" not in foci(cli)
    assert closed(nv, wb, 1) == -1 and closed(nv, wb, 20) == -1
    assert closed(nv, wa, 20) == -1 and closed(nv, wa, 25) != -1
    assert foci(cli)["a.txt"]["ranges"] == [[9, 13], [19, 21], [44, 47]]


def test_edit_to_a_file_with_a_focus_changes_that_focus(cli, nv):
    wa, wb = setup_tour(cli, nv)
    ok(cli("focus", "--steps", "--context", "0"))
    ok(cli("edit", "2", "b.txt:20"))
    assert foci(cli)["a.txt"]["ranges"] == [[10, 12]]
    assert foci(cli)["b.txt"]["ranges"] == [[5, 5], [20, 20]]
    assert closed(nv, wb, 20) == -1 and closed(nv, wb, 10) != -1
    assert closed(nv, wa, 30) != -1


def test_user_edit_moves_the_folds(cli, nv):
    wa, _ = setup_tour(cli, nv)
    ok(cli("focus", "--steps", "--context", "1"))
    buf = find_buf(nv, "a.txt")
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["t1", "t2", "t3"])
    settle()
    assert foci(cli)["a.txt"]["ranges"] == [[12, 16], [32, 34]]
    assert closed(nv, wa, 11) != -1 and closed(nv, wa, 17) != -1 and closed(nv, wa, 35) != -1
    assert closed(nv, wa, 12) == -1 and closed(nv, wa, 14) == -1 and closed(nv, wa, 16) == -1
    assert closed(nv, wa, 32) == -1 and closed(nv, wa, 34) == -1
    # Lines added inside the range make it larger.
    nv.api.buf_set_lines(buf.handle, 14, 14, True, ["x1", "x2"])
    settle()
    assert foci(cli)["a.txt"]["ranges"] == [[12, 18], [34, 36]]
    assert closed(nv, wa, 18) == -1 and closed(nv, wa, 19) != -1


def test_lost_step_and_last_removed_step_end_the_focus_of_the_file(cli, nv):
    wa, wb = setup_tour(cli, nv)
    ok(cli("focus", "--steps"))
    buf = find_buf(nv, "b.txt")
    nv.api.buf_set_lines(buf.handle, 4, 5, True, [])  # the only step of b.txt loses its line
    settle()
    assert "b.txt" not in foci(cli) and "a.txt" in foci(cli)
    assert closed(nv, wb, 1) == -1 and closed(nv, wb, 20) == -1
    ok(cli("remove", "2"))
    ok(cli("remove", "1"))
    assert status(cli)["focus"] == []
    assert closed(nv, wa, 1) == -1 and closed(nv, wa, 50) == -1


def test_focus_steps_for_one_file_with_dim(cli, nv):
    setup_tour(cli, nv)
    out = ok(cli("focus", "--steps", "a.txt", "--dim", "--context", "0"))
    assert out.splitlines() == ["focus: a.txt 10-12 30 (dim)"]
    a, b = find_buf(nv, "a.txt"), find_buf(nv, "b.txt")

    def dim_rows(buf):
        return sorted(m[1] for m in ns_marks(nv, "nvtour_focus", buf.handle) if m[3].get("hl_group") == "NvtourDim")

    rows = dim_rows(a)
    assert len(rows) == 60 - 4 and 9 not in rows and 29 not in rows and 8 in rows
    assert dim_rows(b) == []
    assert [f["file"].rsplit("/", 1)[-1] for f in status(cli)["focus"]] == ["a.txt"]
    # The dim follows the steps too.
    ok(cli("step", "a.txt:50", "--no-jump"))
    rows = dim_rows(a)
    assert len(rows) == 60 - 5 and 49 not in rows
    ok(cli("unfocus"))
    assert dim_rows(a) == []


def test_unfocus_restores_the_fold_options(cli, nv):
    wa, wb = setup_tour(cli, nv)
    nv.command("setlocal foldmethod=indent")  # b.txt window: the user's own value
    before_a = nv.exec_lua("return vim.api.nvim_get_option_value('foldmethod', { scope = 'local', win = ... })", wa.handle)
    ok(cli("focus", "--steps"))
    assert nv.exec_lua("return vim.wo[...].foldmethod", wa.handle) == "manual"
    assert nv.exec_lua("return vim.wo[...].foldmethod", wb.handle) == "manual"
    assert ok(cli("unfocus")).strip() == "unfocused 2 buffer(s)"
    assert nv.exec_lua("return vim.wo[...].foldmethod", wa.handle) == before_a
    assert nv.exec_lua("return vim.wo[...].foldmethod", wb.handle) == "indent"
    assert closed(nv, wa, 1) == -1
    ok(cli("step", "a.txt:40", "--no-jump"))  # no focus any more: the new step does not make one
    assert status(cli)["focus"] == []
    assert nv.exec_lua("return vim.wo[...].foldmethod", wa.handle) == before_a


def test_start_and_clear_end_the_focus(cli, nv):
    wa, _ = setup_tour(cli, nv)
    ok(cli("focus", "--steps"))
    assert closed(nv, wa, 1) != -1
    ok(cli("start", "Again"))
    assert status(cli)["focus"] == [] and closed(nv, wa, 1) == -1
    ok(cli("step", "a.txt:10", "--no-jump"))
    ok(cli("focus", "--steps"))
    assert closed(nv, wa, 1) != -1
    ok(cli("clear"))
    assert closed(nv, wa, 1) == -1


def test_plain_focus_replaces_the_focus_from_steps(cli, nv):
    wa, _ = setup_tour(cli, nv)
    ok(cli("focus", "--steps", "--context", "1"))
    out = ok(cli("focus", "a.txt:50-52", "--context", "0"))
    assert out.strip() == "focus: a.txt 50-52 (fold)"
    assert "from_steps" not in foci(cli)["a.txt"] and foci(cli)["b.txt"]["from_steps"] is True
    ok(cli("step", "a.txt:20", "--no-jump"))  # a.txt does not follow the steps now
    assert foci(cli)["a.txt"]["ranges"] == [[50, 52]]
    assert closed(nv, wa, 20) != -1 and closed(nv, wa, 50) == -1


def test_status_shows_the_focus_from_the_steps(cli, nv):
    setup_tour(cli, nv)
    ok(cli("focus", "--steps", "--context", "1"))
    data = status(cli)
    assert [f["from_steps"] for f in data["focus"]] == [True, True]
    text = ok(cli("status"))
    assert "focus: a.txt 9-13 29-31 (fold, from steps)" in text.splitlines()
    assert "focus: b.txt 4-6 (fold, from steps)" in text.splitlines()
    ok(cli("focus", "--steps", "b.txt", "--dim"))
    assert "focus: b.txt 3-7 (dim, from steps)" in ok(cli("status")).splitlines()
    ok(cli("focus", "b.txt:1-2", "--context", "0"))
    assert "focus: b.txt 1-2 (fold)" in ok(cli("status")).splitlines()  # a plain focus: no note


def test_focus_steps_json_and_a_step_at_a_git_ref(cli, nv):
    ok(cli("start", "Ref"))
    ok(cli("step", "a.txt:10-12"))
    ok(cli("step", "a.txt:20-21", "--ref", "v1", "--no-jump"))
    res = json.loads(ok(cli("--json", "focus", "--steps", "--context", "0")))
    assert res["ok"] is True
    assert [(f["file"].rsplit("/", 1)[-1], f.get("ref"), f["ranges"], f["mode"]) for f in res["focus"]] == [
        ("a.txt", None, [[10, 12]], "fold"),
        ("a.txt", "v1", [[20, 21]], "fold"),
    ]
    text = ok(cli("focus", "--steps", "--context", "0")).splitlines()
    assert text[0].startswith("focus: a.txt 10-12 (fold")
    assert text[1].startswith("focus: a.txt @v1 20-21 (fold")
    assert ok(cli("unfocus", "a.txt")).strip() == "unfocused 2 buffer(s)"


def test_focus_steps_errors(cli, nv):
    r = cli("focus", "--steps")  # no tour
    assert r.returncode == 6 and "no steps" in r.stderr
    ok(cli("start", "E"))
    r = cli("focus", "--steps")
    assert r.returncode == 6 and "no steps" in r.stderr
    ok(cli("step", "a.txt:10", "--no-jump"))
    r = cli("focus", "--steps", "b.txt")
    assert r.returncode == 6 and "no step is in" in r.stderr and "b.txt" in r.stderr
    r = cli("focus", "--steps", "a.txt", "3-4")
    assert r.returncode == 2 and "no range" in r.stderr
    r = cli("focus", "--steps", "a.txt:3-4")
    assert r.returncode == 2 and "plain FILE" in r.stderr
    r = cli("focus", "--steps", "missing.txt")
    assert r.returncode == 6
    r = cli("focus")
    assert r.returncode == 2
    assert status(cli)["focus"] == []
    # A diagram step does not count.
    ok(cli("start", "D"))
    ok(cli("diagram", "flow", "--format", "text", "--no-show", stdin="one\ntwo\n"))
    ok(cli("step", "--diagram", "flow", "1", "--no-jump"))
    r = cli("focus", "--steps")
    assert r.returncode == 6 and "no steps" in r.stderr

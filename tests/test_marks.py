"""Tests of marks: places that the user points at (<leader>wm) and that the agent reads (`nvtour marks`)."""

from __future__ import annotations

import json
import re
import time

import pytest
from test_integration import find_buf, ns_marks, ok

from nvtour.client import lua_source

MARK = "\\wm"  # <leader>wm with the default leader
MARK_NOTE = "\\wM"


@pytest.fixture()
def notes(nv):
    """vim.notify collects its messages in _G.msgs. At the end: the marks are cleared, the runtime
    and the mark keys are as they were, and the tour is cleared."""
    nv.exec_lua("_G.msgs = {}; _G.old_notify = vim.notify; vim.notify = function(m) table.insert(_G.msgs, m) end")
    nv.exec_lua("_G.old_input = vim.ui.input")
    yield lambda: nv.exec_lua("return _G.msgs")
    nv.exec_lua("vim.notify = _G.old_notify; vim.ui.input = _G.old_input; vim.g.nvtour_keys = nil")
    nv.exec_lua("_G.nvtour.dispatch('marks', { clear = true }); _G.nvtour.dispatch('clear', {})")


def press(nv, keys):
    """Type keys and run them to the end (mappings apply)."""
    nv.api.feedkeys(nv.replace_termcodes(keys, True, False, True), "mx", False)


def open_file(nv, name="a.txt", line=1):
    nv.command(f"edit {name}")
    nv.api.win_set_cursor(nv.current.window, [line, 0])
    return nv.current.buffer


def marks_json(cli, *args):
    return json.loads(ok(cli("--json", "marks", *args)))


def signs(nv, buf):
    return ns_marks(nv, "nvtour_marks", buf.handle)


def reload_runtime(nv, version="other-version"):
    """Load the runtime again as a new version, like an upgrade does."""
    nv.exec_lua("local m = (function(...)\n" + lua_source() + "\nend)(...)\nreturn m.VERSION", version)


def fresh_runtime(nv):
    """Forget the runtime and its mark keys, like a nvim where nvtour was never used."""
    nv.exec_lua(
        "if _G.nvtour then _G.nvtour.dispatch('marks', { clear = true }) end; "
        "for _, lhs in ipairs({ '<leader>wm', '<leader>wM' }) do pcall(vim.keymap.del, { 'n', 'x' }, lhs) end; "
        "_G.nvtour = nil"
    )


def test_mark_from_normal_mode_marks_the_cursor_line(cli, nv, notes):
    ok(cli("status"))  # loads the runtime: the keys come with it
    buf = open_file(nv, "a.txt", 3)
    press(nv, MARK)
    assert any("nvtour: mark 1 added (a.txt:3)" in m for m in notes())
    data = marks_json(cli)
    assert data["count"] == 1
    m = data["marks"][0]
    assert (m["n"], m["file"], m["l1"], m["l2"], m["kind"]) == (1, str(nv.call("fnamemodify", "a.txt", ":p")), 3, 3, "line")
    assert m["text"] == ["line 3 of a"] and "ref" not in m and "diagram" not in m and "comment" not in m
    assert abs(m["at"] - time.time()) < 30 and m["age"] >= 0
    sign = signs(nv, buf)
    assert len(sign) == 1 and sign[0][1] == 2 and sign[0][3]["sign_text"].strip() == "◆"
    assert sign[0][3]["sign_hl_group"] == "NvtourUserMark"
    assert nv.exec_lua("return vim.api.nvim_get_hl(0, { name = 'NvtourUserMark' }).link") == "DiagnosticHint"
    assert buf.options["modified"] is False


def test_mark_from_visual_selection_keeps_text_and_kind(cli, nv, notes):
    ok(cli("status"))
    buf = open_file(nv, "a.txt", 1)
    press(nv, "3GVj" + MARK)  # linewise
    assert nv.api.get_mode()["mode"] == "n"  # the selection ends
    nv.api.win_set_cursor(nv.current.window, [10, 2])
    press(nv, "vjl" + MARK)  # charwise: line 10 col 3 to line 11 col 4
    nv.api.win_set_cursor(nv.current.window, [20, 5])
    press(nv, "<C-v>jl" + MARK)  # blockwise
    got = marks_json(cli)["marks"]
    assert [(m["l1"], m["l2"], m["kind"]) for m in got] == [(3, 4, "line"), (10, 11, "char"), (20, 21, "block")]
    assert got[0]["text"] == ["line 3 of a", "line 4 of a"]
    assert got[1]["text"] == ["ne 10 of a", "line"]
    assert got[2]["text"] == ["20", "21"]  # columns 6 to 7
    assert any("mark 2 added (a.txt:10-11)" in m for m in notes())
    sign_rows = sorted(s[1] for s in signs(nv, buf))
    assert sign_rows == [2, 9, 19]  # the first line of each range
    assert buf.options["modified"] is False


def test_mark_text_is_cut_like_where(cli, nv, notes):
    ok(cli("status"))
    open_file(nv, "a.txt", 1)
    nv.api.buf_set_lines(nv.current.buffer, 0, 0, False, [f"extra {i}" for i in range(300)])
    press(nv, "1GVG" + MARK)
    m = marks_json(cli)["marks"][0]
    assert len(m["text"]) == 200 and m["truncated"] is True and m["l1"] == 1 and m["l2"] == 360


def test_mark_in_a_ref_buffer_carries_ref_and_sha(cli, nv, notes, sandbox):
    ok(cli("start", "Ref"))
    ok(cli("step", "a.txt:5-6", "--ref", "v1", "--note", "old"))
    assert nv.exec_lua("return vim.b.nvtour_ref ~= nil")
    nv.api.win_set_cursor(nv.current.window, [5, 0])
    press(nv, MARK)
    m = marks_json(cli)["marks"][0]
    assert m["ref"] == "v1" and len(m["sha"]) == 40 and m["file"] == str(sandbox.ws / "a.txt")
    assert m["l1"] == 5 and m["text"] == ["old line 5 of a"]
    text = ok(cli("marks"))
    assert re.match(r"1\. a\.txt:5 @v1  \(\d+ s ago\)\n     old line 5 of a\Z", text.strip()), text


def test_mark_on_a_diagram_names_the_diagram(cli, nv, notes):
    ok(cli("diagram", "flow", "-", "--ascii", "--no-show", stdin="graph TD\n  A-->B\n"))
    buf = find_buf(nv, "flow")
    nv.command(f"buffer {buf.handle}")
    nv.api.win_set_cursor(nv.current.window, [2, 0])
    press(nv, MARK)
    m = marks_json(cli)["marks"][0]
    assert m["diagram"] == "flow" and m["l1"] == 2
    assert ok(cli("marks")).startswith("1. diagram flow:2  (")


def test_marks_text_output_has_comment_age_and_cut_text(cli, nv, notes):
    ok(cli("status"))
    open_file(nv, "a.txt", 1)
    press(nv, "3GV8j" + MARK)  # nine lines
    nv.exec_lua("vim.ui.input = function(_, cb) cb('why is this not under the lock?') end")
    nv.api.win_set_cursor(nv.current.window, [20, 0])
    press(nv, MARK_NOTE)
    out = ok(cli("marks")).splitlines()
    assert re.fullmatch(r"1\. a\.txt:3-11  \(\d+ s ago\)", out[0]), out
    assert out[1:6] == [f"     line {i} of a" for i in range(3, 8)] and out[6] == "     ..."
    assert re.fullmatch(r'2\. a\.txt:20  \(\d+ s ago\)  "why is this not under the lock\?"', out[7]), out
    assert out[8] == "     line 20 of a" and len(out) == 9
    full = marks_json(cli)["marks"][0]
    assert len(full["text"]) == 9  # --json has the whole text


def test_note_key_asks_for_a_comment(cli, nv, notes):
    ok(cli("status"))
    open_file(nv, "a.txt", 4)
    nv.exec_lua("_G.asked = nil; vim.ui.input = function(o, cb) _G.asked = o.prompt; cb('  check this  ') end")
    press(nv, MARK_NOTE)
    assert nv.exec_lua("return _G.asked") == "Note for the agent: "
    m = marks_json(cli)["marks"][0]
    assert m["comment"] == "check this"
    # An empty input and a cancelled input keep the mark without a comment.
    nv.exec_lua("vim.ui.input = function(_, cb) cb('') end")
    nv.api.win_set_cursor(nv.current.window, [5, 0])
    press(nv, MARK_NOTE)
    nv.exec_lua("vim.ui.input = function(_, cb) cb(nil) end")
    nv.api.win_set_cursor(nv.current.window, [6, 0])
    press(nv, MARK_NOTE)
    got = marks_json(cli)["marks"]
    assert [(m["l1"], m.get("comment")) for m in got] == [(4, "check this"), (5, None), (6, None)]


def test_note_key_in_visual_mode(cli, nv, notes):
    ok(cli("status"))
    open_file(nv, "a.txt", 1)
    nv.exec_lua("vim.ui.input = function(_, cb) cb('these two') end")
    press(nv, "7GVj" + MARK_NOTE)
    m = marks_json(cli)["marks"][0]
    assert (m["l1"], m["l2"], m["comment"]) == (7, 8, "these two")


def test_marks_clear_removes_the_marks_and_the_signs(cli, nv, notes):
    ok(cli("status"))
    buf = open_file(nv, "a.txt", 2)
    press(nv, MARK)
    nv.api.win_set_cursor(nv.current.window, [9, 0])
    press(nv, MARK)
    assert len(signs(nv, buf)) == 2
    before = marks_json(cli, "--clear")
    assert before["count"] == 2
    assert signs(nv, buf) == []
    assert marks_json(cli)["count"] == 0
    assert ok(cli("marks")).strip() == "no marks"
    assert marks_json(cli)["marks"] == []
    # A mark made after a clear is number 1 again.
    press(nv, MARK)
    assert marks_json(cli)["marks"][0]["n"] == 1


def test_marks_without_clear_keep_the_marks(cli, nv, notes):
    ok(cli("status"))
    open_file(nv, "a.txt", 2)
    press(nv, MARK)
    ok(cli("marks"))
    assert marks_json(cli)["count"] == 1


def test_start_and_clear_keep_marks_and_mark_keys(cli, nv, notes):
    ok(cli("status"))
    buf = open_file(nv, "a.txt", 2)
    press(nv, MARK)
    ok(cli("start", "T"))
    ok(cli("step", "b.txt:3"))
    assert marks_json(cli)["count"] == 1
    ok(cli("clear"))
    ok(cli("start", "T2"))
    ok(cli("clear"))
    assert marks_json(cli)["count"] == 1 and len(signs(nv, buf)) == 1
    for lhs in ("<leader>wm", "<leader>wM"):
        assert nv.call("maparg", lhs, "n") != "" and nv.call("maparg", lhs, "x") != ""
    assert nv.call("maparg", "<leader>wp", "n") == ""  # the keys of the tour are gone
    assert json.loads(ok(cli("--json", "status")))["keys"] == {}


def test_wait_returns_at_once_when_a_mark_exists(cli, nv, notes):
    ok(cli("status"))
    open_file(nv, "a.txt", 2)
    press(nv, MARK)
    t0 = time.monotonic()
    out = ok(cli("marks", "--wait", "2"))
    assert time.monotonic() - t0 < 1.5 and out.startswith("1. a.txt:2  (")


def test_wait_is_exit_6_after_the_time_when_there_is_no_mark(cli, nv, notes):
    ok(cli("status"))
    t0 = time.monotonic()
    r = cli("marks", "--wait", "2")
    elapsed = time.monotonic() - t0
    assert r.returncode == 6 and r.stderr.strip() == "no marks after 2 s" and r.stdout == ""
    assert 1.8 <= elapsed < 5
    r = cli("--json", "marks", "--wait", "1")
    assert r.returncode == 6 and json.loads(r.stdout) == {"ok": False, "code": 6, "error": "no marks after 1 s"}


def test_wait_sees_a_mark_made_while_it_waits(cli, nv, notes):
    import threading

    ok(cli("status"))
    open_file(nv, "a.txt", 2)
    result = {}

    def run():
        t0 = time.monotonic()
        result["r"] = cli("marks", "--wait", "20", "--clear")
        result["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=run)
    th.start()
    time.sleep(1.5)
    press(nv, MARK)
    th.join(30)
    assert result["r"].returncode == 0 and result["r"].stdout.startswith("1. a.txt:2  (")
    assert result["elapsed"] < 8
    assert marks_json(cli)["count"] == 0  # --clear removed what it printed


def test_wait_in_a_blocked_nvim_is_exit_5(cli, nv, notes, nvim_proc):
    import pynvim

    ok(cli("status"))
    ui = pynvim.attach("socket", path=nvim_proc.socket)
    ui.ui_attach(80, 24, rgb=True)
    nv.input(':echo "a\\nb\\nc\\nd"<CR>')
    deadline = time.time() + 5
    while not nv.api.get_mode()["blocking"] and time.time() < deadline:
        time.sleep(0.05)
    try:
        t0 = time.monotonic()
        r = cli("marks", "--wait", "10")
        assert r.returncode == 5 and "waiting for input" in r.stderr
        assert time.monotonic() - t0 < 4
    finally:
        nv.input("<CR>")
        ui.ui_detach()
        ui.close()


def test_mark_follows_an_edit_above_it(cli, nv, notes):
    ok(cli("status"))
    buf = open_file(nv, "a.txt", 10)
    press(nv, "10GVj" + MARK)
    nv.api.buf_set_lines(buf.handle, 0, 0, False, ["new one", "new two"])
    m = marks_json(cli)["marks"][0]
    assert (m["l1"], m["l2"]) == (12, 13)
    assert m["text"] == ["line 10 of a", "line 11 of a"]  # the text as it was marked
    assert ok(cli("marks")).startswith("1. a.txt:12-13  (")
    assert [s[1] for s in signs(nv, buf)] == [11]


def test_mark_with_all_its_lines_deleted(cli, nv, notes):
    ok(cli("status"))
    buf = open_file(nv, "a.txt", 5)
    press(nv, "5GVj" + MARK)
    nv.api.buf_set_lines(buf.handle, 2, 9, False, [])
    data = marks_json(cli)["marks"][0]
    assert data["deleted"] is True and (data["l1"], data["l2"]) == (5, 6)
    out = ok(cli("marks")).splitlines()
    assert re.fullmatch(r"1\. a\.txt:5-6  \(lines deleted\)  \(\d+ s ago\)", out[0]), out
    assert out[1:] == ["     line 5 of a", "     line 6 of a"]
    nv.command("undo")  # the undo brings the lines back
    again = marks_json(cli)["marks"][0]
    assert "deleted" not in again and (again["l1"], again["l2"]) == (5, 6)


def test_marks_clear_command(cli, nv, notes):
    ok(cli("status"))
    buf = open_file(nv, "a.txt", 5)
    press(nv, MARK)
    nv.api.win_set_cursor(nv.current.window, [8, 0])
    press(nv, MARK)
    nv.command("NvtourMarksClear")
    assert marks_json(cli)["count"] == 0 and signs(nv, buf) == []
    assert any("2 mark(s) cleared" in m for m in notes())


def test_status_shows_the_number_of_marks(cli, nv, notes):
    ok(cli("status"))
    assert json.loads(ok(cli("--json", "status")))["marks"] == 0
    assert "marks:" not in ok(cli("status"))
    open_file(nv, "a.txt", 5)
    press(nv, MARK)
    nv.api.win_set_cursor(nv.current.window, [8, 0])
    press(nv, MARK)
    data = json.loads(ok(cli("--json", "status")))
    assert data["marks"] == 2 and data["mark_keys"] == {"mark": "<leader>wm", "mark_note": "<leader>wM"}
    text = ok(cli("status"))
    assert "\nmarks: 2\n" in text
    ok(cli("marks", "--clear"))
    assert "marks:" not in ok(cli("status"))


def test_mark_in_a_window_without_a_file_is_refused(cli, nv, notes):
    ok(cli("start", "P"))
    ok(cli("step", "a.txt:3"))
    nv.command("terminal")
    nv.input("<C-\\><C-n>")
    time.sleep(0.2)
    press(nv, MARK)
    assert any("mark: this window shows no file" in m for m in notes())
    nv.command("bwipeout!")
    assert marks_json(cli)["count"] == 0


def test_keys_exist_right_after_attach_without_a_tour(cli, nv, notes, sandbox, nvim_proc):
    fresh_runtime(nv)
    assert nv.call("maparg", "<leader>wm", "n") == ""
    try:
        r = cli("--workspace", str(sandbox.ws), "attach", str(nvim_proc.pid), socket=False)
        assert f"pid {nvim_proc.pid}" in ok(r)
        for lhs in ("<leader>wm", "<leader>wM"):
            assert nv.call("maparg", lhs, "n") != "" and nv.call("maparg", lhs, "x") != ""
        assert nv.exec_lua("return #_G.nvtour.state.tour.steps") == 0
        assert nv.exec_lua("return _G.nvtour.state.keymaps_installed") is False  # no tour keys
        assert nv.call("maparg", "]w", "n") == ""
    finally:
        ok(cli("--workspace", str(sandbox.ws), "attach", "--clear", socket=False))


def test_a_taken_mark_key_is_skipped_with_one_warning(cli, nv, notes):
    fresh_runtime(nv)
    nv.command("nnoremap <leader>wM <Nop>")
    try:
        ok(cli("status"))
        reload_runtime(nv, "v2")  # an upgrade does not warn again
        warned = [m for m in notes() if "<leader>wM" in m]
        assert len(warned) == 1 and "skipping 'mark_note'" in warned[0]
        assert nv.call("maparg", "<leader>wM", "n") == "<Nop>"  # the key of the user stays
        assert nv.call("maparg", "<leader>wm", "n") != ""
        assert json.loads(ok(cli("--json", "status")))["mark_keys"] == {"mark": "<leader>wm"}
    finally:
        nv.command("nunmap <leader>wM")
        fresh_runtime(nv)
        ok(cli("status"))
    assert nv.call("maparg", "<leader>wM", "n") != ""


def test_the_mark_keys_can_be_renamed_or_turned_off(cli, nv, notes):
    nv.exec_lua("vim.g.nvtour_keys = { mark = '<leader>wx', mark_note = '' }")
    fresh_runtime(nv)
    try:
        ok(cli("status"))
        assert nv.call("maparg", "<leader>wx", "n") != ""
        assert nv.call("maparg", "<leader>wm", "n") == "" and nv.call("maparg", "<leader>wM", "n") == ""
        open_file(nv, "a.txt", 3)
        press(nv, "\\wx")
        assert marks_json(cli)["count"] == 1
    finally:
        nv.exec_lua("vim.g.nvtour_keys = nil; pcall(vim.keymap.del, { 'n', 'x' }, '<leader>wx')")
        fresh_runtime(nv)
        ok(cli("status"))


def test_an_upgrade_keeps_the_marks_and_installs_the_keys_once(cli, nv, notes):
    ok(cli("status"))
    buf = open_file(nv, "a.txt", 3)
    press(nv, MARK)
    ok(cli("start", "U"))
    reload_runtime(nv, "v3")
    reload_runtime(nv, "v4")
    assert marks_json(cli)["count"] == 1 and len(signs(nv, buf)) == 1
    maps = [k for k in nv.api.get_keymap("n") if k["lhs"] in ("\\wm", "\\wM")]
    assert len(maps) == 2
    assert json.loads(ok(cli("--json", "status")))["keys"]["next"] == "]w"  # the tour keys are back too
    assert [m for m in notes() if "already mapped" in m] == []
    ok(cli("clear"))
    open_file(nv, "a.txt", 3)
    press(nv, MARK)
    assert marks_json(cli)["count"] == 2


def test_marks_never_write_a_file(cli, nv, notes, sandbox):
    before = (sandbox.ws / "a.txt").read_bytes()
    ok(cli("status"))
    nv.exec_lua("vim.ui.input = function(_, cb) cb('note') end")
    buf = open_file(nv, "a.txt", 3)
    press(nv, "3GVjj" + MARK)
    press(nv, MARK_NOTE)
    nv.api.buf_set_lines(buf.handle, 0, 1, False, [])
    ok(cli("marks", "--clear"))
    assert (sandbox.ws / "a.txt").read_bytes() == before
    nv.command("edit! a.txt")
    assert buf.options["modified"] is False


def test_format_marks_ages_and_empty_list():
    from nvtour import cli

    assert cli.format_marks({"marks": []}, "/w") == "no marks"
    res = {"marks": [
        {"n": 1, "file": "/w/a.cpp", "l1": 3, "l2": 3, "age": 59, "text": ["x"]},
        {"n": 2, "file": "/other/b.cpp", "ref": "v1", "l1": 3, "l2": 5, "age": 7300, "comment": "two\nlines", "text": []},
        {"n": 3, "file": "/w/c.cpp", "l1": 1, "l2": 2, "age": 120, "deleted": True, "text": ["a", "b"], "truncated": True},
    ]}
    assert cli.format_marks(res, "/w").splitlines() == [
        "1. a.cpp:3  (59 s ago)", "     x",
        '2. /other/b.cpp:3-5 @v1  (2 h ago)  "two lines"',
        "3. c.cpp:1-2  (lines deleted)  (2 min ago)", "     a", "     b", "     ...",
    ]

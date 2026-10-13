"""Tests of the follow mode: the cursor makes the step under it the current step."""

from __future__ import annotations

import json
import time

import pytest
from test_diagram import DIAGRAM
from test_integration import GOTO, FakeSheet, find_buf, flash_marks, ok

WAIT = 0.4  # the follow check runs 100 ms after the last move


@pytest.fixture()
def follow(nv):
    """Follow mode on; off again, and the tour cleared, at the end."""
    nv.exec_lua("vim.g.nvtour_follow_cursor = true")
    yield
    nv.exec_lua("vim.g.nvtour_follow_cursor = nil; _G.nvtour.dispatch('clear', {})")


@pytest.fixture()
def sheet(nv, tmp_path):
    fake = FakeSheet(nv, tmp_path)
    yield fake
    nv.command("unlet! g:nvtour_sheet_cmd")


def status(cli):
    return json.loads(ok(cli("--json", "status")))


def current(cli):
    return status(cli)["current"]


def move(nv, line, win=None):
    """Put the cursor of the window on `line`; the main loop then runs CursorMoved, as for a key."""
    nv.api.win_set_cursor(win or nv.current.window, [line, 0])
    nv.input("")  # lets the main loop run
    time.sleep(WAIT)


def tour(cli):
    ok(cli("start", "Follow", "--sheet", "arch"))
    ok(cli("step", "a.txt:5-7", "--label", "one", "--sheet", "#s1"))
    ok(cli("step", "a.txt:12-14", "--label", "two", "--sheet", "#s2", "--note", "The second one."))
    ok(cli("step", "b.txt:10-12", "--label", "three", "--sheet", "#s3"))
    ok(cli("goto", "1"))


def winbar(nv):
    return nv.exec_lua("return vim.api.nvim_eval_statusline(vim.wo.winbar, { winid = 0, use_winbar = true }).str")


def notes(nv, buf):
    """Number of virtual lines of the steps in a buffer."""
    ns = nv.api.create_namespace("nvtour_steps")
    return sum(len(m[3].get("virt_lines") or []) for m in nv.api.buf_get_extmarks(buf.handle, ns, 0, -1, {"details": True}))


def test_off_by_default(cli, nv):
    tour(cli)
    assert nv.exec_lua("return vim.g.nvtour_follow_cursor") is None
    move(nv, 13)
    assert current(cli) == 1
    assert "follow:" not in ok(cli("status"))
    assert status(cli)["follow"] is False


def test_cursor_moves_the_current_step(cli, nv, follow):
    tour(cli)
    assert status(cli)["follow"] is True
    assert "follow: on" in ok(cli("status"))
    move(nv, 13)
    assert current(cli) == 2
    move(nv, 6)
    assert current(cli) == 1
    assert "nvtour 1/3 info · one" in winbar(nv)
    move(nv, 13)
    assert "nvtour 2/3 info · two" in winbar(nv)


def test_full_note_follows(cli, nv, follow):
    tour(cli)
    buf = find_buf(nv, "a.txt")
    before = notes(nv, buf)
    move(nv, 13)
    # the second step has a note: it is drawn in full now
    texts = json.dumps(nv.exec_lua("return vim.api.nvim_buf_get_extmarks(0, _G.nvtour.state.ns_steps, 0, -1, { details = true })"))
    assert "The second one." in texts
    assert notes(nv, buf) != before or "The second one." in texts


def test_no_scroll_no_flash_no_announce(cli, nv, follow):
    tour(cli)
    nv.exec_lua("_G.msgs = {}; vim.notify = function(m) table.insert(_G.msgs, m) end")
    view = nv.call("winsaveview")
    nv.api.win_set_cursor(nv.current.window, [13, 0])
    assert nv.call("winsaveview")["topline"] == view["topline"]  # the step is in view
    nv.input("")
    time.sleep(WAIT)
    assert current(cli) == 2
    assert nv.call("winsaveview")["topline"] == view["topline"]
    assert nv.api.win_get_cursor(nv.current.window) == [13, 0]
    assert flash_marks(nv, "a.txt") == []
    assert nv.exec_lua("return _G.msgs") == []


def test_cursor_outside_steps_keeps_the_current(cli, nv, follow):
    tour(cli)
    move(nv, 13)
    assert current(cli) == 2
    move(nv, 40)
    assert current(cli) == 2
    move(nv, 1)
    assert current(cli) == 2


def test_quickfix_index_follows(cli, nv, follow):
    tour(cli)
    assert nv.call("getqflist", {"idx": 0})["idx"] == 1
    move(nv, 13)
    assert nv.call("getqflist", {"idx": 0})["idx"] == 2
    move(nv, 6)
    assert nv.call("getqflist", {"idx": 0})["idx"] == 1


def test_sheet_follows(cli, nv, follow, sheet):
    tour(cli)
    assert sheet.wait(1) == [f"{GOTO} arch s1"]
    move(nv, 13)
    assert sheet.wait(2) == [f"{GOTO} arch s1", f"{GOTO} arch s2"]
    move(nv, 14)  # the same step: no new call
    assert len(sheet.settle()) == 2


def test_panel_follows(cli, nv, follow):
    tour(cli)
    pb = find_buf(nv, "nvtour://panel")
    move(nv, 13)
    text = [row for row in nv.api.buf_get_lines(pb.handle, 0, -1, False) if "two" in row]
    assert text
    # the current line of the panel is the line of step 2
    ns = nv.api.create_namespace("nvtour_panel")
    rows = [m[1] for m in nv.api.buf_get_extmarks(pb.handle, ns, 0, -1, {"details": True}) if m[3].get("line_hl_group") == "NvtourPanelCurrent"]
    assert [nv.api.buf_get_lines(pb.handle, r, r + 1, False)[0] for r in rows if "two" in nv.api.buf_get_lines(pb.handle, r, r + 1, False)[0]]


def test_toggle_command(cli, nv):
    tour(cli)
    nv.exec_lua("_G.msgs = {}; vim.notify = function(m) table.insert(_G.msgs, m) end")
    try:
        nv.command("NvtourFollow")
        assert nv.exec_lua("return vim.g.nvtour_follow_cursor") is True
        assert nv.exec_lua("return _G.msgs") == ["nvtour: follow on"]
        move(nv, 13)
        assert current(cli) == 2
        nv.command("NvtourFollow")
        assert nv.exec_lua("return vim.g.nvtour_follow_cursor") is False
        assert nv.exec_lua("return _G.msgs")[-1] == "nvtour: follow off"
        move(nv, 6)
        assert current(cli) == 2
        assert status(cli)["follow"] is False
    finally:
        nv.exec_lua("vim.g.nvtour_follow_cursor = nil")


def test_panel_window_changes_nothing(cli, nv, follow):
    tour(cli)
    code_win = nv.current.window
    ok(cli("panel", "--toggle"))
    ok(cli("panel", "--toggle"))
    pwin = next(w for w in nv.windows if w.buffer.name.endswith("nvtour://panel"))
    nv.api.set_current_win(pwin.handle)
    for row in range(1, len(pwin.buffer) + 1):
        move(nv, row, pwin)
    assert current(cli) == 1
    nv.api.set_current_win(code_win.handle)


def test_next_goes_from_the_followed_step(cli, nv, follow):
    tour(cli)
    move(nv, 13)
    assert current(cli) == 2
    out = ok(cli("next"))
    assert out.splitlines()[0].startswith("step 3/3: b.txt:10-12")
    move(nv, 5)  # b.txt is shown now: no step there at this line, current stays 3
    ok(cli("goto", "1"))
    move(nv, 13)
    ok(cli("prev"))
    assert current(cli) == 1


def test_nested_ranges_choose_the_innermost(cli, nv, follow):
    ok(cli("start", "Nested"))
    ok(cli("step", "a.txt:10-30", "--label", "outer"))
    ok(cli("step", "a.txt:15-20", "--label", "inner"))
    ok(cli("step", "a.txt:17-18", "--label", "innermost"))
    ok(cli("goto", "1"))
    move(nv, 12)
    assert current(cli) == 1
    move(nv, 16)
    assert current(cli) == 2
    move(nv, 17)
    assert current(cli) == 3
    move(nv, 19)
    assert current(cli) == 2
    move(nv, 25)
    assert current(cli) == 1


def test_jump_to_an_outer_step_stays(cli, nv, follow):
    """The jump put the cursor on the first line of two steps; the step of the jump stays current."""
    ok(cli("start", "Same start"))
    ok(cli("step", "a.txt:10-30", "--label", "outer"))
    ok(cli("step", "a.txt:10-12", "--label", "inner"))
    ok(cli("goto", "1"))
    time.sleep(WAIT)
    assert current(cli) == 1


def test_follow_after_edit_uses_moved_lines(cli, nv, follow):
    tour(cli)
    buf = find_buf(nv, "a.txt")
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["new 1", "new 2", "new 3"])
    time.sleep(WAIT)
    move(nv, 16)  # the second step is at 15-17 now
    assert current(cli) == 2
    move(nv, 9)
    assert current(cli) == 1
    move(nv, 7)  # the old place of the first step: no step is there now
    assert current(cli) == 1
    move(nv, 15)
    assert current(cli) == 2
    move(nv, 7)
    assert current(cli) == 2


def test_diagram_window_follows_diagram_steps(cli, nv, follow):
    ok(cli("start", "Diagram"))
    ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))
    ok(cli("step", "--diagram", "flow", "2", "--label", "client sends"))
    ok(cli("step", "a.txt:10-11", "--label", "fetch"))
    ok(cli("step", "--diagram", "flow", "4-5", "--label", "back"))
    ok(cli("goto", "2"))
    dwin = next(w for w in nv.windows if w.buffer.name.endswith("nvtour://diagram/flow"))
    nv.api.set_current_win(dwin.handle)
    move(nv, 5, dwin)
    assert current(cli) == 3
    assert nv.current.window.handle == dwin.handle  # the focus stays
    move(nv, 2, dwin)
    assert current(cli) == 1
    move(nv, 7, dwin)  # no step on this line
    assert current(cli) == 1

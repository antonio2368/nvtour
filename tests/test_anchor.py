"""Tests of steps that follow the edits of the user (anchors)."""

from __future__ import annotations

import json
import time

from test_diagram import DIAGRAM
from test_integration import find_buf, lines_of, ns_marks, ok

SETTLE = 0.45  # the sync runs 150 ms after the last edit


def settle():
    time.sleep(SETTLE)


def status(cli):
    return json.loads(ok(cli("--json", "status")))


def a_buf(nv):
    return find_buf(nv, "a.txt")


def steps_of(nv, buf):
    """(row, ...) of the number highlights of the steps in a buffer: one per line of a drawn range."""
    return sorted(m[1] for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("number_hl_group"))


def make_lines(tmp_path, name, count=20, word="alpha"):
    path = tmp_path / name
    path.write_text("".join(f"{word} {i}\n" for i in range(1, count + 1)))
    return path


def test_insert_above_moves_the_step(cli, nv):
    ok(cli("start", "Move"))
    ok(cli("step", "a.txt:5-7", "--label", "one"))
    ok(cli("step", "a.txt:10-12", "--label", "two"))
    buf = a_buf(nv)
    nv.api.buf_set_lines(buf.handle, 7, 7, True, ["new 1", "new 2", "new 3"])
    settle()
    data = status(cli)
    assert [(s["l1"], s["l2"]) for s in data["steps"]] == [(5, 7), (13, 15)]
    assert [(s["orig_l1"], s["orig_l2"]) for s in data["steps"]] == [(5, 7), (10, 12)]
    assert data["steps"][1]["modified"] is True and data["modified_files"] == [str(buf.name)]
    text = ok(cli("status"))
    assert "  2. a.txt:13-15 [info] two  (was 10-12)" in text
    assert "  ▶ 1. a.txt:5-7 [info] one\n" in text
    assert "modified: a.txt" in text.splitlines()
    qf = nv.call("getqflist", {"items": 0})["items"]
    assert [(i["lnum"], i["end_lnum"]) for i in qf] == [(5, 7), (13, 15)]
    eol = [m for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("virt_text_pos") == "eol"]
    assert sorted(m[1] for m in eol) == [4, 12]  # the "← N label" markers moved with the lines
    assert steps_of(nv, buf)[-3:] == [12, 13, 14]
    out = ok(cli("goto", "2"))
    assert out.splitlines()[0] == "step 2/2: a.txt:13-15 [info] two"
    assert out.splitlines()[1] == "  13| line 10 of a"
    assert nv.current.window.cursor[0] == 13
    pb = find_buf(nv, "nvtour://panel")
    assert any("2. •" in line and "13-15" in line for line in lines_of(nv, pb.handle))


def test_gravity_of_the_ends(cli, nv):
    ok(cli("start", "Gravity"))
    ok(cli("step", "a.txt:20-22", "--label", "range"))
    ok(cli("step", "b.txt:1", "--label", "other"))
    ok(cli("goto", "1"))
    nv.command("normal! 22Go")  # a line after the last line of the range
    nv.command("normal! 20GO")  # a line before the first
    settle()
    assert [(s["l1"], s["l2"]) for s in status(cli)["steps"]][0] == (21, 23)
    buf = a_buf(nv)
    assert steps_of(nv, buf) == [20, 21, 22]
    ok(cli("goto", "1"))
    assert nv.current.window.cursor[0] == 21


def test_deleting_the_range_loses_the_step_and_undo_restores_it(cli, nv):
    ok(cli("start", "Lost"))
    ok(cli("step", "a.txt:3", "--label", "first"))
    ok(cli("step", "a.txt:10-12", "--label", "middle", "--note", "a note"))
    ok(cli("step", "a.txt:20", "--label", "last"))
    ok(cli("goto", "1"))
    buf = a_buf(nv)
    nv.api.buf_set_lines(buf.handle, 9, 12, True, [])
    settle()
    data = status(cli)
    assert [bool(s.get("lost")) for s in data["steps"]] == [False, True, False]
    assert data["steps"][1]["lost_reason"] == "lines deleted"
    assert "  2. a.txt:10-12 [info] middle  (lost: lines deleted)" in ok(cli("status"))
    assert [(s["l1"], s["l2"]) for s in data["steps"]][2] == (17, 17)
    assert steps_of(nv, buf) == [2, 16]  # no marks for the lost step
    assert not any(m[3].get("virt_lines") for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[1] == 9)
    pb = find_buf(nv, "nvtour://panel")
    lost_line = [line for line in lines_of(nv, pb.handle) if "2. •" in line][0]
    assert "✗ lost" in lost_line
    lost_marks = [m for m in ns_marks(nv, "nvtour_panel", pb.handle) if m[3].get("hl_group") == "NvtourLost"]
    assert len(lost_marks) == 1
    qf = nv.call("getqflist", {"items": 0})["items"]
    assert qf[1]["text"].endswith("(lost)")
    r = cli("goto", "2")
    assert r.returncode == 6
    assert "step 2: its lines were deleted; re-point it with: nvtour edit 2 FILE:L1-L2" in r.stderr
    r = cli("next")  # ]w skips the lost step
    assert r.returncode == 0 and r.stdout.splitlines()[0] == "step 3/3: a.txt:17 [info] last"
    assert "step 2 is lost" in r.stderr
    assert ok(cli("prev")).splitlines()[0] == "step 1/3: a.txt:3 [info] first"
    nv.command("undo")
    settle()
    data = status(cli)
    assert not any(s.get("lost") for s in data["steps"])
    assert [(s["l1"], s["l2"]) for s in data["steps"]] == [(3, 3), (10, 12), (20, 20)]
    assert steps_of(nv, buf) == [2, 9, 10, 11, 19]


def test_edit_inside_the_range_grows_it(cli, nv):
    ok(cli("start", "Inside"))
    ok(cli("step", "a.txt:10-12", "--expect", "line 11", "--role", "fault", "--note", "inside"))
    buf = a_buf(nv)
    nv.api.buf_set_lines(buf.handle, 11, 11, True, ["inserted line"])  # between lines 11 and 12
    settle()
    step = status(cli)["steps"][0]
    assert (step["l1"], step["l2"]) == (10, 13)
    assert steps_of(nv, buf) == [9, 10, 11, 12]  # the number highlight covers the new line
    marks = [m for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("hl_group") == "NvtourMarkFault"]
    assert [(m[1], m[2], m[3]["end_col"]) for m in marks] == [(10, 0, len("line 11"))]
    assert lines_of(nv, buf.handle)[10] == "line 11 of a"  # the underline is on the text it was made for
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["top"])  # a move of the whole range moves the underline too
    settle()
    marks = [m for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("hl_group") == "NvtourMarkFault"]
    assert [m[1] for m in marks] == [11] and lines_of(nv, buf.handle)[11] == "line 11 of a"


def test_edit_without_location_keeps_the_synced_range(cli, nv):
    ok(cli("start", "Edit"))
    ok(cli("step", "a.txt:5-6", "--label", "first"))
    ok(cli("step", "a.txt:10-12", "--label", "second"))
    buf = a_buf(nv)
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["top 1", "top 2", "top 3"])
    out = ok(cli("edit", "2", "--note", "x"))  # no waiting: edit syncs first
    assert out.splitlines()[0] == "step 2/2: a.txt:13-15 [info] second"
    data = status(cli)
    assert [(s["l1"], s["l2"], s.get("note")) for s in data["steps"]][1] == (13, 15, "x")
    assert steps_of(nv, buf)[-3:] == [12, 13, 14]
    out = ok(cli("edit", "2", "a.txt:30-31"))  # a new location replaces the lines and the original range
    assert out.splitlines()[0] == "step 2/2: a.txt:30-31 [info] second"
    step = status(cli)["steps"][1]
    assert (step["orig_l1"], step["orig_l2"]) == (30, 31) and "was" not in ok(cli("status")).splitlines()[2]


def test_reload_anchors_again_and_checks_expect(cli, nv, tmp_path):
    path = make_lines(tmp_path, "sandbox.txt")
    ok(cli("start", "Reload"))
    ok(cli("step", f"{path}:5-7", "--expect", "alpha 6", "--label", "keeps"))
    ok(cli("step", f"{path}:10-11", "--expect", "alpha 10", "--label", "goes"))
    ok(cli("step", f"{path}:15", "--label", "end"))
    buf = find_buf(nv, "sandbox.txt")
    # An external change: line 1 differs and "alpha 10" is gone (every line below moves up by one).
    lines = path.read_text().splitlines()
    lines[0] = "changed"
    del lines[9]
    path.write_text("\n".join(lines) + "\n")
    nv.command(f"buffer {buf.handle}")
    nv.command("edit!")
    settle()
    data = status(cli)
    keeps, goes, end = data["steps"]
    assert not keeps.get("lost") and (keeps["l1"], keeps["l2"]) == (5, 7)
    assert goes["lost"] is True and "--expect" in goes["lost_reason"] and "alpha 10" in goes["lost_reason"]
    assert not end.get("lost") and (end["l1"], end["l2"]) == (15, 15)
    assert "(lost: --expect" in ok(cli("status"))
    assert steps_of(nv, buf) == [4, 5, 6, 14]
    # A later edit still moves the re-anchored steps.
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["top"])
    settle()
    assert [(s["l1"], s["l2"]) for s in status(cli)["steps"]][0] == (6, 8)
    # A file that is shorter than the range: lost with the range as the reason.
    path.write_text("short 1\nshort 2\n")
    nv.command("edit!")
    settle()
    data = status(cli)
    assert all(s["lost"] for s in data["steps"])
    assert "beyond the end of the file (2 lines)" in data["steps"][0]["lost_reason"]
    assert steps_of(nv, buf) == []


def test_an_edit_syncs_only_its_own_buffer(cli, nv, tmp_path):
    paths = [make_lines(tmp_path, f"many{i}.txt", 12) for i in range(10)]
    ok(cli("start", "Many"))
    for p in paths:
        ok(cli("step", f"{p}:5-6", "--no-jump"))
    ok(cli("goto", "10"))
    bufs = [find_buf(nv, p.name) for p in paths]

    # Count the extmarks that are put in each buffer in the namespace of the step marks.
    nv.exec_lua(
        """
        local ns = vim.api.nvim_create_namespace("nvtour_steps")
        local orig = vim.api.nvim_buf_set_extmark
        _G.spy = { counts = {}, orig = orig }
        vim.api.nvim_buf_set_extmark = function(buf, n, ...)
          if n == ns then _G.spy.counts[tostring(buf)] = (_G.spy.counts[tostring(buf)] or 0) + 1 end
          return orig(buf, n, ...)
        end
        """
    )

    def drawn():
        counts = nv.exec_lua("return _G.spy.counts")
        nv.exec_lua("_G.spy.counts = {}")
        counts = counts if isinstance(counts, dict) else {}  # an empty Lua table comes back as a list
        return {b.handle: counts.get(str(b.handle), 0) for b in bufs}

    try:
        drawn()
        nv.api.buf_set_lines(bufs[3].handle, 0, 0, True, ["x", "y"])
        settle()
        counts = drawn()
    finally:
        nv.exec_lua("vim.api.nvim_buf_set_extmark = _G.spy.orig")
    assert counts.pop(bufs[3].handle) > 0  # the edited buffer is drawn again
    assert set(counts.values()) == {0}  # no other buffer is
    assert [(s["l1"], s["l2"]) for s in status(cli)["steps"]][3] == (7, 8)
    assert steps_of(nv, bufs[3]) == [6, 7]
    # Typing in a burst is one sync.
    for _ in range(5):
        nv.api.buf_set_lines(bufs[5].handle, 0, 0, True, ["z"])
    settle()
    assert [(s["l1"], s["l2"]) for s in status(cli)["steps"]][5] == (10, 11)


def test_clear_detaches(cli, nv):
    ok(cli("start", "Detach"))
    ok(cli("step", "a.txt:5-6"))
    buf = a_buf(nv)
    assert nv.exec_lua("return _G.nvtour.state.attached[...] == true", buf.handle)
    ok(cli("clear", "--keep-buffers"))
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["after clear"])
    settle()
    assert ns_marks(nv, "nvtour_steps", buf.handle) == []
    assert ns_marks(nv, "nvtour_anchor", buf.handle) == []
    assert nv.exec_lua("return next(_G.nvtour.state.attached) == nil and next(_G.nvtour.state.sync_timers) == nil")
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["again"])  # no callback is left to fail
    settle()
    assert ok(cli("status")).startswith("tour: Walkthrough (0 step(s)")


def test_a_step_of_an_older_runtime_gets_an_anchor(cli, nv):
    ok(cli("start", "Upgrade"))
    ok(cli("step", "a.txt:10-11", "--label", "old"))
    ok(cli("step", "a.txt:20", "--label", "other"))
    buf = a_buf(nv)
    # Forget what the new runtime added, as if the steps came from the old one.
    nv.exec_lua(
        """
        local S = _G.nvtour.state
        vim.api.nvim_buf_clear_namespace(0, S.ns_anchor, 0, -1)
        for _, s in ipairs(S.tour.steps) do s.anchor, s.anchor_buf, s.tail, s.orig_l1, s.orig_l2 = nil end
        """
    )
    data = status(cli)
    assert [(s["l1"], s["l2"]) for s in data["steps"]] == [(10, 11), (20, 20)]  # nil fields are fine
    ok(cli("edit", "2", "--label", "other"))  # rerender_all makes the anchors
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["top"])
    settle()
    data = status(cli)
    assert [(s["l1"], s["l2"]) for s in data["steps"]] == [(11, 12), (21, 21)]
    assert [(s["orig_l1"], s["orig_l2"]) for s in data["steps"]] == [(10, 11), (20, 20)]


def test_modified_files_only_lists_changed_buffers(cli, nv):
    ok(cli("start", "Modified"))
    ok(cli("step", "a.txt:5"))
    ok(cli("step", "b.txt:5"))
    text = ok(cli("status"))
    assert "modified:" not in text and "modified_files" not in status(cli)
    nv.api.buf_set_lines(find_buf(nv, "b.txt").handle, 0, 0, True, ["x"])
    settle()
    data = status(cli)
    assert [s["modified"] for s in data["steps"]] == [False, True]
    assert "modified: b.txt" in ok(cli("status")).splitlines()


def test_focus_folds_follow_the_edits(cli, nv):
    ok(cli("focus", "a.txt:20-22", "--context", "1"))
    buf = a_buf(nv)
    win = nv.current.window
    assert nv.call("foldclosed", 10) == 1 and nv.call("foldclosed", 19) == -1
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["t1", "t2", "t3"])
    settle()
    assert nv.call("foldclosed", 10) == 1 and nv.call("foldclosed", 21) == 1  # the lines above the range
    assert nv.call("foldclosed", 22) == -1 and nv.call("foldclosed", 26) == -1  # the range and its context
    assert nv.call("foldclosed", 27) == 27
    assert json.loads(ok(cli("--json", "status")))["focus"][0]["ranges"] == [[22, 26]]
    assert win == nv.current.window


def test_diagram_step_with_missing_expect_is_lost(cli, nv):
    ok(cli("start", "Diagram"))
    ok(cli("diagram", "flow", "--format", "text", "--no-show", stdin=DIAGRAM))
    ok(cli("step", "--diagram", "flow", "7", "--expect", "Store", "--no-jump"))
    changed = DIAGRAM.replace("Store  ", "Disk   ")
    ok(cli("diagram", "flow", "--format", "text", "--no-show", stdin=changed))
    step = status(cli)["steps"][0]
    assert step["lost"] is True and "Store" in step["lost_reason"]
    assert "(lost: --expect" in ok(cli("status"))
    ok(cli("diagram", "flow", "--format", "text", "--no-show", stdin=DIAGRAM))  # the text is back
    assert not status(cli)["steps"][0].get("lost")


def test_a_wiped_buffer_is_anchored_again(cli, nv):
    ok(cli("start", "Wipe"))
    ok(cli("step", "a.txt:10-12", "--label", "one"))
    ok(cli("step", "a.txt:20", "--label", "two"))
    nv.command(f"bwipeout! {a_buf(nv).handle}")
    ok(cli("goto", "2"))  # the file is loaded again; the steps get new anchors
    buf = a_buf(nv)
    nv.api.buf_set_lines(buf.handle, 0, 0, True, ["top"])
    settle()
    assert [(s["l1"], s["l2"]) for s in status(cli)["steps"]] == [(11, 13), (21, 21)]
    assert steps_of(nv, buf) == [10, 11, 12, 20]

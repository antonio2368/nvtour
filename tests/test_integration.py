"""Integration tests against a private headless nvim via the real CLI."""

from __future__ import annotations

import json
import os

import pytest


def ns_marks(nv, name, buf=0):
    """Extmarks (with details) in a namespace."""
    ns = nv.api.create_namespace(name)
    return nv.api.buf_get_extmarks(buf, ns, 0, -1, {"details": True})


def ok(r):
    assert r.returncode == 0, (r.stdout, r.stderr)
    return r.stdout


def lines_of(nv, buf):
    return list(nv.api.buf_get_lines(buf, 0, -1, False))


def find_buf(nv, suffix):
    for b in nv.buffers:
        if b.name.endswith(suffix):
            return b
    return None


def test_step_creates_marks_and_jumps(cli, nv, sandbox):
    out = ok(cli("start", "T1"))
    assert out.strip() == "started: T1"
    out = ok(cli("step", "a.txt:5-7", "--role", "fault", "--label", "bad", "--note", "Explains the bug in a few words."))
    assert out.strip() == "step 1/1: a.txt:5-7 [fault] bad"
    buf = find_buf(nv, "a.txt")
    assert buf is not None
    marks = ns_marks(nv, "nvtour_steps", buf.handle)
    details = [m[3] for m in marks]
    assert sum(1 for d in details if d.get("line_hl_group") == "NvtourLineFault") == 3
    sign = [d for d in details if d.get("sign_text")]
    assert len(sign) == 1 and sign[0]["sign_text"].strip() == "1"
    assert any(d.get("virt_lines") and d.get("virt_lines_above") for d in details)
    assert any(d.get("virt_text") and d.get("virt_text_pos") == "eol" for d in details)
    assert nv.current.buffer.name.endswith("a.txt")
    assert nv.current.window.cursor[0] == 5
    assert nv.eval("maparg(']w', 'n')") != ""


def test_wrapped_note_uses_border_prefixes(cli, nv):
    note = "word " * 60
    ok(cli("step", "a.txt:2", "--note", note))
    buf = find_buf(nv, "a.txt")
    vl = [m[3]["virt_lines"] for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("virt_lines")][0]
    assert len(vl) > 1
    assert vl[0][0][0].startswith("╭") and vl[-1][0][0].startswith("╰")


def test_nav_and_quickfix(cli, nv):
    ok(cli("start", "Nav"))
    ok(cli("step", "a.txt:3-4", "--role", "fault", "--label", "first"))
    ok(cli("step", "b.txt:9", "--role", "fix", "--note", "fix note"))
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 2
    assert ok(cli("prev")).strip() == "step 1/2: a.txt:3-4 [fault] first"
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 1
    assert "already at first step" in ok(cli("prev"))
    ok(cli("goto", "2"))
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 2
    assert nv.current.buffer.name.endswith("b.txt") and nv.current.window.cursor[0] == 9
    assert "already at last step" in ok(cli("next"))
    qf = nv.call("getqflist", {"title": 0, "items": 0, "idx": 0})
    assert qf["title"] == "nvtour: Nav"
    assert [(i["lnum"], i["end_lnum"], i["text"], i["type"]) for i in qf["items"]] == [
        (3, 4, "first", "f"),
        (9, 9, "fix note", "f"),
    ]
    assert qf["idx"] == 2
    bad = cli("goto", "9")
    assert bad.returncode == 6


def test_panel_content_and_mappings(cli, nv):
    ok(cli("start", "Panel title"))
    ok(cli("step", "a.txt:3-4", "--label", "alpha step"))
    ok(cli("step", "b.txt:9", "--label", "beta step"))
    ok(cli("panel", "free text here"))
    pb = find_buf(nv, "nvtour://panel")
    assert pb is not None
    text = "\n".join(lines_of(nv, pb.handle))
    assert "# Panel title" in text
    assert "alpha step" in text and "beta step" in text and "a.txt:3-4" in text
    assert "free text here" in text and "---" in text
    assert "▶ 2." in text
    maps = nv.api.buf_get_keymap(pb.handle, "n")
    assert "<CR>" in [m["lhs"] for m in maps]
    assert "q" in [m["lhs"] for m in maps]
    assert pb.options["modifiable"] is False
    assert ok(cli("panel", "--toggle")).strip() == "panel: hidden"
    assert ok(cli("panel", "--toggle")).strip() == "panel: shown"
    assert ok(cli("panel", "-", stdin="from stdin\n")).strip() == "panel: updated"
    assert "from stdin" in "\n".join(lines_of(nv, pb.handle))


def test_panel_enter_jumps_to_step(cli, nv):
    ok(cli("start", "Enter"))
    ok(cli("step", "a.txt:3-4", "--label", "one"))
    ok(cli("step", "b.txt:9", "--label", "two"))
    pb = find_buf(nv, "nvtour://panel")
    win = [w for w in nv.windows if w.buffer.handle == pb.handle][0]
    nv.current.window = win
    win.cursor = (3, 0)  # step 1 line
    nv.input("<CR>")
    nv.command("sleep 50m")
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 1
    assert nv.current.buffer.name.endswith("a.txt")


def test_focus_fold_and_unfocus(cli, nv):
    nv.command("edit a.txt")
    win = nv.current.window
    before = nv.eval("&foldmethod")
    out = ok(cli("focus", "a.txt:20-22", "40-41", "--context", "1"))
    assert out.strip() == "focus: a.txt 19-23 39-42 (fold)"

    def closed(l):
        return nv.eval(f"foldclosed({l})")

    assert closed(5) != -1 and closed(30) != -1 and closed(50) != -1
    assert closed(20) == -1 and closed(22) == -1 and closed(40) == -1 and closed(19) == -1
    assert nv.eval("&foldmethod") == "manual"
    ok(cli("unfocus"))
    assert nv.eval("&foldmethod") == before
    assert closed(5) == -1


def test_focus_dim(cli, nv):
    ok(cli("focus", "a.txt:10-12", "--dim", "--context", "0"))
    buf = find_buf(nv, "a.txt")
    rows = sorted(m[1] for m in ns_marks(nv, "nvtour_focus", buf.handle) if m[3].get("hl_group") == "NvtourDim")
    assert len(rows) == 60 - 3
    assert 9 not in rows and 11 not in rows and 8 in rows
    ok(cli("unfocus", "a.txt"))
    assert ns_marks(nv, "nvtour_focus", buf.handle) == []


def test_diff_stdin_and_close(cli, nv):
    tabs = len(nv.tabpages)
    ok(cli("diff", "a.txt", "--stdin", "--title", "proposed", stdin="one\ntwo\n"))
    assert len(nv.tabpages) == tabs + 1
    wins = nv.current.tabpage.windows
    assert len(wins) == 2
    assert all(nv.api.win_get_option(w.handle, "diff") for w in wins)
    names = [w.buffer.name for w in wins]
    assert any(n.startswith("nvtour://diff/proposed/") for n in names)
    scratch = [w.buffer for w in wins if w.buffer.name.startswith("nvtour://")][0]
    assert list(scratch[:]) == ["one", "two"] and scratch.options["modifiable"] is False
    ok(cli("diff-close"))
    assert len(nv.tabpages) == tabs
    assert find_buf(nv, "nvtour://diff/proposed/a.txt") is None


def test_diff_ref(cli, nv):
    tabs = len(nv.tabpages)
    ok(cli("diff", "b.txt", "--ref", "HEAD"))
    assert len(nv.tabpages) == tabs + 1
    ok(cli("diff-close"))
    r = cli("diff", "b.txt", "--ref", "no-such-ref")
    assert r.returncode == 6


def test_clear_removes_everything(cli, nv):
    tabs = len(nv.tabpages)
    ok(cli("start", "C"))
    ok(cli("step", "a.txt:3-4", "--role", "fault", "--note", "n", "--label", "l"))
    ok(cli("focus", "b.txt:5-6", "--dim"))
    ok(cli("diff", "a.txt", "--stdin", stdin="x\n"))
    assert ok(cli("clear")).strip() == "cleared"
    for b in nv.buffers:
        assert not b.name.startswith("nvtour://")
        for name in ("nvtour_steps", "nvtour_focus"):
            assert ns_marks(nv, name, b.handle) == []
    assert len(nv.tabpages) == tabs
    for lhs in ("]w", "[w"):
        assert nv.eval(f"maparg('{lhs}', 'n')") == ""
    assert nv.eval("maparg('<leader>wp', 'n')") == ""


def test_clear_after_user_closed_things(cli, nv):
    ok(cli("start", "U"))
    ok(cli("step", "a.txt:3"))
    ok(cli("diff", "a.txt", "--stdin", stdin="x\n"))
    nv.command("tabclose")  # user closes the diff tab
    pb = find_buf(nv, "nvtour://panel")
    for w in nv.windows:
        if w.buffer.handle == pb.handle:
            nv.api.win_close(w.handle, True)
    nv.command("bwipeout! " + str(find_buf(nv, "a.txt").handle))  # buffer wiped
    assert ok(cli("clear")).strip() == "cleared"
    assert find_buf(nv, "nvtour://panel") is None


def test_step_no_jump_and_idempotent_window(cli, nv):
    ok(cli("start", "N"))
    ok(cli("step", "a.txt:3"))
    wins = len(nv.windows)
    ok(cli("step", "a.txt:8-9", "--no-jump", "--label", "later"))
    assert len(nv.windows) == wins
    assert nv.current.window.cursor[0] == 3
    ok(cli("step", "a.txt:10"))
    assert len(nv.windows) == wins


def test_bad_inputs_exit_6(cli):
    assert cli("step", "missing.txt:1").returncode == 6
    assert cli("step", "a.txt:9-3").returncode == 6
    assert cli("step", "a.txt:5000").returncode == 6
    assert cli("step", "a.txt").returncode == 6


def test_where_reports_cursor(cli, nv):
    nv.command("edit b.txt")
    nv.current.window.cursor = (7, 3)
    out = ok(cli("where"))
    assert out.startswith("b.txt:7:4")
    data = json.loads(ok(cli("--json", "where")))
    assert data["line"] == 7 and data["col"] == 4 and data["file"].endswith("b.txt")
    nv.command("normal! 2GVj\x1b")
    out = ok(cli("where"))
    assert "selection: 2-3" in out and "line 2 of b" in out


def test_json_output_has_socket_and_pid(cli, nvim_proc):
    data = json.loads(ok(cli("--json", "step", "a.txt:3")))
    assert data["socket"] == nvim_proc.socket and data["pid"] == nvim_proc.pid and data["ok"] is True


def test_discovery_live_and_stale(cli, sandbox, nvim_proc):
    fake = sandbox.runtime / "nvim.999999.0"
    if os.path.exists("/proc/999999"):
        pytest.skip("pid 999999 exists")
    fake.write_text("")
    try:
        r = cli("--workspace", str(sandbox.ws), "--json", "instances", socket=False)
        data = json.loads(ok(r))
        states = sorted(i["state"] for i in data["instances"])
        assert states == ["live", "stale"]
        live = [i for i in data["instances"] if i["state"] == "live"][0]
        assert live["pid"] == nvim_proc.pid and live["score"] == 100
        text = ok(cli("--workspace", str(sandbox.ws), "instances", socket=False))
        assert f"{nvim_proc.pid}  live  100  {sandbox.ws}" in text and "999999  stale" in text
        r = cli("--workspace", str(sandbox.ws), "attach", socket=False)
        assert f"pid {nvim_proc.pid}" in ok(r) and "exact match" in r.stdout
        r = cli("--workspace", str(sandbox.ws), "instances", "--prune", socket=False)
        assert not fake.exists()
        # auto-selection (pin) works without --socket
        r = cli("--workspace", str(sandbox.ws), "where", socket=False)
        ok(r)
        ok(cli("--workspace", str(sandbox.ws), "attach", "--clear", socket=False))
    finally:
        fake.unlink(missing_ok=True)


def test_no_match_exit_codes(cli, sandbox, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = cli("--workspace", str(other), "where", socket=False)
    assert r.returncode == 3 and "nvtour attach" in r.stderr
    empty = tmp_path / "empty_run"
    empty.mkdir()
    r = cli("where", socket=False, env_extra={"XDG_RUNTIME_DIR": str(empty)})
    assert r.returncode == 4 and "no running nvim found" in r.stderr


def test_doctor(cli, sandbox):
    r = cli("--workspace", str(sandbox.ws), "doctor", socket=False)
    assert r.returncode == 0 and "[ok]" in r.stdout and "Lua runtime" in r.stdout


def test_zz_safety_files_untouched(cli, nv, sandbox, pristine):
    """Run a full session, then verify no buffer was modified and no file changed on disk."""
    ok(cli("start", "Safety"))
    ok(cli("step", "a.txt:5-9", "--role", "fault", "--note", "x"))
    before = {b.name: lines_of(nv, b.handle) for b in nv.buffers if b.name.endswith(".txt")}
    assert before, "a.txt buffer should be loaded"
    ok(cli("focus", "a.txt:5-9"))
    ok(cli("diff", "a.txt", "--stdin", stdin="changed\n"))
    ok(cli("diff-close"))
    ok(cli("unfocus"))
    ok(cli("clear"))
    for b in nv.buffers:
        if b.name.endswith(".txt"):
            assert not nv.api.get_option_value("modified", {"buf": b.handle})
            if b.name in before:
                assert lines_of(nv, b.handle) == before[b.name]
    for path, data in pristine.items():
        assert path.read_bytes() == data

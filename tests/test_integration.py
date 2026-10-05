"""Integration tests against a private headless nvim via the real CLI."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time

import pytest

from conftest import clean_env, wait_for_socket


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


def current(nv):
    return nv.exec_lua("return _G.nvtour.state.tour.current")


def test_nav_and_quickfix(cli, nv):
    ok(cli("start", "Nav"))
    ok(cli("step", "a.txt:3-4", "--role", "fault", "--label", "first"))
    ok(cli("step", "b.txt:9", "--role", "fix", "--note", "fix note"))
    assert current(nv) == 1
    assert "already at first step" in ok(cli("prev"))
    assert ok(cli("next")).strip() == "step 2/2: b.txt:9 [fix]"
    assert ok(cli("prev")).strip() == "step 1/2: a.txt:3-4 [fault] first"
    ok(cli("goto", "2"))
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 2
    assert nv.current.buffer.name.endswith("b.txt") and nv.current.window.cursor[0] == 9
    assert "already at last step" in ok(cli("next"))
    qf = nv.call("getqflist", {"title": 0, "items": 0, "idx": 0})
    assert qf["title"] == "nvtour: Nav"
    assert [(i["lnum"], i["end_lnum"], i["text"]) for i in qf["items"]] == [
        (3, 4, "[fault] first"),
        (9, 9, "[fix] fix note"),
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
    assert "▶ 1." in text
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
    for lhs in ("]w", "[w", "]W", "[W"):
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
    assert nv.call("getqflist", {"idx": 0})["idx"] == 1
    ok(cli("step", "a.txt:10", "--jump"))
    assert len(nv.windows) == wins
    assert nv.current.window.cursor[0] == 10 and current(nv) == 3
    assert nv.call("getqflist", {"idx": 0})["idx"] == 3


def test_tour_stays_on_first_step_and_first_last_keys(cli, nv):
    ok(cli("start", "Order"))
    ok(cli("step", "a.txt:3", "--label", "one"))
    data = json.loads(ok(cli("--json", "step", "b.txt:9", "--label", "two")))
    assert data["jumped"] is False
    ok(cli("step", "a.txt:20", "--label", "three"))
    assert current(nv) == 1
    assert nv.current.buffer.name.endswith("a.txt") and nv.current.window.cursor[0] == 3
    assert nv.call("getqflist", {"idx": 0})["idx"] == 1
    assert nv.eval("maparg('[W', 'n')") != "" and nv.eval("maparg(']W', 'n')") != ""
    nv.command("normal ]W")
    assert current(nv) == 3 and nv.current.window.cursor[0] == 20
    nv.command("normal [W")
    assert current(nv) == 1 and nv.current.window.cursor[0] == 3
    nv.command("NvtourLast")
    assert current(nv) == 3
    nv.command("NvtourFirst")
    assert current(nv) == 1
    assert ok(cli("last")).strip() == "step 3/3: a.txt:20 [info] three"
    assert ok(cli("first")).strip() == "step 1/3: a.txt:3 [info] one"
    ok(cli("clear"))
    assert nv.eval("maparg('[W', 'n')") == "" and nv.eval("maparg(']W', 'n')") == ""
    assert cli("first").returncode == 6


def test_prev_before_first_jump_goes_to_step_1(cli, nv):
    ok(cli("start", "P"))
    ok(cli("step", "a.txt:4", "--no-jump"))
    assert current(nv) == 0
    assert ok(cli("prev")).startswith("step 1/1")
    assert current(nv) == 1


def test_modified_unhideable_buffer_is_never_replaced(cli, nv, sandbox):
    nv.command("set autowrite")
    try:
        nv.command("edit b.txt")
        nv.command("setlocal bufhidden=unload")
        nv.current.buffer[0] = "changed in nvim"
        b = nv.current.buffer
        win = nv.current.window
        r = cli("step", "a.txt:3")
        ok(r)
        assert "kept modified buffer" in r.stderr
        assert win.buffer.handle == b.handle
        assert any(w.buffer.name.endswith("a.txt") for w in nv.windows)
        assert nv.api.get_option_value("modified", {"buf": b.handle}) is True
        assert b[0] == "changed in nvim"
        assert (sandbox.ws / "b.txt").read_text().startswith("line 1 of b")
        assert any(w.buffer.handle == b.handle for w in nv.windows)
    finally:
        nv.command("set noautowrite")


def test_failed_jump_adds_no_step(cli, nv):
    ok(cli("start", "F"))
    nv.command("call bufload(bufadd('a.txt'))")  # loaded already: only the jump runs BufWinEnter
    nv.command("autocmd BufWinEnter a.txt lua error('boom from user autocmd')")
    try:
        r = cli("step", "a.txt:3", "--label", "x")
        assert r.returncode == 5 and "boom" in r.stderr
        assert nv.exec_lua("return #_G.nvtour.state.tour.steps") == 0
        buf = find_buf(nv, "a.txt")
        assert ns_marks(nv, "nvtour_steps", buf.handle) == []
    finally:
        nv.command("autocmd! BufWinEnter a.txt")
    assert ok(cli("step", "a.txt:3")).startswith("step 1/1")


def test_terminal_window_is_left_alone(cli, nv):
    nv.command("edit b.txt")
    nv.command("botright split | terminal")
    term = nv.current.window
    term_buf = term.buffer.handle
    assert nv.api.get_option_value("buftype", {"buf": term_buf}) == "terminal"
    ok(cli("start", "Term"))
    ok(cli("step", "a.txt:6"))
    assert term.buffer.handle == term_buf
    assert nv.current.window.handle == term.handle  # the terminal keeps the focus
    shown = [w for w in nv.windows if w.buffer.name.endswith("a.txt")]
    assert shown and shown[0].cursor[0] == 6
    data = json.loads(ok(cli("--json", "where")))
    assert data["file"].endswith("a.txt") and data["current_window"] is False
    nv.command("bwipeout! " + str(term_buf))


def test_unfocus_restores_only_the_focused_buffer(cli, nv):
    nv.command("edit a.txt")
    win = nv.current.window
    before = nv.eval("&l:foldmethod")
    ok(cli("focus", "a.txt:20-22"))
    nv.command("edit b.txt")
    nv.command("setlocal foldmethod=indent")
    ok(cli("unfocus"))
    assert nv.current.window.handle == win.handle
    assert nv.eval("&l:foldmethod") == "indent"
    nv.command("buffer a.txt")
    assert nv.eval("&l:foldmethod") == before
    assert nv.eval("foldclosed(5)") == -1


def test_focus_during_tour_keeps_the_view(cli, nv):
    ok(cli("start", "Keep"))
    ok(cli("step", "a.txt:3"))
    out = ok(cli("focus", "b.txt:5-6"))
    assert "applied when the file is shown" in out
    assert nv.current.buffer.name.endswith("a.txt") and nv.current.window.cursor[0] == 3
    ok(cli("step", "b.txt:5", "--jump"))
    assert nv.current.buffer.name.endswith("b.txt")
    assert nv.eval("foldclosed(20)") != -1 and nv.eval("foldclosed(5)") == -1


def test_start_keeps_open_panel(cli, nv):
    ok(cli("start", "One"))
    ok(cli("step", "a.txt:3"))
    pb = find_buf(nv, "nvtour://panel")
    assert any(w.buffer.handle == pb.handle for w in nv.windows)
    ok(cli("start", "Two"))
    pb2 = find_buf(nv, "nvtour://panel")
    assert pb2 is not None and pb2.handle == pb.handle
    assert any(w.buffer.handle == pb.handle for w in nv.windows)
    assert "# Two" in "\n".join(lines_of(nv, pb.handle))


def test_unfocus_by_name_with_glob_characters(cli, nv, sandbox):
    f = sandbox.ws / "br[1].txt"
    f.write_text("".join(f"x {i}\n" for i in range(1, 31)))
    try:
        ok(cli("focus", "br[1].txt:10-11", "--dim"))
        assert ok(cli("unfocus", "br[1].txt")).strip() == "unfocused 1 buffer(s)"
    finally:
        f.unlink()


def test_where_blockwise_selection(cli, nv):
    nv.command("edit b.txt")
    nv.command("normal! 2G5l\x16j2l\x1b")
    data = json.loads(ok(cli("--json", "where")))
    sel = data["selection"]
    assert sel["kind"] == "block" and sel["live"] is False
    assert (sel["l1"], sel["l2"], sel["c1"], sel["c2"]) == (2, 3, 6, 8)
    assert sel["text"] == ["2 o", "3 o"]
    assert "previous, may be old" in ok(cli("where"))


def test_cli_validation_and_json_errors(cli, tmp_path):
    assert cli("--timeout", "0", "where").returncode == 2
    assert cli("--timeout", "nan", "where").returncode == 2
    assert cli("focus", "a.txt:3", "--context", "-1").returncode == 2
    r = cli("--json", "step", "missing.txt:1")
    assert r.returncode == 6
    data = json.loads(r.stdout)
    assert data == {"ok": False, "code": 6, "error": data["error"]} and "missing.txt" in data["error"]
    r = cli("--json", "step")
    assert r.returncode == 2 and json.loads(r.stdout)["code"] == 2
    r = cli("panel", "--file", str(tmp_path / "nope.md"))
    assert r.returncode == 6 and "Traceback" not in r.stderr
    r = cli("where", socket=False, env_extra={"NVTOUR_SOCKET": ""})  # discovery still works
    assert r.returncode in (0, 3)
    r = cli("--socket", str(tmp_path / "missing.sock"), "where", socket=False)
    assert r.returncode == 4 and "socket not found" in r.stderr


def test_attach_requires_a_live_socket(cli, sandbox, tmp_path):
    dead = tmp_path / "custom.sock"
    dead.write_text("")
    r = cli("--workspace", str(sandbox.ws), "attach", str(dead), socket=False)
    assert r.returncode == 5
    assert "cannot connect" in r.stderr


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
    regular = sandbox.runtime / "nvim.999998.0"
    if os.path.exists("/proc/999999") or os.path.exists("/proc/999998"):
        pytest.skip("pid 999999 or 999998 exists")
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(fake))  # a real socket file left behind by a dead nvim
    s.close()
    regular.write_text("")  # same name pattern, but not a socket: --prune must not delete it
    try:
        r = cli("--workspace", str(sandbox.ws), "--json", "instances", socket=False)
        data = json.loads(ok(r))
        states = sorted(i["state"] for i in data["instances"])
        assert states == ["live", "stale", "stale"]
        live = [i for i in data["instances"] if i["state"] == "live"][0]
        assert live["pid"] == nvim_proc.pid and live["score"] == 100
        text = ok(cli("--workspace", str(sandbox.ws), "instances", socket=False))
        assert f"{nvim_proc.pid}  live  100  {sandbox.ws}" in text and "999999  stale" in text
        r = cli("--workspace", str(sandbox.ws), "attach", socket=False)
        assert f"pid {nvim_proc.pid}" in ok(r) and "exact match" in r.stdout
        r = cli("--workspace", str(sandbox.ws), "instances", "--prune", socket=False)
        assert "pruned 1 stale socket(s); skipped 1" in ok(r)
        assert not fake.exists() and regular.exists()
        # auto-selection (pin) works without --socket
        r = cli("--workspace", str(sandbox.ws), "where", socket=False)
        ok(r)
        ok(cli("--workspace", str(sandbox.ws), "attach", "--clear", socket=False))
    finally:
        fake.unlink(missing_ok=True)
        regular.unlink(missing_ok=True)


@pytest.fixture()
def second_nvim(sandbox, tmp_path):
    """Another private headless nvim (same runtime dir) whose cwd is outside the workspace."""
    cwd = tmp_path / "second"
    cwd.mkdir()
    proc = subprocess.Popen(["nvim", "--headless", "--clean"], cwd=cwd, env=clean_env(sandbox.runtime),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield wait_for_socket(sandbox.runtime, proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_only_instance_is_selected_with_a_note(cli, sandbox, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = cli("--workspace", str(other), "where", socket=False)
    assert r.returncode == 0 and "using the only running nvim" in r.stderr
    ok(cli("--workspace", str(other), "attach", "--clear", socket=False))


def test_no_match_exit_codes(cli, sandbox, tmp_path, second_nvim):
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = cli("--workspace", str(other), "where", socket=False)
    assert r.returncode == 3 and "nvtour attach" in r.stderr
    empty = tmp_path / "empty_run"
    empty.mkdir()
    r = cli("where", socket=False, env_extra={"XDG_RUNTIME_DIR": str(empty)})
    assert r.returncode == 4 and "no running nvim found" in r.stderr


def test_blocked_nvim_fails_fast_and_runs_nothing(cli, nv, sandbox, nvim_proc):
    import pynvim

    ok(cli("start", "B"))
    ui = pynvim.attach("socket", path=nvim_proc.socket)
    ui.ui_attach(80, 24, rgb=True)  # prompts only wait for the user when a UI is attached
    nv.input(':echo "a\\nb\\nc\\nd"<CR>')  # hit-enter prompt: nvim queues requests until <CR>
    deadline = time.time() + 5
    while not nv.api.get_mode()["blocking"] and time.time() < deadline:
        time.sleep(0.05)
    assert nv.api.get_mode()["blocking"] is True
    try:
        t0 = time.monotonic()
        r = cli("step", "a.txt:3")
        assert r.returncode == 5 and "waiting for input" in r.stderr and "not run" in r.stderr
        assert time.monotonic() - t0 < 4
        data = json.loads(ok(cli("--workspace", str(sandbox.ws), "--json", "instances", socket=False)))
        assert [i["state"] for i in data["instances"] if i["pid"] == nvim_proc.pid] == ["blocked"]
    finally:
        nv.input("<CR>")
        ui.ui_detach()
        ui.close()
    assert nv.exec_lua("return #_G.nvtour.state.tour.steps") == 0


def test_quickfix_list_is_reused_and_user_list_restored(cli, nv):
    nv.call("setqflist", [], " ", {"title": "user list", "items": [{"filename": "b.txt", "lnum": 1}]})
    ok(cli("start", "Q1"))
    ok(cli("step", "a.txt:3"))
    qid = nv.call("getqflist", {"id": 0})["id"]
    assert nv.call("getqflist", {"title": 0})["title"] == "nvtour: Q1"
    ok(cli("start", "Q2"))
    assert nv.call("getqflist", {"id": 0})["id"] == qid
    assert nv.call("getqflist", {"title": 0})["title"] == "nvtour: Q2"
    ok(cli("clear"))
    assert nv.call("getqflist", {"title": 0})["title"] == "user list"
    assert nv.call("getqflist", {"id": qid, "title": 0})["title"] == "nvtour (cleared)"


def test_clear_unlists_buffers_nvtour_added(cli, nv):
    ok(cli("start", "L"))
    ok(cli("step", "a.txt:3"))
    ok(cli("step", "b.txt:4"))
    b = find_buf(nv, "b.txt")
    assert nv.api.get_option_value("buflisted", {"buf": b.handle}) is True
    ok(cli("clear"))
    assert nv.api.get_option_value("buflisted", {"buf": b.handle}) is False
    assert nv.api.get_option_value("buflisted", {"buf": find_buf(nv, "a.txt").handle}) is True  # shown
    nv.command("silent! %bwipeout!")
    ok(cli("step", "b.txt:4", "--no-jump"))
    ok(cli("clear", "--keep-buffers"))
    assert nv.api.get_option_value("buflisted", {"buf": find_buf(nv, "b.txt").handle}) is True


def test_long_words_are_split_to_the_note_width(cli, nv):
    ok(cli("step", "a.txt:2", "--note", "x" * 300))
    buf = find_buf(nv, "a.txt")
    vl = [m[3]["virt_lines"] for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("virt_lines")][0]
    widths = [sum(len(chunk[0]) for chunk in line[1:]) for line in vl]
    assert len(vl) > 3 and max(widths) <= nv.eval("winwidth(0)")


def test_swap_file_does_not_prompt(cli, nv, sandbox, tmp_path):
    swapdir = tmp_path / "swap"
    swapdir.mkdir()
    f = sandbox.ws / "swapped.txt"
    f.write_text("one\ntwo\n")
    other_sock = tmp_path / "other.sock"
    other = subprocess.Popen(
        ["nvim", "--headless", "--clean", "--listen", str(other_sock), "--cmd", f"set directory={swapdir}//", str(f)],
        cwd=sandbox.ws, env=clean_env(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    old_dir = nv.eval("&directory")
    try:
        deadline = time.time() + 10
        while not any(swapdir.iterdir()) and time.time() < deadline:
            time.sleep(0.05)
        assert any(swapdir.iterdir()), "the other nvim did not create a swap file"
        nv.command(f"set directory={swapdir}//")
        r = cli("step", "swapped.txt:1")
        ok(r)
        assert "has a swap file" in r.stderr
        assert nv.api.get_mode()["blocking"] is False
        assert lines_of(nv, find_buf(nv, "swapped.txt").handle) == ["one", "two"]
    finally:
        nv.command("set directory=" + old_dir.replace(" ", "\\ ").replace(",", "\\,"))
        other.kill()
        other.wait(timeout=10)
        f.unlink()


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

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


def content(line):
    """Chunks of a virtual line without the band or the frame: no gutter, padding, border or lead."""
    out = []
    for text, hl in line:
        groups = hl if isinstance(hl, list) else [hl]
        groups = [g for g in groups if g != "NvtourNoteBg"]
        if not groups or text == "▎" or groups[0].startswith("NvtourFrame"):
            continue
        if groups[0] == "NvtourNoteBorder" and (text == "╶─ " or text.strip() == ""):
            continue
        out.append([text, groups[0]])
    return out


@pytest.fixture()
def band(nv):
    """Draw the virtual lines as a band (vim.g.nvtour_note_style), not in a frame."""
    nv.command("let g:nvtour_note_style = 'band'")
    yield
    nv.command("unlet! g:nvtour_note_style")


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
    assert out.splitlines() == ["step 1/1: a.txt:5-7 [fault] bad", "  5| line 5 of a"]
    buf = find_buf(nv, "a.txt")
    assert buf is not None
    marks = ns_marks(nv, "nvtour_steps", buf.handle)
    details = [m[3] for m in marks]
    assert not any(d.get("line_hl_group") or d.get("hl_group") == "NvtourLineFault" for d in details)  # no tint
    signs = sorted((m[1], m[3]["sign_text"]) for m in marks if m[3].get("sign_text"))
    assert signs == [(4, " ▎"), (5, " ▎"), (6, " ▎")]  # a bar along the range
    assert all(d["sign_hl_group"] == "NvtourSignFault" for d in details if d.get("sign_text"))
    assert sum(1 for d in details if d.get("number_hl_group") == "NvtourNumberFault") == 3
    assert any(d.get("virt_lines") and d.get("virt_lines_above") for d in details)
    eol = [d["virt_text"] for d in details if d.get("virt_text_pos") == "eol"]
    assert eol == [[["  ← ", "NvtourLabelFault"], ["1", "NvtourSignFault"], [" bad", "NvtourLabelFault"]]]
    assert nv.current.buffer.name.endswith("a.txt")
    assert nv.current.window.cursor[0] == 5
    assert nv.eval("maparg(']w', 'n')") != ""


def test_wrapped_note_uses_border_prefixes(cli, nv, band):
    note = "word " * 60
    ok(cli("step", "a.txt:2", "--note", note))
    buf = find_buf(nv, "a.txt")
    vl = [m[3]["virt_lines"] for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("virt_lines")][0]
    assert len(vl) > 1
    assert content(vl[0])[0][0].startswith("╭") and content(vl[-1])[0][0].startswith("╰")


def current(nv):
    return nv.exec_lua("return _G.nvtour.state.tour.current")


def test_nav_and_quickfix(cli, nv):
    ok(cli("start", "Nav"))
    ok(cli("step", "a.txt:3-4", "--role", "fault", "--label", "first"))
    ok(cli("step", "b.txt:9", "--role", "fix", "--note", "fix note"))
    assert current(nv) == 1
    assert "already at first step" in ok(cli("prev"))
    assert ok(cli("next")).splitlines()[0] == "step 2/2: b.txt:9 [fix]"
    assert ok(cli("prev")).splitlines()[0] == "step 1/2: a.txt:3-4 [fault] first"
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
    lines = lines_of(nv, pb.handle)
    assert lines[:6] == ["# Panel title", "", "a.txt", "▶ 1. • 3-4  alpha step", "b.txt", "  2. • 9    beta step"]
    text = "\n".join(lines)
    assert "free text here" in text and "---" in text
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
    assert ok(cli("last")).splitlines()[0] == "step 3/3: a.txt:20 [info] three"
    assert ok(cli("first")).splitlines()[0] == "step 1/3: a.txt:3 [info] one"
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


def step_list(nv):
    return nv.exec_lua("local o = {} for _, s in ipairs(_G.nvtour.state.tour.steps) do"
                       " o[#o + 1] = { s.n, s.l1, s.label or '' } end return o")


def test_expect_checks_the_highlighted_text(cli, nv):
    ok(cli("start", "E"))
    ok(cli("step", "a.txt:5-7", "--expect", "line 6 of a"))
    r = cli("step", "a.txt:9", "--expect", "line 10 of a")
    assert r.returncode == 6 and 'line 9 is "line 9 of a"' in r.stderr
    assert nv.exec_lua("return #_G.nvtour.state.tour.steps") == 1


def test_insert_edit_remove_steps(cli, nv):
    ok(cli("start", "Edit"))
    ok(cli("step", "a.txt:3", "--label", "A"))
    ok(cli("step", "a.txt:9", "--label", "C"))
    assert ok(cli("step", "a.txt:6", "--label", "B", "--at", "2")).startswith("step 2/3: a.txt:6 [info] B")
    assert step_list(nv) == [[1, 3, "A"], [2, 6, "B"], [3, 9, "C"]]
    buf = find_buf(nv, "a.txt")
    numbers = sorted((m[1], m[3]["virt_text"][1][0]) for m in ns_marks(nv, "nvtour_steps", buf.handle)
                     if m[3].get("virt_text"))
    assert numbers == [(2, "1"), (5, "2"), (8, "3")]
    out = ok(cli("edit", "2", "a.txt:7-8", "--label", "B2", "--role", "fault", "--note", "moved"))
    assert out.splitlines() == ["step 2/3: a.txt:7-8 [fault] B2", "  7| line 7 of a"]
    assert cli("edit", "2", "--expect", "nope").returncode == 6
    ok(cli("goto", "3"))
    assert ok(cli("remove", "1")).strip() == "removed step 1; 2 step(s) left"
    assert step_list(nv) == [[1, 7, "B2"], [2, 9, "C"]]
    assert current(nv) == 2
    assert cli("remove", "5").returncode == 6
    qf = nv.call("getqflist", {"items": 0})["items"]
    assert [i["lnum"] for i in qf] == [7, 9]


def test_status_reports_everything(cli, nv):
    ok(cli("start", "S"))
    ok(cli("step", "a.txt:3", "--role", "fault", "--label", "first"))
    ok(cli("step", "b.txt:4"))
    ok(cli("focus", "b.txt:4-5", "--dim"))
    data = json.loads(ok(cli("--json", "status")))
    assert data["title"] == "S" and data["total"] == 2 and data["current"] == 1
    assert [s["l1"] for s in data["steps"]] == [3, 4]
    assert data["focus"][0]["mode"] == "dim" and data["panel"]["open"] is True
    assert data["keys"]["first"] == "[W" and data["keys"]["next"] == "]w"
    text = ok(cli("status"))
    assert "tour: S (2 step(s), current 1)" in text and "▶ 1. a.txt:3 [fault] first" in text
    assert "keys: ]w next, [w prev, [W first, ]W last" in text
    ok(cli("clear"))
    data = json.loads(ok(cli("--json", "status")))
    assert data["total"] == 0 and data["keys"] == {}


def test_panel_footer_and_skipped_key_warning(cli, nv):
    nv.command("nnoremap ]W <Nop>")
    try:
        data = json.loads(ok(cli("--json", "start", "K")))  # start installs the keys
        assert "last" not in data["keys"] and any("]W" in w for w in data["warnings"])
        r = cli("step", "a.txt:3")
        assert "last" not in json.loads(ok(cli("--json", "status")))["keys"] and r.returncode == 0
        pb = find_buf(nv, "nvtour://panel")
        footer = lines_of(nv, pb.handle)[-1]
        assert footer.startswith("`]w` next · `[w` prev · `[W` first · `<leader>wp` panel")
        assert footer.endswith("`q` close")
    finally:
        ok(cli("clear"))
        nv.command("nunmap ]W")


def test_doctor(cli, sandbox):
    r = cli("--workspace", str(sandbox.ws), "doctor", socket=False)
    assert r.returncode == 0 and "[ok]" in r.stdout and "Lua runtime" in r.stdout


def test_zz_safety_files_untouched(cli, nv, sandbox, pristine):
    """Run a full session, then verify no buffer was modified and no file changed on disk."""
    ok(cli("start", "Safety"))
    ok(cli("step", "a.txt:5-9", "--role", "fault", "--note", "x"))
    ok(cli("step", "a.txt:5-9", "--ref", "v1", "--note", "old", "--jump"))
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


def step_marks(nv, suffix):
    buf = find_buf(nv, suffix)
    return [m for m in ns_marks(nv, "nvtour_steps", buf.handle)]


def test_only_the_current_step_is_expanded(cli, nv, band):
    ok(cli("start", "Cur"))
    ok(cli("step", "a.txt:3-4", "--role", "fault", "--note", "First note. It has two lines.\nSecond line."))
    ok(cli("step", "a.txt:9-10", "--role", "flow", "--note", "Other note"))

    def bar_rows():
        return sorted(m[1] for m in step_marks(nv, "a.txt") if "▎" in m[3].get("sign_text", ""))

    def notes():
        return {m[1]: [content(l) for l in m[3]["virt_lines"]] for m in step_marks(nv, "a.txt") if m[3].get("virt_lines")}

    assert bar_rows() == [2, 3]
    vl = notes()
    assert len(vl[2]) == 2 and vl[2][0][0][0] == "╭ "
    assert len(vl[8]) == 1 and vl[8][0][0][0] == "╶ " and vl[8][0][-1] == ["Other note", "NvtourNoteCollapsed"]
    ok(cli("next"))
    assert bar_rows() == [8, 9]
    vl = notes()
    assert vl[2][0][0][0] == "╶ " and vl[2][0][-1] == [" …", "NvtourNoteCollapsed"]
    assert vl[8][0][0][0] == "▸ "
    eol = sorted((m[1], m[3]["virt_text"][1][0]) for m in step_marks(nv, "a.txt") if m[3].get("virt_text"))
    assert eol == [(2, "1"), (8, "2")]  # every step keeps its number


def test_note_inline_markdown(cli, nv, band):
    ok(cli("step", "a.txt:3", "--note", "Calls `erase()` on **it** and `a b`. Lone ` stays."))
    vl = [m[3]["virt_lines"] for m in step_marks(nv, "a.txt") if m[3].get("virt_lines")][0]
    chunks = [tuple(c) for line in vl for c in content(line)[1:]]  # without the border prefixes
    assert ("erase()", "NvtourNoteCode") in chunks and ("it", "NvtourNoteBold") in chunks
    assert ("a b", "NvtourNoteCode") in chunks
    text = " ".join(" ".join(c[0] for c in chunks).split())
    assert "**" not in text and "`erase" not in text and "Lone ` stays." in text


def test_expect_text_is_marked_in_the_current_step(cli, nv):
    ok(cli("start", "Mark"))
    ok(cli("step", "a.txt:5-6", "--role", "fault", "--expect", "of a"))
    ok(cli("step", "a.txt:9", "--expect", "line"))
    marks = [(m[1], m[2], m[3]["end_col"]) for m in step_marks(nv, "a.txt") if m[3].get("hl_group") == "NvtourMarkFault"]
    assert marks == [(4, 7, 11), (5, 7, 11)]  # "line 5 of a": bytes 7-11
    assert not [m for m in step_marks(nv, "a.txt") if m[3].get("hl_group") == "NvtourMarkInfo"]
    ok(cli("edit", "1", "--expect", ""))
    ok(cli("next"))
    assert [m[1] for m in step_marks(nv, "a.txt") if m[3].get("hl_group") == "NvtourMarkInfo"] == [8]
    assert not [m for m in step_marks(nv, "a.txt") if m[3].get("hl_group") == "NvtourMarkFault"]
    data = json.loads(ok(cli("--json", "status")))
    assert [s.get("expect") for s in data["steps"]] == [None, "line"]


def view(nv):
    return nv.call("winsaveview")


def test_jump_keeps_note_and_range_in_view(cli, nv):
    ok(cli("start", "View"))
    ok(cli("step", "a.txt:1", "--note", "A note on the first line.\nSecond line."))
    v = view(nv)
    assert v["topline"] == 1 and v["topfill"] == 4  # the framed note above line 1 is shown
    ok(cli("step", "a.txt:20-55", "--note", "Long range.\nTwo lines.", "--jump"))
    v = view(nv)
    assert v["topline"] == 20 and v["topfill"] == 4 and nv.current.window.cursor[0] == 20
    ok(cli("step", "a.txt:40", "--note", "Short.", "--jump"))
    top, bottom = nv.eval("line('w0')"), nv.eval("line('w$')")
    assert top < 40 < bottom and abs((40 - top) - (bottom - 40)) <= 2  # centred
    ok(cli("step", "a.txt:58", "--jump"))
    assert nv.eval("line('w$')") == 60 and nv.eval("winline()") > nv.eval("winheight(0)") // 2  # not past the end


def eval_winbar(nv, win):
    return nv.api.eval_statusline("%{%v:lua.nvtour.winbar()%}",
                                  {"winid": win.handle, "use_winbar": True, "maxwidth": 200})["str"]


def test_winbar_shows_position_and_next_step(cli, nv):
    ok(cli("start", "Bar"))
    ok(cli("step", "a.txt:3", "--role", "fault", "--label", "100% bad"))
    ok(cli("step", "b.txt:9", "--label", "the fix"))
    win = [w for w in nv.windows if w.buffer.name.endswith("a.txt")][0]
    assert nv.api.get_option_value("winbar", {"win": win.handle, "scope": "local"}) == "%{%v:lua.nvtour.winbar()%}"
    assert eval_winbar(nv, win).rstrip() == " nvtour 1/2 fault · 100% bad"  # narrow window: no room for "next"
    ok(cli("panel", "--toggle"))
    text = eval_winbar(nv, win)
    assert text.startswith(" nvtour 1/2 fault · 100% bad") and text.endswith("next ]w: b.txt:9 the fix ")
    ok(cli("edit", "2", "--label", "x" * 40))
    assert eval_winbar(nv, win).endswith("next ]w: b.txt:9 ")  # a next label that does not fit is dropped
    ok(cli("edit", "1", "--label", "y" * 100))
    text = eval_winbar(nv, win)
    assert text.startswith(" nvtour 1/2 fault · yyy") and "…" in text and "next" not in text  # cut at its end
    assert nv.call("strdisplaywidth", text.rstrip()) <= win.width
    ok(cli("edit", "1", "--label", "100% bad"))
    ok(cli("edit", "2", "--label", "the fix"))
    ok(cli("next"))
    assert eval_winbar(nv, win).rstrip().endswith("last step · [W first")
    ok(cli("clear"))
    assert nv.api.get_option_value("winbar", {"win": win.handle, "scope": "local"}) == ""
    nv.command("buffer a.txt")  # a remembered winbar from the tour is removed
    assert nv.api.get_option_value("winbar", {"win": nv.current.window.handle, "scope": "local"}) == ""


def test_user_winbar_is_kept(cli, nv):
    nv.command("set winbar=mine")
    try:
        ok(cli("step", "a.txt:3"))
        assert nv.eval("&l:winbar") == "" and nv.eval("&winbar") == "mine"
        nv.command("let g:nvtour_winbar = v:false")
        nv.command("set winbar=")
        ok(cli("step", "b.txt:4", "--jump"))
        assert nv.eval("&l:winbar") == ""
    finally:
        nv.command("set winbar= | unlet! g:nvtour_winbar")


def flash_marks(nv, suffix):
    return ns_marks(nv, "nvtour_flash", find_buf(nv, suffix).handle)


def test_jump_flashes_the_range(cli, nv):
    ok(cli("start", "Flash"))
    ok(cli("step", "a.txt:3-5"))
    ok(cli("step", "b.txt:30", "--jump"))  # last line of b.txt
    marks = flash_marks(nv, "b.txt")
    assert len(marks) == 1 and marks[0][1] == 29 and marks[0][3]["hl_group"] == "NvtourFlash"
    assert flash_marks(nv, "a.txt") == []  # the previous flash is removed at once
    nv.exec_lua("vim.wait(600, function() return false end)")
    assert flash_marks(nv, "b.txt") == []
    nv.command("let g:nvtour_flash = 0")
    try:
        ok(cli("goto", "1"))
        assert flash_marks(nv, "a.txt") == []
    finally:
        nv.command("unlet g:nvtour_flash")


def test_panel_groups_files_and_colours_roles(cli, nv):
    ok(cli("start", "Groups"))
    ok(cli("step", "a.txt:3", "--role", "fault", "--label", "one"))
    ok(cli("step", "a.txt:10-12", "--role", "flow", "--note", "`x` is set here.\nMore."))
    ok(cli("step", "b.txt:9", "--role", "fix"))
    pb = find_buf(nv, "nvtour://panel")
    assert lines_of(nv, pb.handle)[2:7] == [
        "a.txt", "▶ 1. ✗ 3      one", "  2. → 10-12  x is set here.", "b.txt", "  3. ✓ 9"]
    marks = ns_marks(nv, "nvtour_panel", pb.handle)
    groups = {(m[1], m[3].get("hl_group")) for m in marks if m[3].get("hl_group")}
    assert {(2, "NvtourPanelFile"), (3, "NvtourSignFault"), (4, "NvtourSignFlow"), (6, "NvtourSignFix")} <= groups
    progress = [m[3]["virt_text"] for m in marks if m[1] == 0 and m[3].get("virt_text")]
    assert progress == [[["  1/3", "NvtourPanelProgress"]]]
    win = [w for w in nv.windows if w.buffer.handle == pb.handle][0]
    nv.current.window = win
    win.cursor = (6, 0)  # the b.txt file line
    nv.input("<CR>")
    nv.command("sleep 50m")
    assert current(nv) == 3


def ref_buf(nv, ref, rel):
    return find_buf(nv, f"nvtour://{ref}/{rel}")


def test_ref_step_shows_the_old_version(cli, nv, sandbox):
    ok(cli("start", "Ref"))
    out = ok(cli("step", "a.txt:3-4", "--ref", "v1", "--role", "fault", "--label", "old", "--expect", "old line 3"))
    assert out.splitlines() == ["step 1/1: a.txt:3-4 @v1 [fault] old", "  3| old line 3 of a"]
    buf = nv.current.buffer
    assert buf.name == "nvtour://v1/a.txt" and nv.current.window.cursor[0] == 3
    assert lines_of(nv, buf.handle)[0] == "old line 1 of a" and len(buf) == 40
    opts = {o: nv.api.get_option_value(o, {"buf": buf.handle}) for o in
            ("buftype", "bufhidden", "modifiable", "readonly", "modified", "swapfile", "buflisted")}
    assert opts == {"buftype": "nofile", "bufhidden": "hide", "modifiable": False, "readonly": True,
                    "modified": False, "swapfile": False, "buflisted": True}
    info = nv.api.buf_get_var(buf.handle, "nvtour_ref")
    assert info["ref"] == "v1" and len(info["sha"]) == 40 and info["file"] == str(sandbox.ws / "a.txt")
    signs = sorted(m[1] for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("sign_text"))
    assert signs == [2, 3]
    assert not any(b.name == str(sandbox.ws / "a.txt") for b in nv.buffers)  # the real file is not loaded
    data = json.loads(ok(cli("--json", "status")))
    assert data["steps"][0]["ref"] == "v1" and data["steps"][0]["sha"] == info["sha"]
    assert "▶ 1. a.txt:3-4 @v1 [fault] old" in ok(cli("status"))
    ok(cli("clear"))
    assert ref_buf(nv, "v1", "a.txt") is None
    assert nv.exec_lua("return vim.tbl_count(_G.nvtour.state.refs)") == 0


def test_ref_step_next_to_the_working_tree(cli, nv, sandbox):
    ok(cli("start", "Before/after"))
    ok(cli("step", "a.txt:3", "--ref", "v1", "--label", "old"))
    ok(cli("step", "a.txt:3", "--label", "new"))
    ok(cli("step", "a.txt:9", "--ref", "v1", "--label", "old again"))
    pb = find_buf(nv, "nvtour://panel")
    assert lines_of(nv, pb.handle)[2:8] == [
        "a.txt @v1", "▶ 1. • 3  old", "a.txt", "  2. • 3  new", "a.txt @v1", "  3. • 9  old again"]
    win = nv.current.window
    nwins = len(nv.windows)
    ok(cli("panel", "--toggle"))  # room for the right side of the winbar
    assert eval_winbar(nv, win).startswith(" nvtour 1/3 info @v1 · old")
    assert eval_winbar(nv, win).endswith("next ]w: a.txt:3 new ")  # the same file, another version
    qf = nv.call("getqflist", {"items": 0})["items"]
    ref = ref_buf(nv, "v1", "a.txt").handle
    assert [i["bufnr"] for i in qf][0] == ref and qf[0]["lnum"] == 3
    assert nv.call("bufname", qf[1]["bufnr"]) == str(sandbox.ws / "a.txt")
    ok(cli("next"))
    assert nv.current.window == win and nv.current.buffer.name == str(sandbox.ws / "a.txt")
    assert eval_winbar(nv, win).endswith("next ]w: a.txt:9 @v1 old again ")
    ok(cli("next"))
    assert nv.current.window == win and nv.current.buffer.handle == ref  # the ref buffer is a tour window
    assert len(nv.windows) == nwins - 1  # only the panel closed; no split was opened


def test_ref_step_on_deleted_files(cli, nv):
    ok(cli("start", "Deleted"))
    assert ok(cli("step", "olddir/x.cpp:3", "--ref", "v1")).splitlines() == [
        "step 1/1: olddir/x.cpp:3 @v1 [info]", "  3|     return 1;"]
    buf = ref_buf(nv, "v1", "olddir/x.cpp")
    assert nv.api.get_option_value("filetype", {"buf": buf.handle}) == "cpp"
    ok(cli("step", "gone.txt:2", "--ref", "v1"))
    assert lines_of(nv, ref_buf(nv, "v1", "gone.txt").handle)[1] == "gone line 2"


def test_ref_step_errors(cli, nv, tmp_path):
    ok(cli("start", "Errors"))
    r = cli("step", "a.txt:3", "--ref", "no-such-ref")
    assert r.returncode == 6 and "unknown git ref: no-such-ref" in r.stderr
    r = cli("step", "gone.txt:2", "--ref", "HEAD")
    assert r.returncode == 6 and "gone.txt is not a file at HEAD" in r.stderr
    r = cli("step", "olddir:1", "--ref", "v1")
    assert r.returncode == 6  # a directory is not a file
    r = cli("step", "a.txt:50", "--ref", "v1")
    assert r.returncode == 6 and "beyond end of file (40 lines): a.txt @v1" in r.stderr
    r = cli("step", "a.txt:3", "--ref", "v1", "--expect", "nope")
    assert r.returncode == 6 and "a.txt:3 @v1" in r.stderr
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n")
    assert cli("step", f"{outside}:1", "--ref", "v1").returncode == 6
    r = cli("edit", "1", "--ref", "v1")
    assert r.returncode == 2 and "--ref needs a location" in r.stderr
    assert json.loads(ok(cli("--json", "status")))["total"] == 0


def test_edit_moves_a_step_between_versions(cli, nv, sandbox):
    ok(cli("start", "Move"))
    ok(cli("step", "a.txt:3", "--label", "s"))
    assert ok(cli("edit", "1", "a.txt:5", "--ref", "v1")).splitlines() == [
        "step 1/1: a.txt:5 @v1 [info] s", "  5| old line 5 of a"]
    assert ns_marks(nv, "nvtour_steps", find_buf(nv, "/ws/a.txt").handle) == []
    assert ok(cli("edit", "1", "--label", "t")).startswith("step 1/1: a.txt:5 @v1 [info] t")  # stays on v1
    assert ok(cli("edit", "1", "a.txt:6")).startswith("step 1/1: a.txt:6 [info] t")  # back on the working tree
    assert ns_marks(nv, "nvtour_steps", ref_buf(nv, "v1", "a.txt").handle) == []


def test_ref_buffer_is_made_again_after_wipe_or_unload(cli, nv):
    ok(cli("start", "Wipe"))
    ok(cli("step", "a.txt:3", "--ref", "v1", "--note", "Old code."))
    ok(cli("step", "a.txt:7", "--ref", "v1", "--label", "same buffer"))
    ok(cli("step", "b.txt:4", "--jump"))
    for how in ("bwipeout!", "bdelete!"):
        nv.command(f"{how} {ref_buf(nv, 'v1', 'a.txt').handle}")
        ok(cli("goto", "1"))
        buf = nv.current.buffer
        assert buf.name == "nvtour://v1/a.txt" and lines_of(nv, buf.handle)[2] == "old line 3 of a"
        marks = ns_marks(nv, "nvtour_steps", buf.handle)
        assert sorted({m[1] for m in marks if m[3].get("virt_text")}) == [2, 6]  # both steps drawn again
        assert nv.exec_lua("return _G.nvtour.state.tour.steps[2].buf") == buf.handle
        ok(cli("goto", "3"))


def test_where_in_a_ref_buffer(cli, nv, sandbox):
    ok(cli("start", "Where"))
    ok(cli("step", "a.txt:3", "--ref", "v1"))
    text = ok(cli("where"))
    assert text.startswith("a.txt:3:1 @v1  mode=n") and "at git ref v1 (" in text and "buftype" not in text
    data = json.loads(ok(cli("--json", "where")))
    assert data["file"] == str(sandbox.ws / "a.txt") and data["ref"] == "v1" and len(data["sha"]) == 40


def link_texts(nv, suffix, above=True):
    """Text of the virtual lines above (or below) the ranges of a file: { row: [line, ...] }."""
    out = {}
    for m in step_marks(nv, suffix):
        d = m[3]
        if d.get("virt_lines") and bool(d.get("virt_lines_above")) == above:
            texts = ["".join(c[0] for c in content(line)) for line in d["virt_lines"]]
            out[m[1]] = [t.rstrip() for t in texts if t.strip()]  # no frame rules
    return out


def test_via_links_between_steps(cli, nv):
    ok(cli("start", "Links"))
    ok(cli("step", "a.txt:3", "--role", "fault", "--label", "check"))
    ok(cli("step", "b.txt:9", "--role", "flow", "--via", "calls `evict()`", "--note", "Erases."))
    ok(cli("step", "a.txt:20-21", "--from", "1", "--via", "back"))
    # Step 1: the next step is in another file, and its link comes from here.
    assert link_texts(nv, "a.txt") == {}
    assert link_texts(nv, "a.txt", above=False) == {2: ["→ next 2 · b.txt:9: calls evict()"]}
    below = content([m[3]["virt_lines"][0] for m in step_marks(nv, "a.txt") if m[3].get("virt_lines")][0])
    assert ["next 2", "NvtourSignFlow"] in below and ["evict()", "NvtourNoteCode"] in below
    ok(cli("next"))
    assert link_texts(nv, "b.txt") == {8: ["◇ b.txt", "Erases."]}  # the --via text is only on the next line
    assert link_texts(nv, "b.txt", above=False) == {8: ["→ next 3 · a.txt:20 (from 1): back"]}
    assert link_texts(nv, "a.txt", above=False) == {}  # only the current step shows links
    ok(cli("next"))
    # The link comes from step 1 (same file), but the user comes from b.txt: the file is shown.
    assert link_texts(nv, "a.txt") == {19: ["◇ a.txt"]}
    pb = find_buf(nv, "nvtour://panel")
    assert lines_of(nv, pb.handle)[2:9] == [
        "a.txt", "  1. ✗ 3      check", "    ↓ calls `evict()`", "b.txt", "  2. → 9      Erases.",
        "    ↓ from 1: back", "a.txt"]
    data = json.loads(ok(cli("--json", "status")))
    assert [(s.get("via"), s.get("from")) for s in data["steps"]] == [
        (None, None), ("calls `evict()`", None), ("back", 1)]
    assert "       ↓ from 1: back" in ok(cli("status")).splitlines()
    # Edit and remove the links.
    ok(cli("edit", "3", "--from", "0"))
    ok(cli("goto", "2"))
    assert link_texts(nv, "b.txt", above=False) == {8: ["→ next 3 · a.txt:20: back"]}
    ok(cli("edit", "3", "--via", ""))
    assert link_texts(nv, "b.txt", above=False) == {8: ["→ next 3 · a.txt:20"]}  # another file: still shown
    ok(cli("edit", "3", "--from", "1", "--via", "again"))
    ok(cli("remove", "1"))  # the link falls back to the step before it
    data = json.loads(ok(cli("--json", "status")))
    assert [s.get("from") for s in data["steps"]] == [None, None]
    ok(cli("goto", "1"))
    ok(cli("edit", "2", "--via", "word " * 20))
    footer = link_texts(nv, "b.txt", above=False)[8]
    assert len(footer) > 1 and footer[0].startswith("→ next 2") and all(l.startswith("word") for l in footer[1:])
    assert cli("step", "a.txt:5", "--from", "9").returncode == 6
    assert cli("edit", "2", "--from", "2").returncode == 6
    assert cli("step", "a.txt:5", "--from", "0").returncode == 2


def test_links_show_a_change_of_version(cli, nv, sandbox):
    sha = subprocess.run(["git", "-C", str(sandbox.ws), "rev-parse", "v1"], capture_output=True, text=True,
                         check=True).stdout.strip()
    ok(cli("start", "Versions"))
    ok(cli("step", "a.txt:5"))
    ok(cli("step", "a.txt:5", "--ref", "v1"))
    ok(cli("step", "a.txt:6"))
    ok(cli("step", "a.txt:8"))
    ok(cli("step", "a.txt:5", "--ref", sha[:12]))
    assert link_texts(nv, "a.txt") == {}  # the first step: nothing to compare with
    assert link_texts(nv, "a.txt", above=False) == {4: ["→ next 2 · a.txt:5 @v1"]}
    ok(cli("next"))
    old = ref_buf(nv, "v1", "a.txt").handle
    above = {m[1]: [t for t in ("".join(c[0] for c in content(l)) for l in m[3]["virt_lines"]) if t.strip()]
             for m in ns_marks(nv, "nvtour_steps", old) if m[3].get("virt_lines") and m[3].get("virt_lines_above")}
    assert above == {4: [f"◇ a.txt @v1 ({sha[:12]})"]}
    assert link_texts(nv, "a.txt", above=False) == {}
    below = [["".join(c[0] for c in content(l)) for l in m[3]["virt_lines"]]
             for m in ns_marks(nv, "nvtour_steps", old) if m[3].get("virt_lines") and not m[3].get("virt_lines_above")]
    assert below == [["→ next 3 · a.txt:6 · working tree"]]
    ok(cli("next"))
    assert link_texts(nv, "a.txt")[5] == ["◇ a.txt · working tree"]
    ok(cli("next"))
    assert 7 not in link_texts(nv, "a.txt")  # the same file and version: nothing to show
    ok(cli("next"))
    old = ref_buf(nv, "v1", "a.txt").handle  # the same commit as v1: the same buffer
    lines = [[t for t in ("".join(c[0] for c in content(l)) for l in m[3]["virt_lines"]) if t.strip()]
             for m in ns_marks(nv, "nvtour_steps", old) if m[3].get("virt_lines")]
    assert lines == [[f"◇ a.txt @{sha[:12]}"]]  # a sha ref: not twice


def test_jump_adds_to_the_jumplist(cli, nv):
    ok(cli("start", "Jumps"))
    ok(cli("step", "a.txt:3"))
    ok(cli("step", "b.txt:9", "--jump"))
    assert nv.current.buffer.name.endswith("b.txt")
    nv.input("<C-o>")
    nv.command("sleep 50m")
    assert nv.current.buffer.name.endswith("a.txt") and nv.current.window.cursor[0] == 3


def test_virtual_lines_are_a_band(cli, nv, band):
    nv.command("set number signcolumn=yes")
    try:
        ok(cli("start", "Band"))
        ok(cli("step", "a.txt:5-6", "--role", "fault", "--note", "Current note."))
        ok(cli("step", "a.txt:9", "--note", "Other note."))
        win = nv.current.window
        info = nv.call("getwininfo", win.handle)[0]
        marks = [m[3] for m in step_marks(nv, "a.txt") if m[3].get("virt_lines")]
        assert all(d.get("virt_lines_leftcol") for d in marks)
        for d in marks:
            for line in d["virt_lines"]:
                assert sum(nv.call("strdisplaywidth", t) for t, _ in line) == info["width"]  # the full width
                assert all("NvtourNoteBg" in (hl if isinstance(hl, list) else [hl]) for _, hl in line)
        cur = [d for d in marks if any(c[0] == "▎" for c in d["virt_lines"][0])]
        assert len(cur) == 1  # only the current step has the bar
        line = cur[0]["virt_lines"][0]
        bar = next(i for i, c in enumerate(line) if c[0] == "▎")
        assert line[bar][1] == ["NvtourNoteBg", "NvtourSignFault"]
        # The bar is in the column of the range bar: the second cell of the sign column.
        col = sum(nv.call("strdisplaywidth", t) for t, _ in line[:bar])
        numw = max(nv.eval("&numberwidth"), len(str(nv.call("line", "$"))) + 1)
        assert col == info["textoff"] - numw - 1
        assert "".join(t for t, _ in line[:bar + 1]).strip() == "▎" and len("".join(t for t, _ in line[:bar + 2])) >= info["textoff"]
    finally:
        nv.command("set nonumber signcolumn=auto")


def test_virtual_lines_are_framed(cli, nv):
    nv.command("set number signcolumn=yes")
    try:
        ok(cli("start", "Frame"))
        ok(cli("step", "a.txt:5-6", "--role", "fault", "--note", "Current note."))
        ok(cli("step", "b.txt:9", "--note", "Other note.", "--via", "why"))
        ok(cli("step", "a.txt:12", "--note", "Third note."))
        info = nv.call("getwininfo", nv.current.window.handle)[0]
        marks = {m[1]: m[3] for m in step_marks(nv, "a.txt") if m[3].get("virt_lines")}
        block = [content_text for content_text in marks[4]["virt_lines"]]
        assert marks[4].get("virt_lines_leftcol") and marks[4].get("virt_lines_above")
        texts = ["".join(t for t, _ in line) for line in block]
        assert [t.strip()[0] + t.strip()[-1] for t in texts] == ["▎╮", "▎│", "▎╯"]  # bar, then the frame
        assert texts[0].replace("▎", " ").strip().startswith("╭─") and "Current note." in texts[1]
        for line in block:
            assert sum(nv.call("strdisplaywidth", t) for t, _ in line) == info["width"]
            assert ["▎", "NvtourSignFault"] in line
        assert all(hl == "NvtourFrameFault" for t, hl in block[1] if t.strip().startswith("│") or t.strip().endswith("│"))
        nxt = marks[5]["virt_lines"]  # below the range: the "next" line with a lead
        assert len(nxt) == 1 and ["╶─ ", "NvtourFrameFault"] in nxt[0]
        assert "".join(c[0] for c in content(nxt[0])) == "→ next 2 · b.txt:9: why"
        collapsed = marks[11]["virt_lines"]  # step 3, not current: a grey lead, no bar
        assert len(collapsed) == 1 and ["╶─ ", "NvtourNoteBorder"] in collapsed[0]
        assert not any(t == "▎" for t, _ in collapsed[0])
        assert "".join(c[0] for c in content(collapsed[0])) == "Third note."
    finally:
        nv.command("set nonumber signcolumn=auto")

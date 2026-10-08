"""Tests of ``nvtour diagram`` and of steps on a diagram."""

from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from nvtour import diagram
from nvtour.errors import NvtourError
from test_integration import eval_winbar, find_buf, lines_of, ns_marks, ok

DIAGRAM = """\
┌────────┐     ┌─────────┐
│ Client │────►│ Server  │
└────────┘     └────┬────┘
                    │ fetch
                    ▼
               ┌─────────┐
               │ Store   │
               └─────────┘
"""


def diagram_buf(nv, name):
    return find_buf(nv, "nvtour://diagram/" + name)


def wins_of(nv, buf):
    return list(nv.call("win_findbuf", buf.handle))


def fake_renderer(tmp_path, body):
    """A renderer command (for NVTOUR_MERMAID_ASCII / NVTOUR_GRAPH_EASY) that runs ``body`` in Python."""
    script = tmp_path / "renderer.py"
    script.write_text("import sys\n" + body)
    return f"{sys.executable} {script}"


# ---------------------------------------------------------------------------
# Rendering (no nvim)
# ---------------------------------------------------------------------------


def test_clean_removes_colours_tabs_and_blank_edges():
    text = "\n\n\x1b[31m┌─┐\x1b[0m   \n│\tx│\n\n\n"
    assert diagram.clean(text) == ["┌─┐", "│       x│"]


def test_guess_format_and_names():
    assert diagram.guess_format(None) == "mermaid"
    assert diagram.guess_format("a/flow.MMD") == "mermaid"
    assert diagram.guess_format("g.gv") == "dot" and diagram.guess_format("g.dot") == "dot"
    assert diagram.guess_format("art.txt") == "text"
    assert diagram.check_name("commit-v2.1") == "commit-v2.1"
    for bad in ("", "-x", "a/b", "a b"):
        with pytest.raises(NvtourError) as exc:
            diagram.check_name(bad)
        assert exc.value.code == 2


def test_parse_link(sandbox):
    lines = ["│ hits = 44 │"]
    target = str(sandbox.ws / "a.txt")
    link = diagram.parse_link(f"hits = 44={target}:3-4", lines)
    assert link == {"text": "hits = 44", "file": target, "l1": 3, "l2": 4}
    for spec, code in ((f"missing={target}:3", 6), (f"hits={target}:70", 6), ("hits", 2),
                       (f"={target}:3", 2), (f"hits={target}", 6)):
        with pytest.raises(NvtourError) as exc:
            diagram.parse_link(spec, lines)
        assert exc.value.code == code, spec


def test_header_row():
    seq = ["┌─────┐     ┌─────┐", "│ get │     │ map │", "└──┬──┘     └──┬──┘", "   │           │"]
    assert diagram.header_row("sequenceDiagram\n  A->>B: x", "mermaid", seq) == 2
    assert diagram.header_row("%% note\n\n  sequenceDiagram", "mermaid", seq) == 2
    assert diagram.header_row("---\nconfig:\n  x: 1\n---\nsequenceDiagram", "mermaid", seq) == 2
    asc = ["+-----+", "| get |", "+--+--+", "   |"]
    assert diagram.header_row("sequenceDiagram", "mermaid", asc) == 2
    assert diagram.header_row("graph LR\nA-->B", "mermaid", seq) == 0
    assert diagram.header_row("sequenceDiagram", "dot", seq) == 0
    assert diagram.header_row("sequenceDiagram", "mermaid", ["A ─► B"]) == 0


# ---------------------------------------------------------------------------
# The diagram buffer and window
# ---------------------------------------------------------------------------


def test_diagram_opens_above_the_code(cli, nv, sandbox):
    ok(cli("start", "Diagram"))
    ok(cli("step", "b.txt:3", "--label", "code"))
    code_win = nv.current.window
    nwins = len(nv.windows)
    out = ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))
    rows = out.splitlines()
    assert rows[0] == "diagram flow: 8 lines, 27 columns, 0 link(s), shown"
    assert rows[1] == "1| ┌────────┐     ┌─────────┐" and rows[8] == "8|                └─────────┘"
    buf = diagram_buf(nv, "flow")
    assert lines_of(nv, buf.handle) == DIAGRAM.splitlines()
    opts = {o: nv.api.get_option_value(o, {"buf": buf.handle}) for o in
            ("buftype", "bufhidden", "modifiable", "readonly", "modified", "swapfile", "filetype")}
    assert opts == {"buftype": "nofile", "bufhidden": "hide", "modifiable": False, "readonly": True,
                    "modified": False, "swapfile": False, "filetype": "nvtourdiagram"}
    assert nv.api.buf_get_var(buf.handle, "nvtour_diagram") == "flow"
    assert len(nv.windows) == nwins + 1
    (win,) = wins_of(nv, buf)
    assert nv.call("win_screenpos", win)[0] < nv.call("win_screenpos", code_win.handle)[0]  # above
    assert nv.call("win_screenpos", win)[1] == nv.call("win_screenpos", code_win.handle)[1]
    assert nv.api.win_get_height(win) == 9  # the lines and the winbar row
    assert nv.api.get_option_value("wrap", {"win": win}) is False
    assert nv.api.get_option_value("winfixheight", {"win": win}) is True
    assert nv.current.window == code_win and code_win.buffer.name == str(sandbox.ws / "b.txt")  # focus stays
    marks = ns_marks(nv, "nvtour_diagram", buf.handle)
    line_marks = [(m[1], m[2], m[3]["end_col"]) for m in marks if m[3]["hl_group"] == "NvtourDiagramLine"]
    assert (0, 0, len("┌────────┐".encode())) in line_marks
    assert any(r == 4 for r, _, _ in line_marks)  # the arrow head ▼
    status = ok(cli("status"))
    assert "diagram: flow (8 lines, 0 link(s))" in status


def test_no_show_and_replacing_the_lines(cli, nv):
    ok(cli("start", "Replace"))
    nwins = len(nv.windows)
    out = ok(cli("diagram", "flow", "--format", "text", "--no-show", stdin=DIAGRAM))
    assert out.splitlines()[0].endswith(", not shown")
    buf = diagram_buf(nv, "flow")
    assert wins_of(nv, buf) == [] and len(nv.windows) == nwins
    ok(cli("step", "--diagram", "flow", "6-7", "--label", "store", "--no-jump"))
    shorter = "\n".join(DIAGRAM.splitlines()[:5]) + "\n"
    r = cli("diagram", "flow", "--format", "text", stdin=shorter)
    assert r.returncode == 6 and "step 1 (lines 6-7) is beyond the new diagram (5 lines)" in r.stderr
    assert len(lines_of(nv, buf.handle)) == 8  # unchanged
    changed = DIAGRAM.replace("Store  ", "Storage")
    ok(cli("diagram", "flow", "--format", "text", stdin=changed))
    assert diagram_buf(nv, "flow").handle == buf.handle and lines_of(nv, buf.handle)[6] == "               │ Storage │"
    numbers = sorted(m[1] for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("number_hl_group"))
    assert numbers == [5, 6]  # the step marks are drawn again on the new lines


# ---------------------------------------------------------------------------
# Steps on a diagram
# ---------------------------------------------------------------------------


def test_steps_go_between_the_diagram_and_the_code(cli, nv, sandbox):
    ok(cli("start", "Read path"))
    ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))
    out = ok(cli("step", "--diagram", "flow", "2", "--role", "flow", "--label", "client sends",
                 "--expect", "Server", "--note", "The client sends a request to the server."))
    assert out.splitlines() == ["step 1/1: diagram flow:2 [flow] client sends", "  2| │ Client │────►│ Server  │"]
    buf = diagram_buf(nv, "flow")
    (dwin,) = wins_of(nv, buf)
    assert nv.current.window.handle == dwin and nv.current.window.cursor[0] == 2
    ok(cli("step", "a.txt:10-11", "--label", "fetch", "--via", "the server fetches the value"))
    ok(cli("step", "--diagram", "flow", "4-5", "--label", "back", "--via", "the store answers"))
    signs = [m[1] for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("sign_text")]
    assert signs == [1]
    marks = [m for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("hl_group") == "NvtourMarkFlow"]
    assert len(marks) == 1 and marks[0][1] == 1
    ok(cli("panel", "--toggle"))  # room for the label in the winbar
    assert eval_winbar(nv, nv.current.window).startswith(" nvtour 1/3 flow · diagram flow · client sends")
    pb = find_buf(nv, "nvtour://panel")
    assert lines_of(nv, pb.handle)[2:8] == [
        "diagram flow", "▶ 1. → 2      client sends", "    ↓ the server fetches the value", "a.txt",
        "  2. • 10-11  fetch", "    ↓ the store answers"]
    nwins = len(nv.windows)
    ok(cli("next"))
    code = nv.current.window
    assert code.handle != dwin and code.buffer.name == str(sandbox.ws / "a.txt") and code.cursor[0] == 10
    assert nv.api.win_get_buf(dwin).handle == buf.handle  # the diagram stays in view above the code
    assert len(nv.windows) == nwins  # the code step did not take the diagram window, no new split
    ok(cli("next"))
    assert nv.current.window.handle == dwin and nv.current.window.cursor[0] == 4
    assert code.buffer.name == str(sandbox.ws / "a.txt")
    qf = nv.call("getqflist", {"items": 0})["items"]
    assert qf[0]["bufnr"] == buf.handle and qf[2]["bufnr"] == buf.handle
    assert nv.call("bufname", qf[1]["bufnr"]) == str(sandbox.ws / "a.txt")
    status = ok(cli("status"))
    assert "  1. diagram flow:2 [flow] client sends" in status and "▶ 3. diagram flow:4-5 [info] back" in status
    data = json.loads(ok(cli("--json", "status")))
    assert [s.get("diagram") for s in data["steps"]] == ["flow", None, "flow"]


def test_edit_moves_a_step_onto_a_diagram(cli, nv):
    ok(cli("start", "Edit"))
    ok(cli("diagram", "flow", "--format", "text", "--no-show", stdin=DIAGRAM))
    ok(cli("step", "a.txt:3", "--label", "code"))
    out = ok(cli("edit", "1", "7", "--diagram", "flow"))
    assert out.splitlines()[0] == "step 1/1: diagram flow:7 [info] code"
    out = ok(cli("edit", "1", "a.txt:5"))
    assert out.splitlines()[0] == "step 1/1: a.txt:5 [info] code"
    assert cli("edit", "1", "--diagram", "flow").returncode == 2


def test_step_errors(cli, nv):
    ok(cli("start", "Errors"))
    r = cli("step", "--diagram", "nope", "1")
    assert r.returncode == 6 and "no diagram \"nope\"" in r.stderr
    ok(cli("diagram", "flow", "--format", "text", "--no-show", stdin=DIAGRAM))
    r = cli("step", "--diagram", "flow", "9")
    assert r.returncode == 6 and "beyond end of file (8 lines): diagram flow" in r.stderr
    r = cli("step", "--diagram", "flow", "2", "--expect", "Storage")
    assert r.returncode == 6 and "not found in diagram flow:2" in r.stderr
    assert cli("step", "--diagram", "flow", "2", "--ref", "v1").returncode == 2
    assert cli("step", "--diagram", "flow", "a.txt:2").returncode == 6
    assert cli("diagram", "bad/name", "--format", "text", stdin=DIAGRAM).returncode == 2
    assert cli("diagram", "empty", "--format", "text", stdin="\n\n").returncode == 6
    assert json.loads(ok(cli("--json", "status")))["total"] == 0


def test_wiped_diagram_buffer_is_made_again(cli, nv):
    ok(cli("start", "Wipe"))
    ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))
    ok(cli("step", "--diagram", "flow", "2", "--label", "client"))
    old = diagram_buf(nv, "flow").handle
    nv.command(f"bwipeout! {old}")
    ok(cli("goto", "1"))
    buf = diagram_buf(nv, "flow")
    assert buf.handle != old and lines_of(nv, buf.handle) == DIAGRAM.splitlines()
    assert nv.current.buffer.handle == buf.handle
    assert [m[1] for m in ns_marks(nv, "nvtour_steps", buf.handle) if m[3].get("sign_text")] == [1]
    assert ns_marks(nv, "nvtour_diagram", buf.handle)  # the lines are coloured again


def test_clear_removes_the_diagram_and_its_window(cli, nv):
    ok(cli("start", "Clear"))
    ok(cli("step", "a.txt:3"))
    nwins = len(nv.windows)
    ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))
    assert len(nv.windows) == nwins + 1
    ok(cli("clear"))
    assert diagram_buf(nv, "flow") is None
    assert nv.exec_lua("return vim.tbl_count(_G.nvtour.state.diagrams)") == 0
    assert all(not w.buffer.name.startswith("nvtour://") for w in nv.windows)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_enter_on_a_link_opens_the_code(cli, nv, sandbox):
    ok(cli("start", "Links"))
    ok(cli("step", "b.txt:3"))
    code_win = nv.current.window
    out = ok(cli("diagram", "flow", "--format", "text", "--link", "Server=a.txt:20-22", "--link", "Store=b.txt:7",
                 stdin=DIAGRAM))
    assert ", 2 link(s), shown" in out.splitlines()[0]
    buf = diagram_buf(nv, "flow")
    (dwin,) = wins_of(nv, buf)
    links = [(m[1], m[2], m[3]["end_col"]) for m in ns_marks(nv, "nvtour_diagram", buf.handle)
             if m[3]["hl_group"] == "NvtourDiagramLink"]
    server = DIAGRAM.splitlines()[1].encode().index(b"Server")
    assert sorted(links) == [(1, server, server + 6), (6, DIAGRAM.splitlines()[6].encode().index(b"Store"),
                                                     DIAGRAM.splitlines()[6].encode().index(b"Store") + 5)]
    nv.current.window = dwin
    nv.api.win_set_cursor(dwin, [2, server + 2])
    nv.command('execute "normal \\<CR>"')
    assert nv.current.window == code_win
    assert code_win.buffer.name == str(sandbox.ws / "a.txt") and code_win.cursor[0] == 20
    assert nv.api.win_get_buf(dwin).handle == buf.handle
    nv.command("execute \"normal \\<C-o>\"")
    assert code_win.buffer.name == str(sandbox.ws / "b.txt")  # the jumplist goes back
    nv.current.window = dwin
    nv.api.win_set_cursor(dwin, [1, 0])
    nv.command('execute "normal \\<CR>"')  # no link: the usual <CR> moves down
    assert nv.current.window.handle == dwin and nv.current.window.cursor[0] == 2


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_renderer_arguments_and_output(cli, nv, tmp_path):
    ok(cli("start", "Renderer"))
    cmd = fake_renderer(tmp_path, "src = sys.stdin.read()\n"
                                  "print('\\x1b[32m' + ' '.join(sys.argv[1:]) + '\\x1b[0m   ')\n"
                                  "print('[' + src.strip() + ']')\n")
    out = ok(cli("diagram", "m", "--no-show", stdin="graph LR\nA-->B", env_extra={"NVTOUR_MERMAID_ASCII": cmd}))
    assert out.splitlines()[1:] == ["1| -f -", "2| [graph LR", "3| A-->B]"]
    out = ok(cli("diagram", "m", "--no-show", "--ascii", stdin="x", env_extra={"NVTOUR_MERMAID_ASCII": cmd}))
    assert out.splitlines()[1] == "1| -f - --ascii"
    src = tmp_path / "g.dot"
    src.write_text("digraph { a -> b }")
    out = ok(cli("diagram", "g", str(src), "--no-show", env_extra={"NVTOUR_GRAPH_EASY": cmd}))
    assert out.splitlines()[1] == "1| --from=dot --as=boxart"
    out = ok(cli("diagram", "g", "--format", "easy", "--ascii", "--no-show", stdin="[a]->[b]",
                 env_extra={"NVTOUR_GRAPH_EASY": cmd}))
    assert out.splitlines()[1] == "1| --from=txt --as=ascii"


def test_renderer_errors(cli, nv, tmp_path):
    ok(cli("start", "Renderer errors"))
    failing = fake_renderer(tmp_path, "print('Syntax error in line 2', file=sys.stderr)\nsys.exit(1)\n")
    r = cli("diagram", "m", stdin="graph LR\nA-->", env_extra={"NVTOUR_MERMAID_ASCII": failing})
    assert r.returncode == 6 and "mermaid-ascii could not render the diagram: Syntax error in line 2" in r.stderr
    r = cli("diagram", "m", stdin="graph LR", env_extra={"NVTOUR_MERMAID_ASCII": str(tmp_path / "missing")})
    assert r.returncode == 2 and "cannot run mermaid-ascii" in r.stderr
    if not shutil.which("graph-easy", path="/usr/bin:/bin"):
        r = cli("diagram", "g", "--format", "dot", stdin="digraph {}", env_extra={"PATH": "/usr/bin:/bin"})
        assert r.returncode == 2 and "graph-easy not found for --format dot" in r.stderr
    assert diagram_buf(nv, "m") is None and diagram_buf(nv, "g") is None


def real_mermaid_ascii():
    return os.environ.get("NVTOUR_MERMAID_ASCII") or shutil.which("mermaid-ascii")


@pytest.mark.skipif(not real_mermaid_ascii(), reason="mermaid-ascii is not installed")
def test_real_mermaid_sequence_diagram(cli, nv):
    ok(cli("start", "Mermaid"))
    src = "sequenceDiagram\n    participant S as Server\n    participant D as Store\n    S->>D: fetch\n    D-->>S: value\n"
    out = ok(cli("diagram", "seq", "--link", "fetch=a.txt:3", stdin=src,
                 env_extra={"NVTOUR_MERMAID_ASCII": real_mermaid_ascii()}))
    text = "\n".join(lines_of(nv, diagram_buf(nv, "seq").handle))
    assert "Server" in text and "fetch" in text and "►" in text
    assert out.splitlines()[0].endswith(", 1 link(s), header line 2, shown")
    assert "│ Server │" in out.splitlines()[2]


# ---------------------------------------------------------------------------
# Header line
# ---------------------------------------------------------------------------

SEQUENCE = "┌────────┐     ┌───────┐\n│ Client │     │ Store │\n└────┬───┘     └───┬───┘\n" + \
    "     │ get         │\n     ├────────────►│\n" * 10


def diagram_winbar(nv, win):
    """The winbar of ``win`` as text, evaluated with ``win`` as the current window."""
    return nv.exec_lua("""
        local win = ...
        return vim.api.nvim_win_call(win, function()
          return vim.api.nvim_eval_statusline(vim.wo[win].winbar, { winid = win, use_winbar = true, maxwidth = 200 }).str
        end)""", win)


def scroll(nv, win, topline, leftcol=0):
    nv.exec_lua("""
        local win, top, left = ...
        vim.api.nvim_win_call(win, function() vim.fn.winrestview({ topline = top, leftcol = left }) end)""",
                win, topline, leftcol)


def test_header_line_stays_in_the_winbar(cli, nv):
    ok(cli("start", "Header"))
    ok(cli("step", "a.txt:3", "--label", "code"))
    out = ok(cli("diagram", "seq", "--format", "text", "--header", "2", stdin=SEQUENCE))
    assert out.splitlines()[0] == "diagram seq: 23 lines, 24 columns, 0 link(s), header line 2, shown"
    assert "seq (23 lines" in ok(cli("status"))
    (win,) = wins_of(nv, diagram_buf(nv, "seq"))
    assert nv.api.get_option_value("winbar", {"win": win, "scope": "local"}) == "%{%v:lua.nvtour.winbar()%}"
    assert diagram_winbar(nv, win).startswith(" nvtour 1/1")  # the header line is in view
    scroll(nv, win, 2)
    assert diagram_winbar(nv, win).startswith(" nvtour 1/1")  # line 2 is the top line
    pad = " " * nv.call("getwininfo", win)[0]["textoff"]
    scroll(nv, win, 3)
    assert diagram_winbar(nv, win) == pad + "│ Client │     │ Store │"
    scroll(nv, win, 3, leftcol=5)
    assert diagram_winbar(nv, win) == pad + "ent │     │ Store │"  # cut at 'leftcol', as the lines below
    scroll(nv, win, 1)
    assert diagram_winbar(nv, win).startswith(" nvtour 1/1")
    nv.api.set_option_value("number", True, {"win": win})  # a gutter: the header moves right with the text
    scroll(nv, win, 3)
    textoff = nv.call("getwininfo", win)[0]["textoff"]
    assert textoff > 0 and diagram_winbar(nv, win) == " " * textoff + "│ Client │     │ Store │"


def test_header_line_without_the_tour_winbar(cli, nv):
    ok(cli("start", "No bar"))
    nv.vars["nvtour_winbar"] = False
    try:
        ok(cli("diagram", "plain", "--format", "text", stdin=SEQUENCE))  # no header: no winbar
        (win,) = wins_of(nv, diagram_buf(nv, "plain"))
        assert nv.api.get_option_value("winbar", {"win": win, "scope": "local"}) == ""
        ok(cli("diagram", "seq", "--format", "text", "--header", "2", stdin=SEQUENCE))
        assert nv.api.get_option_value("winbar", {"win": win, "scope": "local"}) == "%{%v:lua.nvtour.winbar()%}"
        assert diagram_winbar(nv, win) == ""
        scroll(nv, win, 5)
        assert diagram_winbar(nv, win).strip() == "│ Client │     │ Store │"
    finally:
        nv.vars["nvtour_winbar"] = None


def test_header_errors(cli, nv):
    ok(cli("start", "Header errors"))
    for n in ("24", "-1"):
        r = cli("diagram", "seq", "--format", "text", "--header", n, stdin=SEQUENCE)
        assert r.returncode == 6 and f"--header {n} is not a line of the diagram (1-23, or 0)" in r.stderr
    out = ok(cli("diagram", "seq", "--format", "text", "--header", "0", "--no-show", stdin=SEQUENCE))
    assert "header" not in out.splitlines()[0]


# ---------------------------------------------------------------------------
# Side of the diagram window
# ---------------------------------------------------------------------------


def test_split_puts_the_diagram_beside_the_code(cli, nv):
    columns = nv.options["columns"]
    nv.options["columns"] = 160
    try:
        split_checks(cli, nv)
    finally:
        nv.options["columns"] = columns


def split_checks(cli, nv):
    ok(cli("start", "Split"))
    ok(cli("step", "b.txt:3", "--label", "code"))
    code_win = nv.current.window
    room = code_win.width
    ok(cli("diagram", "flow", "--format", "text", "--split", "right", stdin=DIAGRAM))
    (win,) = wins_of(nv, diagram_buf(nv, "flow"))
    assert nv.call("win_screenpos", win)[0] == nv.call("win_screenpos", code_win.handle)[0]  # the same row
    assert nv.call("win_screenpos", win)[1] > nv.call("win_screenpos", code_win.handle)[1]  # on the right
    assert abs(nv.api.win_get_width(win) - code_win.width) <= 1  # half of the code window each
    assert nv.api.win_get_width(win) + code_win.width + 1 == room  # and the separator
    assert nv.api.get_option_value("winfixwidth", {"win": win}) is False
    for _ in range(2):  # the panel opens, then closes: the code and the diagram share the width again
        ok(cli("panel", "--toggle"))
        assert abs(nv.api.win_get_width(win) - code_win.width) <= 1
    assert nv.api.get_option_value("wrap", {"win": win}) is False
    assert nv.current.window == code_win
    ok(cli("diagram", "flow", "--format", "text", "--split", "left", stdin=DIAGRAM))  # moved to the left
    (left,) = wins_of(nv, diagram_buf(nv, "flow"))
    assert left != win and nv.call("win_screenpos", left)[1] < nv.call("win_screenpos", code_win.handle)[1]
    ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))  # no --split: the window stays
    assert wins_of(nv, diagram_buf(nv, "flow")) == [left]
    ok(cli("step", "--diagram", "flow", "2", "--label", "client", "--no-jump"))
    nv.api.win_close(left, True)
    ok(cli("goto", "2"))  # the window is made again on the side of the last --split
    (again,) = wins_of(nv, diagram_buf(nv, "flow"))
    assert nv.call("win_screenpos", again)[1] < nv.call("win_screenpos", code_win.handle)[1]
    ok(cli("diagram", "flow", "--format", "text", "--split", "below", stdin=DIAGRAM))
    (below,) = wins_of(nv, diagram_buf(nv, "flow"))
    assert nv.call("win_screenpos", below)[0] > nv.call("win_screenpos", code_win.handle)[0]
    assert nv.api.win_get_height(below) == 9 and nv.api.get_option_value("winfixheight", {"win": below}) is True
    ok(cli("start", "Again"))  # start forgets the side
    ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))
    (top,) = wins_of(nv, diagram_buf(nv, "flow"))
    assert nv.call("win_screenpos", top)[0] < nv.call("win_screenpos", code_win.handle)[0]
    r = cli("diagram", "flow", "--format", "text", "--split", "up", stdin=DIAGRAM)
    assert r.returncode == 2

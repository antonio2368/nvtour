"""Tests of ranges by text: FILE:/TEXT/ (parse tables are in test_unit.py)."""

from __future__ import annotations

import json

import pytest

from nvtour import cli as nvcli
from nvtour import diagram
from nvtour.errors import NvtourError
from nvtour.ranges import resolve_query
from test_diagram import DIAGRAM, diagram_buf, wins_of
from test_integration import ok

CODE = [
    "int f() {",  # 1
    "  return foo(1);",  # 2
    "}",  # 3
    "",  # 4
    "int g() {",  # 5
    "  return foo(2);",  # 6
    "}",  # 7
    "tail }",  # 8
]


def q(start, end=None, plus=0, nth=None, near=None, minus=0):
    return {"start": start, "end": end, "minus": minus, "plus": plus, "nth": nth, "near": near}


# (lines, query, (l1, l2) or the error message): the SAME table runs on the Python and on the Lua resolver.
CASES = [
    (CODE, q("int f"), (1, 1)),
    (CODE, q("foo(2)"), (6, 6)),
    (CODE, q("int g", plus=2), (5, 7)),
    (CODE, q("int g", end="}"), (5, 7)),
    (CODE, q("int f", end="}"), (1, 3)),
    (CODE, q("foo(", nth=1), (2, 2)),
    (CODE, q("foo(", nth=2), (6, 6)),
    (CODE, q("foo(", near=5), (6, 6)),
    (CODE, q("foo(", near=1), (2, 2)),
    (CODE, q("foo(", near=4), (2, 2)),  # the same distance: the earlier line
    (CODE, q("foo(", near=999), (6, 6)),
    (CODE, q("foo(", nth=1, end="}", plus=1), (2, 4)),
    (CODE, q("}", nth=2, end="tail"), (7, 8)),
    (CODE, q("tail }", end="tail"), (8, 8)),  # END may be on the START line
    (CODE, q("foo(2)", minus=1), (5, 6)),
    (CODE, q("foo(2)", minus=1, plus=1), (5, 7)),
    (CODE, q("foo(2)", minus=9), (1, 6)),  # -N stops at line 1
    (CODE, q("foo(", nth=2, minus=2, end="tail"), (4, 8)),  # END is searched from the match, not from -N
    (CODE, q("nope"), "text 'nope' not found in f.c (8 lines)"),
    (CODE, q("foo(", nth=3), "text 'foo(' has 2 match(es) in f.c, but @3 was asked"),
    (CODE, q("int f", end="zzz"), "end text 'zzz' not found in f.c after line 1 (8 lines)"),
    (CODE, q("int g", end="int f"), "end text 'int f' not found in f.c after line 5 (8 lines)"),  # only after START
    (CODE, q("}"), "text '}' matches 3 lines in f.c; choose one with @N or ~L:\n  3| }\n  7| }\n  8| tail }"),
    (["x"] * 12, q("x"), "text 'x' matches 12 lines in f.c; choose one with @N or ~L:\n"
     + "".join(f"  {n}| x\n" for n in range(1, 11)) + "  ... and 2 more"),
    (["é ü"] * 2 + ["last"], q("last"), (3, 3)),
    ([], q("x"), "text 'x' not found in f.c (0 lines)"),
]


@pytest.mark.parametrize("lines,query,expected", CASES)
def test_resolve_query_python(lines, query, expected):
    if isinstance(expected, str):
        with pytest.raises(NvtourError) as e:
            resolve_query(lines, query, "f.c")
        assert e.value.code == 6 and e.value.message == expected
    else:
        assert resolve_query(lines, query, "f.c") == expected


LUA_CALL = """
local lines, q, name = ...
local ok, a, b = pcall(_G.nvtour._resolve_query, lines, q, name)
if ok then return { a, b } end
return { err = a.msg, code = a.code }
"""


def test_resolve_query_lua_same_table(cli, nv):
    ok(cli("status"))  # loads the runtime
    for lines, query, expected in CASES:
        res = nv.exec_lua(LUA_CALL, lines, {k: v for k, v in query.items() if v is not None}, "f.c")
        if isinstance(expected, str):
            assert res == {"err": expected, "code": 6}, (query, res)
        else:
            assert res == list(expected), (query, res)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def step_json(cli, *args):
    return json.loads(ok(cli("--json", "step", *args)))


def test_step_by_text(cli, nv, sandbox):
    ok(cli("start", "Q"))
    out = ok(cli("step", "a.txt:/line 12 /", "--label", "twelve"))
    assert out.splitlines() == ["step 1/1: a.txt:12 [info] twelve", "  12| line 12 of a"]
    assert nv.current.window.cursor[0] == 12
    data = json.loads(ok(cli("--json", "status")))
    assert (data["steps"][0]["l1"], data["steps"][0]["l2"]) == (12, 12)
    res = step_json(cli, "a.txt:/line 20 /", "--no-jump")
    assert (res["l1"], res["l2"], res["resolved_from"]) == (20, 20, "/line 20 /")
    plain = step_json(cli, "a.txt:5", "--no-jump")
    assert plain.get("resolved_from") is None  # a step by numbers has no resolved_from
    assert ok(cli("step", "b.txt:/line 7 /", "--no-jump")).startswith("step 4/4: b.txt:7 ")


def test_text_with_colons_and_a_relative_path(cli, nv, tmp_path):
    f = tmp_path / "cache.cpp"
    f.write_text("a\nvoid Cache::get(int k)\nb\nhttp://x/y\n")
    ok(cli("start", "Q"))
    assert ok(cli("step", f"{f}:/Cache::get/")).startswith("step 1/1: ")
    res = step_json(cli, f"{f}:/http:\\/\\/x\\/y/", "--no-jump")
    assert (res["l1"], res["resolved_from"]) == (4, "/http:\\/\\/x\\/y/")


def test_ambiguous_text_is_exit_6_and_lists_the_lines(cli, nv):
    ok(cli("start", "Q"))
    r = cli("step", "a.txt:/line 3/")
    assert r.returncode == 6
    assert "text 'line 3' matches 11 lines in a.txt; choose one with @N or ~L:" in r.stderr
    assert "  3| line 3 of a\n  30| line 30 of a\n" in r.stderr
    assert "  ... and 1 more" in r.stderr and "39| line 39" not in r.stderr
    assert json.loads(ok(cli("--json", "status")))["steps"] == []  # no half step


def test_not_found_and_other_errors(cli, nv):
    ok(cli("start", "Q"))
    r = cli("step", "a.txt:/no such text/")
    assert r.returncode == 6 and "not found in a.txt (60 lines)" in r.stderr
    r = cli("step", "a.txt:/line 3 /@2")
    assert r.returncode == 6 and "has 1 match(es) in a.txt, but @2 was asked" in r.stderr
    r = cli("step", "a.txt:/line 3 /../zzz/")
    assert r.returncode == 6 and "end text 'zzz' not found in a.txt after line 3" in r.stderr
    r = cli("step", "a.txt:/line 3")  # no closing slash
    assert r.returncode == 6 and "'/' is missing" in r.stderr
    r = cli("step", "missing.txt:/x/")
    assert r.returncode == 6 and "file not found" in r.stderr


def test_nth_and_near(cli, nv):
    ok(cli("start", "Q"))
    assert ok(cli("step", "a.txt:/line 3/@2", "--no-jump")).startswith("step 1/1: a.txt:30 ")
    assert ok(cli("step", "a.txt:/line 3/~34", "--no-jump")).startswith("step 2/2: a.txt:34 ")
    assert ok(cli("step", "a.txt:/line 3/~1", "--no-jump")).startswith("step 3/3: a.txt:3 ")
    assert ok(cli("step", "a.txt:/line 3/~999", "--no-jump")).startswith("step 4/4: a.txt:39 ")
    assert ok(cli("step", "a.txt:/of a/@60", "--no-jump")).startswith("step 5/5: a.txt:60 ")


def test_range_from_start_to_end_and_plus(cli, nv):
    ok(cli("start", "Q"))
    assert ok(cli("step", "a.txt:/line 8 /../line 11 /", "--no-jump")).startswith("step 1/1: a.txt:8-11 ")
    assert ok(cli("step", "a.txt:/line 8 /+2", "--no-jump")).startswith("step 2/2: a.txt:8-10 ")
    assert ok(cli("step", "a.txt:/line 3/@2../line 34 /+1", "--no-jump")).startswith("step 3/3: a.txt:30-35 ")
    assert ok(cli("step", "a.txt:/line 3/@2+2", "--no-jump")).startswith("step 4/4: a.txt:30-32 ")
    assert ok(cli("step", "a.txt:/line 20 /../line 20 /", "--no-jump")).startswith("step 5/5: a.txt:20 ")
    r = cli("step", "a.txt:/line 59 /+5")
    assert r.returncode == 6 and "range 59-64 is beyond end of file (60 lines)" in r.stderr


def test_lines_before_the_text(cli, nv):
    ok(cli("start", "Q"))
    assert ok(cli("step", "a.txt:/line 12 /-2", "--no-jump")).startswith("step 1/1: a.txt:10-12 ")
    assert ok(cli("step", "a.txt:/line 12 /-2..+1", "--no-jump")).startswith("step 2/2: a.txt:10-13 ")
    assert ok(cli("step", "a.txt:/line 12 /-1../line 14 /", "--no-jump")).startswith("step 3/3: a.txt:11-14 ")
    assert ok(cli("step", "a.txt:/line 2 /-5", "--no-jump")).startswith("step 4/4: a.txt:1-2 ")  # stops at line 1
    res = step_json(cli, "a.txt:/line 20 /-1..+1", "--no-jump")
    assert (res["l1"], res["l2"], res["resolved_from"]) == (19, 21, "/line 20 /-1+1")


def test_query_at_a_git_ref_reads_the_old_content(cli, nv):
    ok(cli("start", "Q"))
    out = ok(cli("step", "a.txt:/old line 33 /", "--ref", "v1", "--jump"))
    assert out.splitlines() == ["step 1/1: a.txt:33 @v1 [info]", "  33| old line 33 of a"]
    r = cli("step", "a.txt:/old line 33 /")  # the working tree has no such line
    assert r.returncode == 6 and "not found in a.txt (60 lines)" in r.stderr
    r = cli("step", "a.txt:/line 55 /", "--ref", "v1")  # v1 has 40 lines
    assert r.returncode == 6 and "not found in a.txt @v1 (40 lines)" in r.stderr


def test_expect_with_a_query(cli, nv):
    ok(cli("start", "Q"))
    assert ok(cli("step", "a.txt:/line 12 /../line 14 /", "--expect", "line 13 of a", "--no-jump")).startswith("step 1/1: a.txt:12-14")
    r = cli("step", "a.txt:/line 12 /+1", "--expect", "line 14 of a")
    assert r.returncode == 6 and "--expect 'line 14 of a' not found" in r.stderr.replace('"', "'")
    assert "; it is on line 14" in r.stderr


def test_expect_error_tells_where_the_text_is(cli, nv):
    ok(cli("start", "Q"))
    r = cli("step", "a.txt:5", "--expect", "line 3")  # lines 3, 30-39
    assert r.returncode == 6 and r.stderr.rstrip().endswith("; it is on lines 3, 30, 31 and 8 more")
    r = cli("step", "a.txt:36", "--expect", "line 3 ")
    assert r.returncode == 6 and r.stderr.rstrip().endswith("; it is on line 3")
    r = cli("step", "a.txt:5", "--expect", "no such text")
    assert r.returncode == 6 and r.stderr.rstrip().endswith("; it is not in the file")


def test_edit_by_text(cli, nv):
    ok(cli("start", "Q"))
    ok(cli("step", "a.txt:1", "--label", "one"))
    out = ok(cli("edit", "1", "a.txt:/line 9 /"))
    assert out.splitlines() == ["step 1/1: a.txt:9 [info] one", "  9| line 9 of a"]
    res = json.loads(ok(cli("--json", "edit", "1", "a.txt:/line 20 /+1")))
    assert (res["l1"], res["l2"], res["resolved_from"]) == (20, 21, "/line 20 /+1")
    r = cli("edit", "1", "a.txt:/line 4/")
    assert r.returncode == 6 and "matches" in r.stderr
    assert json.loads(ok(cli("--json", "status")))["steps"][0]["l1"] == 20  # the failed edit changed nothing
    ok(cli("edit", "1", "a.txt:3"))  # numbers again: nothing is left of the old query
    assert json.loads(ok(cli("--json", "edit", "1", "--label", "x"))).get("resolved_from") is None


def test_focus_by_text(cli, nv):
    out = ok(cli("focus", "a.txt:/line 3 /../line 5 /", "--context", "0"))
    assert out.strip() == "focus: a.txt 3-5 (fold)"
    out = ok(cli("focus", "a.txt:/line 3 /../line 5 /", "/line 20 /", "40-41", "/line 50 /+1", "--context", "0"))
    assert out.strip() == "focus: a.txt 3-5 20 40-41 50-51 (fold)"
    r = cli("focus", "a.txt:/line 3 /", "/line 4/")
    assert r.returncode == 6 and "matches" in r.stderr
    r = cli("focus", "a.txt:/line 3 /", "/zzz/")
    assert r.returncode == 6 and "not found in a.txt (60 lines)" in r.stderr
    ok(cli("unfocus"))


def test_diagram_link_by_text(cli, nv, sandbox):
    out = ok(cli("diagram", "flow", "--format", "text", "--link", "Server=a.txt:/line 4 /", "--link", "Store=b.txt:/line 7 /+1",
                 stdin=DIAGRAM))
    assert ", 2 link(s), shown" in out.splitlines()[0]
    buf = diagram_buf(nv, "flow")
    (dwin,) = wins_of(nv, buf)
    server = DIAGRAM.splitlines()[1].encode().index(b"Server")
    nv.current.window = dwin
    nv.api.win_set_cursor(dwin, [2, server + 2])
    nv.command('execute "normal \\<CR>"')
    code = nv.current.window
    assert code.buffer.name == str(sandbox.ws / "a.txt") and code.cursor[0] == 4
    r = cli("diagram", "flow", "--format", "text", "--link", "Server=a.txt:/line 4/", stdin=DIAGRAM)
    assert r.returncode == 6 and "--link 'Server': text 'line 4' matches" in r.stderr
    r = cli("diagram", "flow", "--format", "text", "--link", "Server=a.txt:/zzz/", stdin=DIAGRAM)
    assert r.returncode == 6 and "--link 'Server': text 'zzz' not found in a.txt (60 lines)" in r.stderr


def test_parse_link_with_a_query(sandbox, tmp_path):
    target = str(sandbox.ws / "a.txt")
    lines = ["│ x = y │"]
    link = diagram.parse_link(f"x = y={target}:/line 4 /../line 6 /", lines)  # the link text has '='
    assert link == {"text": "x = y", "file": target, "l1": 4, "l2": 6}
    (tmp_path / "eq.txt").write_text("a\nlet x = 1\nb\n")
    link = diagram.parse_link(f"x = y={tmp_path}/eq.txt:/x = 1/", lines)  # and so has the text range
    assert (link["text"], link["l1"], link["l2"]) == ("x = y", 2, 2)
    assert nvcli.query_args(q("a = b", plus=1))["resolved_from"] == "/a = b/+1"


def test_step_on_a_diagram_by_label(cli, nv):
    ok(cli("start", "Q"))
    ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))
    out = ok(cli("step", "--diagram", "flow", "/Server/", "--label", "server", "--no-jump"))
    assert out.splitlines()[0] == "step 1/1: diagram flow:2 [info] server"
    res = step_json(cli, "--diagram", "flow", "/fetch/+1", "--no-jump")
    assert (res["l1"], res["l2"], res["resolved_from"]) == (4, 5, "/fetch/+1")
    out = ok(cli("edit", "1", "--diagram", "flow", "/Store/"))
    assert out.splitlines()[0].startswith("step 1/2: diagram flow:7 ")
    r = cli("step", "--diagram", "flow", "/┌/")
    assert r.returncode == 6 and "matches" in r.stderr and "diagram flow" in r.stderr
    r = cli("step", "--diagram", "flow", "/Nothing/")
    assert r.returncode == 6 and "not found in diagram flow (8 lines)" in r.stderr

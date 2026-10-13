"""Tests of `apply`: a whole tour from a TOML or JSON file, in one call."""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys

import pytest

from conftest import clean_env
from nvtour import cli as nvtour_cli
from nvtour.errors import NvtourError
from test_integration import GOTO, find_buf, lines_of, ok, sheet  # noqa: F401 (the fixture)

DIAGRAM = """\
┌────┐
│ A  │
└────┘
   │ get
┌────┐
│ B  │
└────┘
"""

# Five steps, a focus and a text diagram: every key of the format is used.
TOUR = f'''
title = "Apply tour"
sheet = "arch"
panel = """
**Cause**: it breaks.
**Fix**: do not.
"""

[[focus]]
file = "b.txt"
ranges = ["10-12", "/line 20 of b/"]
dim = true
context = 1

[[diagram]]
name = "flow"
format = "text"
source = """
{DIAGRAM}"""
links = ["get=a.txt:/line 8 of a/"]
split = "below"

[[step]]
at = "a.txt:/line 3 of a/"
role = "fault"
label = "first"
note = "The note of the first step."
expect = ["line 3"]
chapter = "The bug"
sheet = "#intro"

[[step]]
at = "b.txt:5-6"
role = "flow"
label = "second"
via = "it goes on"
suggest = ["x = 1;", "y = 2;"]

[[step]]
at = "a.txt:20-22"
from = 1
via = "back"
sheet = "other#s3"

[[step]]
at = "5-6"
diagram = "flow"
expect = "B"
chapter = "The fix"

[[step]]
at = "a.txt:/line 40 of a/+1"
role = "fix"
'''

SMALL = """
title = "Small"

[[step]]
at = "a.txt:3"
label = "one"

[[step]]
at = "b.txt:4-5"
label = "two"
"""


def status(cli):
    return json.loads(ok(cli("--json", "status")))


def stable(data):
    """The status without what changes by itself."""
    return {k: v for k, v in data.items() if k not in ("pid", "socket")}


def apply(cli, text, *flags):
    return cli("apply", *flags, "-", stdin=text)


def test_toml_tour_in_one_call(cli, nv, sheet):
    out = ok(apply(cli, TOUR))
    assert out.splitlines() == [
        "step 1/5: a.txt:3 [fault] first  ↔ sheet arch#intro",
        "  3| line 3 of a",
        "step 2/5: b.txt:5-6 [flow] second",
        "  5| line 5 of b",
        "step 3/5: a.txt:20-22 [info]  ↔ sheet other#s3",
        "  20| line 20 of a",
        "step 4/5: diagram flow:5-6 [info]",
        "  5| ┌────┐",
        "step 5/5: a.txt:40-41 [fix]",
        "  40| line 40 of a",
        "panel: 2 lines",
        "chapters: 2",
        "jumped to step 1",
    ]
    data = status(cli)
    assert (data["title"], data["tour_sheet"], data["total"], data["current"]) == ("Apply tour", "arch", 5, 1)
    steps = data["steps"]
    assert [s.get("label") for s in steps][:2] == ["first", "second"]
    assert [s["role"] for s in steps] == ["fault", "flow", "info", "info", "fix"]
    assert steps[0]["expect"] == ["line 3"] and steps[0]["note"] == "The note of the first step."
    assert steps[0]["sheet"] == "arch#intro" and steps[2]["sheet"] == "other#s3"
    assert steps[1]["via"] == "it goes on" and steps[1]["suggest"] == ["x = 1;", "y = 2;"]
    assert steps[2]["from"] == 1 and steps[2]["via"] == "back" and steps[1].get("from") is None
    assert steps[3]["diagram"] == "flow" and (steps[3]["l1"], steps[3]["l2"]) == (5, 6) and steps[3]["expect"] == ["B"]
    assert (steps[4]["l1"], steps[4]["l2"]) == (40, 41)  # /line 40 of a/+1
    assert [(c["name"], c["step"]) for c in data["chapters"]] == [("The bug", 1), ("The fix", 4)]
    assert data["focus"] == [{"file": str(nv.call("getcwd") + "/b.txt"), "ranges": [[9, 13], [19, 21]], "mode": "dim"}]
    assert data["diagrams"] == [{"name": "flow", "count": 7, "links": 1}]
    panel = lines_of(nv, find_buf(nv, "nvtour://panel").handle)
    assert any("**Cause**: it breaks." in line for line in panel) and any(line.startswith("## The bug") for line in panel)
    # one jump, to step 1: the view is on a.txt:3 and the sheet got the call of step 1 only
    assert nv.current.buffer.name.endswith("a.txt") and nv.current.window.cursor[0] == 3
    assert sheet.wait(1) == [f"{GOTO} arch intro"] and len(sheet.settle()) == 1
    # one list for the whole tour, at step 1
    qf = nv.call("getqflist", {"id": 0, "items": 0, "idx": 0, "title": 0})
    assert len(qf["items"]) == 5 and qf["idx"] == 1 and qf["title"] == "nvtour: Apply tour"


def test_json_input(cli):
    tour = {"title": "Json", "panel": "p", "step": [
        {"at": "a.txt:3", "label": "one", "expect": "line 3", "chapter": "C", "suggest": "z = 0;\n"},
        {"at": "b.txt:4", "role": "fix", "from": 1}]}
    out = ok(apply(cli, json.dumps(tour)))
    assert out.splitlines()[0] == "step 1/2: a.txt:3 [info] one"
    assert out.splitlines()[-3:] == ["panel: 1 line", "chapters: 1", "jumped to step 1"]
    data = status(cli)
    assert data["title"] == "Json" and data["steps"][1]["from"] == 1 and data["steps"][0]["suggest"] == ["z = 0;"]
    r = cli("apply", "-", stdin='  \n{"step": [{"at": "a.txt:3", "lable": "x"}]}')  # leading white space is fine
    assert r.returncode == 2 and 'step[1]: unknown key "lable" (did you mean "label"?)' in r.stderr
    r = cli("apply", "-", stdin='{"step": [')
    assert r.returncode == 2 and "bad JSON" in r.stderr


def test_json_output_has_the_same_data(cli):
    data = json.loads(ok(cli("--json", "apply", "-", stdin=SMALL)))
    assert data["ok"] and data["total"] == 2 and data["chapters"] == 0 and data["panel_lines"] == 0
    assert data["jumped_to"] == 1 and data["current"] == 1
    assert [(s["n"], s["total"], s["l1"], s["l2"], s["label"], s["text"]) for s in data["steps"]] == [
        (1, 2, 3, 3, "one", "line 3 of a"), (2, 2, 4, 5, "two", "line 4 of b")]
    assert "keys" not in data["steps"][0]


def test_the_tour_is_at_step_1_and_no_jump_leaves_the_view(cli, nv):
    ok(cli("start", "Old"))
    ok(cli("step", "b.txt:20", "--label", "old"))
    ok(apply(cli, SMALL, "--no-jump"))
    assert status(cli)["current"] == 0  # like a first step with --no-jump
    out = ok(apply(cli, SMALL))
    assert out.splitlines()[-1] == "jumped to step 1" and status(cli)["current"] == 1
    out = ok(apply(cli, SMALL, "--no-jump"))
    assert "jumped" not in out


def test_a_new_tour_replaces_the_old_one(cli, nv, sheet):
    ok(apply(cli, TOUR))
    assert find_buf(nv, "nvtour://diagram/flow") is not None
    ok(apply(cli, SMALL))
    data = status(cli)
    assert (data["title"], data["total"], data["chapters"], data["focus"], data["diagrams"]) == ("Small", 2, [], [], [])
    assert data.get("tour_sheet") is None and find_buf(nv, "nvtour://diagram/flow") is None


def test_append_adds_steps_and_keeps_the_title(cli, sheet):
    ok(cli("start", "Kept", "--sheet", "arch"))
    ok(cli("step", "a.txt:3", "--label", "old one"))
    ok(cli("chapter", "Pending"))
    extra = '''
panel = "added"

[[step]]
at = "b.txt:4"
label = "new one"
via = "on"
from = 1
sheet = "#x"
chapter = "Added"

[[step]]
at = "b.txt:6"
label = "new two"
from = 2
'''
    out = ok(apply(cli, extra, "--append"))
    lines = out.splitlines()
    assert lines[0] == "step 2/3: b.txt:4 [info] new one  ↔ sheet arch#x" and lines[2].startswith("step 3/3: b.txt:6")
    assert lines[-3:] == ["panel: 1 line", "chapters: 1", "jumped to step 2"]  # the first new step
    data = status(cli)
    assert data["title"] == "Kept" and data["tour_sheet"] == "arch" and data["total"] == 3 and data["current"] == 2
    assert [s["label"] for s in data["steps"]] == ["old one", "new one", "new two"]
    # `from` is the number in the final tour: the old step is 1, the first new step is 2
    assert [s.get("from") for s in data["steps"]] == [None, 1, 2]
    # the pending chapter took the first new step, and the chapter of the file replaced its name
    assert [(c["name"], c["step"]) for c in data["chapters"]] == [("Added", 2)]
    ok(apply(cli, extra.replace('panel = "added"', ""), "--append", "--no-jump"))
    assert status(cli)["total"] == 5 and status(cli)["current"] == 2


def test_append_refuses_a_title_or_a_sheet(cli):
    ok(cli("start", "Kept"))
    for head in ('title = "New"\n', 'sheet = "arch"\n'):
        r = apply(cli, head + '[[step]]\nat = "a.txt:3"\n', "--append")
        assert r.returncode == 2 and "apply --append" in r.stderr, r.stderr
    assert status(cli)["total"] == 0 and status(cli)["title"] == "Kept"


def test_a_bad_expect_in_step_4_leaves_the_old_tour(cli, nv):
    ok(apply(cli, SMALL))
    before = stable(status(cli))
    bad = '[[step]]\nat = "a.txt:1"\n' * 3 + '[[step]]\nat = "a.txt:2"\nexpect = ["nowhere"]\n'
    r = apply(cli, 'title = "Never"\n' + bad)
    assert r.returncode == 6, (r.stdout, r.stderr)
    assert r.stderr.startswith('step[4]: --expect "nowhere" not found in a.txt:2'), r.stderr
    assert stable(status(cli)) == before
    r = cli("--json", "apply", "-", stdin=bad)
    assert json.loads(r.stdout)["code"] == 6 and "step[4]" in json.loads(r.stdout)["error"]
    assert stable(status(cli)) == before
    marks = nv.exec_lua("return #_G.nvtour.state.tour.steps")
    assert marks == 2


@pytest.mark.parametrize("entry, message", [
    ('[[step]]\nat = "a.txt:99"\n', "step[1]: range 99-99 is beyond end of file (60 lines): a.txt"),
    ('[[step]]\nat = "a.txt:/line 1/"\n', "step[1]: text 'line 1' matches"),
    ('[[step]]\nat = "a.txt:/zzz/"\n', "step[1]: text 'zzz' not found in a.txt (60 lines)"),
    ('[[step]]\nat = "nofile.txt:3"\n', "step[1]: file not found"),
    ('[[step]]\nat = "a.txt:3"\nfrom = 1\n', "step[1]: from 1: no such step before this one"),
    ('[[step]]\nat = "a.txt:3"\n[[step]]\nat = "a.txt:3"\nfrom = 2\n', "step[2]: from 2: no such step before this one"),
    ('[[step]]\nat = "a.txt:3"\nsheet = "#x"\n', "step[1]: --sheet #x: the tour has no sheet"),
    ('[[step]]\nat = "a.txt:3"\nsheet = "x"\n', "step[1]: --sheet 'x': expected SHEET#STEP"),
    ('[[step]]\nat = "a.txt:3"\nchapter = "  "\n', 'step[1]: "chapter" must not be empty'),
    ('[[step]]\nat = "5-6"\ndiagram = "nope"\n', 'step[1]: no diagram "nope" in the file'),
    ('[[diagram]]\nname = "d"\nformat = "text"\nsource = "one\\ntwo"\n[[step]]\nat = "a.txt:3"\n[[step]]\nat = "2-3"\ndiagram = "d"\n',
     "step[2]: range 2-3 is beyond end of file (2 lines): diagram d"),
    ('[[diagram]]\nname = "d"\nformat = "text"\nsource = "one"\nlinks = ["zzz=a.txt:3"]\n', "diagram[1]: --link text 'zzz' is not in the diagram"),
    ('[[focus]]\nfile = "a.txt"\nranges = ["70-71"]\n', "focus[1]: bad range 70-71 for"),
    ('[[focus]]\nfile = "a.txt"\nranges = ["/zzz/"]\n', "focus[1]: text 'zzz' not found in a.txt"),
])
def test_errors_name_the_entry_and_change_nothing(cli, entry, message):
    ok(apply(cli, SMALL))
    before = stable(status(cli))
    r = apply(cli, entry)
    assert r.returncode in (2, 6) and message in r.stderr, r.stderr
    assert stable(status(cli)) == before


def test_unknown_keys_and_types_are_exit_2(cli):
    ok(apply(cli, SMALL))
    before = stable(status(cli))
    step = '[[step]]\nat = "a.txt:3"\n'
    for text, message in [
        (step * 2 + '[[step]]\nat = "a.txt:3"\nlable = "x"\n', 'step[3]: unknown key "lable" (did you mean "label"?)'),
        (step + '[[stpe]]\n', 'tour: unknown key "stpe" (did you mean "step"?)'),
        ('titel = "x"\n' + step, 'tour: unknown key "titel" (did you mean "title"?)'),
        ('[[chapter]]\nname = "x"\n' + step, 'tour: unknown key "chapter" (a chapter starts at a step'),
        ('[[focus]]\nfile = "a.txt"\nranges = ["1-2"]\nlines = 3\n', 'focus[1]: unknown key "lines"'),
        ('[[diagram]]\nname = "d"\nsource = "x"\nfomat = "text"\n', 'diagram[1]: unknown key "fomat" (did you mean "format"?)'),
        ('[[step]]\nat = "a.txt:3"\nzzzzzz = 1\n', 'step[1]: unknown key "zzzzzz" (the keys are: at, ref,'),
        ('[[step]]\nat = "a.txt:3"\nlabel = 3\n', 'step[1]: "label" must be a string'),
        ('[[step]]\nat = "a.txt:3"\nfrom = 0\n', 'step[1]: "from" must be 1 or more'),
        ('[[step]]\nat = "a.txt:3"\nfrom = true\n', 'step[1]: "from" must be an integer'),
        ('[[step]]\nat = "a.txt:3"\nrole = "bad"\n', 'step[1]: "role" must be one of: fault, flow'),
        ('[[step]]\nat = "a.txt:3"\nexpect = [1]\n', 'step[1]: "expect" must be a string or a list of strings'),
        ('[[step]]\nlabel = "x"\n', 'step[1]: missing the key "at"'),
        ('[[focus]]\nfile = "a.txt"\n', 'focus[1]: missing the key "ranges"'),
        ('[[diagram]]\nname = "d"\n', 'diagram[1]: give one of "source" and "file"'),
        ('[[diagram]]\nname = "d"\nsource = "x"\nfile = "y"\n', 'diagram[1]: give one of "source" and "file"'),
        ('[[diagram]]\nname = "d"\nsource = "x"\n[[diagram]]\nname = "d"\nsource = "y"\n', 'diagram[2]: the name "d" is used by diagram[1]'),
        ('[step]\nat = "a.txt:3"\n', 'tour: "step" must be an array of tables ([[step]])'),
        ('title = 3\n', 'tour: "title" must be a string'),
        ('sheet = "-x"\n', "sheet: --sheet: bad sheet id '-x'"),
        ('title = \n', "bad TOML"),
    ]:
        r = apply(cli, text)
        assert r.returncode == 2 and message in r.stderr, (text, r.stderr)
    assert stable(status(cli)) == before


def test_parse_tour_normalizes_the_values():
    tour = nvtour_cli.parse_tour('''
title = "T"
[[step]]
at = "a:1"
expect = "x"
suggest = ["l1", "l2"]
[[step]]
at = "a:2"
expect = ["x", "y"]
suggest = "l1\\nl2\\n"
chapter = "C"
''')
    assert tour["title"] == "T" and tour["focus"] == [] and tour["diagram"] == [] and "sheet" not in tour
    assert tour["step"][0] == {"at": "a:1", "expect": ["x"], "suggest": "l1\nl2"}
    assert tour["step"][1] == {"at": "a:2", "expect": ["x", "y"], "suggest": "l1\nl2\n", "chapter": "C"}
    assert nvtour_cli.parse_tour("")["step"] == []
    assert nvtour_cli.parse_tour('{"step": [{"at": "a:1", "label": null}]}')["step"] == [{"at": "a:1"}]
    with pytest.raises(NvtourError) as e:
        nvtour_cli.parse_tour("[1, 2]")
    assert e.value.code == 2


def test_check_changes_nothing_and_prints_the_steps(cli, nv, sheet):
    ok(apply(cli, SMALL))
    before = stable(status(cli))
    bufs = nv.exec_lua("return #vim.api.nvim_list_bufs()")
    out = ok(apply(cli, TOUR, "--check"))
    assert out.splitlines() == [
        "ok step 1/5: a.txt:3 [fault] first  ↔ sheet arch#intro",
        "  3| line 3 of a",
        "ok step 2/5: b.txt:5-6 [flow] second",
        "  5| line 5 of b",
        "ok step 3/5: a.txt:20-22 [info]  ↔ sheet other#s3",
        "  20| line 20 of a",
        "ok step 4/5: diagram flow:5-6 [info]",
        "  5| ┌────┐",
        "ok step 5/5: a.txt:40-41 [fix]",
        "  40| line 40 of a",
        "check: 5 steps ok",
    ]
    assert stable(status(cli)) == before
    assert find_buf(nv, "nvtour://diagram/flow") is None and sheet.settle() == []
    data = json.loads(ok(cli("--json", "apply", "-", "--check", stdin=SMALL)))
    assert data["check"] is True and data["total"] == 2 and data["steps"][1]["text"] == "line 4 of b"
    assert stable(status(cli)) == before
    assert nv.exec_lua("return #vim.api.nvim_list_bufs()") >= bufs  # buffers that were loaded stay loaded
    out = ok(apply(cli, '[[step]]\nat = "a.txt:3"\n', "--check"))
    assert out.splitlines()[-1] == "check: 1 step ok"
    r = apply(cli, SMALL + '[[step]]\nat = "a.txt:2"\nexpect = ["nowhere"]\n', "--check")
    assert r.returncode == 6 and r.stderr.startswith("step[3]: --expect") and stable(status(cli)) == before


def test_check_with_append_numbers_from_the_end_of_the_tour(cli):
    ok(apply(cli, SMALL))
    out = ok(apply(cli, '[[step]]\nat = "a.txt:9"\nfrom = 2\nchapter = "X"\n', "--append", "--check"))
    assert out.splitlines()[0] == "ok step 3/3: a.txt:9 [info]"
    r = apply(cli, '[[step]]\nat = "a.txt:9"\nfrom = 3\n', "--append", "--check")
    assert r.returncode == 6 and "step[1]: from 3: no such step before this one (2 step(s)" in r.stderr
    assert status(cli)["total"] == 2


def test_stdin_on_a_terminal_is_refused(sandbox, nvim_proc, monkeypatch):
    master, slave = pty.openpty()
    try:
        cmd = [sys.executable, "-m", "nvtour.cli", "--socket", nvim_proc.socket, "apply"]
        r = subprocess.run(cmd, cwd=sandbox.ws, env=clean_env(sandbox.runtime), stdin=slave, capture_output=True,
                           text=True, timeout=30)
    finally:
        os.close(master)
        os.close(slave)
    assert r.returncode == 2 and "apply - reads stdin, but stdin is a terminal" in r.stderr

    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    with pytest.raises(NvtourError) as e:
        nvtour_cli.read_tour("-")
    assert e.value.code == 2


def test_file_argument(cli, tmp_path):
    path = tmp_path / "tour.toml"
    path.write_text(SMALL)
    out = ok(cli("apply", str(path)))
    assert out.splitlines()[0] == "step 1/2: a.txt:3 [info] one"
    (tmp_path / "tour.json").write_text(json.dumps({"step": [{"at": "a.txt:5"}]}))
    assert ok(cli("apply", str(tmp_path / "tour.json"))).splitlines()[0] == "step 1/1: a.txt:5 [info]"
    r = cli("apply", str(tmp_path / "missing.toml"))
    assert r.returncode == 6 and "file not found" in r.stderr


def test_entries_at_a_git_ref(cli, nv, sandbox):
    tour = '''
[[step]]
at = "a.txt:3-4"
ref = "v1"
role = "fault"
label = "old"
expect = "old line 3"

[[step]]
at = "a.txt:/old line 10/"
ref = "v1"
label = "old again"

[[step]]
at = "a.txt:3"
label = "new"
'''
    out = ok(apply(cli, tour))
    assert out.splitlines()[:5] == [
        "step 1/3: a.txt:3-4 @v1 [fault] old", "  3| old line 3 of a",
        "step 2/3: a.txt:10 @v1 [info] old again", "  10| old line 10 of a",
        "step 3/3: a.txt:3 [info] new"]
    data = status(cli)
    assert [s.get("ref") for s in data["steps"]] == ["v1", "v1", None] and len(data["steps"][0]["sha"]) == 40
    assert data["steps"][0]["sha"] == data["steps"][1]["sha"]
    bufs = [b for b in nv.buffers if b.name.startswith("nvtour://v1/")]
    assert len(bufs) == 1 and len(bufs[0][:]) == 40  # the old a.txt, made once
    r = apply(cli, tour.replace("old line 3", "nowhere"))
    assert r.returncode == 6 and r.stderr.startswith('step[1]: --expect "nowhere" not found in a.txt:3-4 @v1')
    r = apply(cli, '[[step]]\nat = "a.txt:3"\nref = "no-such-ref"\n')
    assert r.returncode == 6 and r.stderr.startswith("step[1]: unknown git ref: no-such-ref")
    r = apply(cli, '[[step]]\nat = "a.txt:50"\nref = "v1"\n')
    assert r.returncode == 6 and "step[1]: range 50-50 is beyond end of file (40 lines): a.txt @v1" in r.stderr


def test_a_git_blob_is_sent_once(sandbox, monkeypatch):
    monkeypatch.chdir(sandbox.ws)
    calls = []
    real = nvtour_cli.gitutil.blob_at
    monkeypatch.setattr(nvtour_cli.gitutil, "blob_at", lambda *a: calls.append(a) or real(*a))
    args = nvtour_cli.build_parser().parse_args(["apply", "-"])
    monkeypatch.setattr(nvtour_cli, "read_tour", lambda path: '''
[[step]]
at = "a.txt:3"
ref = "v1"
[[step]]
at = "a.txt:5"
ref = "v1"
[[step]]
at = "a.txt:7"
ref = "HEAD"
[[step]]
at = "a.txt:9"
ref = "v1^{commit}"
''')
    req = nvtour_cli.apply_request(args, {"workspace": str(sandbox.ws)})
    assert len(calls) == 3  # v1, HEAD and v1^{commit} are read once each
    # the lines go with the first step on a commit and file; "v1^{commit}" is the same commit as v1
    assert [("lines" in s) for s in req["steps"]] == [True, False, True, False]
    assert all(v is not None for s in req["steps"] for v in s.values())  # nested nils do not survive msgpack
    assert req["append"] is None and req["check"] is None and req["no_jump"] is None and req["focus"] == []


def test_text_queries_in_at_and_in_focus(cli, nv):
    tour = '''
[[focus]]
file = "a.txt"
ranges = ["/line 10 of a/../line 12 of a/"]

[[step]]
at = "a.txt:/line 10 of a/../line 12 of a/"
expect = ["line 11"]

[[step]]
at = "b.txt:/line 1 of b/@1+1"

[[step]]
at = "b.txt:/line 2/~25"
'''
    out = ok(apply(cli, tour))
    assert out.splitlines()[:6] == [
        "step 1/3: a.txt:10-12 [info]", "  10| line 10 of a",
        "step 2/3: b.txt:1-2 [info]", "  1| line 1 of b",
        "step 3/3: b.txt:25 [info]", "  25| line 25 of b"]
    assert nv.eval("foldclosed(1)") == 1 and nv.eval("foldclosed(11)") == -1  # the focus is folded in the shown file
    data = json.loads(ok(cli("--json", "apply", "-", stdin=tour)))
    assert data["steps"][0]["resolved_from"] == "/line 10 of a/../line 12 of a/"
    assert data["steps"][2]["resolved_from"] == "/line 2/~25"
    r = apply(cli, '[[step]]\nat = "a.txt:3"\n[[step]]\nat = "a.txt:/line 1/"\n')
    assert r.returncode == 6 and r.stderr.startswith("step[2]: text 'line 1' matches") and "choose one with @N or ~L" in r.stderr


def test_diagram_steps_and_diagram_options(cli, nv, tmp_path):
    (tmp_path / "flow.txt").write_text(DIAGRAM)
    tour = f'''
[[diagram]]
name = "one"
file = "{tmp_path / 'flow.txt'}"
ascii = true
header = 2
show = false

[[diagram]]
name = "two"
format = "text"
source = "x\\ny\\nz"
links = ["y=a.txt:7-8"]
split = "right"

[[step]]
at = "6"
diagram = "one"
expect = "B"
label = "on one"

[[step]]
at = "/y/"
diagram = "two"
label = "on two"

[[step]]
at = "a.txt:3"
'''
    out = ok(apply(cli, tour))
    assert out.splitlines()[0] == "step 1/3: diagram one:6 [info] on one"
    assert out.splitlines()[2] == "step 2/3: diagram two:2 [info] on two" and out.splitlines()[3] == "  2| y"
    data = status(cli)
    assert data["diagrams"] == [{"name": "one", "count": 7, "links": 0}, {"name": "two", "count": 3, "links": 1}]
    assert find_buf(nv, "nvtour://diagram/one") is not None
    r = apply(cli, tour.replace("header = 2", "header = 9"))
    assert r.returncode == 6 and r.stderr.startswith("diagram[1]: header 9 is not a line of the diagram")
    assert status(cli)["diagrams"] == data["diagrams"]


def test_append_can_use_a_diagram_of_the_tour(cli):
    ok(apply(cli, '[[diagram]]\nname = "d"\nformat = "text"\nsource = "a\\nb\\nc"\n[[step]]\nat = "a.txt:3"\n'))
    more = '[[step]]\nat = "3"\ndiagram = "d"\nexpect = "c"\n'
    out = ok(apply(cli, more, "--append"))
    assert out.splitlines()[0] == "step 2/2: diagram d:3 [info]"
    r = apply(cli, more.replace('"3"', '"4"', 1), "--append")
    assert r.returncode == 6 and "step[1]: range 4-4 is beyond end of file (3 lines): diagram d" in r.stderr
    # a new tour does not have the old diagram
    r = apply(cli, more)
    assert r.returncode == 6 and 'step[1]: no diagram "d" in the file' in r.stderr
    # a diagram of the file that is shorter than a step of the tour on it
    r = apply(cli, '[[diagram]]\nname = "d"\nformat = "text"\nsource = "a"\n', "--append")
    assert r.returncode == 6 and r.stderr.startswith("diagram[1]: step 2 (lines 3) is beyond the new diagram (1 lines)")
    assert status(cli)["total"] == 2


def test_sheet_links_use_the_sheet_of_the_new_tour(cli, sheet):
    ok(cli("start", "Old", "--sheet", "old"))
    r = apply(cli, '[[step]]\nat = "a.txt:3"\nsheet = "#x"\n')  # the old sheet does not count
    assert r.returncode == 6 and "the tour has no sheet" in r.stderr
    out = ok(apply(cli, 'sheet = "new"\n[[step]]\nat = "a.txt:3"\nsheet = "#x"\n'))
    assert out.splitlines()[0] == "step 1/1: a.txt:3 [info]  ↔ sheet new#x"
    assert sheet.wait(1) == [f"{GOTO} new x"]
    r = cli("apply", "-", stdin='sheet = "-bad"\n')
    assert r.returncode == 2 and "bad sheet id" in r.stderr


def test_panel_is_left_alone_without_text_and_changed_with_it(cli, nv):
    ok(apply(cli, 'panel = "first text"\n[[step]]\nat = "a.txt:3"\n'))
    panel = lambda: "\n".join(lines_of(nv, find_buf(nv, "nvtour://panel").handle))  # noqa: E731
    assert "first text" in panel()
    out = ok(apply(cli, '[[step]]\nat = "a.txt:4"\n', "--append"))
    assert out.splitlines()[-3] == "panel: 1 line" and "first text" in panel()
    ok(apply(cli, '[[step]]\nat = "a.txt:4"\n'))  # a new tour: no text
    assert "first text" not in panel()
    assert ok(apply(cli, "")).splitlines() == ["panel: 0 lines", "chapters: 0"]  # no steps: nothing to jump to
    assert status(cli)["total"] == 0


def test_the_files_are_not_changed(cli, sandbox, sheet):
    before = {p: p.read_bytes() for p in (sandbox.ws / "a.txt", sandbox.ws / "b.txt")}
    ok(apply(cli, TOUR))
    assert {p: p.read_bytes() for p in before} == before


# The tour of the documentation (README.md and DESIGN.md section 15), with the paths and texts of the
# test repository: the same keys and the same three steps.
DOCUMENTED = f'''
title = "Dangling iterator in cleanup"
sheet = "arch"                       # optional, like start --sheet
panel = """
**Cause**: use after erase. **Fix**: copy the value before `erase()`.
"""

[[focus]]                            # optional, like nvtour focus
file = "a.txt"
ranges = ["10-12", "/line 20 of a/../line 22 of a/"]
dim = false
context = 2

[[diagram]]                          # optional, like nvtour diagram
name = "read"
format = "text"
source = """
{DIAGRAM}"""
links = ["get=a.txt:/line 8 of a/+1"]
split = "above"

[[step]]
at = "a.txt:/line 3 of a/"           # FILE:L1[-L2] or any text range
role = "fault"
label = "dangling iterator"
note = "The iterator is used after `erase()` invalidated it."
expect = ["line 3"]                  # list or one string
suggest = """
        x = 1;
"""                                  # string with new lines, or a list of lines
sheet = "#evict"                     # SHEET#STEP or #STEP
chapter = "The bug"                  # starts a chapter at this step

[[step]]
at = "a.txt:/line 3 of a/"
ref = "v1"                           # optional: the file at a git ref (not with diagram)
role = "context"
label = "the old code"

[[step]]
at = "2"                             # with diagram: L1[-L2] or a text range of the diagram
diagram = "read"                     # optional: a step on a diagram (not with ref)
label = "the read"
via = "the cleanup thread runs `evict()`"
from = 1                             # optional: the link comes from step 1 (an earlier step)
'''


def test_the_documented_tour_shape_works(cli, nv, sheet):
    before = stable(status(cli))
    out = ok(apply(cli, DOCUMENTED, "--check"))
    assert out.splitlines() == [
        "ok step 1/3: a.txt:3 [fault] dangling iterator  ↔ sheet arch#evict",
        "  3| line 3 of a",
        "ok step 2/3: a.txt:3 @v1 [context] the old code",
        "  3| old line 3 of a",
        "ok step 3/3: diagram read:2 [info] the read",
        "  2| │ A  │",
        "check: 3 steps ok",
    ]
    assert stable(status(cli)) == before
    out = ok(apply(cli, DOCUMENTED))
    assert out.splitlines() == [line[3:] if line.startswith("ok ") else line for line in out.splitlines()[:6]] + [
        "panel: 1 line", "chapters: 1", "jumped to step 1"]
    data = status(cli)
    assert (data["title"], data["tour_sheet"], data["total"], data["current"]) == ("Dangling iterator in cleanup", "arch", 3, 1)
    steps = data["steps"]
    assert steps[1]["ref"] == "v1" and steps[2]["diagram"] == "read" and steps[2]["from"] == 1
    assert steps[0]["suggest"] == ["        x = 1;"] and steps[0]["expect"] == ["line 3"]
    assert [(c["name"], c["step"]) for c in data["chapters"]] == [("The bug", 1)]
    assert [f["mode"] for f in data["focus"]] == ["fold"] and data["diagrams"] == [{"name": "read", "count": 7, "links": 1}]
    assert sheet.wait(1) == [f"{GOTO} arch evict"]


def test_the_examples_of_the_docs_parse():
    import re
    from pathlib import Path

    from nvtour.cli import STEP_KEYS, parse_tour

    root = Path(__file__).resolve().parent.parent
    for name, pattern in (("README.md", r"nvtour apply - <<\'EOF\'\n(.*?)\nEOF\n"), ("DESIGN.md", r"## 15\..*?```toml\n(.*?)\n```")):
        text = (root / name).read_text()
        m = re.search(pattern, text, re.S)
        assert m, name
        tour = parse_tour(m.group(1))
        assert len(tour["step"]) == 3 and len(tour["focus"]) == 1 and len(tour["diagram"]) == 1, name
        assert set().union(*tour["step"]) == set(STEP_KEYS), name  # every key of a step is shown
        assert ("ref" in tour["step"][1]) and ("diagram" in tour["step"][2]), name
        assert not any("ref" in s and "diagram" in s for s in tour["step"]), name
        assert tour["step"][2]["from"] == 1 and "from" not in tour["step"][0], name

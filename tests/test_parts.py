"""Tests of parts: the places of a range that the note links to with [words](a), and their focus."""

from __future__ import annotations

import json

import pytest
from test_integration import content, ok, step_marks
from test_side import card, dispatch, side  # noqa: F401 (the fixture)

NBSP = " "


@pytest.fixture()
def wide(nv):
    nv.command("set columns=110")
    yield
    nv.command("set columns=80")


def eol(nv):
    """Row -> the end-of-line text of the marks of a.txt."""
    out: dict[int, str] = {}
    for m in step_marks(nv, "a.txt"):
        if m[3].get("virt_text_pos") == "eol":
            out[m[1]] = out.get(m[1], "") + "".join(c[0] for c in m[3]["virt_text"])
    return out


def hl(nv, group):
    return sorted((m[1], m[2], m[3].get("end_col")) for m in step_marks(nv, "a.txt") if m[3].get("hl_group") == group)


def note_chunks(nv, row):
    m = next(m[3] for m in step_marks(nv, "a.txt") if m[3].get("virt_lines_above") and m[1] == row)
    return [c for line in m["virt_lines"] for c in content(line)]


def test_parts_show_their_letters_and_the_note_links(cli, nv, wide):
    ok(cli("start", "Parts"))
    ok(cli("step", "a.txt:5-8", "--label", "four", "--part", "5", "--part", "6", "--part-text", "line 6",
           "--part", "/line 6/", "--part-text", "of a", "--part", "/line 7/../line 8/",
           "--note", "Lead.\n[The first](a), [the second](b) and [all](a,b,c,d).\nSee (a) and [x](note)."))
    ok(cli("step", "a.txt:20", "--note", "[Other](a) words.", "--part", "20"))
    # One tag for each line at its end, after the step marker on l1; nothing inside the code.
    assert eol(nv) == {4: "  ← 1 four  (a)", 5: "  (b, c)", 6: "  (d)", 19: "  ← 2"}
    assert not any(m[3].get("virt_text_pos") == "inline" for m in step_marks(nv, "a.txt"))
    assert hl(nv, "NvtourPartText") == [(5, 0, 6), (5, 7, 11)]  # the expressions of b and c
    chunks = note_chunks(nv, 4)
    text = "".join(c[0] for c in chunks)
    assert f"The first{NBSP}(a), the second{NBSP}(b) and all{NBSP}(a,{NBSP}b,{NBSP}c,{NBSP}d)." in text
    assert "See (a) and [x](note)." in text  # "(a)" alone, and a link to a word, stay as typed
    assert ["The first", "NvtourNote"] in chunks and [f"{NBSP}(a)", "NvtourPartRef"] in chunks
    # The linked words are dotted (NvtourPartLink after the note group).
    words = next(m for m in step_marks(nv, "a.txt") if m[3].get("virt_lines_above") and m[1] == 4)
    groups = [c[1] for line in words[3]["virt_lines"] for c in line if c[0] == "The first"]
    assert groups and "NvtourPartLink" in groups[0]
    # The other step shows the words of its links only, and no tags.
    collapsed = note_chunks(nv, 19)
    assert "".join(c[0] for c in collapsed).endswith("Other words.")
    data = json.loads(ok(cli("--json", "status")))
    assert data["steps"][0]["parts"] == ["a: 5 (lines 5-5)", "b: 6 (lines 6-6) `line 6`",
                                         "c: /line 6/ (lines 6-6) `of a`", "d: /line 7/../line 8/ (lines 7-8)"]


def test_focus_dims_the_rest_of_the_range(cli, nv, wide):
    ok(cli("start", "Focus"))
    ok(cli("step", "a.txt:5-7", "--part", "6", "--part-text", "line 6", "--part", "6", "--part-text", "of a",
           "--part", "7", "--note", "[one](a) [two](b) [three](c)"))
    ok(cli("step", "a.txt:20"))
    ok(cli("goto", "1"))

    def dimmed():
        return hl(nv, "NvtourDim")

    res = dispatch(nv, "part", dir=1)
    assert res["part"] == "a" and res["message"] == "nvtour: part a (1/3) · 6"
    # The other lines, and on line 6 all but "line 6" (bytes 0-6 of "line 6 of a").
    assert dimmed() == [(4, 0, 11), (5, 6, 11), (6, 0, 11)]
    assert "  (a, b)" in eol(nv).values()
    tag = next(m[3]["virt_text"] for m in step_marks(nv, "a.txt") if m[1] == 5 and m[3].get("virt_text_pos") == "eol")
    assert ["a", "NvtourPartRefOn"] in [list(c) for c in tag] and ["b", "NvtourPartRef"] in [list(c) for c in tag]
    # The words linked to the focused part get a background.
    words = next(m for m in step_marks(nv, "a.txt") if m[3].get("virt_lines_above") and m[1] == 4)
    assert any("NvtourPartFocus" in c[1] for line in words[3]["virt_lines"] for c in line if c[0] == "one")
    assert not any("NvtourPartFocus" in c[1] for line in words[3]["virt_lines"] for c in line if c[0] == "two")
    dispatch(nv, "part", dir=1)
    assert dimmed() == [(4, 0, 11), (5, 0, 7), (6, 0, 11)]  # b: all but "of a"
    dispatch(nv, "part", dir=1)
    assert dimmed() == [(4, 0, 11), (5, 0, 11)]  # c has no expression: its whole line stays
    assert dispatch(nv, "part", dir=1).get("part") is None and dimmed() == []  # past the last: no focus
    assert dispatch(nv, "part", dir=-1)["part"] == "c"
    assert dispatch(nv, "part", k=1)["part"] == "a"
    ok(cli("next"))  # the focus ends with the step
    ok(cli("prev"))
    assert dimmed() == []
    ok(cli("next"))
    assert dispatch(nv, "part", dir=1)["message"] == "nvtour: this step has no parts"


def test_the_keys_focus_the_parts(cli, nv):
    ok(cli("start", "Keys"))
    ok(cli("step", "a.txt:5-7", "--part", "6", "--note", "[x](a)"))
    assert nv.call("maparg", "]r", "n") != "" and nv.call("maparg", "[r", "n") != ""
    nv.feedkeys(nv.replace_termcodes("]r", True, False, True), "x", False)
    assert hl(nv, "NvtourDim") == [(4, 0, 11), (6, 0, 11)]


@pytest.mark.parametrize("args, message", [
    (["--part", "9"], "part a (9): lines 9-9 are not in the step (lines 5-7)"),
    (["--part", "5", "--part", "/line 9/"], "part b (/line 9/): text 'line 9' not found in the step (lines 5-7)"),
    (["--part", "6", "--part-text", "line 7"], 'part a (6): text "line 7" is not in its lines 6-6'),
    (["--part", "6", "--note", "[x](a) [y](b)"], "the note links to part b, but the step has only part a"),
    (["--note", "[x](a)"], "the note links to part a, but the step has no parts"),
])
def test_bad_parts_are_exit_6(cli, nv, args, message):
    ok(cli("start", "Bad"))
    r = cli("step", "a.txt:5-7", *args)
    assert r.returncode == 6 and message in r.stderr, r.stderr
    assert json.loads(ok(cli("--json", "status")))["total"] == 0


def test_part_text_follows_its_part(cli, nv):
    ok(cli("start", "Usage"))
    r = cli("step", "a.txt:5-7", "--part-text", "x")
    assert r.returncode == 2 and "--part-text needs a --part before it" in r.stderr
    r = cli("step", "a.txt:5-7", "--part", "6", "--part-text", "line", "--part-text", "of")
    assert r.returncode == 2 and "--part '6' has a text already" in r.stderr


def test_edit_replaces_and_checks_the_parts(cli, nv):
    ok(cli("start", "Edit"))
    ok(cli("step", "a.txt:5-7", "--part", "/line 6/", "--note", "[x](a)"))
    assert cli("edit", "1", "--note", "[x](b)").returncode == 6  # no part b
    r = cli("edit", "1", "a.txt:10-12")  # the old part is checked again on the new lines
    assert r.returncode == 6 and "text 'line 6' not found in the step (lines 10-12)" in r.stderr
    ok(cli("edit", "1", "--part", "6", "--part", "7", "--note", "[x](a) [y](b)"))
    assert json.loads(ok(cli("--json", "status")))["steps"][0]["parts"] == ["a: 6 (lines 6-6)", "b: 7 (lines 7-7)"]
    assert cli("edit", "1", "--part", "", "--note", "[x](a)").returncode == 6
    ok(cli("edit", "1", "--part", "", "--note", "plain"))
    assert json.loads(ok(cli("--json", "status")))["steps"][0].get("parts") is None


def test_parts_in_a_tour_file(cli, nv, wide):
    tour = '[[step]]\nat = "a.txt:5-7"\nparts = ["6", { at = "/line 7/", text = "of a" }]\nnote = "[x](a) [y](b)"\n'
    ok(cli("apply", "-", stdin=tour))
    assert eol(nv) == {4: "  ← 1", 5: "  (a)", 6: "  (b)"}
    r = cli("apply", "-", stdin='[[step]]\nat = "a.txt:5-7"\nparts = [{ at = "6", txt = "x" }]\n')
    assert r.returncode == 2 and 'step[1]: "parts" item 1: unknown key "txt" (the keys are: at, text)' in r.stderr
    r = cli("apply", "-", stdin='[[step]]\nat = "a.txt:5-7"\nparts = ["9"]\n')
    assert r.returncode == 6 and r.stderr.startswith("step[1]: part a (9): lines 9-9 are not in the step")


def test_the_card_links_focus_their_part(cli, nv, side):  # noqa: F811
    ok(cli("start", "Card"))
    ok(cli("step", "a.txt:5-7", "--part", "6", "--part", "7", "--note", "Lead.\n[one](a) and [two](b)."))
    dispatch(nv, "view", mode="side")
    win, lines = card(nv)
    row = lines.index("[one](a) and [two](b).")  # raw markdown: the conceal shows the words
    buf = nv.exec_lua("return _G.nvtour.state.card.buf")
    marks = nv.api.buf_get_extmarks(buf, nv.api.create_namespace("nvtour_parts"), 0, -1, {"details": True})
    assert [(m[1], m[2], m[3].get("hl_group")) for m in marks if m[3].get("hl_group")] == [
        (row, 0, "NvtourPartLink"), (row, 13, "NvtourPartLink")]
    assert [m[3]["virt_text"][0][0] for m in marks if m[3].get("virt_text")] == [" (a)", " (b)"]
    assert eol(nv) == {5: "  (a)", 6: "  (b)"}  # the side view keeps the tags in the code
    nv.api.set_current_win(win)
    nv.api.win_set_cursor(win, [row + 1, 15])  # on "[two](b)"
    nv.api.exec_autocmds("CursorMoved", {"buffer": buf})
    assert hl(nv, "NvtourDim") == [(4, 0, 11), (5, 0, 11)]
    nv.api.win_set_cursor(win, [1, 0])  # off the links: the focus of the cursor ends
    nv.api.exec_autocmds("CursorMoved", {"buffer": buf})
    assert hl(nv, "NvtourDim") == []

"""Tests of chapters: headings in front of steps, in the panel, the winbar, the keys and the status."""

from __future__ import annotations

import json
import time

from test_integration import (  # noqa: F401 (sheet and band are fixtures)
    GOTO,
    band,
    content,
    eval_winbar,
    find_buf,
    link_texts,
    lines_of,
    ns_marks,
    ok,
    sheet,
    step_marks,
)


def status(cli):
    return json.loads(ok(cli("--json", "status")))


def chapters(cli):
    return [(c["name"], c["step"]) for c in status(cli)["chapters"]]


def current(nv):
    return nv.exec_lua("return _G.nvtour.state.tour.current")


def panel_lines(nv):
    return lines_of(nv, find_buf(nv, "nvtour://panel").handle)


def tour(cli, count, role="info"):
    """A tour of `count` steps on a.txt: step i is on line i * 3."""
    ok(cli("start", "Chapters"))
    for i in range(1, count + 1):
        ok(cli("step", f"a.txt:{i * 3}", "--role", role, "--label", f"s{i}"))


def test_chapter_text_output_and_list(cli):
    tour(cli, 3)
    assert ok(cli("chapter", "Why it fails", "--at", "2")).strip() == 'chapter "Why it fails" at step 2'
    assert ok(cli("chapter", "The fix")).strip() == 'chapter "The fix" pending (next step)'
    assert ok(cli("chapter", "Start", "--at", "1")).splitlines() == ['chapter "Start" at step 1']
    assert ok(cli("chapter", "--list")).splitlines() == [
        'chapter "Start" at step 1',
        'chapter "Why it fails" at step 2',
        'chapter "The fix" pending (next step)',
    ]
    data = json.loads(ok(cli("--json", "chapter", "--list")))
    assert data["chapters"] == [
        {"name": "Start", "step": 1},
        {"name": "Why it fails", "step": 2},
        {"name": "The fix", "step": None},
    ]
    data = json.loads(ok(cli("--json", "chapter", "Again", "--at", "2")))
    assert (data["name"], data["step"], data["pending"], data["replaced"]) == ("Again", 2, False, "Why it fails")
    ok(cli("clear"))
    assert ok(cli("chapter", "--list")).strip() == "no chapters"


def test_at_after_the_last_step_is_pending_and_beyond_is_an_error(cli):
    tour(cli, 2)
    assert ok(cli("chapter", "Next", "--at", "3")).strip() == 'chapter "Next" pending (next step)'
    for bad in ("4", "0", "-1"):
        r = cli("chapter", "Bad", "--at", bad)
        assert r.returncode == 6 and "out of range" in r.stderr, (bad, r.stderr)
    assert chapters(cli) == [("Next", None)]
    r = cli("--json", "chapter", "Bad", "--at", "9")
    assert r.returncode == 6 and json.loads(r.stdout)["ok"] is False


def test_usage_errors(cli):
    tour(cli, 1)
    for args in ((), ("--list", "Name"), ("--remove", "1", "Name"), ("--at", "1", "--list"), ("  ",)):
        r = cli("chapter", *args)
        assert r.returncode == 2, (args, r.stdout, r.stderr)
    assert chapters(cli) == []


def test_chapter_follows_its_step_on_insert(cli, nv):
    tour(cli, 3)
    ok(cli("chapter", "Two", "--at", "2"))
    ok(cli("step", "b.txt:5", "--at", "1", "--label", "new first"))
    ok(cli("step", "b.txt:6", "--at", "3", "--label", "new third"))
    data = status(cli)
    assert [s["label"] for s in data["steps"]] == ["new first", "s1", "new third", "s2", "s3"]
    assert chapters(cli) == [("Two", 4)]  # still in front of s2
    text = ok(cli("status")).splitlines()
    assert text[text.index("  ## Two") + 1].startswith("    4. a.txt:6 [info] s2")


def test_removing_the_step_of_a_chapter_moves_it_to_the_next_step(cli):
    tour(cli, 4)
    ok(cli("chapter", "A", "--at", "2"))
    ok(cli("chapter", "B", "--at", "4"))
    ok(cli("remove", "2"))  # A goes to what is now step 2 (was s3)
    assert chapters(cli) == [("A", 2), ("B", 3)]
    assert [s["label"] for s in status(cli)["steps"]][1] == "s3"
    ok(cli("remove", "1"))  # not a chapter step: the numbers shift
    assert chapters(cli) == [("A", 1), ("B", 2)]


def test_removing_the_last_step_drops_its_chapter(cli):
    tour(cli, 3)
    ok(cli("chapter", "End", "--at", "3"))
    ok(cli("remove", "3"))
    assert chapters(cli) == []


def test_a_moved_chapter_does_not_replace_the_chapter_of_the_next_step(cli):
    tour(cli, 3)
    ok(cli("chapter", "A", "--at", "2"))
    ok(cli("chapter", "B", "--at", "3"))
    ok(cli("remove", "2"))
    assert chapters(cli) == [("B", 2)]  # A is dropped: the step after it starts a chapter already


def test_pending_chapter_takes_the_next_step_added(cli):
    ok(cli("start", "Pending"))
    assert ok(cli("chapter", "Before any step")).strip() == 'chapter "Before any step" pending (next step)'
    assert chapters(cli) == [("Before any step", None)]
    ok(cli("step", "a.txt:3"))
    assert chapters(cli) == [("Before any step", 1)]
    ok(cli("step", "a.txt:6"))
    assert chapters(cli) == [("Before any step", 1)]  # only the next step took it
    ok(cli("chapter", "Middle"))
    ok(cli("step", "a.txt:9", "--at", "1"))  # a pending chapter takes a step added at any place
    assert chapters(cli) == [("Middle", 1), ("Before any step", 2)]  # tour order


def test_two_chapters_at_one_step_replace_the_name(cli):
    tour(cli, 2)
    ok(cli("chapter", "First name", "--at", "2"))
    out = ok(cli("chapter", "Second name", "--at", "2"))
    assert out.strip() == 'chapter "Second name" at step 2, replacing "First name"'
    assert chapters(cli) == [("Second name", 2)]
    ok(cli("chapter", "One"))
    ok(cli("chapter", "Two"))  # one pending chapter at most
    assert chapters(cli) == [("Second name", 2), ("Two", None)]


def test_chapter_remove(cli, nv):
    tour(cli, 3)
    ok(cli("chapter", "A", "--at", "1"))
    ok(cli("chapter", "B", "--at", "3"))
    assert ok(cli("chapter", "--remove", "3")).strip() == "removed chapter at step 3"
    assert chapters(cli) == [("A", 1)]
    assert not any(line == "## B" for line in panel_lines(nv))
    r = cli("chapter", "--remove", "3")
    assert r.returncode == 6 and "no chapter starts at step 3" in r.stderr
    assert cli("chapter", "--remove", "0").returncode == 6
    assert cli("chapter", "--remove", "9").returncode == 6
    ok(cli("chapter", "Wait"))
    assert ok(cli("chapter", "--remove", "4")).strip() == "removed pending chapter"
    assert cli("chapter", "--remove", "4").returncode == 6
    assert chapters(cli) == [("A", 1)]
    assert [s["label"] for s in status(cli)["steps"]] == ["s1", "s2", "s3"]  # the steps stay


def test_start_and_clear_drop_chapters(cli, nv):
    tour(cli, 2)
    ok(cli("chapter", "A", "--at", "1"))
    ok(cli("chapter", "Pending"))
    ok(cli("start", "Again"))
    assert chapters(cli) == []
    ok(cli("step", "a.txt:3"))
    assert chapters(cli) == []  # the pending chapter of the old tour is gone too
    ok(cli("chapter", "B", "--at", "1"))
    ok(cli("clear"))
    assert chapters(cli) == []
    assert nv.exec_lua("return #_G.nvtour.state.tour.chapters") == 0


def test_status_text_and_json(cli):
    tour(cli, 3)
    ok(cli("chapter", "Why", "--at", "1"))
    ok(cli("chapter", "How", "--at", "3"))
    ok(cli("chapter", "Later"))
    data = status(cli)
    assert data["chapters"] == [{"name": "Why", "step": 1}, {"name": "How", "step": 3}, {"name": "Later", "step": None}]
    lines = ok(cli("status")).splitlines()
    assert lines[1] == "  ## Why"
    assert lines[2].startswith("  ▶ 1. a.txt:3")
    assert lines[3].startswith("    2. a.txt:6")
    assert lines[4] == "  ## How"
    assert lines[5].startswith("    3. a.txt:9")
    assert lines[6] == "  ## Later (pending: the next step added)"
    assert "]c next_chapter, [c prev_chapter" in ok(cli("status"))


def test_step_result_has_the_chapter_and_the_text_stays_the_same(cli):
    tour(cli, 3)
    ok(cli("chapter", "Why", "--at", "2"))
    data = json.loads(ok(cli("--json", "goto", "1")))
    assert "chapter" not in data  # step 1 is before the first chapter
    data = json.loads(ok(cli("--json", "goto", "3")))
    assert data["chapter"] == "Why"  # step 3 is in the chapter that started at step 2
    out = ok(cli("goto", "2"))
    assert out.splitlines()[0] == "step 2/3: a.txt:6 [info] s2"
    assert "Why" not in out


def test_panel_shows_the_chapters(cli, nv):
    tour(cli, 4)
    ok(cli("step", "b.txt:2", "--label", "other file"))
    ok(cli("chapter", "Why it fails", "--at", "1"))
    ok(cli("chapter", "The fix", "--at", "3"))
    ok(cli("chapter", "The other file", "--at", "5"))
    text = panel_lines(nv)
    assert text[:4] == ["# Chapters", "", "## Why it fails", "a.txt"]
    assert text.count("a.txt") == 2  # the file name line starts again after the boundary
    i = text.index("## The fix")
    assert text[i - 1] == "" and text[i + 1] == "a.txt" and text[i + 2].lstrip(" ▶").startswith("3.")
    j = text.index("## The other file")
    assert text[j + 1] == "b.txt" and text[j + 2].lstrip(" ▶").startswith("5.")
    pb = find_buf(nv, "nvtour://panel")
    marks = [m for m in ns_marks(nv, "nvtour_panel", pb.handle) if m[3].get("hl_group") == "NvtourPanelChapter"]
    assert sorted(m[1] for m in marks) == [2, i, j]
    assert nv.eval("synIDattr(hlID('NvtourPanelChapter'), 'name')") == "NvtourPanelChapter"
    assert nv.api.get_hl(0, {"name": "NvtourPanelChapter", "link": True}).get("link") == "Title"
    assert text[-1].startswith("`]w` next") and "`]c` next chapter · `[c` prev chapter" in text[-1]
    ok(cli("chapter", "--remove", "5"))
    ok(cli("chapter", "--remove", "3"))
    ok(cli("chapter", "--remove", "1"))
    assert not any(line.startswith("## ") for line in panel_lines(nv))
    assert "next chapter" not in panel_lines(nv)[-1]  # the chapter keys show only with chapters


def test_panel_keeps_the_link_line_of_the_first_step_of_a_chapter(cli, nv):
    ok(cli("start", "Links"))
    ok(cli("step", "a.txt:3", "--label", "one"))
    ok(cli("step", "a.txt:6", "--label", "two", "--via", "the same file"))
    ok(cli("step", "a.txt:9", "--label", "three", "--from", "1"))
    ok(cli("step", "a.txt:12", "--label", "four", "--via", "no heading here"))
    ok(cli("chapter", "Second part", "--at", "2"))
    ok(cli("chapter", "Third part", "--at", "3"))
    text = panel_lines(nv)
    i = text.index("## Second part")
    assert text[i - 1] == ""
    assert text[i + 1] == "    ↓ the same file"
    assert text[i + 2] == "a.txt"  # the file name line starts again after the heading
    assert text[i + 3].lstrip(" ▶").startswith("2.")
    j = text.index("## Third part")
    assert text[j + 1] == "    ↓ from 1"
    assert text[j + 2] == "a.txt" and text[j + 3].lstrip(" ▶").startswith("3.")
    assert text[j + 4] == "    ↓ no heading here"  # inside a chapter: as before, no file name line
    assert text[j + 5].lstrip(" ▶").startswith("4.")
    # the heading, the link line, the file name line and the step line all go to step 2
    for row in range(i + 1, i + 5):  # 1-based lines
        assert nv.exec_lua("return _G.nvtour.state.panel.line_map[...]", row) == 2


def test_panel_progress_text_stays(cli, nv):
    tour(cli, 3)
    ok(cli("chapter", "Why", "--at", "2"))
    ok(cli("goto", "3"))
    pb = find_buf(nv, "nvtour://panel")
    progress = [m[3]["virt_text"][0][0] for m in ns_marks(nv, "nvtour_panel", pb.handle) if m[3].get("virt_text")]
    assert progress == ["  3/3"]


def test_enter_on_a_chapter_line_goes_to_its_first_step(cli, nv):
    tour(cli, 4)
    ok(cli("chapter", "The fix", "--at", "3"))
    ok(cli("chapter", "Start", "--at", "1"))
    assert current(nv) == 1
    text = panel_lines(nv)
    row = text.index("## The fix") + 1
    nv.api.set_current_win(nv.eval("bufwinid(%d)" % find_buf(nv, "nvtour://panel").handle))
    nv.api.win_set_cursor(0, (row, 0))
    nv.input("<CR>")
    assert current(nv) == 3
    assert nv.eval("line('.')") == 9


def test_chapter_keys(cli, nv):
    tour(cli, 6)
    ok(cli("chapter", "A", "--at", "1"))
    ok(cli("chapter", "B", "--at", "3"))
    ok(cli("chapter", "C", "--at", "6"))
    assert nv.eval("maparg(']c', 'n')") != "" and nv.eval("maparg('[c', 'n')") != ""
    assert nv.exec_lua("return _G.nvtour.state.keys.next_chapter") == "]c"
    nv.input("]c")
    assert current(nv) == 3
    nv.input("]c")
    assert current(nv) == 6
    nv.input("]c")
    assert current(nv) == 6  # already in the last chapter
    ok(cli("goto", "4"))
    nv.input("[c")
    assert current(nv) == 3  # inside a chapter, not at its start: the start of this chapter
    nv.input("[c")
    assert current(nv) == 1  # at the start: the chapter before
    nv.input("[c")
    assert current(nv) == 1  # no chapter before the first
    ok(cli("goto", "5"))
    nv.input("]c")
    assert current(nv) == 6
    nv.command("NvtourPrevChapter")
    assert current(nv) == 3
    nv.command("NvtourNextChapter")
    assert current(nv) == 6
    nv.command("NvtourFirst")
    assert current(nv) == 1


def test_chapter_keys_with_steps_before_the_first_chapter(cli, nv):
    tour(cli, 4)
    ok(cli("chapter", "A", "--at", "3"))
    ok(cli("goto", "1"))
    nv.input("]c")
    assert current(nv) == 3
    ok(cli("goto", "2"))
    nv.input("[c")
    assert current(nv) == 2  # no chapter starts before step 2


def test_chapter_keys_without_chapters_notify(cli, nv):
    tour(cli, 2)
    nv.command("let g:msgs = []")
    nv.exec_lua("vim.notify = function(m) vim.g.msgs = vim.list_extend(vim.g.msgs, { m }) end")
    try:
        nv.input("]c")
        nv.input("[c")
        msgs = nv.eval("g:msgs")
        assert any("no chapters" in m for m in msgs) and len(msgs) == 2, msgs
        assert current(nv) == 1
        ok(cli("chapter", "Only pending"))  # a pending chapter is not a place to go to
        nv.input("]c")
        assert current(nv) == 1
    finally:
        nv.exec_lua("vim.notify = nil")
        nv.command("unlet g:msgs")


def test_chapter_keys_skip_lost_steps(cli, nv):
    ok(cli("start", "Lost"))
    for i in (1, 2, 3, 4):
        ok(cli("step", f"a.txt:{i * 5}", "--label", f"s{i}"))
    ok(cli("chapter", "A", "--at", "1"))
    ok(cli("chapter", "B", "--at", "2"))
    ok(cli("chapter", "C", "--at", "4"))
    nv.command("buffer a.txt")
    nv.api.buf_set_lines(find_buf(nv, "a.txt").handle, 9, 15, False, [])  # deletes the lines of steps 2 and 3
    time.sleep(0.5)
    assert [s.get("lost") for s in status(cli)["steps"]][1:3] == [True, True]
    ok(cli("goto", "1"))
    nv.input("]c")
    assert current(nv) == 4  # B has no step that can be shown: C is next
    nv.input("[c")
    assert current(nv) == 1
    nv.command("silent! undo")


def test_chapter_keys_keep_their_meaning_in_diff_mode(cli, nv):
    tour(cli, 3)
    ok(cli("chapter", "A", "--at", "1"))
    ok(cli("chapter", "B", "--at", "3"))
    ok(cli("goto", "1"))
    nv.command("setlocal diff")
    try:
        nv.input("]c")  # the key of vim: no change in this window, so nothing happens
        assert current(nv) == 1
    finally:
        nv.command("setlocal nodiff")
    nv.input("]c")
    assert current(nv) == 3


def test_chapter_key_is_not_taken_when_mapped(cli, nv):
    nv.command("nnoremap ]c <Nop>")
    try:
        data = json.loads(ok(cli("--json", "start", "K")))
        assert "next_chapter" not in data["keys"] and any("]c" in w for w in data["warnings"])
        assert data["keys"]["prev_chapter"] == "[c"
    finally:
        ok(cli("clear"))
        nv.command("nunmap ]c")
    assert nv.eval("maparg('[c', 'n')") == ""


def test_winbar_shows_the_chapter(cli, nv):
    tour(cli, 3, role="fault")
    ok(cli("chapter", "Why it fails", "--at", "2"))
    ok(cli("panel", "--toggle"))  # closed: a wide window
    win = [w for w in nv.windows if w.buffer.name.endswith("a.txt")][0]
    assert eval_winbar(nv, win).startswith(" nvtour 1/3 fault · s1")  # step 1 is in no chapter
    ok(cli("goto", "2"))
    text = eval_winbar(nv, win)
    assert text.startswith(" nvtour Why it fails · 2/3 fault · s2")
    assert "%#" not in text and text.endswith("next ]w: line 9 s3 ")
    ok(cli("goto", "3"))
    assert eval_winbar(nv, win).startswith(" nvtour Why it fails · 3/3 fault · s3")  # later steps stay in it
    ok(cli("chapter", "100% sure", "--at", "2"))
    assert eval_winbar(nv, win).startswith(" nvtour 100% sure · 3/3")
    ok(cli("chapter", "--remove", "2"))
    assert eval_winbar(nv, win).startswith(" nvtour 3/3 fault · s3")


def test_winbar_drops_the_chapter_first(cli, nv):
    tour(cli, 2, role="fault")
    ok(cli("chapter", "A long chapter name that needs room", "--at", "1"))
    win = [w for w in nv.windows if w.buffer.name.endswith("a.txt")][0]
    assert win.width < 60  # the panel is open
    ok(cli("goto", "1"))
    text = eval_winbar(nv, win)
    assert text.rstrip() == " nvtour 1/2 fault · s1"  # no room for the chapter: it goes before the label is cut
    ok(cli("edit", "1", "--label", "y" * 100))
    text = eval_winbar(nv, win)
    assert "chapter" not in text and "…" in text and text.startswith(" nvtour 1/2 fault · yyy")
    ok(cli("edit", "1", "--label", "s1"))
    ok(cli("chapter", "Short", "--at", "1"))
    assert eval_winbar(nv, win).startswith(" nvtour Short · 1/2 fault · s1")
    assert nv.call("strdisplaywidth", eval_winbar(nv, win).rstrip()) <= win.width


def test_state_of_an_older_runtime_without_chapters(cli, nv):
    tour(cli, 2)
    nv.exec_lua("_G.nvtour.state.tour.chapters = nil")  # a tour made before chapters existed
    assert chapters(cli) == []
    ok(cli("goto", "2"))
    ok(cli("remove", "1"))
    nv.exec_lua("_G.nvtour.state.tour.chapters = nil")
    ok(cli("step", "a.txt:12"))
    nv.exec_lua("_G.nvtour.state.tour.chapters = nil")
    assert ok(cli("chapter", "Back", "--at", "1")).strip() == 'chapter "Back" at step 1'
    nv.exec_lua("_G.nvtour.state.tour.chapters = nil")
    assert panel_lines(nv)[0] == "# Chapters"
    nv.exec_lua("return _G.nvtour.winbar()")
    nv.input("]c")  # no crash without the list
    assert ok(cli("chapter", "--list")).strip() == "no chapters"


def test_set_chapter_helper(cli, nv):
    tour(cli, 2)
    nv.exec_lua("local S = _G.nvtour.state; _G.nvtour.set_chapter('From Lua', S.tour.steps[2])")
    assert chapters(cli) == [("From Lua", 2)]
    nv.exec_lua("_G.nvtour.set_chapter('Pending from Lua')")
    assert chapters(cli) == [("From Lua", 2), ("Pending from Lua", None)]


def test_a_multi_line_name_is_one_line(cli, nv):
    tour(cli, 1)
    out = ok(cli("chapter", "Two\n  lines", "--at", "1"))
    assert out.strip() == 'chapter "Two lines" at step 1'
    assert "## Two lines" in panel_lines(nv)


# --- the heading of a chapter above its first step ---------------------------------------------------


def peek_text(nv, n):
    """The lines of the peek float of step `n` (closed again)."""
    nv.command(f"NvtourPeek {n}")
    text = nv.exec_lua("local p = _G.nvtour.state.peek; return vim.api.nvim_buf_get_lines(p.buf, 0, -1, false)")
    nv.command(f"NvtourPeek {n}")
    return text


def test_current_first_step_of_a_chapter_has_the_heading_before_the_other_lines(cli, nv, sheet):
    ok(cli("start", "Heading", "--sheet", "arch"))
    ok(cli("step", "a.txt:3", "--sheet", "#intro", "--note", "One."))
    ok(cli("step", "b.txt:5", "--sheet", "flow#x", "--note", "Two."))
    ok(cli("chapter", "Why it fails", "--at", "2"))
    ok(cli("goto", "2"))
    assert link_texts(nv, "b.txt") == {4: ["## Why it fails", "◇ b.txt", "↔ sheet flow", "Two."]}
    line = [m[3]["virt_lines"] for m in step_marks(nv, "b.txt") if m[3].get("virt_lines_above")][0]
    chunks = [c for l in line for c in content(l)]
    assert ["## ", "NvtourPanelChapter"] in chunks
    assert ["Why it fails", "NvtourPanelChapter"] in chunks
    ok(cli("goto", "1"))  # a step in front of the chapter: no heading
    assert link_texts(nv, "a.txt") == {2: ["↔ sheet arch", "One."]}


def test_heading_of_the_current_step_without_a_note_and_in_the_band_style(cli, nv, band):
    tour(cli, 2)
    ok(cli("chapter", "Start", "--at", "1"))
    assert link_texts(nv, "a.txt") == {2: ["## Start"]}  # the current step, no note
    ok(cli("goto", "2"))
    assert link_texts(nv, "a.txt") == {2: ["## Start"]}  # not current now: still there


def test_not_current_first_step_of_a_chapter_has_a_heading_line(cli, nv):
    tour(cli, 4)
    ok(cli("chapter", "One", "--at", "1"))
    ok(cli("chapter", "Three", "--at", "3"))
    ok(cli("goto", "2"))
    assert link_texts(nv, "a.txt") == {2: ["## One"], 8: ["## Three"]}  # steps 2 and 4: no chapter start
    line = [m[3]["virt_lines"] for m in step_marks(nv, "a.txt") if m[1] == 8 and m[3].get("virt_lines_above")][0]
    assert ["Three", "NvtourPanelChapter"] in [c for l in line for c in content(l)]
    assert "╶─ " in [c[0] for l in line for c in l]  # in the grey lead of a step that is not current


def test_not_current_first_step_with_a_note_has_the_heading_before_the_collapsed_note(cli, nv):
    ok(cli("start", "Collapsed"))
    ok(cli("step", "a.txt:3", "--note", "Current note."))
    ok(cli("step", "a.txt:6", "--note", "Other note.\nSecond line."))
    ok(cli("chapter", "Two", "--at", "2"))
    assert link_texts(nv, "a.txt")[5] == ["## Two", "Other note. …"]
    ok(cli("goto", "2"))
    assert link_texts(nv, "a.txt")[5] == ["## Two", "Other note.", "Second line."]
    assert link_texts(nv, "a.txt")[2] == ["Current note."]


def test_every_chapter_start_has_a_heading_not_only_the_change(cli, nv):
    tour(cli, 3)
    ok(cli("chapter", "A", "--at", "1"))
    ok(cli("chapter", "B", "--at", "2"))  # right after A: shown as well
    ok(cli("chapter", "C", "--at", "3"))
    ok(cli("goto", "3"))
    assert link_texts(nv, "a.txt") == {2: ["## A"], 5: ["## B"], 8: ["## C"]}


def test_heading_follows_chapter_changes(cli, nv):
    tour(cli, 3)
    assert link_texts(nv, "a.txt") == {}
    ok(cli("chapter", "Two", "--at", "2"))  # chapter --at draws it
    assert link_texts(nv, "a.txt") == {5: ["## Two"]}
    ok(cli("chapter", "Renamed", "--at", "2"))
    assert link_texts(nv, "a.txt") == {5: ["## Renamed"]}
    ok(cli("chapter", "Moved", "--at", "3"))
    assert link_texts(nv, "a.txt") == {5: ["## Renamed"], 8: ["## Moved"]}
    ok(cli("chapter", "--remove", "2"))  # chapter --remove clears it
    assert link_texts(nv, "a.txt") == {8: ["## Moved"]}
    ok(cli("remove", "3"))  # the last step goes: so does its chapter
    assert link_texts(nv, "a.txt") == {}


def test_heading_stays_with_a_step_that_gets_a_new_neighbour(cli, nv):
    tour(cli, 3)
    ok(cli("chapter", "Two", "--at", "2"))
    ok(cli("step", "a.txt:1", "--at", "1"))
    assert link_texts(nv, "a.txt") == {5: ["## Two"]}  # still in front of the step on line 6


def test_heading_moves_to_the_next_step_when_its_step_is_removed(cli, nv):
    tour(cli, 4)
    ok(cli("chapter", "A", "--at", "2"))
    ok(cli("remove", "2"))  # A starts at what was step 3 (line 9)
    assert link_texts(nv, "a.txt") == {8: ["## A"]}
    ok(cli("chapter", "B", "--at", "3"))
    ok(cli("remove", "2"))  # the next step has a chapter already: A is dropped
    assert link_texts(nv, "a.txt") == {11: ["## B"]}


def test_pending_chapter_shows_nothing_until_it_has_a_step(cli, nv):
    tour(cli, 2)
    ok(cli("chapter", "Later"))
    assert link_texts(nv, "a.txt") == {}
    ok(cli("step", "a.txt:12"))
    assert link_texts(nv, "a.txt") == {11: ["## Later"]}


def test_apply_with_chapter_shows_the_heading(cli, nv):
    toml = """
[[step]]
at = "a.txt:3"
chapter = "First part"
note = "Note one."

[[step]]
at = "a.txt:6"
note = "Note two."

[[step]]
at = "a.txt:9"
chapter = "Second part"
"""
    ok(cli("apply", "-", stdin=toml))
    assert link_texts(nv, "a.txt") == {2: ["## First part", "Note one."], 5: ["Note two."], 8: ["## Second part"]}


def test_peek_of_a_chapter_start_has_the_heading_first(cli, nv):
    ok(cli("start", "Peek"))
    ok(cli("step", "a.txt:3", "--note", "One."))
    ok(cli("step", "a.txt:6", "--note", "Two."))
    ok(cli("step", "b.txt:9", "--note", "Three."))
    ok(cli("chapter", "Middle", "--at", "2"))
    ok(cli("chapter", "Other file", "--at", "3"))
    ok(cli("goto", "1"))
    assert peek_text(nv, 2)[:3] == ["## Middle", "◇ a.txt", "Two."]  # the file line is added after the heading
    assert peek_text(nv, 3)[:3] == ["## Other file", "◇ b.txt", "Three."]
    assert peek_text(nv, 1)[:2] == ["◇ a.txt", "One."]  # no chapter


def test_long_chapter_name_is_cut(cli, nv):
    nv.vars["nvtour_note_width"] = 40
    try:
        tour(cli, 2)
        ok(cli("chapter", "A very long chapter name that does not fit into the note width at all", "--at", "1"))
        ok(cli("chapter", "Another extremely long chapter name that does not fit either", "--at", "2"))
        texts = link_texts(nv, "a.txt")
        for row in (2, 5):
            (line,) = texts[row]
            assert line.startswith("## ") and line.endswith("…")
            assert len(line) <= 40
        assert [p for p in panel_lines(nv) if p.startswith("## ")][0].endswith("at all")  # the panel is not cut
    finally:
        del nv.vars["nvtour_note_width"]

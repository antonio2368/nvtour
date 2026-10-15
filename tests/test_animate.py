"""Tests of the animation of a jump: the scroll in the same buffer and the fade to another buffer."""

from __future__ import annotations

import subprocess
import time

import pytest
from test_integration import ok

MS = 400  # long enough to look at the frames between two RPC calls


@pytest.fixture()
def animate(nv):
    """Animation on (a headless nvim does not animate by default), with slow scrolls and fades."""
    nv.exec_lua(
        f"vim.g.nvtour_animate = true; vim.g.nvtour_scroll_ms = {MS}; vim.g.nvtour_fade_ms = {MS}; vim.o.termguicolors = true"
    )
    yield
    nv.exec_lua(
        "vim.g.nvtour_animate = nil; vim.g.nvtour_scroll_ms = nil; vim.g.nvtour_fade_ms = nil; "
        "vim.g.nvtour_title_ms = nil; vim.g.nvtour_file_ms = nil; vim.g.nvtour_follow_cursor = nil; "
        "_G.nvtour.state.animate = nil"
    )


@pytest.fixture()
def side_view(nv):
    """Room for the step card of the side view; the inline view again at the end."""
    nv.command("set columns=200")
    yield
    nv.exec_lua("_G.nvtour.state.view = nil; _G.nvtour.state.card.user_closed = false; _G.nvtour.dispatch('clear', {})")
    nv.command("set columns=80")


def hide_card(nv):
    """The side view with its card hidden: the only view where the curtain shows the title card (the
    inline view shows the change in the step, the side view lights it up in the card). After `start`,
    which shows the card again."""
    nv.exec_lua("_G.nvtour.state.view = 'side'; _G.nvtour.state.card.user_closed = true")


def running(nv):
    """The number of windows with an animation (read without dispatch, which ends them)."""
    return nv.exec_lua("return vim.tbl_count(_G.nvtour.state.anims)")


def topline(nv):
    return nv.exec_lua("return vim.fn.winsaveview().topline")


def floats(nv):
    """The buffer names of the floats."""
    return nv.exec_lua(
        "local out = {} for _, w in ipairs(vim.api.nvim_list_wins()) do "
        "if vim.api.nvim_win_get_config(w).relative ~= '' then "
        "out[#out + 1] = vim.api.nvim_buf_get_name(vim.api.nvim_win_get_buf(w)) end end return out"
    )


def curtain_rows(nv):
    """For each row of the curtain: the end column of its NvtourFade (the gutter), or "row" when the
    whole row fades (line_hl_group). Not the text of a card."""
    return nv.exec_lua(
        "local ns = vim.api.nvim_create_namespace('nvtour_fade') "
        "local b = vim.fn.bufnr('nvtour://fade') "
        "local out = {} for _, m in ipairs(vim.api.nvim_buf_get_extmarks(b, ns, 0, -1, { details = true })) do "
        "if m[4].line_hl_group == 'NvtourFade' then out[#out + 1] = 'row' "
        "elseif m[4].hl_group == 'NvtourFade' then out[#out + 1] = m[4].end_col end end return out"
    )


def card(nv):
    """The text on the curtain (the title card), one string per row that has text."""
    lines = nv.exec_lua(
        "local b = vim.fn.bufnr('nvtour://fade') return b == -1 and {} or vim.api.nvim_buf_get_lines(b, 0, -1, false)"
    )
    return [line.strip() for line in lines if line.strip()]


def flashed(nv):
    ns = nv.exec_lua("return vim.api.nvim_create_namespace('nvtour_flash')")
    return nv.exec_lua(f"return #vim.api.nvim_buf_get_extmarks(0, {ns}, 0, -1, {{}})")


def wait_done(nv, timeout=3.0):
    deadline = time.time() + timeout
    while running(nv) and time.time() < deadline:
        time.sleep(0.02)
    assert running(nv) == 0


def end_view(cli, nv, n):
    """The topline of a jump to step `n` without animation."""
    nv.exec_lua("_G.nvtour.state.animate = false")
    ok(cli("goto", str(n)))
    top = topline(nv)
    nv.exec_lua("_G.nvtour.state.animate = nil")
    return top


def test_headless_does_not_animate_by_default(cli, nv):
    ok(cli("start", "Anim"))
    assert nv.exec_lua("return _G.nvtour.dispatch('status', {}).animate") is False
    ok(cli("step", "a.txt:2", "--jump"))
    ok(cli("step", "a.txt:55"))
    ok(cli("goto", "2"))
    assert running(nv) == 0


def test_scroll_in_the_same_file(cli, nv, animate):
    ok(cli("start", "Anim"))
    ok(cli("step", "a.txt:2"))
    ok(cli("step", "a.txt:50"))
    final = end_view(cli, nv, 2)
    ok(cli("goto", "1"))
    wait_done(nv)
    time.sleep(0.35)  # the flash of step 1 ends
    start = topline(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    assert running(nv) == 1
    assert flashed(nv) == 0  # the flash comes after the scroll
    seen = [topline(nv)]
    while running(nv):
        seen.append(topline(nv))
        time.sleep(0.02)
    assert seen[0] < final  # it starts from the side of the old view
    # Several frames between the two views: the keys of :normal! do not end it as keys of the user do.
    assert len({t for t in seen if start < t < final}) >= 3
    assert topline(nv) == final and nv.current.window.cursor[0] == 50
    assert flashed(nv) == 1
    assert floats(nv) == []


def test_fade_to_another_file(cli, nv, animate):
    ok(cli("start", "Anim"))
    ok(cli("step", "a.txt:5"))
    ok(cli("step", "b.txt:20"))
    final = end_view(cli, nv, 2)
    ok(cli("goto", "1"))
    wait_done(nv)
    old_top = topline(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    # The first half: the old file again, read-only, under the curtain.
    assert nv.current.buffer.name.endswith("a.txt") and topline(nv) == old_top
    assert nv.exec_lua("return vim.bo.modifiable") is False
    assert floats(nv) == ["nvtour://fade"]
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 2  # the step changed at once
    deadline = time.time() + 3
    while nv.current.buffer.name.endswith("a.txt") and time.time() < deadline:
        time.sleep(0.02)
    assert running(nv) == 1  # the second half: the new file under the curtain
    assert nv.current.buffer.name.endswith("b.txt") and topline(nv) == final
    wait_done(nv)
    assert floats(nv) == [] and flashed(nv) == 1
    assert nv.exec_lua("return vim.fn.bufnr('nvtour://fade')") == -1
    assert nv.exec_lua("return vim.bo[vim.fn.bufnr('a.txt')].modifiable") is True
    assert nv.exec_lua("return vim.fn.bufname('#')").endswith("a.txt")  # as after a jump without animation


def test_fade_keeps_the_rows_that_look_the_same(cli, nv, animate, tmp_path):
    """The same file at another commit: only the line that changed (and the rows of the step) fade."""
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    lines = [f"same line {i}" for i in range(1, 41)]
    (tmp_path / "x.txt").write_text("\n".join(lines) + "\n")
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "x.txt"], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    lines[25] = "changed line 26"
    (tmp_path / "x.txt").write_text("\n".join(lines) + "\n")
    ok(cli("start", "Anim"))
    ok(cli("step", f"{tmp_path}/x.txt:30", "--note", "now"))
    ok(cli("step", f"{tmp_path}/x.txt:30", "--ref", "HEAD", "--note", "before"))
    ok(cli("goto", "1"))
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    rows = curtain_rows(nv)
    info = nv.exec_lua("return vim.fn.getwininfo(vim.api.nvim_get_current_win())[1]")
    assert len(rows) == info["height"]  # the text area, without the winbar
    # The notes around the step differ in height (the new one names the commit, the old one has a
    # link to the next step): the old text lines up with the larger block of rows that look the same,
    # above the step (lines 23 to 29 but 26) or below it (from line 31).
    gutter_only = [r for r in rows if r != "row"]
    assert len(gutter_only) >= 6
    assert "row" in rows  # line 26 and the rows of the step fade
    wait_done(nv)


def test_fade_to_the_same_file_at_a_commit(cli, nv, animate):
    ok(cli("start", "Anim"))
    ok(cli("step", "a.txt:30"))
    ok(cli("step", "a.txt:30", "--ref", "v1"))
    ok(cli("goto", "1"))
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    assert nv.current.buffer.name.endswith("/a.txt")  # the working tree first
    assert floats(nv) == ["nvtour://fade"]
    wait_done(nv)
    assert nv.current.buffer.name == "nvtour://v1/a.txt" and floats(nv) == []


def test_map_line_between_versions(cli, nv):
    ok(cli("start", "Anim"))  # loads the runtime

    def map_line(old, new, line):
        return nv.exec_lua("return _G.nvtour._anim.map_line(...)", old, new, line)

    old = ["a", "b", "c", "d", "e"]
    assert map_line(old, old, 3) == 3
    assert map_line(old, ["x", "y", "a", "b", "c", "d", "e"], 5) == 3  # two lines added above
    assert map_line(old, ["a", "d", "e"], 2) == 4  # b and c removed
    assert map_line(old, ["a", "B", "C", "d", "e"], 3) == 2  # in a change: its first line
    assert map_line(old, ["a", "b", "c", "d", "e", "f", "g"], 7) == 5  # past the end


def test_a_command_or_a_key_ends_the_animation(cli, nv, animate):
    ok(cli("start", "Anim"))
    ok(cli("step", "a.txt:2"))
    ok(cli("step", "a.txt:50"))
    final = end_view(cli, nv, 2)
    ok(cli("goto", "1"))
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    assert running(nv) == 1
    nv.exec_lua("_G.nvtour.dispatch('status', {})")
    assert running(nv) == 0 and topline(nv) == final and flashed(nv) == 1
    ok(cli("goto", "1"))
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    assert running(nv) == 1
    nv.input("<Ignore>")
    time.sleep(0.1)
    assert running(nv) == 0 and topline(nv) == final


def test_follow_mode_ignores_the_scroll(cli, nv, animate):
    nv.exec_lua("vim.g.nvtour_follow_cursor = true")
    ok(cli("start", "Anim"))
    ok(cli("step", "a.txt:2"))
    ok(cli("step", "a.txt:20"))  # the scroll passes over it
    ok(cli("step", "a.txt:50"))
    ok(cli("goto", "1"))
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 3 })")
    wait_done(nv)
    time.sleep(0.3)  # the debounce of the follow mode
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 3


def test_toggle(cli, nv, animate):
    ok(cli("start", "Anim"))
    res = nv.exec_lua("return _G.nvtour.dispatch('animate', {})")
    assert res["ok"] and res["animate"] is False and res["message"] == "nvtour: animation off"
    ok(cli("step", "a.txt:2", "--jump"))
    ok(cli("step", "a.txt:55"))
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    assert running(nv) == 0
    nv.command("NvtourAnimate on")
    assert nv.exec_lua("return _G.nvtour.dispatch('status', {}).animate") is True
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 1 })")
    assert running(nv) == 1
    res = nv.exec_lua("return _G.nvtour.dispatch('animate', { mode = 'sideways' })")
    assert not res["ok"] and "on, off or toggle" in res["error"]
    assert any(m.get("desc") == "nvtour animate" for m in nv.api.get_keymap("n"))


def jump_card(nv, n, shows=True):
    """Jump to step `n`; return the floats at once, and the title card when it shows (with `shows`;
    it comes when the curtain is opaque), else the text on the curtain at the end."""
    nv.exec_lua(f"_G.nvtour.dispatch('goto', {{ n = {n} }})")
    wins = floats(nv)
    assert card(nv) == []  # not while the curtain is still clear: it would show through
    lines = []
    deadline = time.time() + 2
    while running(nv) and time.time() < deadline:
        lines = card(nv) or lines
        if lines and shows:
            break
        time.sleep(0.01)
    return wins, lines


def test_title_card_names_the_new_file(cli, nv, animate, side_view):
    ok(cli("start", "Anim"))
    ok(cli("step", "a.txt:5"))
    ok(cli("step", "b.txt:20"))
    hide_card(nv)
    ok(cli("goto", "1"))
    wait_done(nv)
    _, lines = jump_card(nv, 2)
    assert lines[0].startswith("╭") and lines[-1].startswith("╰")
    assert lines[1:-1] == ["│ ◇ b.txt │"]
    wait_done(nv)


def test_title_card_names_the_commit(cli, nv, animate, side_view):
    ok(cli("start", "Anim"))
    ok(cli("step", "a.txt:30"))
    ok(cli("step", "a.txt:30", "--ref", "v1"))
    hide_card(nv)
    ok(cli("goto", "1"))
    wait_done(nv)
    _, lines = jump_card(nv, 2)
    assert len(lines) == 3 and lines[1].startswith("│ ◇ a.txt @v1 (")  # the tag is not the sha: both
    wait_done(nv)


def test_a_new_chapter_fades_with_its_name(cli, nv, animate, side_view):
    ok(cli("start", "Anim"))
    ok(cli("chapter", "One"))
    ok(cli("step", "a.txt:2"))
    ok(cli("step", "a.txt:30"))
    ok(cli("chapter", "Two"))
    ok(cli("step", "a.txt:55"))
    hide_card(nv)
    final = end_view(cli, nv, 3)
    ok(cli("goto", "1"))
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")  # the same chapter, the same file: a scroll
    assert floats(nv) == [] and running(nv) == 1
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 3 })")  # a new chapter in the same file: a fade
    assert floats(nv) == ["nvtour://fade"]
    assert nv.exec_lua("return vim.bo.modifiable") is False  # the old view, read-only
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    wait_done(nv)
    wins, lines = jump_card(nv, 3)
    assert wins == ["nvtour://fade"] and lines[1:-1] == ["│ Two │"]  # with its name only
    wait_done(nv)
    assert topline(nv) == final and nv.exec_lua("return vim.bo.modifiable") is True
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 3


def hold(nv, n):
    """The time the title card of the jump from step n - 1 to step n stays."""
    return nv.exec_lua(f"local s = _G.nvtour.state.tour.steps return _G.nvtour._anim.card(s[{n}], s[{n} - 1]).hold")


def test_the_card_stays_for_each_line_that_it_shows(cli, nv, animate):
    nv.exec_lua("vim.g.nvtour_title_ms = 700; vim.g.nvtour_file_ms = 300")
    ok(cli("start", "Anim"))
    ok(cli("chapter", "One"))
    ok(cli("step", "a.txt:2"))
    ok(cli("step", "b.txt:20"))  # the file
    ok(cli("chapter", "Two"))
    ok(cli("step", "b.txt:25"))  # the chapter
    ok(cli("chapter", "Three"))
    ok(cli("step", "a.txt:5"))  # both
    ok(cli("step", "a.txt:9"))  # nothing
    assert [hold(nv, n) for n in (2, 3, 4, 5)] == [300, 700, 1000, 0]
    nv.exec_lua("vim.g.nvtour_title_ms = nil; vim.g.nvtour_file_ms = nil")
    assert hold(nv, 4) == 3000  # 1500 each by default


def test_a_card_line_with_0_ms_is_left_out(cli, nv, animate, side_view):
    nv.exec_lua("vim.g.nvtour_title_ms = 0")
    ok(cli("start", "Anim"))
    ok(cli("chapter", "One"))
    ok(cli("step", "a.txt:2"))
    ok(cli("chapter", "Two"))
    ok(cli("step", "a.txt:50"))
    ok(cli("step", "b.txt:20"))
    hide_card(nv)
    ok(cli("goto", "1"))
    wait_done(nv)
    assert jump_card(nv, 2, shows=False) == ([], [])  # a new chapter only scrolls
    wait_done(nv)
    wins, lines = jump_card(nv, 3)
    assert wins == ["nvtour://fade"] and lines[1:-1] == ["│ ◇ b.txt │"]  # the file line stays
    wait_done(nv)
    nv.exec_lua("vim.g.nvtour_file_ms = 0")
    ok(cli("goto", "2"))
    wait_done(nv)
    assert jump_card(nv, 3, shows=False) == (["nvtour://fade"], [])  # a fade without a card
    wait_done(nv)


def test_the_inline_view_has_no_title_card(cli, nv, animate):
    ok(cli("start", "Anim"))
    ok(cli("chapter", "One"))
    ok(cli("step", "a.txt:2"))
    ok(cli("chapter", "Two"))
    ok(cli("step", "a.txt:50"))
    ok(cli("step", "b.txt:20"))
    ok(cli("goto", "1"))
    wait_done(nv)
    assert jump_card(nv, 2, shows=False) == ([], [])  # a new chapter in the same file: a scroll
    wait_done(nv)
    assert jump_card(nv, 3, shows=False) == (["nvtour://fade"], [])  # a new file: a fade, no card
    wait_done(nv)


def card_lights(nv):
    """The lit parts of the side card: (group, the lit text) per mark of side.flash."""
    return nv.exec_lua(
        "local c = _G.nvtour.state.card local ns = vim.api.nvim_create_namespace('nvtour_card_flash') "
        "local out = {} for _, m in ipairs(vim.api.nvim_buf_get_extmarks(c.buf, ns, 0, -1, { details = true })) do "
        "local t = vim.api.nvim_buf_get_text(c.buf, m[2], m[3], m[4].end_row, m[4].end_col, {}) "
        "out[#out + 1] = { m[4].hl_group, table.concat(t, '|') } end return out"
    )


def test_the_side_card_lights_up_what_changed(cli, nv, animate, side_view):
    nv.exec_lua("vim.g.nvtour_title_ms = 300; vim.g.nvtour_file_ms = 300")
    ok(cli("start", "Anim"))
    ok(cli("chapter", "One"))
    ok(cli("step", "a.txt:2"))
    ok(cli("chapter", "Two"))
    ok(cli("step", "b.txt:20"))  # the chapter and the file
    ok(cli("step", "b.txt:30"))  # nothing
    ok(cli("step", "a.txt:30", "--ref", "v1"))  # the file and the version
    nv.exec_lua("_G.nvtour.state.view = 'side'")
    ok(cli("goto", "1"))
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    lit = card_lights(nv)  # at once: the code is shown, the card says what changed
    assert ["NvtourCardFlashChapter", "Two"] in lit
    files = [t for g, t in lit if g == "NvtourCardFlashFile"]
    assert len(files) == 1 and files[0].startswith("b.txt:20|"), lit
    assert card(nv) == []  # no title card on the curtain
    assert "%#NvtourWinbarFlash#Two · %*" in nv.exec_lua("return _G.nvtour.winbar()")  # and in the winbar
    wait_done(nv)
    assert card_lights(nv) == []  # the light is gone at the end
    assert "%#NvtourPanelChapter#Two · %*" in nv.exec_lua("return _G.nvtour.winbar()")
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 3 })")
    assert card_lights(nv) == []  # nothing changed: nothing lights up
    assert "NvtourWinbarFlash" not in nv.exec_lua("return _G.nvtour.winbar()")
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 4 })")
    assert "NvtourWinbarFlash" not in nv.exec_lua("return _G.nvtour.winbar()")  # only a new file: not the chapter
    wait_done(nv)
    ok(cli("goto", "3"))
    wait_done(nv)
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 4 })")
    lit = card_lights(nv)
    assert [g for g, _ in lit] == ["NvtourCardFlashFile"] and "git ref v1" in lit[0][1], lit
    wait_done(nv)


def test_the_card_light_blinks(cli, nv, animate, side_view):
    nv.exec_lua("vim.g.nvtour_title_ms = 1000; vim.g.nvtour_file_ms = 1000")
    ok(cli("start", "Anim"))
    ok(cli("chapter", "One"))
    ok(cli("step", "a.txt:2"))
    ok(cli("chapter", "Two"))
    ok(cli("step", "b.txt:20"))
    nv.exec_lua("_G.nvtour.state.view = 'side'")
    ok(cli("goto", "1"))
    wait_done(nv)
    light = nv.exec_lua("return vim.api.nvim_get_hl(0, { name = 'NvtourFlash' }).bg")
    nv.exec_lua("_G.nvtour.dispatch('goto', { n = 2 })")
    seen, bars = set(), set()
    deadline = time.time() + 2
    while running(nv) and time.time() < deadline:
        seen.add(nv.exec_lua("return vim.api.nvim_get_hl(0, { name = 'NvtourCardFlashChapter' }).bg") == light)
        bars.add(nv.exec_lua("return vim.api.nvim_get_hl(0, { name = 'NvtourWinbarFlash' }).bg") == light)
        time.sleep(0.03)
    assert seen == {True, False} and bars == {True, False}  # on and off, in the card and in the winbar
    assert card_lights(nv) == []


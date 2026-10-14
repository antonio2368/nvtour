"""Tests of the side view: the step card on the left and the range styles in the code."""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from test_integration import find_buf, ns_marks, ok

WAIT = 0.3


@pytest.fixture()
def side(nv):
    """vim.notify collects its messages in _G.msgs; the view and the style are reset at the end. The
    screen is wide enough for the card, the code and the panel."""
    nv.command("set columns=200")
    nv.exec_lua("_G.msgs = {}; _G.old_notify = vim.notify; vim.notify = function(m) table.insert(_G.msgs, m) end")
    yield lambda: nv.exec_lua("return _G.msgs")
    nv.exec_lua("vim.notify = _G.old_notify; vim.g.nvtour_view = nil; vim.g.nvtour_range_style = nil")
    nv.exec_lua("_G.nvtour.state.view = nil; _G.nvtour.state.range_style = nil; _G.nvtour.dispatch('clear', {})")
    nv.command("set columns=80")


def tour(cli):
    ok(cli("start", "Side"))
    ok(cli("step", "a.txt:5-7", "--label", "one", "--note", "The **first** one has `code`.", "--expect", "line 6"))
    ok(
        cli(
            "step", "a.txt:12-14", "--label", "two", "--role", "fix", "--via", "then `two`",
            "--note", "The second one.", "--suggest", "new twelve\n",
        )
    )
    ok(cli("step", "b.txt:10-12", "--label", "three", "--note", "The third one."))
    ok(cli("goto", "1"))


def dispatch(nv, cmd, **args):
    return nv.exec_lua("return _G.nvtour.dispatch(...)", cmd, args)


def card(nv):
    """The window and the lines of the card, or (None, None) when it is closed."""
    return nv.exec_lua(
        "local c = _G.nvtour.state.card; "
        "if c.win and vim.api.nvim_win_is_valid(c.win) then "
        "return { c.win, vim.api.nvim_buf_get_lines(c.buf, 0, -1, false) } end return { vim.NIL, vim.NIL }"
    )


def details(nv, suffix):
    return [m[3] | {"row": m[1]} for m in ns_marks(nv, "nvtour_steps", find_buf(nv, suffix).handle)]


def virt_text(lines):
    return "\n".join("".join(c[0] for c in line) for line in lines)


def test_side_view_moves_the_step_into_the_card(cli, nv, side):
    tour(cli)
    assert any(d.get("virt_lines") for d in details(nv, "a.txt"))  # inline: the note is in the code
    res = dispatch(nv, "view")
    assert res["ok"] and res["view"] == "side"
    win, lines = card(nv)
    assert nv.api.buf_get_name(nv.api.win_get_buf(win)) == "nvtour://step"
    assert nv.api.win_get_position(win) == [0, 0]  # on the left
    text = "\n".join(lines)
    assert lines[0].startswith("1/3 • info") and lines[1] == "one"
    assert "a.txt:5-7" in text
    assert "The **first** one has `code`." in text  # markdown: the buffer conceals the markers
    assert lines[lines.index("Watch") + 1] == "• line 6"
    at = lines.index("Next  ]w")
    assert lines[at + 1 : at + 4] == ["2 ✓ two", "line 12", "↓ then `two`"]
    at = lines.index("Previous  [w")
    assert lines[at + 1] == "first step"
    marks = details(nv, "a.txt")
    # No note, link line or eol marker in the code: the numbers, the rules and the --expect marks.
    assert not any(d.get("virt_text_pos") == "eol" for d in marks)
    assert [(d["row"], d["hl_group"]) for d in marks if d.get("hl_group", "").startswith("NvtourMark")] == [(5, "NvtourMarkInfo")]
    vl = [d for d in marks if d.get("virt_lines")]
    assert len(vl) == 2  # the rule above and the rule below the current step
    above = next(d for d in vl if d.get("virt_lines_above"))
    assert above["row"] == 4 and "── 1 • one ──" in virt_text(above["virt_lines"])
    assert "first" not in virt_text(above["virt_lines"])
    assert sum(1 for d in marks if d.get("number_hl_group")) == 6  # the other steps keep their numbers
    assert not any(d.get("sign_text") for d in marks)


def test_card_follows_the_current_step_and_git(cli, nv, side, sandbox):
    nv.command("let g:nvtour_view = 'side'")
    tour(cli)
    _, lines = card(nv)
    def git(*args):
        return subprocess.run(["git", "-C", str(sandbox.ws), *args], capture_output=True, text=True).stdout.strip()

    sha, branch = git("rev-parse", "HEAD"), git("branch", "--show-current")
    assert f"working tree · {branch} @ {sha[:12]}" in lines and "repo ws" in lines
    ok(cli("next"))
    _, lines = card(nv)
    assert lines[0] == "2/3 ✓ fix" and "↑ from 1: then `two`" in lines
    at = lines.index("Previous  [w")
    assert lines[at + 1 : at + 3] == ["1 • one", "line 5"]
    marks = details(nv, "a.txt")
    below = next(d for d in marks if d.get("virt_lines") and not d.get("virt_lines_above"))
    assert "suggested" in virt_text(below["virt_lines"]) and "new twelve" in virt_text(below["virt_lines"])
    assert any(d.get("hl_group") == "NvtourStrike" for d in marks)  # the suggestion stays in the code
    ok(cli("next"))
    _, lines = card(nv)
    assert "b.txt:10-12" in lines and lines[lines.index("Next  ]w") + 1] == "last step"
    assert nv.api.win_get_position(card(nv)[0]) == [0, 0]


def test_card_names_the_version(cli, nv, side, sandbox):
    nv.command("let g:nvtour_view = 'side'")
    ok(cli("start", "Versions"))
    ok(cli("step", "a.txt:3", "--ref", "v1", "--label", "old"))
    sha = subprocess.run(["git", "-C", str(sandbox.ws), "rev-parse", "v1^{commit}"], capture_output=True, text=True).stdout.strip()
    _, lines = card(nv)
    assert f"git ref v1 · {sha[:12]}" in lines and "repo ws" in lines
    ok(cli("step", "a.txt:3", "--ref", sha, "--label", "by hash", "--jump"))
    assert f"commit {sha[:12]}" in card(nv)[1]
    ok(cli("step", "a.txt:3", "--label", "now", "--jump"))
    assert any(line.startswith("working tree · ") for line in card(nv)[1])
    win = card(nv)[0]
    assert nv.api.get_option_value("statuscolumn", {"win": win}) == "  "  # a left margin


def test_range_styles(cli, nv, side):
    tour(cli)
    res = dispatch(nv, "range_style", style="bracket")
    assert res["view"] == "side"  # a style is shown in the side view only
    assert not any(d.get("virt_lines") for d in details(nv, "a.txt"))  # no rules
    signs = sorted((d["row"], d["sign_text"]) for d in details(nv, "a.txt") if d.get("sign_text"))
    assert signs == [(4, " ┌"), (5, " │"), (6, " └")]
    dispatch(nv, "range_style", style="rules+spotlight")
    marks = details(nv, "a.txt")
    dim = sorted((d["row"], d["end_row"]) for d in marks if d.get("hl_group") == "NvtourDim")
    assert dim == [(0, 4), (7, 59)]
    assert any(d.get("virt_lines_above") for d in marks)
    for bad in ("glow", "box"):
        res = dispatch(nv, "range_style", style=bad)
        assert not res["ok"] and res["code"] == 2 and bad in res["error"]
    nv.command("let g:nvtour_range_style = 'box'")  # a style that is gone: the default
    dispatch(nv, "view", mode="side")
    assert nv.exec_lua("return _G.nvtour.dispatch('status', {}).range_style") == "rules+spotlight"
    nv.exec_lua("_G.nvtour.state.range_style = nil")
    assert nv.exec_lua("return _G.nvtour.dispatch('status', {}).range_style") == "rules"


def test_keys_cycle_the_style_and_toggle_the_view(cli, nv, side):
    tour(cli)
    nv.input("\\wv")
    time.sleep(WAIT)
    assert card(nv)[0] is not None and "nvtour: side view" in side()
    nv.input("\\wr")
    time.sleep(WAIT)
    assert "nvtour: range style bracket" in side()
    status = json.loads(ok(cli("--json", "status")))
    assert status["view"] == "side" and status["range_style"] == "bracket" and status["card"] is True
    assert "view: side (bracket), card open" in ok(cli("status"))
    for style in ("spotlight", "rules+spotlight", "rules"):  # the cycle goes round
        nv.input("\\wr")
        time.sleep(WAIT)
        assert f"nvtour: range style {style}" in side()
    nv.input("\\wv")
    time.sleep(WAIT)
    assert card(nv)[0] is None
    assert any("first" in virt_text(d["virt_lines"]) for d in details(nv, "a.txt") if d.get("virt_lines"))


def test_card_keys(cli, nv, side):
    tour(cli)
    dispatch(nv, "view", mode="side")
    win, lines = card(nv)
    nv.current.window = win
    row = lines.index("Next  ]w") + 1  # the title line of the next step
    nv.api.win_set_cursor(win, [row + 1, 0])
    nv.input("<CR>")
    time.sleep(WAIT)
    assert nv.exec_lua("return _G.nvtour.state.tour.current") == 2
    win, _ = card(nv)
    nv.current.window = win
    nv.input("q")
    time.sleep(WAIT)
    assert card(nv)[0] is None and nv.exec_lua("return _G.nvtour.state.view") == "side"  # like the panel


def test_card_toggle(cli, nv, side):
    tour(cli)
    nv.input("\\ws")  # in the inline view: shows the card and turns the side view on
    time.sleep(WAIT)
    assert card(nv)[0] is not None and "nvtour: step card shown" in side()
    nv.input("\\ws")
    time.sleep(WAIT)
    assert card(nv)[0] is None and "nvtour: step card hidden" in side()
    assert any(d.get("virt_lines_above") for d in details(nv, "a.txt"))  # still the side view: the rules
    ok(cli("next"))
    assert card(nv)[0] is None  # a hidden card stays hidden on a jump
    assert "<leader>ws card" in ok(cli("status"))
    nv.input("\\wv")  # side -> inline
    time.sleep(WAIT)
    nv.input("\\wv")  # inline -> side: with its card
    time.sleep(WAIT)
    assert card(nv)[0] is not None


def test_clear_closes_the_card(cli, nv, side):
    tour(cli)
    dispatch(nv, "view", mode="side")
    assert card(nv)[0] is not None
    ok(cli("clear"))
    assert card(nv)[0] is None and find_buf(nv, "nvtour://step") is None
    ok(cli("start", "Again"))
    ok(cli("step", "a.txt:3", "--label", "x"))
    assert card(nv)[1][1] == "x"  # the view stays side for the next tour

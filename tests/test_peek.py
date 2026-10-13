"""Tests of the peek float: the full content of a step at the cursor, without making it current."""

from __future__ import annotations

import json
import time

import pytest
from test_diagram import DIAGRAM
from test_integration import find_buf, ok

WAIT = 0.3
KEY = "\\wk"  # <leader>wk with the default leader


@pytest.fixture()
def notes(nv):
    """vim.notify collects its messages in _G.msgs; restored, and the tour cleared, at the end."""
    nv.exec_lua("_G.msgs = {}; _G.old_notify = vim.notify; vim.notify = function(m) table.insert(_G.msgs, m) end")
    yield lambda: nv.exec_lua("return _G.msgs")
    nv.exec_lua("vim.notify = _G.old_notify; vim.g.nvtour_follow_cursor = nil; vim.g.nvtour_keys = nil")
    nv.exec_lua("_G.nvtour.dispatch('clear', {})")


def tour(cli):
    ok(cli("start", "Peek"))
    ok(cli("step", "a.txt:5-7", "--label", "one", "--note", "The first one."))
    ok(
        cli(
            "step", "a.txt:12-14", "--label", "two", "--role", "fix", "--note", "The **second** one has `code`.",
            "--suggest", "    new twelve\n    new thirteen\n",
        )
    )
    ok(cli("step", "b.txt:10-12", "--label", "three", "--note", "The third one."))
    ok(cli("goto", "1"))


def floats(nv):
    return nv.exec_lua(
        "return vim.tbl_filter(function(w) return vim.api.nvim_win_get_config(w).relative ~= '' end, vim.api.nvim_list_wins())"
    )


def peek_lines(nv):
    """The lines of the peek float, or None when there is none."""
    return nv.exec_lua(
        "local p = _G.nvtour.state.peek; "
        "if p.win and vim.api.nvim_win_is_valid(p.win) then return vim.api.nvim_buf_get_lines(p.buf, 0, -1, false) end"
    )


def put(nv, line, win=None):
    """Move the cursor like a key would: the main loop runs CursorMoved."""
    nv.api.win_set_cursor(win or nv.current.window, [line, 0])
    nv.input("")
    time.sleep(WAIT)


def press(nv, keys):
    nv.input(keys)
    time.sleep(WAIT)


def goto_line(nv, line):
    put(nv, line)
    assert floats(nv) == []


def test_key_opens_the_step_under_the_cursor(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    press(nv, KEY)
    wins = floats(nv)
    assert len(wins) == 1
    cfg = nv.api.win_get_config(wins[0])
    assert cfg["relative"] != "" and cfg["focusable"] is False and cfg["zindex"] == 60
    assert cfg["border"][0] == "╭"
    assert nv.api.buf_get_name(nv.api.win_get_buf(wins[0])) == "nvtour://peek"
    text = "\n".join(peek_lines(nv))
    assert "◇ a.txt" in text
    assert "The second one has code." in text
    assert "╭ suggested " in text and "│ + new twelve" in text and "│ + new thirteen" in text
    assert "→ next 3 · b.txt:10" in text
    assert nv.current.window.handle != wins[0]  # not focused
    assert nv.api.win_get_option(wins[0], "winhighlight") == "FloatBorder:NvtourFrameFix"
    # the same lines of the note as the note under the code: bold and code are highlighted
    marks = nv.exec_lua(
        "local p = _G.nvtour.state.peek; local ns = vim.api.nvim_create_namespace('nvtour_peek'); "
        "return vim.tbl_map(function(m) return m[4].hl_group end, vim.api.nvim_buf_get_extmarks(p.buf, ns, 0, -1, { details = true }))"
    )
    assert "NvtourNoteBold" in marks and "NvtourNoteCode" in marks and "NvtourSuggest" in marks
    # nothing else changed: the current step is still step 1
    assert json.loads(ok(cli("--json", "status")))["current"] == 1


def test_a_step_in_another_file_has_its_arrival_line(cli, nv, notes):
    tour(cli)
    ok(cli("goto", "3"))
    goto_line(nv, 11)
    press(nv, KEY)
    text = "\n".join(peek_lines(nv))
    assert "◇ b.txt" in text and "The third one." in text
    assert "next" not in text  # the last step


def test_cursor_move_closes(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    press(nv, KEY)
    assert len(floats(nv)) == 1
    put(nv, 20)
    assert floats(nv) == []
    assert nv.exec_lua("return _G.nvtour.state.peek.win") is None
    assert not any(b.name == "nvtour://peek" for b in nv.buffers)


def test_key_again_toggles(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    press(nv, KEY)
    assert len(floats(nv)) == 1
    press(nv, KEY)
    assert floats(nv) == []
    press(nv, KEY)
    assert len(floats(nv)) == 1


@pytest.mark.parametrize("key", ["<Esc>", "q"])
def test_esc_and_q_close(cli, nv, notes, key):
    tour(cli)
    goto_line(nv, 13)
    press(nv, KEY)
    assert len(floats(nv)) == 1
    press(nv, key)
    assert floats(nv) == []


def test_q_is_the_macro_key_again_after_close(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    buf = nv.current.buffer.handle
    press(nv, KEY)
    assert {m["lhs"] for m in nv.api.buf_get_keymap(buf, "n")} >= {"q", "<Esc>"}
    press(nv, "q")
    assert nv.api.buf_get_keymap(buf, "n") == []  # no mapping is left behind
    press(nv, KEY)
    put(nv, 20)
    assert nv.api.buf_get_keymap(buf, "n") == []


def test_buffer_local_keys_of_the_user_stay(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    buf = nv.current.buffer.handle
    nv.command("nnoremap <buffer> q :let g:user_q = 1<CR>")
    try:
        press(nv, KEY)
        maps = {m["lhs"]: m.get("rhs") for m in nv.api.buf_get_keymap(buf, "n")}
        assert maps["q"] == ":let g:user_q = 1<CR>"  # not replaced
        assert "<Esc>" in maps
        press(nv, "<Esc>")
        assert floats(nv) == []
        assert {m["lhs"] for m in nv.api.buf_get_keymap(buf, "n")} == {"q"}  # the user's map is still there
    finally:
        nv.command("nunmap <buffer> q")


def test_leaving_the_window_closes(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    press(nv, KEY)
    nv.command("split")
    time.sleep(WAIT)
    assert floats(nv) == []
    nv.command("close")


def test_no_step_here(cli, nv, notes):
    tour(cli)
    goto_line(nv, 30)
    press(nv, KEY)
    assert floats(nv) == []
    assert any("no step here" in m for m in notes())


def test_innermost_step_wins(cli, nv, notes):
    ok(cli("start", "Nested"))
    ok(cli("step", "a.txt:10-20", "--label", "outer", "--note", "Outer note."))
    ok(cli("step", "a.txt:14-15", "--label", "inner", "--note", "Inner note."))
    ok(cli("goto", "1"))
    goto_line(nv, 14)
    press(nv, KEY)
    assert "Inner note." in "\n".join(peek_lines(nv))
    put(nv, 11)
    press(nv, KEY)
    assert "Outer note." in "\n".join(peek_lines(nv))


def test_command_peeks_a_step_by_number(cli, nv, notes):
    tour(cli)
    nv.command("NvtourPeek 3")
    assert len(floats(nv)) == 1
    assert "The third one." in "\n".join(peek_lines(nv))
    assert json.loads(ok(cli("--json", "status")))["current"] == 1
    nv.command("NvtourPeek 2")  # another step: only one float, now for step 2
    assert len(floats(nv)) == 1
    assert "The second one" in "\n".join(peek_lines(nv))
    nv.command("NvtourPeek 2")  # the same step again closes it
    assert floats(nv) == []


def test_command_without_number_peeks_the_cursor_step(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    nv.command("NvtourPeek")
    assert "The second one" in "\n".join(peek_lines(nv))
    nv.command("NvtourPeek")
    assert floats(nv) == []
    goto_line(nv, 30)
    nv.command("NvtourPeek")
    assert floats(nv) == [] and any("no step here" in m for m in notes())


@pytest.mark.parametrize("arg", ["9", "0"])
def test_command_out_of_range_warns(cli, nv, notes, arg):
    tour(cli)
    nv.command(f"NvtourPeek {arg}")
    assert floats(nv) == []
    assert any("no such step" in m for m in notes())


def test_panel_k_peeks_the_step_of_the_line(cli, nv, notes):
    tour(cli)
    panel = next(w for w in nv.windows if w.buffer.name.endswith("nvtour://panel"))
    line = nv.exec_lua("for l, n in pairs(_G.nvtour.state.panel.line_map) do if n == 2 then return l end end")
    nv.api.set_current_win(panel.handle)
    put(nv, line, panel)
    press(nv, "K")
    assert len(floats(nv)) == 1
    assert "The second one" in "\n".join(peek_lines(nv))
    assert nv.current.window.handle == panel.handle
    assert json.loads(ok(cli("--json", "status")))["current"] == 1
    press(nv, "K")  # again: closed
    assert floats(nv) == []
    put(nv, 1, panel)
    press(nv, "K")  # the title line is no step
    assert floats(nv) == []
    put(nv, line, panel)
    press(nv, "K")
    assert len(floats(nv)) == 1
    press(nv, "<Esc>")
    assert floats(nv) == []
    assert nv.api.buf_get_keymap(panel.buffer.handle, "n")  # the panel keys are still there


def test_clear_closes_it(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    press(nv, KEY)
    assert len(floats(nv)) == 1
    ok(cli("clear"))
    assert floats(nv) == []
    assert not any(b.name == "nvtour://peek" for b in nv.buffers)
    assert nv.exec_lua("return vim.fn.maparg('<leader>wk', 'n')") == ""


def test_start_closes_it(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    press(nv, KEY)
    ok(cli("start", "Again"))
    assert floats(nv) == []


def test_no_autocmds_are_left_behind(cli, nv, notes):
    tour(cli)
    goto_line(nv, 13)
    press(nv, KEY)
    assert nv.exec_lua("return #vim.api.nvim_get_autocmds({ group = 'nvtour_peek' })") > 0
    press(nv, "<Esc>")
    assert nv.exec_lua("return pcall(vim.api.nvim_get_autocmds, { group = 'nvtour_peek' })") is False


def test_peek_with_follow_keeps_the_current_step(cli, nv, notes):
    tour(cli)
    nv.exec_lua("vim.g.nvtour_follow_cursor = true")
    goto_line(nv, 6)
    press(nv, KEY)  # peek of step 1, the current one: still a peek
    assert len(floats(nv)) == 1
    nv.command("NvtourPeek 3")
    time.sleep(WAIT)
    assert len(floats(nv)) == 1  # the follow check did not close it, and it did not move the cursor
    assert json.loads(ok(cli("--json", "status")))["current"] == 1
    assert nv.api.win_get_cursor(nv.current.window) == [6, 0]
    put(nv, 13)  # now the cursor follows: step 2 is current, the peek is closed
    assert floats(nv) == []
    assert json.loads(ok(cli("--json", "status")))["current"] == 2


def test_peek_does_not_change_the_other_steps(cli, nv, notes):
    tour(cli)
    buf = find_buf(nv, "a.txt")
    ns = nv.api.create_namespace("nvtour_steps")
    before = json.dumps(nv.api.buf_get_extmarks(buf.handle, ns, 0, -1, {"details": True}))
    goto_line(nv, 13)
    press(nv, KEY)
    assert len(floats(nv)) == 1
    assert json.dumps(nv.api.buf_get_extmarks(buf.handle, ns, 0, -1, {"details": True})) == before


def test_ref_step_and_diagram_step(cli, nv, notes):
    ok(cli("start", "Kinds"))
    ok(cli("step", "a.txt:5-9", "--ref", "v1", "--note", "Old a.", "--label", "old"))
    ok(cli("diagram", "flow", "--format", "text", stdin=DIAGRAM))
    ok(cli("step", "--diagram", "flow", "2", "--label", "client sends", "--note", "The client sends."))
    ok(cli("goto", "1"))
    nv.command("NvtourPeek 1")
    text = "\n".join(peek_lines(nv))
    assert "Old a." in text and "@v1" in text
    press(nv, "<Esc>")
    assert floats(nv) == []
    ok(cli("goto", "2"))
    dwin = next(w for w in nv.windows if w.buffer.name.endswith("nvtour://diagram/flow"))
    nv.api.set_current_win(dwin.handle)
    put(nv, 2, dwin)
    press(nv, KEY)
    assert len(floats(nv)) == 1
    assert "The client sends." in "\n".join(peek_lines(nv))


def test_lost_step_from_the_command_not_from_the_code(cli, nv, notes):
    ok(cli("start", "Lost"))
    ok(cli("step", "a.txt:10-12", "--label", "gone", "--note", "Will be lost.", "--no-jump"))
    ok(cli("step", "a.txt:30", "--label", "stays"))
    buf = find_buf(nv, "a.txt")
    nv.api.buf_set_lines(buf.handle, 9, 12, False, [])
    assert json.loads(ok(cli("--json", "status")))["steps"][0].get("lost") is True
    ok(cli("goto", "2"))
    goto_line(nv, 10)
    press(nv, KEY)  # no range: no step here
    assert floats(nv) == [] and any("no step here" in m for m in notes())
    nv.command("NvtourPeek 1")
    text = "\n".join(peek_lines(nv))
    assert "✗ lost" in text and "Will be lost." in text
    nv.command("NvtourPeek 1")
    assert floats(nv) == []


def test_key_can_be_renamed_or_removed(cli, nv, notes):
    nv.exec_lua("vim.g.nvtour_keys = { peek = '<leader>wo' }")
    tour(cli)
    assert nv.exec_lua("return _G.nvtour.state.keys.peek") == "<leader>wo"
    assert nv.exec_lua("return vim.fn.maparg('<leader>wk', 'n')") == ""
    goto_line(nv, 13)
    press(nv, "\\wo")
    assert len(floats(nv)) == 1
    press(nv, "<Esc>")
    ok(cli("clear"))
    assert nv.exec_lua("return vim.fn.maparg('<leader>wo', 'n')") == ""
    nv.exec_lua("vim.g.nvtour_keys = { peek = '' }")
    tour(cli)
    assert nv.exec_lua("return _G.nvtour.state.keys.peek") is None
    assert "peek" not in ok(cli("status")).split("keys:")[1]


def test_key_listed_in_status_and_panel_footer(cli, nv, notes):
    tour(cli)
    assert "<leader>wk peek" in ok(cli("status"))
    footer = nv.exec_lua("local l = vim.api.nvim_buf_get_lines(vim.fn.bufnr('nvtour://panel'), 0, -1, false); return l[#l]")
    assert "`<leader>wk` peek" in footer


def test_taken_key_is_skipped_with_a_warning(cli, nv, notes):
    nv.command("nnoremap <leader>wk <Nop>")
    try:
        data = json.loads(ok(cli("--json", "start", "Taken")))
        assert "peek" not in data["keys"] and any("<leader>wk" in w for w in data["warnings"])
    finally:
        nv.command("nunmap <leader>wk")


def test_note_width_limits_the_float(cli, nv, notes):
    tour(cli)
    nv.exec_lua("vim.g.nvtour_note_width = 40")
    try:
        goto_line(nv, 13)
        press(nv, KEY)
        assert nv.api.win_get_width(floats(nv)[0]) == 40
    finally:
        nv.exec_lua("vim.g.nvtour_note_width = nil")


def test_a_float_is_at_most_half_the_window(cli, nv, notes):
    ok(cli("start", "Tall"))
    note = "\n\n".join(f"Paragraph {i} of a long note." for i in range(1, 30))
    ok(cli("step", "a.txt:5-7", "--note", note))
    ok(cli("goto", "1"))
    nv.command("NvtourPeek 1")
    win = floats(nv)[0]
    assert nv.api.win_get_height(win) <= nv.current.window.height // 2

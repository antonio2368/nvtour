"""Unit tests: ranges, scoring, pin cache, socket precedence."""

from __future__ import annotations

import pytest

from nvtour import cli, discover
from nvtour.errors import EXIT_BAD_FILE, NvtourError
from nvtour.ranges import parse_file_range, parse_range


def test_parse_single_and_range(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    assert parse_file_range(f"{f}:10") == (str(f), 10, 10)
    assert parse_file_range(f"{f}:10-20") == (str(f), 10, 20)
    assert parse_range("3-3") == (3, 3)


@pytest.mark.parametrize("bad", ["20-10", "0", "a-b", "", "5-", "-5"])
def test_parse_range_errors(bad):
    with pytest.raises(NvtourError) as e:
        parse_range(bad)
    assert e.value.code == EXIT_BAD_FILE


def test_parse_line_col_and_comma(tmp_path):
    f = tmp_path / "f.cpp"
    f.write_text("x")
    assert parse_file_range(f"{f}:12:5") == (str(f), 12, 12)
    assert parse_file_range(f"{f}:12-14:5") == (str(f), 12, 14)
    assert parse_file_range(f"{f}:12,15") == (str(f), 12, 15)
    # a missing file is reported without the line number glued to its name
    assert parse_file_range(f"{tmp_path}/missing.cpp:3:1")[0] == str(tmp_path / "missing.cpp")


def test_file_literally_named_with_a_line_number(tmp_path):
    f = tmp_path / "odd:12"
    f.write_text("x")
    assert parse_file_range(f"{f}:5") == (str(f), 5, 5)
    assert parse_file_range(f"{tmp_path}/odd:12:5") == (str(f), 5, 5)


def test_parse_file_range_needs_colon():
    with pytest.raises(NvtourError):
        parse_file_range("nocolon")


W = "/home/u/proj"


@pytest.mark.parametrize(
    "cwd,bufs,expected",
    [
        (W, [], 100),
        (W + "/src", [], 80),
        ("/home/u", [], 60),
        ("/elsewhere", [W + "/x.cpp"], 40),
        ("/elsewhere", ["/other/y.cpp"], 0),
    ],
)
def test_score_table(cwd, bufs, expected):
    assert discover.score_instance(cwd, W, bufs)[0] == expected


def test_score_sibling_dir_is_not_ancestor():
    assert discover.score_instance(W + "-other", W, [])[0] == 0


def test_pin_cache_roundtrip(tmp_path):
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    sock = tmp_path / f"nvim.{__import__('os').getpid()}.0"  # our own pid is alive
    sock.write_text("")
    assert discover.read_pin(W, env) is None
    discover.write_pin(W, str(sock), env)
    assert discover.pin_path(W, env).parent == tmp_path / "nvtour"
    assert discover.read_pin(W, env) == str(sock)
    assert discover.clear_pin(W, env) is True
    assert discover.read_pin(W, env) is None
    assert discover.clear_pin(W, env) is False


def test_dead_pin_is_removed(tmp_path):
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    discover.write_pin(W, str(tmp_path / "nvim.999999999.0"), env)
    assert discover.read_pin(W, env) is None
    assert not discover.pin_path(W, env).exists()


def test_custom_socket_pin_survives(tmp_path):
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    sock = tmp_path / "custom.sock"
    sock.write_text("")
    discover.write_pin(W, str(sock), env, pid=999999999)  # pid not visible here: checked by existence
    assert discover.read_pin(W, env) == str(sock)
    sock.unlink()
    assert discover.read_pin(W, env) is None


def test_plain_text_pin_from_older_version(tmp_path):
    import os

    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    sock = tmp_path / f"nvim.{os.getpid()}.0"
    sock.write_text("")
    p = discover.pin_path(W, env)
    p.parent.mkdir(parents=True)
    p.write_text(str(sock))
    assert discover.read_pin(W, env) == str(sock)


def test_stdin_on_a_terminal_is_refused(monkeypatch):
    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    with pytest.raises(NvtourError) as e:
        cli.read_text_arg("-", "--note -")
    assert e.value.code == 2


def test_socket_precedence(tmp_path):
    import os

    live = tmp_path / f"nvim.{os.getpid()}.0"
    live.write_text("")
    a, b, c = (tmp_path / n for n in "abc")
    for p in (a, b, c):
        p.write_text("")
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "NVTOUR_SOCKET": str(b), "NVIM": str(c)}
    discover.write_pin(W, str(live), env)
    assert discover.explicit_socket(str(a), W, env) == (str(a), "--socket")
    assert discover.explicit_socket(None, W, env) == (str(b), "$NVTOUR_SOCKET")
    del env["NVTOUR_SOCKET"]
    assert discover.explicit_socket(None, W, env) == (str(c), "$NVIM")
    del env["NVIM"]
    assert discover.explicit_socket(None, W, env) == (str(live), "pin")
    discover.clear_pin(W, env)
    assert discover.explicit_socket(None, W, env) is None


def test_parser_accepts_globals_anywhere():
    p = cli.build_parser()
    assert p.parse_args(["--json", "where"]).json is True
    assert p.parse_args(["where", "--json"]).json is True
    assert p.parse_args(["--socket", "/s", "where"]).socket == "/s"
    assert p.parse_args(["where"]).timeout == 5.0

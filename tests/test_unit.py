"""Unit tests: ranges, scoring, pin cache, socket precedence."""

from __future__ import annotations

import pytest

from nvtour import cli, discover
from nvtour.errors import EXIT_BAD_FILE, EXIT_USAGE, NvtourError
from nvtour.ranges import format_query, parse_file_range, parse_query, parse_range, parse_range_or_query, parse_spec


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


def q(start, end=None, plus=0, nth=None, near=None, minus=0):
    return {"start": start, "end": end, "minus": minus, "plus": plus, "nth": nth, "near": near}


QUERIES = [
    ("/foo/", q("foo")),
    ("/two words/", q("two words")),
    ("/foo/+3", q("foo", plus=3)),
    ("/foo/@2", q("foo", nth=2)),
    ("/foo/~40", q("foo", near=40)),
    ("/foo/@2+2", q("foo", nth=2, plus=2)),
    ("/foo/~7+1", q("foo", near=7, plus=1)),
    ("/a/../b/", q("a", end="b")),
    ("/a/@2../b/", q("a", end="b", nth=2)),
    ("/a/~9../b/+4", q("a", end="b", near=9, plus=4)),
    ("/a\\/b/", q("a/b")),  # an escaped slash is part of the text
    ("/http:\\/\\/x/", q("http://x")),
    ("/Cache::get/", q("Cache::get")),
    ("/ x /", q(" x ")),
    ("/\\n/", q("\\n")),  # other backslashes stay as they are
    ("/foo/-2", q("foo", minus=2)),
    ("/foo/-2+3", q("foo", minus=2, plus=3)),
    ("/foo/@2-1+1", q("foo", nth=2, minus=1, plus=1)),
    ("/a/~9-2../b/+4", q("a", end="b", near=9, minus=2, plus=4)),
]


def test_parse_query_minus_dot_dot_plus():
    assert parse_query("/foo/-5..+6") == q("foo", minus=5, plus=6)  # the form agents guess
    assert format_query(parse_query("/foo/-5..+6")) == "/foo/-5+6"



@pytest.mark.parametrize("text,expected", QUERIES)
def test_parse_query_table(text, expected):
    assert parse_query(text) == expected
    assert parse_query(format_query(expected)) == expected  # the canonical text reads back the same


@pytest.mark.parametrize("bad", ["foo", "/foo", "//", "/foo/@0", "/foo/~0", "/foo/@2~3", "/foo/+", "/a/+1../b/",
                                 "/a/../", "/a/../b", "/foo/x", "/foo/@", "/a\\/",
                                 "/foo/-", "/foo/+1-2", "/foo/-2@1", "/foo/..+2", "/a/../b/-1", "/foo/-1..+"])
def test_parse_query_errors(bad):
    with pytest.raises(ValueError):
        parse_query(bad)


def test_parse_spec_text_forms(tmp_path):
    f = tmp_path / "a.cpp"
    f.write_text("x")
    assert parse_spec(f"{f}:/foo/") == (str(f), None, None, q("foo"))
    assert parse_spec(f"{f}:/Cache::get/@2+1") == (str(f), None, None, q("Cache::get", nth=2, plus=1))
    assert parse_spec(f"{f}:/a/../b/") == (str(f), None, None, q("a", end="b"))
    assert parse_spec(f"{f}:/a\\/b/") == (str(f), None, None, q("a/b"))
    assert parse_spec("a.cpp:/Cache::get/", str(tmp_path))[0] == str(tmp_path / "a.cpp")


def test_parse_spec_numbers_still_work(tmp_path):
    f = tmp_path / "f.cpp"
    f.write_text("x")
    assert parse_spec(f"{f}:7") == (str(f), 7, 7, None)
    assert parse_spec(f"{f}:7-9") == (str(f), 7, 9, None)
    assert parse_spec(f"{f}:12:5") == (str(f), 12, 12, None)  # L:COL
    assert parse_spec(f"{f}:12-14:5") == (str(f), 12, 14, None)


def test_parse_spec_path_with_colon(tmp_path):
    d = tmp_path / "dir:/sub"
    d.mkdir(parents=True)
    f = d / "f.cpp"
    f.write_text("x")
    # ':/' is in the path, but "/sub/f.cpp:5" is not a query: the last ':' splits
    assert parse_spec(f"{f}:5") == (str(f), 5, 5, None)
    assert parse_spec(f"{f}:5:2") == (str(f), 5, 5, None)
    # a query after such a path still works
    assert parse_spec(f"{f}:/foo/") == (str(f), None, None, q("foo"))


def test_parse_spec_bad_text_range_is_reported(tmp_path):
    with pytest.raises(NvtourError) as e:
        parse_spec(f"{tmp_path}/a.cpp:/foo")
    assert e.value.code == EXIT_BAD_FILE and "'/' is missing" in e.value.message


def test_parse_file_range_refuses_a_query(tmp_path):
    with pytest.raises(NvtourError):
        parse_file_range(f"{tmp_path}/a.cpp:/foo/")


def test_parse_range_or_query():
    assert parse_range_or_query("3-5") == (3, 5, None)
    assert parse_range_or_query("/a/../b/") == (None, None, q("a", end="b"))
    with pytest.raises(NvtourError):
        parse_range_or_query("/a")


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


def _git_repo(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q"], check=True)
    (repo / "sub" / "f.txt").write_text("one\ntwo\n")
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "1"], check=True)
    first = subprocess.run(git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (repo / "sub" / "f.txt").unlink()
    (repo / "sub").rmdir()
    subprocess.run(git + ["commit", "-qam", "2"], check=True)
    return repo.resolve(), git, first


def test_blob_at_is_bound_to_the_commit(tmp_path):
    from nvtour import gitutil

    repo, _git, first = _git_repo(tmp_path)
    blob = gitutil.blob_at(str(repo / "sub" / "f.txt"), "HEAD~1")  # the directory no longer exists
    assert (blob.rel, blob.sha, blob.text) == ("sub/f.txt", first, "one\ntwo\n")
    with pytest.raises(NvtourError, match="not a file at HEAD"):
        gitutil.blob_at(str(repo / "sub" / "f.txt"), "HEAD")
    with pytest.raises(NvtourError, match="unknown git ref"):
        gitutil.blob_at(str(repo / "sub" / "f.txt"), "--output=x")  # never read as an option
    with pytest.raises(NvtourError, match="not inside a git repository"):
        gitutil.blob_at(str(tmp_path / "f.txt"), "HEAD")


def test_text_lines():
    assert cli.text_lines("a\nb\n") == ["a", "b"]
    assert cli.text_lines("a\nb") == ["a", "b"]
    assert cli.text_lines("") == []


@pytest.mark.parametrize("value, expected", [
    ("arch#intro", {"sheet": "arch", "step": "intro"}),
    ("arch#3", {"sheet": "arch", "step": "3"}),
    ("arch#0", {"sheet": "arch", "step": "0"}),
    ("a_b-1#step.one-2", {"sheet": "a_b-1", "step": "step.one-2"}),
    ("_x#_y", {"sheet": "_x", "step": "_y"}),
    ("7#8", {"sheet": "7", "step": "8"}),
    ("#intro", {"sheet": "", "step": "intro"}),
    ("#2", {"sheet": "", "step": "2"}),
    ("", ""),
])
def test_parse_sheet_link(value, expected):
    assert cli.parse_sheet_link(value) == expected


@pytest.mark.parametrize("bad", ["intro", "arch", "a b#1", "-a#1", "a#", "#", "a#-1", "a#1x", "a#b c", "a#b#c", "a/b#1", "é#1"])
def test_parse_sheet_link_errors(bad):
    with pytest.raises(NvtourError) as e:
        cli.parse_sheet_link(bad)
    assert e.value.code == EXIT_USAGE and "--sheet" in e.value.message


def test_check_sheet_id():
    assert cli.check_sheet_id("a-b_1") == "a-b_1"
    for bad in ("", "-a", "a b", "a#b", "a.b"):
        with pytest.raises(NvtourError) as e:
            cli.check_sheet_id(bad)
        assert e.value.code == EXIT_USAGE


def test_build_request_sheet():
    parser = cli.build_parser()
    args = parser.parse_args(["step", "a.txt:1", "--sheet", "s#2"])
    # location() needs a file; only the link is checked here
    args.spec = __file__ + ":1"
    _cmd, req = cli.build_request(args, "/w")
    assert req["sheet"] == {"sheet": "s", "step": "2"}
    args = parser.parse_args(["start", "T", "--sheet", "main"])
    assert cli.build_request(args, "/w")[1]["sheet"] == "main"
    args = parser.parse_args(["start", "T"])
    assert cli.build_request(args, "/w")[1]["sheet"] is None
    args = parser.parse_args(["edit", "1", "--sheet", ""])
    assert cli.build_request(args, "/w")[1]["sheet"] == ""


def test_sheet_env(monkeypatch):
    monkeypatch.delenv("SHEET_SOCKET", raising=False)
    monkeypatch.delenv("SHEET_HOME", raising=False)
    assert cli.sheet_env() == {}
    monkeypatch.setenv("SHEET_SOCKET", "tcp://127.0.0.1:1")
    monkeypatch.setenv("SHEET_HOME", "")
    monkeypatch.setenv("PATH_UNRELATED", "x")
    assert cli.sheet_env() == {"SHEET_SOCKET": "tcp://127.0.0.1:1"}

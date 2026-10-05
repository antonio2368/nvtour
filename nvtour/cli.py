"""nvtour command line interface."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from typing import Any, Mapping, NoReturn

from . import __version__, discover, gitutil, ranges
from .client import Client, lua_version
from .errors import (
    EXIT_BAD_FILE,
    EXIT_NO_MATCH,
    EXIT_NO_NVIM,
    EXIT_RPC,
    EXIT_USAGE,
    NvtourError,
)

ROLES = ["fault", "flow", "fix", "context", "info"]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class Parser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors raise NvtourError, so ``--json`` can report them."""

    def error(self, message: str) -> NoReturn:
        raise NvtourError(EXIT_USAGE, f"{self.prog}: {message}\n{self.format_usage().strip()}")


def positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number: {text!r}")
    return value


def non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser (global flags are accepted before or after the subcommand)."""
    sup = argparse.SUPPRESS

    def add_globals(p: argparse.ArgumentParser, default: bool) -> None:
        d = (lambda v: v) if default else (lambda v: sup)
        p.add_argument("--socket", metavar="PATH", default=d(None), help="nvim socket to use")
        p.add_argument("--workspace", metavar="DIR", default=d(None), help="workspace directory")
        p.add_argument("--json", action="store_true", default=d(False), help="print JSON")
        p.add_argument("--timeout", type=positive_float, metavar="SECONDS", default=d(5.0), help="RPC timeout")

    parser = Parser(prog="nvtour", description="Read-only guided code walkthroughs in a running Neovim.")
    parser.add_argument("--version", action="version", version=f"nvtour {__version__}")
    add_globals(parser, True)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def cmd(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        add_globals(p, False)
        return p

    p = cmd("instances", "list nvim instances")
    p.add_argument("--prune", action="store_true", help="unlink stale sockets")

    p = cmd("attach", "select and pin an instance")
    p.add_argument("target", nargs="?", metavar="PID|SOCKET")
    p.add_argument("--clear", action="store_true", help="remove the pin")

    cmd("where", "show cursor, selection and visible range")

    p = cmd("start", "start a new tour")
    p.add_argument("title", nargs="?", default="")

    p = cmd("step", "add a step (only the first step of a tour jumps to it)")
    p.add_argument("spec", metavar="FILE:L1[-L2]")
    p.add_argument("--note", metavar="TEXT", help="note text, or - to read stdin")
    p.add_argument("--label")
    p.add_argument("--role", choices=ROLES, default="info")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--jump", action="store_true", help="jump to this step even if it is not the first")
    g.add_argument("--no-jump", action="store_true", help="do not jump, even for the first step")

    p = cmd("goto", "go to step N")
    p.add_argument("n", type=int)
    cmd("next", "go to the next step")
    cmd("prev", "go to the previous step")
    cmd("first", "go to the first step")
    cmd("last", "go to the last step")

    p = cmd("focus", "fold or dim everything except the given ranges")
    p.add_argument("spec", metavar="FILE:L1-L2")
    p.add_argument("more", nargs="*", metavar="L3-L4")
    p.add_argument("--context", type=non_negative_int, default=2)
    p.add_argument("--dim", action="store_true")

    p = cmd("unfocus", "undo focus")
    p.add_argument("file", nargs="?")

    p = cmd("diff", "read-only diff of FILE against another version")
    p.add_argument("file")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ref", metavar="GITREF")
    g.add_argument("--file", dest="other_file", metavar="PATH")
    g.add_argument("--stdin", action="store_true")
    p.add_argument("--title")

    cmd("diff-close", "close nvtour diff tabs")

    p = cmd("panel", "show or update the side panel")
    p.add_argument("text", nargs="?", metavar="TEXT|-")
    p.add_argument("--file", dest="text_file", metavar="PATH")
    p.add_argument("--toggle", action="store_true")
    p.add_argument("--clear", action="store_true")

    cmd("clear", "remove everything nvtour created")
    cmd("doctor", "check the installation")
    return parser


# ---------------------------------------------------------------------------
# Context and socket resolution
# ---------------------------------------------------------------------------


def resolve_workspace(arg: str | None, cwd: str | None = None) -> str:
    """``--workspace`` or git toplevel of cwd or cwd, always realpath'd."""
    cwd = cwd or os.getcwd()
    if arg:
        return os.path.realpath(arg)
    return gitutil.toplevel(cwd) or os.path.realpath(cwd)


def pick_socket(args: argparse.Namespace, workspace: str, env: Mapping[str, str], use_pin: bool = True, save_pin: bool = True) -> tuple[str, str]:
    """Choose the nvim socket following the precedence rules; returns (socket, source)."""
    if args.socket and not os.path.exists(args.socket):
        raise NvtourError(EXIT_NO_NVIM, f"socket not found: {args.socket}")
    chosen = discover.explicit_socket(args.socket, workspace, env, use_pin)
    if chosen:
        return chosen
    inst = discover.select(discover.inspect_all(workspace, env), workspace)
    if save_pin:
        discover.write_pin(workspace, inst.socket, env, inst.pid)
    return inst.socket, "discovery"


def display_path(path: str, workspace: str) -> str:
    """Path relative to the workspace when inside it."""
    return os.path.relpath(path, workspace) if path.startswith(workspace + os.sep) else path


def fmt_range(l1: int, l2: int) -> str:
    return str(l1) if l1 == l2 else f"{l1}-{l2}"


# ---------------------------------------------------------------------------
# Command implementations: each returns (result dict, text)
# ---------------------------------------------------------------------------


def read_stdin(what: str) -> str:
    """Read all of stdin; refuse to wait on an interactive terminal."""
    if sys.stdin is None or sys.stdin.isatty():
        raise NvtourError(EXIT_USAGE, f"{what} reads stdin, but stdin is a terminal; pipe the text or use a heredoc")
    return sys.stdin.read()


def read_text_arg(value: str | None, what: str) -> str | None:
    """Return stdin content when ``value`` is '-', else the value."""
    return read_stdin(what) if value == "-" else value


def build_request(args: argparse.Namespace, workspace: str) -> tuple[str, dict[str, Any]]:
    """Translate parsed CLI arguments into a (command, Lua args) pair."""
    c = args.command
    base: dict[str, Any] = {"workspace": workspace}
    if c == "start":
        return "start", {**base, "title": args.title or None}
    if c == "step":
        path, l1, l2 = ranges.parse_file_range(args.spec)
        ranges.require_file(path)
        return "step", {**base, "file": path, "l1": l1, "l2": l2, "note": read_text_arg(args.note, "--note -"),
                        "label": args.label, "role": args.role, "jump": args.jump or None, "no_jump": args.no_jump or None}
    if c == "goto":
        return "goto", {"n": args.n}
    if c in ("next", "prev", "first", "last", "clear", "where", "diff-close"):
        return c, {}
    if c == "focus":
        path, l1, l2 = ranges.parse_file_range(args.spec)
        ranges.require_file(path)
        rs = [[l1, l2]] + [list(ranges.parse_range(r)) for r in args.more]
        return "focus", {**base, "file": path, "ranges": rs, "context": args.context, "dim": args.dim or None}
    if c == "unfocus":
        return "unfocus", {"file": ranges.resolve_path(args.file) if args.file else None}
    if c == "diff":
        path = ranges.resolve_path(args.file)
        ranges.require_file(path)
        if args.ref:
            text, title = gitutil.show(path, args.ref), args.ref
        elif args.other_file:
            other = ranges.resolve_path(args.other_file)
            ranges.require_file(other)
            with open(other, encoding="utf-8", errors="replace") as fh:
                text, title = fh.read(), os.path.basename(other)
        else:
            text, title = read_stdin("diff --stdin"), "stdin"
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return "diff", {"file": path, "lines": lines, "title": args.title or title}
    if c == "panel":
        text = args.text
        if args.text_file:
            path = ranges.resolve_path(args.text_file)
            ranges.require_file(path)
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            text = read_text_arg(text, "panel -")
        return "panel", {**base, "text": text, "toggle": args.toggle or None, "clear": args.clear or None}
    raise NvtourError(EXIT_USAGE, f"unknown command {c}")


def format_result(args: argparse.Namespace, res: dict[str, Any], workspace: str) -> str:
    """One-line (or short) human output for a successful command."""
    c = args.command
    if c in ("step", "goto", "next", "prev", "first", "last"):
        if res.get("message"):
            return str(res["message"])
        label = f" {res['label']}" if res.get("label") else ""
        loc = f"{display_path(res['file'], workspace)}:{fmt_range(res['l1'], res['l2'])}"
        return f"step {res['n']}/{res['total']}: {loc} [{res['role']}]{label}"
    if c == "start":
        return f"started: {res.get('title') or 'Walkthrough'}"
    if c == "focus":
        rs = " ".join(fmt_range(a, b) for a, b in res["ranges"])
        later = "; applied when the file is shown" if res.get("deferred") else ""
        return f"focus: {display_path(res['file'], workspace)} {rs} ({res['mode']}{later})"
    if c == "unfocus":
        return f"unfocused {res.get('count', 0)} buffer(s)"
    if c == "diff":
        return f"diff: {res['title']} <-> {res['base']}"
    if c == "diff-close":
        return f"diff closed ({res.get('closed', 0)} tab(s))"
    if c == "panel":
        return f"panel: {res['status']}"
    if c == "clear":
        return "cleared"
    if c == "where":
        return format_where(res, workspace)
    return json.dumps(res)


def format_where(res: dict[str, Any], workspace: str) -> str:
    """Multi-line output for ``where``."""
    f = display_path(res["file"], workspace) if res["file"] else "[No Name]"
    flags = []
    if res.get("modified"):
        flags.append("modified (differs from disk)")
    if res.get("buftype"):
        flags.append(f"buftype={res['buftype']}")
    if res.get("current_window") is False:
        flags.append("not the current window")
    extra = f"  [{', '.join(flags)}]" if flags else ""
    out = [f"{f}:{res['line']}:{res['col']}  mode={res['mode']}  visible={res['top']}-{res['bottom']}  cwd={res['cwd']}{extra}"]
    sel = res.get("selection")
    if sel:
        text = sel.get("text") or []
        when = "live" if sel.get("live") else "previous, may be old"
        cut = ", truncated" if sel.get("truncated") else ""
        out.append(f"selection: {fmt_range(sel['l1'], sel['l2'])} ({when}, {sel.get('kind')}, {len(text)} line(s) shown{cut})")
        out.extend(f"  {line}" for line in text)
    return "\n".join(out)


def emit(args: argparse.Namespace, res: dict[str, Any], text: str, socket: str | None = None) -> None:
    """Print JSON or text."""
    if args.json:
        data = dict(res)
        if socket:
            data["socket"] = socket
        print(json.dumps(data))
    else:
        print(text)


def run_remote(args: argparse.Namespace, workspace: str, env: Mapping[str, str]) -> int:
    """Commands that talk to the selected nvim instance."""
    cmd, payload = build_request(args, workspace)
    sock, _source = pick_socket(args, workspace, env)
    client = Client(sock, args.timeout).connect()
    try:
        client.ensure_lua_loaded()
        res = client.call(cmd, payload)
    finally:
        client.close()
    if not res.get("ok"):
        raise NvtourError(int(res.get("code") or EXIT_RPC), str(res.get("error", "unknown error")))
    emit(args, res, format_result(args, res, workspace), sock)
    if not args.json:
        for w in res.get("warnings") or []:
            print(f"warning: {w}", file=sys.stderr)
    return 0


def run_instances(args: argparse.Namespace, workspace: str, env: Mapping[str, str]) -> int:
    insts = discover.inspect_all(workspace, env)
    pruned = 0
    if args.prune:
        for i in insts:
            if i.state == "stale":
                try:
                    os.unlink(i.socket)
                    pruned += 1
                except OSError:
                    pass
    if args.json:
        print(json.dumps({"workspace": workspace, "instances": [i.to_dict() for i in insts], "pruned": pruned}))
    else:
        text = discover.format_instances(insts)
        if text:
            print(text)
        if args.prune:
            print(f"pruned {pruned} stale socket(s)")
    return 0


def run_attach(args: argparse.Namespace, workspace: str, env: Mapping[str, str]) -> int:
    if args.clear:
        removed = discover.clear_pin(workspace, env)
        emit(args, {"ok": True, "cleared": removed}, "unpinned" if removed else "no pin set")
        return 0
    target = args.target
    if target and not target.isdigit():
        if not os.path.exists(target):
            raise NvtourError(EXIT_NO_NVIM, f"socket not found: {target}")
        inst = discover.inspect_socket(os.path.abspath(target), workspace, args.timeout)
    elif target:
        insts = discover.inspect_all(workspace, env)
        inst = next((i for i in insts if i.pid == int(target) and i.state == "live"), None)
        if inst is None:
            states = ", ".join(f"{i.socket} is {i.state}" for i in insts if i.pid == int(target))
            raise NvtourError(EXIT_NO_MATCH, f"no live nvim with pid {target}" + (f" ({states})" if states else ""))
    else:
        chosen = discover.explicit_socket(args.socket, workspace, env, use_pin=False)
        if chosen and not os.path.exists(chosen[0]):
            raise NvtourError(EXIT_NO_NVIM, f"socket not found: {chosen[0]}")
        if chosen:
            inst = discover.inspect_socket(chosen[0], workspace, args.timeout)
        else:
            inst = discover.select(discover.inspect_all(workspace, env), workspace)
    discover.write_pin(workspace, inst.socket, env, inst.pid)
    kind = inst.kind or "manual"
    text = f"attached: pid {inst.pid}  socket {inst.socket}  cwd {inst.cwd}  ({kind})"
    emit(args, {"ok": True, **inst.to_dict(), "workspace": workspace}, text)
    return 0


def nvim_version() -> tuple[str | None, str]:
    """Return (path, version string) for the nvim binary on PATH."""
    path = shutil.which("nvim")
    if not path:
        return None, ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return path, ""
    first = out.splitlines()[0] if out else ""
    return path, first.replace("NVIM", "").strip().lstrip("v")


def version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.split("-")[0].split("."):
        if p.isdigit():
            parts.append(int(p))
        else:
            break
    return tuple(parts)


def run_doctor(args: argparse.Namespace, workspace: str, env: Mapping[str, str]) -> int:
    """Read-only environment checklist; the exit code is the first failed required check."""
    checks: list[dict[str, Any]] = []
    code = 0

    def add(ok: bool | None, name: str, detail: str, fail_code: int | None = None) -> None:
        nonlocal code
        checks.append({"ok": ok, "name": name, "detail": detail, "required": fail_code is not None})
        if ok is False and fail_code and not code:
            code = fail_code

    def report() -> int:
        if args.json:
            print(json.dumps({"ok": code == 0, "code": code, "checks": checks}))
        else:
            for c in checks:
                mark = {True: "[ok]  ", False: "[FAIL]", None: "[--]  "}[c["ok"]]
                print(f"{mark} {c['name']}: {c['detail']}")
        return code

    path, ver = nvim_version()
    if path:
        add(version_tuple(ver) >= (0, 10), "nvim on PATH", f"{path} {ver} (optional; the running nvim is checked below)")
    else:
        add(None, "nvim on PATH", "not found (optional; nvtour only talks to a running nvim)")
    try:
        import pynvim

        add(True, "pynvim importable", f"pynvim {getattr(pynvim, '__version__', '?')}")
    except ImportError as exc:
        add(False, "pynvim importable", f"{exc}; install with: pip install pynvim", EXIT_RPC)
        return report()
    add(True, "workspace", workspace)
    add(True, "runtime dirs scanned", ", ".join(discover.runtime_dirs(env)) or "(none)")
    insts = discover.inspect_all(workspace, env)
    live = [i for i in insts if i.state == "live"]
    stale = [i for i in insts if i.state == "stale"]
    unresp = [i for i in insts if i.state == "unresponsive"]
    add(bool(live) or None, "live sockets", f"{len(live)} live, {len(stale)} stale, {len(unresp)} unresponsive")
    chosen: str | None = None
    try:
        if args.socket and not os.path.exists(args.socket):
            raise NvtourError(EXIT_NO_NVIM, f"socket not found: {args.socket}")
        expl = discover.explicit_socket(args.socket, workspace, env, use_pin=True)
        if expl:
            chosen = expl[0]
            add(True, "selected instance", f"{expl[0]} (via {expl[1]})")
        else:
            inst = discover.select(insts, workspace)
            chosen = inst.socket
            add(True, "selected instance", f"pid {inst.pid}  {inst.socket}  score {inst.score} ({inst.kind})")
    except NvtourError as exc:
        add(False, "selected instance", exc.message.replace("\n", " | "), exc.code)
    if chosen:
        client = Client(chosen, args.timeout)
        try:
            client.connect()
            server = client.exec_lua("local v = vim.version() return { v.major, v.minor, v.patch }") or []
            sver = ".".join(str(x) for x in server)
            add(tuple(server) >= (0, 10), "nvim version", f"{sver} (need >= 0.10)", EXIT_RPC)
            loaded, expected = client.loaded_version(), lua_version()
            if loaded == expected:
                add(True, "Lua runtime", f"loaded version {loaded}")
            elif loaded is None:
                add(None, "Lua runtime", f"not loaded yet; version {expected} is injected on first use")
            else:
                add(None, "Lua runtime", f"loaded version {loaded}; version {expected} is injected on the next command")
        except NvtourError as exc:
            add(False, "Lua runtime", exc.message, exc.code)
        finally:
            client.close()
    return report()


def run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Dispatch to the right command runner."""
    workspace = resolve_workspace(args.workspace)
    if args.command == "doctor":
        return run_doctor(args, workspace, env)
    try:
        import pynvim  # noqa: F401
    except ImportError:
        raise NvtourError(EXIT_RPC, "pynvim is not installed; install with: pip install pynvim") from None
    if args.command == "instances":
        return run_instances(args, workspace, env)
    if args.command == "attach":
        return run_attach(args, workspace, env)
    return run_remote(args, workspace, env)


def fail(code: int, message: str, as_json: bool) -> int:
    """Report an error: JSON on stdout with ``--json``, else the message on stderr."""
    if as_json:
        print(json.dumps({"ok": False, "code": code, "error": message}))
    else:
        print(message, file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        as_json = args.json
        return run(args, os.environ)
    except NvtourError as exc:
        return fail(exc.code, exc.message, as_json)
    except OSError as exc:
        return fail(EXIT_BAD_FILE, str(exc), as_json)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - one line instead of a traceback
        return fail(EXIT_RPC, f"internal error: {type(exc).__name__}: {exc}", as_json)


if __name__ == "__main__":
    sys.exit(main())

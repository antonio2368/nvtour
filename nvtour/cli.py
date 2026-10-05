"""nvtour command line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Mapping

from . import LUA_VERSION, __version__, discover, gitutil, ranges
from .client import Client
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


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser (global flags are accepted before or after the subcommand)."""
    sup = argparse.SUPPRESS

    def add_globals(p: argparse.ArgumentParser, default: bool) -> None:
        d = (lambda v: v) if default else (lambda v: sup)
        p.add_argument("--socket", metavar="PATH", default=d(None), help="nvim socket to use")
        p.add_argument("--workspace", metavar="DIR", default=d(None), help="workspace directory")
        p.add_argument("--json", action="store_true", default=d(False), help="print JSON")
        p.add_argument("--timeout", type=float, metavar="SECONDS", default=d(5.0), help="RPC timeout")

    parser = argparse.ArgumentParser(prog="nvtour", description="Read-only guided code walkthroughs in a running Neovim.")
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

    p = cmd("step", "add a step and jump to it")
    p.add_argument("spec", metavar="FILE:L1[-L2]")
    p.add_argument("--note", metavar="TEXT", help="note text, or - to read stdin")
    p.add_argument("--label")
    p.add_argument("--role", choices=ROLES, default="info")
    p.add_argument("--no-jump", action="store_true")

    p = cmd("goto", "go to step N")
    p.add_argument("n", type=int)
    cmd("next", "go to the next step")
    cmd("prev", "go to the previous step")

    p = cmd("focus", "fold or dim everything except the given ranges")
    p.add_argument("spec", metavar="FILE:L1-L2")
    p.add_argument("more", nargs="*", metavar="L3-L4")
    p.add_argument("--context", type=int, default=2)
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
    chosen = discover.explicit_socket(args.socket, workspace, env, use_pin)
    if chosen:
        return chosen
    inst = discover.select(discover.inspect_all(workspace, env), workspace)
    if save_pin:
        discover.write_pin(workspace, inst.socket, env)
    return inst.socket, "discovery"


def display_path(path: str, workspace: str) -> str:
    """Path relative to the workspace when inside it."""
    return os.path.relpath(path, workspace) if path.startswith(workspace + os.sep) else path


def fmt_range(l1: int, l2: int) -> str:
    return str(l1) if l1 == l2 else f"{l1}-{l2}"


# ---------------------------------------------------------------------------
# Command implementations: each returns (result dict, text)
# ---------------------------------------------------------------------------


def read_text_arg(value: str | None) -> str | None:
    """Return stdin content when ``value`` is '-', else the value."""
    return sys.stdin.read() if value == "-" else value


def build_request(args: argparse.Namespace, workspace: str) -> tuple[str, dict[str, Any]]:
    """Translate parsed CLI arguments into a (command, Lua args) pair."""
    c = args.command
    base: dict[str, Any] = {"workspace": workspace}
    if c == "start":
        return "start", {**base, "title": args.title or None}
    if c == "step":
        path, l1, l2 = ranges.parse_file_range(args.spec)
        ranges.require_file(path)
        return "step", {**base, "file": path, "l1": l1, "l2": l2, "note": read_text_arg(args.note), "label": args.label,
                        "role": args.role, "no_jump": args.no_jump or None}
    if c == "goto":
        return "goto", {"n": args.n}
    if c in ("next", "prev", "clear", "where", "diff-close"):
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
            text, title = sys.stdin.read(), "stdin"
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return "diff", {"file": path, "lines": lines, "title": args.title or title}
    if c == "panel":
        text = args.text
        if args.text_file:
            with open(args.text_file, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            text = read_text_arg(text)
        return "panel", {**base, "text": text, "toggle": args.toggle or None, "clear": args.clear or None}
    raise NvtourError(EXIT_USAGE, f"unknown command {c}")


def format_result(args: argparse.Namespace, res: dict[str, Any], workspace: str) -> str:
    """One-line (or short) human output for a successful command."""
    c = args.command
    if c in ("step", "goto", "next", "prev"):
        if res.get("message"):
            return str(res["message"])
        label = f" {res['label']}" if res.get("label") else ""
        loc = f"{display_path(res['file'], workspace)}:{fmt_range(res['l1'], res['l2'])}"
        return f"step {res['n']}/{res['total']}: {loc} [{res['role']}]{label}"
    if c == "start":
        return f"started: {res.get('title') or 'Walkthrough'}"
    if c == "focus":
        rs = " ".join(fmt_range(a, b) for a, b in res["ranges"])
        return f"focus: {display_path(res['file'], workspace)} {rs} ({res['mode']})"
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
    out = [f"{f}:{res['line']}:{res['col']}  mode={res['mode']}  visible={res['top']}-{res['bottom']}  cwd={res['cwd']}"]
    sel = res.get("selection")
    if sel:
        text = sel.get("text") or []
        out.append(f"selection: {fmt_range(sel['l1'], sel['l2'])} ({len(text)} line(s) shown)")
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
    insts = discover.inspect_all(workspace, env)
    inst: discover.Instance | None = None
    if args.target:
        if args.target.isdigit():
            inst = next((i for i in insts if i.pid == int(args.target) and i.state == "live"), None)
            if inst is None:
                raise NvtourError(EXIT_NO_MATCH, f"no live nvim with pid {args.target}")
        else:
            sock = os.path.realpath(args.target) if os.path.exists(args.target) else args.target
            inst = next((i for i in insts if i.socket == args.target or i.socket == sock), None)
            if inst is None:
                info = discover.probe(args.target, args.timeout)
                if info is None:
                    raise NvtourError(EXIT_RPC, f"cannot connect to {args.target}")
                score, kind = discover.score_instance(str(info.get("cwd", "")), workspace, list(info.get("buffers", [])))
                inst = discover.Instance(args.target, int(info.get("pid", 0)), "live", str(info.get("cwd", "")),
                                         list(info.get("buffers", [])), int(info.get("tabpages", 0)), "", score, kind)
    else:
        chosen = discover.explicit_socket(args.socket, workspace, env, use_pin=False)
        if chosen:
            info = discover.probe(chosen[0], args.timeout)
            if info is None:
                raise NvtourError(EXIT_RPC, f"cannot connect to {chosen[0]}")
            score, kind = discover.score_instance(str(info.get("cwd", "")), workspace, list(info.get("buffers", [])))
            inst = discover.Instance(chosen[0], int(info.get("pid", 0)), "live", str(info.get("cwd", "")),
                                     list(info.get("buffers", [])), int(info.get("tabpages", 0)), "", score, kind)
        else:
            inst = discover.select(insts, workspace)
    discover.write_pin(workspace, inst.socket, env)
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
    """Read-only environment checklist."""
    checks: list[dict[str, Any]] = []

    def add(ok: bool | None, name: str, detail: str) -> None:
        checks.append({"ok": ok, "name": name, "detail": detail})

    path, ver = nvim_version()
    add(bool(path and version_tuple(ver) >= (0, 10)), "nvim on PATH", f"{path or 'not found'} {ver} (need >= 0.10)")
    try:
        import pynvim

        add(True, "pynvim importable", f"pynvim {getattr(pynvim, '__version__', '?')}")
    except ImportError as exc:
        add(False, "pynvim importable", str(exc))
    add(True, "workspace", workspace)
    add(True, "runtime dirs scanned", ", ".join(discover.runtime_dirs(env)) or "(none)")
    insts = discover.inspect_all(workspace, env)
    live = [i for i in insts if i.state == "live"]
    stale = [i for i in insts if i.state == "stale"]
    unresp = [i for i in insts if i.state == "unresponsive"]
    add(bool(live), "live sockets", f"{len(live)} live, {len(stale)} stale, {len(unresp)} unresponsive")
    code = 0
    chosen: str | None = None
    try:
        expl = discover.explicit_socket(args.socket, workspace, env, use_pin=True)
        if expl:
            chosen = expl[0]
            add(True, "selected instance", f"{expl[0]} (via {expl[1]})")
        else:
            inst = discover.select(insts, workspace)
            chosen = inst.socket
            add(True, "selected instance", f"pid {inst.pid}  {inst.socket}  score {inst.score} ({inst.kind})")
    except NvtourError as exc:
        code = exc.code
        add(False, "selected instance", exc.message.replace("\n", " | "))
    if chosen:
        client = Client(chosen, args.timeout)
        try:
            client.connect()
            loaded = client.loaded_version()
            if loaded is None:
                add(None, "Lua runtime", f"not loaded yet; version {LUA_VERSION} is injected on first use")
            else:
                add(loaded == LUA_VERSION, "Lua runtime", f"loaded version {loaded} (expected {LUA_VERSION})")
        except NvtourError as exc:
            code = code or exc.code
            add(False, "Lua runtime", exc.message)
        finally:
            client.close()
    if args.json:
        print(json.dumps({"ok": code == 0, "checks": checks}))
    else:
        for c in checks:
            mark = {True: "[ok]  ", False: "[FAIL]", None: "[--]  "}[c["ok"]]
            print(f"{mark} {c['name']}: {c['detail']}")
    return code


def run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Dispatch to the right command runner."""
    workspace = resolve_workspace(args.workspace)
    if args.command == "instances":
        return run_instances(args, workspace, env)
    if args.command == "attach":
        return run_attach(args, workspace, env)
    if args.command == "doctor":
        return run_doctor(args, workspace, env)
    return run_remote(args, workspace, env)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return run(args, os.environ)
    except NvtourError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

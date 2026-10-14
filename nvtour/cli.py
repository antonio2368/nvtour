"""nvtour command line interface."""

from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from typing import Any, Mapping, NoReturn

from . import __version__, diagram, discover, gitutil, ranges
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

SHEET_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*\Z")  # the id rule of `sheet`
SHEET_STEP_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")


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


def positive_int(text: str) -> int:
    value = non_negative_int(text)
    if value == 0:
        raise argparse.ArgumentTypeError(f"must be positive: {text!r}")
    return value


def check_sheet_id(text: str) -> str:
    """A sheet id, as `sheet` writes it: letters, digits, ``_`` and ``-``, not starting with ``-``."""
    if not SHEET_ID_RE.match(text):
        raise NvtourError(EXIT_USAGE, f"--sheet: bad sheet id {text!r}; use letters, digits, _ and - (not - first)")
    return text


def parse_sheet_link(value: str) -> dict[str, str] | str:
    """``--sheet`` of step and edit: ``SHEET#STEP``, ``#STEP`` (the sheet of the tour) or ``''`` (no link).

    STEP is a number (0 is the overview) or a step id. Returns ``''`` or ``{"sheet": ID or "", "step": STEP}``;
    the Lua side fills in the sheet of the tour and fails (exit 6) when there is none."""
    if value == "":
        return ""
    sheet, sep, step = value.partition("#")
    if not sep:
        raise NvtourError(EXIT_USAGE, f"--sheet {value!r}: expected SHEET#STEP (or #STEP for the sheet of the tour)")
    if sheet:
        check_sheet_id(sheet)
    if not step:
        raise NvtourError(EXIT_USAGE, f"--sheet {value!r}: the step after # is missing (a number or a step id)")
    if not (step.isascii() and step.isdigit()) and not SHEET_STEP_ID_RE.match(step):
        raise NvtourError(EXIT_USAGE, f"--sheet {value!r}: bad step {step!r}; use a number (0: overview) or a step id")
    return {"sheet": sheet, "step": step}


SHEET_ENV_VARS = ("SHEET_SOCKET", "SHEET_HOME")


def sheet_env() -> dict[str, str]:
    """The variables of the viewer that the agent has set; they go with a sheet link to the runtime,
    which passes them to ``sheet goto`` (a forward may be set after nvim was started)."""
    return {k: os.environ[k] for k in SHEET_ENV_VARS if os.environ.get(k)}


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

    p = cmd("marks", "show the places that the user marked (<leader>wm in nvim)")
    p.add_argument("--wait", type=positive_float, metavar="SECONDS",
                   help="wait until there is a mark; exit 6 after SECONDS without one")
    p.add_argument("--clear", action="store_true", help="remove the marks that are printed")

    p = cmd("start", "start a new tour")
    p.add_argument("title", nargs="?", default="")
    p.add_argument("--sheet", metavar="SHEET", help="the sheet this tour goes with; '#STEP' links to it")

    p = cmd("step", "add a step (only the first step of a tour jumps to it)")
    p.add_argument("spec", metavar="FILE:L1[-L2]|FILE:/TEXT/", help="the location, by line numbers or by text; "
                   "L1[-L2] or /TEXT/ with --diagram")
    p.add_argument("--ref", metavar="GITREF", help="show FILE as it is at GITREF (read-only), not the working tree")
    p.add_argument("--diagram", metavar="NAME", help="put the step on lines L1[-L2] of diagram NAME")
    p.add_argument("--note", metavar="TEXT", help="note text, or - to read stdin")
    p.add_argument("--label")
    p.add_argument("--role", choices=ROLES, default="info")
    p.add_argument("--expect", metavar="TEXT", action="append",
                   help="fail (exit 6) unless TEXT occurs in the range; TEXT is underlined in the current step "
                        "(repeat it for more texts)")
    p.add_argument("--via", metavar="TEXT", help="why the tour goes to this step from the step before it (or from --from N)")
    p.add_argument("--suggest", metavar="TEXT", help="code that would replace the range, shown below it (read-only), "
                   "or - to read stdin")
    p.add_argument("--from", dest="from_", type=positive_int, metavar="N", help="the link (--via) comes from step N, not from the step before")
    p.add_argument("--at", type=int, metavar="N", help="insert as step N instead of appending")
    p.add_argument("--sheet", metavar="SHEET#STEP", help="show STEP of the sheet SHEET (number or step id) when this "
                   "step becomes current; #STEP uses the sheet of 'start --sheet'")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--jump", action="store_true", help="jump to this step even if it is not the first")
    g.add_argument("--no-jump", action="store_true", help="do not jump, even for the first step")

    p = cmd("edit", "change step N (location, note, label or role)")
    p.add_argument("n", type=int)
    p.add_argument("spec", nargs="?", metavar="FILE:L1[-L2]|FILE:/TEXT/",
                   help="new location (on the working tree unless --ref; L1[-L2] or /TEXT/ with --diagram)")
    p.add_argument("--ref", metavar="GITREF", help="the new location is FILE at GITREF (needs FILE:L1[-L2])")
    p.add_argument("--diagram", metavar="NAME", help="the new location is lines L1[-L2] of diagram NAME")
    p.add_argument("--note", metavar="TEXT", help="new note, - to read stdin, '' to remove")
    p.add_argument("--label", help="new label, '' to remove")
    p.add_argument("--role", choices=ROLES)
    p.add_argument("--expect", metavar="TEXT", action="append",
                   help="fail (exit 6) unless TEXT occurs in the range; TEXT is underlined in the current step "
                        "(repeat it for more texts; the list replaces the old one; '' removes it)")
    p.add_argument("--via", metavar="TEXT", help="new link text, '' to remove")
    p.add_argument("--suggest", metavar="TEXT", help="new suggested code, - to read stdin, '' to remove")
    p.add_argument("--from", dest="from_", type=non_negative_int, metavar="N", help="the link comes from step N (0: the step before it)")
    p.add_argument("--sheet", metavar="SHEET#STEP", help="new sheet link, '' to remove")
    p.add_argument("--jump", action="store_true", help="jump to the step")

    p = cmd("remove", "remove step N")
    p.add_argument("n", type=int)

    p = cmd("apply", "make a whole tour from a TOML or JSON file (see DESIGN.md, Tour file format)")
    p.add_argument("file", nargs="?", default="-", metavar="FILE|-", help="the tour file (default: stdin)")
    p.add_argument("--append", action="store_true", help="add the steps to the current tour, not a new tour "
                   "(the file has no title or sheet then)")
    p.add_argument("--check", action="store_true", help="check everything and print the resolved steps, "
                   "but change nothing")
    p.add_argument("--no-jump", action="store_true", help="do not jump to the first new step")

    p = cmd("chapter", "start a chapter (a heading) at a step, remove one, or list them")
    p.add_argument("name", nargs="?", help="the name of the chapter")
    p.add_argument("--at", type=int, metavar="N", help="start the chapter at step N (default: the next step added; "
                   "steps + 1 is the same)")
    p.add_argument("--remove", type=int, metavar="N", help="remove the chapter that starts at step N")
    p.add_argument("--list", action="store_true", help="list the chapters in tour order")

    cmd("status", "show the tour, focus, panel and keys")

    p = cmd("goto", "go to step N")
    p.add_argument("n", type=int)
    cmd("next", "go to the next step")
    cmd("prev", "go to the previous step")
    cmd("first", "go to the first step")
    cmd("last", "go to the last step")

    p = cmd("focus", "fold or dim everything except the given ranges (or the ranges of the steps)")
    p.add_argument("spec", nargs="?", metavar="FILE:L1-L2|FILE:/TEXT/|FILE",
                   help="the first range; with --steps: only this file (no range)")
    p.add_argument("more", nargs="*", metavar="L3-L4|/TEXT/")
    p.add_argument("--context", type=non_negative_int, default=2)
    p.add_argument("--dim", action="store_true")
    p.add_argument("--steps", action="store_true",
                   help="focus every toured file (or only FILE) on the ranges of its steps; the focus follows "
                        "the steps when they change (unfocus ends it)")

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

    p = cmd("diagram", "render a diagram into a read-only buffer that steps can point to")
    p.add_argument("name")
    p.add_argument("source", nargs="?", default="-", metavar="FILE|-", help="the diagram source (default: stdin)")
    p.add_argument("--format", choices=diagram.FORMATS,
                   help="source format (default: from the file extension, else mermaid)")
    p.add_argument("--link", metavar="TEXT=FILE:L1[-L2]|TEXT=FILE:/TEXT/", action="append",
                   help="<CR> on TEXT in the diagram opens the range (repeat it for more links)")
    p.add_argument("--ascii", action="store_true", help="plain ASCII lines instead of Unicode box drawing")
    p.add_argument("--header", type=int, metavar="N",
                   help="line N stays in view in the winbar when it scrolls out (0: none; "
                        "default: the participant names of a Mermaid sequence diagram)")
    p.add_argument("--split", choices=["above", "below", "left", "right", "full"],
                   help="side of the code window for a new diagram window, or full: a tab page of "
                        "its own (default: above; a diagram window on another side is moved)")
    p.add_argument("--no-show", action="store_true", help="do not open the diagram window")

    p = cmd("panel", "show or update the side panel")
    p.add_argument("text", nargs="?", metavar="TEXT|-")
    p.add_argument("--file", dest="text_file", metavar="PATH")
    p.add_argument("--toggle", action="store_true")
    p.add_argument("--clear", action="store_true")

    p = cmd("clear", "remove everything nvtour created")
    p.add_argument("--keep-buffers", action="store_true", help="keep toured files in the buffer list")
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
    if inst.kind == "only instance":
        print(f"nvtour: using the only running nvim (pid {inst.pid}, cwd {inst.cwd}); "
              f"it does not match {workspace}. Undo with: nvtour attach --clear", file=sys.stderr)
    if save_pin:
        discover.write_pin(workspace, inst.socket, env, inst.pid)
    return inst.socket, "discovery"


def display_path(path: str, workspace: str) -> str:
    """Path relative to the workspace when inside it."""
    return os.path.relpath(path, workspace) if path.startswith(workspace + os.sep) else path


def fmt_range(l1: int, l2: int) -> str:
    return str(l1) if l1 == l2 else f"{l1}-{l2}"


def fmt_loc(s: Mapping[str, Any], workspace: str) -> str:
    """``path:L1-L2``, with `` @REF`` for a step at a git ref; ``diagram NAME:L1-L2`` on a diagram."""
    if s.get("diagram"):
        return f"diagram {s['diagram']}:{fmt_range(s['l1'], s['l2'])}"
    loc = f"{display_path(s['file'], workspace)}:{fmt_range(s['l1'], s['l2'])}"
    return f"{loc} @{s['ref']}" if s.get("ref") else loc


# ---------------------------------------------------------------------------
# Command implementations: each returns (result dict, text)
# ---------------------------------------------------------------------------


def read_stdin(what: str) -> str:
    """Read all of stdin; refuse to wait on an interactive terminal."""
    if sys.stdin is None or sys.stdin.isatty():
        raise NvtourError(EXIT_USAGE, f"{what} reads stdin, but stdin is a terminal; pipe the text or use a heredoc")
    return sys.stdin.read()


def text_lines(text: str) -> list[str]:
    """Lines of ``text``; a final newline does not add an empty line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def lua_query(query: dict[str, Any]) -> dict[str, Any]:
    """A text range for Lua; absent parts are left out (nested nils do not survive msgpack)."""
    return {k: v for k, v in query.items() if v is not None}


def query_args(query: dict[str, Any]) -> dict[str, Any]:
    """Step location args of a text range, in place of ``l1`` and ``l2``."""
    return {"query": lua_query(query), "resolved_from": ranges.format_query(query)}


def focus_range(l1: int | None, l2: int | None, query: dict[str, Any] | None) -> list[int] | dict[str, Any]:
    """One focus range: ``[l1, l2]``, or ``{query}`` for a text range."""
    return {"query": lua_query(query)} if query else [l1, l2]  # type: ignore[list-item]


def location(spec: str, ref: str | None, diagram_name: str | None = None,
             blobs: dict[tuple[str, str], tuple[gitutil.Blob, list[str]]] | None = None) -> dict[str, Any]:
    """Lua args for a step location: ``FILE:L1[-L2]`` on the working tree or at ``ref``, or
    ``L1[-L2]`` of a diagram. A text range (``FILE:/TEXT/``) gives ``query`` (the Lua runtime finds
    the lines in the buffer) and ``resolved_from`` instead of ``l1`` and ``l2``. ``blobs`` (``apply``)
    keeps the blob of each ``(file, ref)``, so that it is read from git once."""
    if diagram_name is not None:
        if ref is not None:
            raise NvtourError(EXIT_USAGE, "--ref and --diagram cannot be used together")
        l1, l2, query = ranges.parse_range_or_query(spec)
        name = diagram.check_name(diagram_name)
        loc: dict[str, Any] = {"diagram": name, "file": diagram.buffer_name(name)}
        loc.update(query_args(query) if query else {"l1": l1, "l2": l2})
        return loc
    path, l1, l2, query = ranges.parse_spec(spec)
    loc = {"file": path}
    loc.update(query_args(query) if query else {"l1": l1, "l2": l2})
    if ref is None:
        ranges.require_file(path)
    else:
        if blobs is not None and (path, ref) in blobs:
            blob, lines = blobs[(path, ref)]
        else:
            blob = gitutil.blob_at(path, ref)
            lines = text_lines(blob.text)
            if blobs is not None:
                blobs[(path, ref)] = (blob, lines)
        loc.update(ref=ref, sha=blob.sha, rel=blob.rel, lines=lines)
    return loc


def read_text_arg(value: str | None, what: str) -> str | None:
    """Return stdin content when ``value`` is '-', else the value."""
    return read_stdin(what) if value == "-" else value


def diagram_args(name: str, source: str, fmt: str, ascii_only: bool, link_specs: list[str], header: int | None,
                 split: str | None, show: bool, header_opt: str = "--header") -> dict[str, Any]:
    """Lua args of a diagram (the ``diagram`` command and a ``[[diagram]]`` of ``apply``): the source is
    rendered here, the links are checked against the rendered lines."""
    lines = diagram.render(source, fmt, ascii_only)
    links = [diagram.parse_link(spec, lines) for spec in link_specs]
    if header is None:
        header = diagram.header_row(source, fmt, lines)
    if not 0 <= header <= len(lines):
        raise NvtourError(EXIT_BAD_FILE, f"{header_opt} {header} is not a line of the diagram (1-{len(lines)}, or 0)")
    return {"name": name, "lines": lines, "links": links, "header": header, "split": split, "show": show}


# ---------------------------------------------------------------------------
# Tour files (`apply`)
# ---------------------------------------------------------------------------


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("must be a string")
    return value


def _name(value: Any) -> str:
    if not _string(value).strip():
        raise ValueError("must not be empty")
    return value


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


def _integer(minimum: int) -> Any:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an integer")
        if value < minimum:
            raise ValueError(f"must be {minimum} or more")
        return value
    return check


def _choice(options: list[str]) -> Any:
    def check(value: Any) -> str:
        if value not in options:
            raise ValueError(f"must be one of: {', '.join(options)}")
        return value  # type: ignore[no-any-return]
    return check


def _strings(message: str = "must be a list of strings") -> Any:
    def check(value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(message)
        return value
    return check


def _one_or_more(value: Any) -> list[str]:
    """One string or a list of strings, as a list."""
    return [value] if isinstance(value, str) else _strings("must be a string or a list of strings")(value)


def _lines(value: Any) -> str:
    """Text as one string, or as a list of lines."""
    return value if isinstance(value, str) else "\n".join(_strings("must be a string or a list of lines")(value))


# What a tour file can have: key -> check (it returns the value to use). `required` keys must be there.
TOUR_KEYS: dict[str, Any] = {"title": _string, "sheet": _string, "panel": _string}
FOCUS_KEYS: dict[str, Any] = {"file": _name, "ranges": _strings(), "dim": _boolean, "context": _integer(0)}
DIAGRAM_KEYS: dict[str, Any] = {
    "name": _name, "format": _choice(diagram.FORMATS), "source": _string, "file": _name, "links": _strings(),
    "split": _choice(["above", "below", "left", "right", "full"]), "ascii": _boolean, "header": _integer(0),
    "show": _boolean,
}
STEP_KEYS: dict[str, Any] = {
    "at": _name, "ref": _name, "diagram": _name, "role": _choice(ROLES), "label": _string, "note": _string,
    "expect": _one_or_more, "via": _string, "from": _integer(1), "suggest": _lines, "sheet": _string,
    "chapter": _name,
}
TOUR_TABLES = {"focus": (FOCUS_KEYS, ["file", "ranges"]), "diagram": (DIAGRAM_KEYS, ["name"]),
               "step": (STEP_KEYS, ["at"])}


def tour_error(where: str, message: str) -> NvtourError:
    return NvtourError(EXIT_USAGE, f"{where}: {message}")


def unknown_key(key: Any, keys: Any, where: str) -> NvtourError:
    """The error for a key that a table does not have, with the closest key when there is one."""
    close = difflib.get_close_matches(str(key), list(keys), n=1)
    if close:
        hint = f' (did you mean "{close[0]}"?)'
    elif key == "chapter":
        hint = ' (a chapter starts at a step: give the step a "chapter")'
    else:
        hint = f" (the keys are: {', '.join(keys)})"
    return tour_error(where, f'unknown key "{key}"{hint}')


def check_entry(entry: Any, keys: Mapping[str, Any], required: list[str], where: str) -> dict[str, Any]:
    """The values of one table of a tour file, checked: only ``keys``, each of the right type."""
    if not isinstance(entry, dict):
        raise tour_error(where, "must be a table")
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key not in keys:
            raise unknown_key(key, keys, where)
        if value is None:  # JSON null is the same as no key
            continue
        try:
            out[key] = keys[key](value)
        except ValueError as exc:
            raise tour_error(where, f'"{key}" {exc}') from None
    for key in required:
        if key not in out:
            raise tour_error(where, f'missing the key "{key}"')
    return out


def parse_tour(text: str) -> dict[str, Any]:
    """The tour file ``text`` (TOML, or JSON when it starts with ``{``), checked: unknown keys and wrong
    types are exit 2, naming the entry (``step[3]: unknown key "lable" (did you mean "label"?)``; the
    entries are numbered from 1 in the order of the file). Returns ``title``, ``sheet`` and ``panel``
    when given, and the lists ``focus``, ``diagram`` and ``step`` of the checked tables (a key that is
    not in the file is not in the table; ``expect`` is a list, ``suggest`` a string). Touches no
    file and no other state."""
    is_json = text.lstrip().startswith("{")
    try:
        data = json.loads(text) if is_json else tomllib.loads(text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NvtourError(EXIT_USAGE, f"tour file: bad {'JSON' if is_json else 'TOML'}: {exc}") from None
    if not isinstance(data, dict):
        raise NvtourError(EXIT_USAGE, "tour file: the top level must be a table (an object)")
    for key in data:
        if key not in TOUR_KEYS and key not in TOUR_TABLES:
            raise unknown_key(key, [*TOUR_KEYS, *TOUR_TABLES], "tour")
    tour = check_entry({k: v for k, v in data.items() if k in TOUR_KEYS}, TOUR_KEYS, [], "tour")
    for name, (keys, required) in TOUR_TABLES.items():
        entries = data.get(name)
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise tour_error("tour", f'"{name}" must be an array of tables ([[{name}]])')
        tour[name] = [check_entry(e, keys, required, f"{name}[{i}]") for i, e in enumerate(entries, 1)]
    for i, d in enumerate(tour["diagram"], 1):
        if ("source" in d) == ("file" in d):
            raise tour_error(f"diagram[{i}]", 'give one of "source" and "file"')
    return tour


def scoped(where: str, fn: Any) -> Any:
    """Run ``fn``; an error of nvtour from it gets ``where`` (``step[3]``) in front of its message."""
    try:
        return fn()
    except NvtourError as exc:
        raise NvtourError(exc.code, f"{where}: {exc.message}") from None


def read_tour(path: str) -> str:
    """The text of the tour file ``path``, or of stdin for ``-``."""
    if path == "-":
        return read_stdin("apply -")
    full = ranges.resolve_path(path)
    ranges.require_file(full)
    with open(full, encoding="utf-8") as fh:
        return fh.read()


def tour_diagram(d: dict[str, Any]) -> dict[str, Any]:
    """Lua args of one ``[[diagram]]``: the same as ``nvtour diagram NAME`` with the options of the table."""
    name = diagram.check_name(d["name"])
    if "file" in d:
        path = ranges.resolve_path(d["file"])
        ranges.require_file(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            source, fmt = fh.read(), d.get("format") or diagram.guess_format(path)
    else:
        source, fmt = d["source"], d.get("format") or "mermaid"
    req = diagram_args(name, source, fmt, d.get("ascii", False), d.get("links", []), d.get("header"),
                       d.get("split"), d.get("show", True), header_opt="header")
    return {k: v for k, v in req.items() if v is not None}  # nested nils do not survive msgpack


def tour_focus(f: dict[str, Any]) -> dict[str, Any]:
    """Lua args of one ``[[focus]]``: the same as ``nvtour focus`` with the options of the table."""
    path = ranges.resolve_path(f["file"])
    ranges.require_file(path)
    req: dict[str, Any] = {"file": path, "ranges": [focus_range(*ranges.parse_range_or_query(r)) for r in f["ranges"]],
                           "context": f.get("context", 2)}
    if f.get("dim"):
        req["dim"] = True
    return req


def tour_step(st: dict[str, Any], blobs: dict[tuple[str, str], tuple[gitutil.Blob, list[str]]],
              sent: set[tuple[str, str]]) -> dict[str, Any]:
    """Lua args of one ``[[step]]``. The lines of a git blob go with the first step on it only."""
    req = location(st["at"], st.get("ref"), st.get("diagram"), blobs)
    if "sha" in req:
        key = (req["sha"], req["file"])
        if key in sent:
            del req["lines"]
        sent.add(key)
    for key in ("role", "label", "note", "via", "suggest", "from"):
        if key in st:
            req[key] = st[key]
    if st.get("expect"):
        req["expect"] = st["expect"]
    link = parse_sheet_link(st["sheet"]) if "sheet" in st else ""
    if link:
        req["sheet"] = link
    if "chapter" in st:
        req["chapter"] = st["chapter"]
    return req


def apply_request(args: argparse.Namespace, base: dict[str, Any]) -> dict[str, Any]:
    """Lua args of ``apply``: the tour file, checked and with everything resolved that Python does
    (git blobs once per file and ref, diagram renderers, sheet links)."""
    tour = parse_tour(read_tour(args.file))
    if args.append:
        for key in ("title", "sheet"):
            if key in tour:
                raise NvtourError(EXIT_USAGE, f'apply --append: the file has "{key}"; a tour that is added to has '
                                  "its own title and sheet")
    sheet = None
    if "sheet" in tour:
        sheet = scoped("sheet", lambda: check_sheet_id(tour["sheet"]))
    names: dict[str, int] = {}
    for i, d in enumerate(tour["diagram"], 1):
        if d["name"] in names:
            raise tour_error(f"diagram[{i}]", f'the name "{d["name"]}" is used by diagram[{names[d["name"]]}] already')
        names[d["name"]] = i
    blobs: dict[tuple[str, str], tuple[gitutil.Blob, list[str]]] = {}
    sent: set[tuple[str, str]] = set()
    diagrams = [scoped(f"diagram[{i}]", lambda d=d: tour_diagram(d)) for i, d in enumerate(tour["diagram"], 1)]
    focus = [scoped(f"focus[{i}]", lambda f=f: tour_focus(f)) for i, f in enumerate(tour["focus"], 1)]
    steps = [scoped(f"step[{i}]", lambda st=st: tour_step(st, blobs, sent)) for i, st in enumerate(tour["step"], 1)]
    return {**base, "title": tour.get("title"), "sheet": sheet, "sheet_env": sheet_env() or None,
            "panel": tour.get("panel"), "focus": focus, "diagrams": diagrams, "steps": steps,
            "append": args.append or None, "check": args.check or None, "no_jump": args.no_jump or None}


def build_request(args: argparse.Namespace, workspace: str) -> tuple[str, dict[str, Any]]:
    """Translate parsed CLI arguments into a (command, Lua args) pair."""
    c = args.command
    base: dict[str, Any] = {"workspace": workspace}
    if c in ("step", "edit") and args.note == "-" and args.suggest == "-":
        raise NvtourError(EXIT_USAGE, f"{c}: only one of --note and --suggest can read stdin (-)")
    if c == "start":
        sheet = check_sheet_id(args.sheet) if args.sheet is not None else None
        return "start", {**base, "title": args.title or None, "sheet": sheet,
                         "sheet_env": sheet_env() if sheet else None}
    if c == "step":
        return "step", {**base, **location(args.spec, args.ref, args.diagram), "note": read_text_arg(args.note, "--note -"),
                        "label": args.label, "role": args.role, "expect": args.expect, "via": args.via,
                        "suggest": read_text_arg(args.suggest, "--suggest -"), "from": args.from_, "at": args.at, "jump": args.jump or None, "no_jump": args.no_jump or None,
                        "sheet": parse_sheet_link(args.sheet) if args.sheet is not None else None,
                        "sheet_env": sheet_env() if args.sheet else None}
    if c == "edit":
        req: dict[str, Any] = {**base, "n": args.n, "note": read_text_arg(args.note, "--note -"), "label": args.label,
                               "role": args.role, "expect": args.expect, "via": args.via, "from": args.from_,
                               "suggest": read_text_arg(args.suggest, "--suggest -"),
                               "jump": args.jump or None,
                               "sheet": parse_sheet_link(args.sheet) if args.sheet is not None else None,
                               "sheet_env": sheet_env() if args.sheet else None}
        if args.spec:
            req.update(location(args.spec, args.ref, args.diagram))
        elif args.ref is not None:
            raise NvtourError(EXIT_USAGE, "edit: --ref needs a location FILE:L1[-L2]")
        elif args.diagram is not None:
            raise NvtourError(EXIT_USAGE, "edit: --diagram needs a location L1[-L2]")
        return "edit", req
    if c == "diagram":
        name = diagram.check_name(args.name)
        if args.source == "-":
            source, fmt = read_stdin("diagram -"), args.format or "mermaid"
        else:
            path = ranges.resolve_path(args.source)
            ranges.require_file(path)
            with open(path, encoding="utf-8", errors="replace") as fh:
                source, fmt = fh.read(), args.format or diagram.guess_format(path)
        return "diagram", {**base, **diagram_args(name, source, fmt, args.ascii, args.link or [], args.header,
                                                  args.split, not args.no_show)}
    if c == "apply":
        return "apply", apply_request(args, base)
    if c == "remove":
        return "remove", {"n": args.n}
    if c == "chapter":
        modes = [args.name is not None, args.remove is not None, args.list]
        if sum(modes) != 1:
            raise NvtourError(EXIT_USAGE, 'chapter: give a name ("Name"), or --remove N, or --list')
        if args.at is not None and args.name is None:
            raise NvtourError(EXIT_USAGE, "chapter: --at goes with a name")
        if args.name is not None and not args.name.strip():
            raise NvtourError(EXIT_USAGE, "chapter: the name is empty")
        return "chapter", {"name": args.name, "at": args.at, "remove": args.remove, "list": args.list or None}
    if c == "goto":
        return "goto", {"n": args.n}
    if c == "clear":
        return c, {"keep_buffers": args.keep_buffers or None}
    if c == "marks":
        return "marks", {"clear": args.clear or None}
    if c in ("next", "prev", "first", "last", "where", "diff-close", "status"):
        return c, {}
    if c == "focus" and args.steps:
        if args.more:
            raise NvtourError(EXIT_USAGE, "focus --steps: the ranges come from the steps; give no range")
        path = None
        if args.spec:
            path = ranges.resolve_path(args.spec)
            if not os.path.isfile(path):
                try:
                    ranges.parse_spec(args.spec)
                except NvtourError:
                    pass  # not a range: the file is missing
                else:
                    raise NvtourError(EXIT_USAGE, "focus --steps: give a plain FILE, without a range")
            ranges.require_file(path)
        return "focus_steps", {**base, "file": path, "context": args.context, "dim": args.dim or None}
    if c == "focus":
        if not args.spec:
            raise NvtourError(EXIT_USAGE, "focus: give FILE:L1-L2 (or --steps)")
        path, l1, l2, query = ranges.parse_spec(args.spec)
        ranges.require_file(path)
        rs = [focus_range(l1, l2, query)] + [focus_range(*ranges.parse_range_or_query(r)) for r in args.more]
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
        return "diff", {"file": path, "lines": text_lines(text), "title": args.title or title}
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


def fmt_sheet(res: Mapping[str, Any]) -> str:
    """The sheet link at the end of a step line."""
    return f"  ↔ sheet {res['sheet']}" if res.get("sheet") else ""


def fmt_moved(s: Mapping[str, Any]) -> str:
    """``(lost: reason)`` for a step whose lines are gone, ``(was L1-L2)`` for a step that moved."""
    if s.get("lost"):
        return f"  (lost: {s['lost_reason']})" if s.get("lost_reason") else "  (lost)"
    if s.get("orig_l1") is not None and (s["orig_l1"], s.get("orig_l2")) != (s["l1"], s["l2"]):
        return f"  (was {fmt_range(s['orig_l1'], s.get('orig_l2') or s['orig_l1'])})"
    return ""


def fmt_focus(f: Mapping[str, Any], workspace: str, status: bool = False) -> str:
    """``focus: file ranges (mode)``; a focus from the steps says so in ``status`` and a file at a git
    ref has `` @REF``. Right after the command a focus that waits for its file says so."""
    rs = " ".join(fmt_range(a, b) for a, b in f["ranges"])
    at = f" @{f['ref']}" if f.get("ref") else ""
    note = ""
    if f.get("from_steps") and status:
        note = ", from steps"
    elif f.get("deferred"):
        note = "; applied when the file is shown"
    return f"focus: {display_path(f['file'], workspace)}{at} {rs} ({f['mode']}{note})"


def format_step(res: Mapping[str, Any], workspace: str, prefix: str = "") -> str:
    """``step N/T: file:L1-L2 [role] label`` and the first highlighted line, so the agent can check it."""
    label = f" {res['label']}" if res.get("label") else ""
    lost = "  (lost)" if res.get("lost") else ""
    out = f"{prefix}step {res['n']}/{res['total']}: {fmt_loc(res, workspace)} [{res['role']}]{label}{fmt_sheet(res)}{lost}"
    if res.get("text") is not None:
        out += f"\n  {res['l1']}| {res['text']}"  # lets the agent see what it highlighted
    return out


def format_apply(res: Mapping[str, Any], workspace: str) -> str:
    """The step lines of ``apply``, then its summary; with ``--check`` each step line starts with ``ok `` and
    there is no summary but ``check: N steps ok``."""
    steps = res.get("steps") or []
    if res.get("check"):
        out = [format_step(s, workspace, "ok ") for s in steps]
        out.append(f"check: {len(steps)} step{'' if len(steps) == 1 else 's'} ok")
        return "\n".join(out)
    out = [format_step(s, workspace) for s in steps]
    n = res["panel_lines"]
    out.append(f"panel: {n} line{'' if n == 1 else 's'}")
    out.append(f"chapters: {res['chapters']}")
    if res.get("jumped_to"):
        out.append(f"jumped to step {res['jumped_to']}")
    return "\n".join(out)


def format_result(args: argparse.Namespace, res: dict[str, Any], workspace: str) -> str:
    """One-line (or short) human output for a successful command."""
    c = args.command
    if c in ("step", "edit", "goto", "next", "prev", "first", "last"):
        if res.get("message"):
            return str(res["message"])
        return format_step(res, workspace)
    if c == "apply":
        return format_apply(res, workspace)
    if c == "remove":
        return f"removed step {res['removed']}; {res['total']} step(s) left"
    if c == "status":
        return format_status(res, workspace)
    if c == "chapter":
        return format_chapter(args, res)
    if c == "start":
        return f"started: {res.get('title') or 'Walkthrough'}{fmt_sheet(res)}"
    if c == "focus":
        if args.steps:
            return "\n".join(fmt_focus(f, workspace) for f in res["focus"])
        return fmt_focus(res, workspace)
    if c == "unfocus":
        return f"unfocused {res.get('count', 0)} buffer(s)"
    if c == "diff":
        return f"diff: {res['title']} <-> {res['base']}"
    if c == "diff-close":
        return f"diff closed ({res.get('closed', 0)} tab(s))"
    if c == "diagram":
        return format_diagram(res)
    if c == "panel":
        return f"panel: {res['status']}"
    if c == "clear":
        return "cleared"
    if c == "where":
        return format_where(res, workspace)
    if c == "marks":
        return format_marks(res, workspace)
    return json.dumps(res)


def fmt_chapter(c: Mapping[str, Any]) -> str:
    """``chapter "Name" at step 3`` or ``chapter "Name" pending (next step)``."""
    where = f"at step {c['step']}" if c.get("step") else "pending (next step)"
    return f'chapter "{c["name"]}" {where}'


def format_chapter(args: argparse.Namespace, res: Mapping[str, Any]) -> str:
    """One short line per action; ``--list`` has one line per chapter, in tour order."""
    if args.list:
        return "\n".join(fmt_chapter(c) for c in res.get("chapters") or []) or "no chapters"
    if args.remove is not None:
        return "removed pending chapter" if res.get("pending") else f"removed chapter at step {res['removed']}"
    replaced = f', replacing "{res["replaced"]}"' if res.get("replaced") else ""
    return fmt_chapter(res) + replaced


def format_keys(keys: Mapping[str, str] | None) -> str:
    order = ["next", "prev", "first", "last", "panel", "clear", "next_chapter", "prev_chapter", "peek", "view", "card", "range_style"]
    return ", ".join(f"{keys[k]} {k}" for k in order if keys and keys.get(k)) or "(none installed)"


def format_status(res: dict[str, Any], workspace: str) -> str:
    """Multi-line output for ``status``."""
    title = res.get("title") or "Walkthrough"
    out = [f"tour: {title} ({res['total']} step(s), current {res['current'] or '-'})"]
    if res.get("tour_sheet"):
        out.append(f"sheet: {res['tour_sheet']}")
    heads = {c["step"]: c["name"] for c in res.get("chapters") or [] if c.get("step")}
    for s in res.get("steps") or []:
        if s["n"] in heads:
            out.append(f"  ## {heads[s['n']]}")
        mark = "▶" if s["n"] == res["current"] else " "
        label = f" {s['label']}" if s.get("label") else ""
        out.append(f"  {mark} {s['n']}. {fmt_loc(s, workspace)} [{s['role']}]{label}{fmt_sheet(s)}{fmt_moved(s)}")
        if s.get("via") or s.get("from"):
            src = f"from {s['from']}" if s.get("from") else ""
            via = " ".join((s.get("via") or "").split())
            out.append(f"       ↓ {src}{': ' if src and via else ''}{via}")
        if s.get("suggest"):
            n = len(s["suggest"])
            out.append(f"       + suggested: {n} line{'' if n == 1 else 's'}")
    for c in res.get("chapters") or []:
        if not c.get("step"):
            out.append(f"  ## {c['name']} (pending: the next step added)")
    if res.get("modified_files"):
        out.append("modified: " + ", ".join(display_path(p, workspace) for p in res["modified_files"]))
    for f in res.get("focus") or []:
        out.append(fmt_focus(f, workspace, status=True))
    for d in res.get("diagrams") or []:
        out.append(f"diagram: {d['name']} ({d['count']} lines, {d['links']} link(s))")
    if res.get("follow"):
        out.append("follow: on")
    if res.get("marks"):
        out.append(f"marks: {res['marks']}")
    panel = res.get("panel") or {}
    out.append(f"panel: {'open' if panel.get('open') else 'closed'}   diff tabs: {res.get('diff_tabs', 0)}")
    if res.get("view") == "side":
        out.append(f"view: side ({res.get('range_style') or 'rules'}), card {'open' if res.get('card') else 'closed'}")
    out.append(f"keys: {format_keys(res.get('keys'))}")
    return "\n".join(out)


def format_diagram(res: dict[str, Any]) -> str:
    """The size of the diagram, then its numbered lines, so steps can name them."""
    lines = res.get("lines") or []
    width = max((len(line) for line in lines), default=0)
    shown = "shown" if res.get("shown") else "not shown"
    header = f"header line {res['header']}, " if res.get("header") else ""
    out = [f"diagram {res['name']}: {len(lines)} lines, {width} columns, {res.get('links', 0)} link(s), {header}{shown}"]
    digits = len(str(len(lines)))
    out.extend(f"{i:>{digits}}| {line}" for i, line in enumerate(lines, 1))
    return "\n".join(out)


def format_where(res: dict[str, Any], workspace: str) -> str:
    """Multi-line output for ``where``."""
    f = display_path(res["file"], workspace) if res["file"] else "[No Name]"
    flags = []
    if res.get("modified"):
        flags.append("modified (differs from disk)")
    if res.get("ref"):
        flags.append(f"at git ref {res['ref']} ({res['sha'][:12]}), read-only")
    elif res.get("buftype"):
        flags.append(f"buftype={res['buftype']}")
    if res.get("current_window") is False:
        flags.append("not the current window")
    extra = f"  [{', '.join(flags)}]" if flags else ""
    at = f" @{res['ref']}" if res.get("ref") else ""
    out = [f"{f}:{res['line']}:{res['col']}{at}  mode={res['mode']}  visible={res['top']}-{res['bottom']}  cwd={res['cwd']}{extra}"]
    sel = res.get("selection")
    if sel:
        text = sel.get("text") or []
        when = "live" if sel.get("live") else "previous, may be old"
        cut = ", truncated" if sel.get("truncated") else ""
        out.append(f"selection: {fmt_range(sel['l1'], sel['l2'])} ({when}, {sel.get('kind')}, {len(text)} line(s) shown{cut})")
        out.extend(f"  {line}" for line in text)
    return "\n".join(out)


MARK_TEXT_LINES = 5  # lines of a mark that the text output shows; --json has all of them


def fmt_age(seconds: int) -> str:
    if seconds < 60:
        return f"{max(seconds, 0)} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def format_marks(res: Mapping[str, Any], workspace: str) -> str:
    """Like ``where``: ``N. FILE:L1-L2 [@REF] [(lines deleted)] (AGE) ["comment"]``, then the first lines."""
    out = []
    for m in res.get("marks") or []:
        head = f"{m['n']}. {fmt_loc(m, workspace)}"
        if m.get("deleted"):
            head += "  (lines deleted)"
        head += f"  ({fmt_age(int(m.get('age') or 0))})"
        if m.get("comment"):
            head += f'  "{" ".join(m["comment"].split())}"'
        out.append(head)
        text = m.get("text") or []
        out.extend(f"     {line}" for line in text[:MARK_TEXT_LINES])
        if len(text) > MARK_TEXT_LINES or m.get("truncated"):
            out.append("     ...")
    return "\n".join(out) or "no marks"


def emit(args: argparse.Namespace, res: dict[str, Any], text: str, socket: str | None = None) -> None:
    """Print JSON or text."""
    if args.json:
        data = dict(res)
        if socket:
            data["socket"] = socket
        print(json.dumps(data))
    else:
        print(text)


def rpc(sock: str, timeout: float, cmd: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One short call to the nvim at ``sock``; a result that is not ok raises with its exit code."""
    client = Client(sock, timeout).connect()
    try:
        client.check_not_blocked()
        client.ensure_lua_loaded()
        res = client.call(cmd, payload)
    finally:
        client.close()
    if not res.get("ok"):
        raise NvtourError(int(res.get("code") or EXIT_RPC), str(res.get("error", "unknown error")))
    return res


def wait_for_marks(sock: str, timeout: float, seconds: float) -> None:
    """Poll ``marks_count`` every 0.5 s (short calls, each inside ``timeout``) until there is a mark;
    exit 6 after ``seconds``. A blocked nvim fails the call as usual (exit 5)."""
    deadline = time.monotonic() + seconds
    while rpc(sock, timeout, "marks_count", {}).get("count", 0) == 0:
        left = deadline - time.monotonic()
        if left <= 0:
            raise NvtourError(EXIT_BAD_FILE, f"no marks after {seconds:g} s")
        time.sleep(min(0.5, left))


def run_remote(args: argparse.Namespace, workspace: str, env: Mapping[str, str], sock: str | None = None) -> int:
    """Commands that talk to the selected nvim instance (``sock``: already chosen)."""
    cmd, payload = build_request(args, workspace)
    sock = sock or pick_socket(args, workspace, env)[0]
    res = rpc(sock, args.timeout, cmd, payload)
    if cmd == "diagram":
        res["lines"] = payload["lines"]  # the agent picks step ranges from them
    emit(args, res, format_result(args, res, workspace), sock)
    if not args.json:
        for w in res.get("warnings") or []:
            print(f"warning: {w}", file=sys.stderr)
    return 0


def run_marks(args: argparse.Namespace, workspace: str, env: Mapping[str, str]) -> int:
    """``marks``: with --wait, first wait for a mark. --clear removes in the same call that returns the
    marks, so only what is printed is removed."""
    sock = pick_socket(args, workspace, env)[0]
    if args.wait is not None:
        wait_for_marks(sock, args.timeout, args.wait)
    return run_remote(args, workspace, env, sock)


def run_instances(args: argparse.Namespace, workspace: str, env: Mapping[str, str]) -> int:
    insts = discover.inspect_all(workspace, env)
    pruned = skipped = 0
    if args.prune:
        for i in insts:
            if i.state == "stale":
                if discover.unlink_stale_socket(i.socket):
                    pruned += 1
                else:
                    skipped += 1
    if args.json:
        print(json.dumps({"workspace": workspace, "instances": [i.to_dict() for i in insts], "pruned": pruned,
                          "skipped": skipped}))
    else:
        text = discover.format_instances(insts)
        if text:
            print(text)
        if args.prune:
            extra = f"; skipped {skipped} (not a socket owned by you)" if skipped else ""
            print(f"pruned {pruned} stale socket(s){extra}")
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
    try:  # the keys of the marks are installed when the runtime loads
        client = Client(inst.socket, args.timeout).connect()
        try:
            client.ensure_lua_loaded()
        finally:
            client.close()
    except NvtourError as exc:
        print(f"warning: could not load the nvtour runtime: {exc.message}", file=sys.stderr)
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


SHEET_CMD_LUA = ("local c = vim.g.nvtour_sheet_cmd "
                 "if type(c) ~= 'string' or c == '' then c = 'sheet' end "
                 "return { c, vim.fn.executable(c) == 1 }")


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
    for program, command in diagram.tool_status():
        add(True if command else None, f"{program} (diagrams)", command or "not found (optional; needed for 'nvtour diagram')")
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
            # The runtime starts the command inside that nvim, so its g:nvtour_sheet_cmd and PATH count.
            sheet = client.exec_lua(SHEET_CMD_LUA) or []
            if sheet and sheet[1]:
                add(True, "sheet command", f"{sheet[0]} is executable (steps with --sheet are shown in the sheet)")
            else:
                add(None, "sheet command", f"{sheet[0] if sheet else 'sheet'} is not executable in the running nvim "
                    "(optional; steps with --sheet then do nothing)")
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
    else:
        found = shutil.which("sheet")
        add(True if found else None, "sheet command", found or "not found on PATH (optional; steps with --sheet then do nothing)")
    return report()


def run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Dispatch to the right command runner."""
    workspace = resolve_workspace(args.workspace)
    if args.command == "doctor":
        return run_doctor(args, workspace, env)
    if args.command == "instances":
        return run_instances(args, workspace, env)
    if args.command == "attach":
        return run_attach(args, workspace, env)
    if args.command == "marks":
        return run_marks(args, workspace, env)
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

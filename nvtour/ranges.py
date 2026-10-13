"""Parsing of ``FILE:L1[-L2]`` specs (also ``FILE:L1,L2`` and ``FILE:L:COL``), of text queries
(``FILE:/TEXT/``, see ``parse_query``) and path resolution."""

from __future__ import annotations

import os
import re
from typing import Any, NamedTuple

from .errors import EXIT_BAD_FILE, NvtourError

_RANGE_RE = re.compile(r"^(\d+)(?:[-,](\d+))?$")
# FILE:L:COL as printed by compilers, `rg -n --column` and LSP tools; the column is ignored.
_COL_RE = re.compile(r"^(.+):(\d+(?:[-,]\d+)?):(\d+)$")


def parse_range(text: str) -> tuple[int, int]:
    """Parse ``L1``, ``L1-L2`` or ``L1,L2`` into an inclusive (l1, l2) pair."""
    m = _RANGE_RE.match(text.strip())
    if not m:
        raise NvtourError(EXIT_BAD_FILE, f"bad range {text!r}: expected L1 or L1-L2")
    l1 = int(m.group(1))
    l2 = int(m.group(2)) if m.group(2) else l1
    if l1 < 1:
        raise NvtourError(EXIT_BAD_FILE, f"bad range {text!r}: lines start at 1")
    if l2 < l1:
        raise NvtourError(EXIT_BAD_FILE, f"bad range {text!r}: L1 must not be greater than L2")
    return l1, l2


def resolve_path(path: str, cwd: str | None = None) -> str:
    """Resolve ``path`` relative to ``cwd`` (default: process cwd) and realpath it."""
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.realpath(path)


class Spec(NamedTuple):
    """A parsed location: numbers (``l1``, ``l2``) or a ``query``, never both."""

    path: str
    l1: int | None
    l2: int | None
    query: dict[str, Any] | None


def _scan_text(s: str, i: int) -> tuple[str, int]:
    """Text of ``s`` from ``i`` up to the next slash that is not ``\\/``; returns (text, index after the slash)."""
    out: list[str] = []
    while i < len(s):
        if s.startswith("\\/", i):
            out.append("/")
            i += 2
        elif s[i] == "/":
            return "".join(out), i + 1
        else:
            out.append(s[i])
            i += 1
    raise ValueError("a '/' is missing at the end of the text")


_MOD_RE = re.compile(r"(?:@(\d+)|~(\d+))?")
_PLUS_RE = re.compile(r"(?:\+(\d+))?")


def parse_query(text: str) -> dict[str, Any]:
    """Parse ``/TEXT/[@N|~L][+N]`` or ``/START/[@N|~L]../END/[+N]`` into
    ``{start, end, plus, nth, near}`` (``end``, ``nth`` and ``near`` are None when absent).

    ``\\/`` is a slash inside TEXT. Raises ValueError with the reason when ``text`` is not a query."""
    if not text.startswith("/"):
        raise ValueError("a query starts with '/'")
    start, i = _scan_text(text, 1)
    m = _MOD_RE.match(text, i)
    assert m is not None
    i = m.end()
    nth = int(m.group(1)) if m.group(1) else None
    near = int(m.group(2)) if m.group(2) else None
    end = None
    if text.startswith("../", i):
        end, i = _scan_text(text, i + 3)
        if not end:
            raise ValueError("the END text is empty")
    plus_m = _PLUS_RE.match(text, i)
    assert plus_m is not None
    if plus_m.end() != len(text):
        raise ValueError(f"unexpected {text[i:]!r} after the text")
    if not start:
        raise ValueError("the text is empty")
    if nth == 0 or near == 0:
        raise ValueError("@N and ~L count from 1")
    return {"start": start, "end": end, "plus": int(plus_m.group(1) or 0), "nth": nth, "near": near}


def format_query(q: dict[str, Any]) -> str:
    """The canonical text of a query (``parse_query`` reads it back)."""
    esc = lambda t: t.replace("/", "\\/")  # noqa: E731
    out = "/" + esc(q["start"]) + "/"
    if q.get("nth"):
        out += f"@{q['nth']}"
    elif q.get("near"):
        out += f"~{q['near']}"
    if q.get("end"):
        out += "../" + esc(q["end"]) + "/"
    if q.get("plus"):
        out += f"+{q['plus']}"
    return out


def resolve_query(lines: list[str], q: dict[str, Any], name: str = "file") -> tuple[int, int]:
    """Lines (1-based, inclusive) that query ``q`` selects in ``lines``. The Lua runtime has the
    same function (``resolve_query`` in nvtour.lua); tests/test_query.py runs both on one table."""
    start = q["start"]
    hits = [n for n, line in enumerate(lines, 1) if start in line]
    if not hits:
        raise NvtourError(EXIT_BAD_FILE, f"text '{start}' not found in {name} ({len(lines)} lines)")
    if q.get("nth"):
        if q["nth"] > len(hits):
            raise NvtourError(EXIT_BAD_FILE, f"text '{start}' has {len(hits)} match(es) in {name}, but @{q['nth']} was asked")
        l1 = hits[q["nth"] - 1]
    elif q.get("near"):
        l1 = min(hits, key=lambda n: (abs(n - q["near"]), n))
    elif len(hits) > 1:
        shown = [f"  {n}| {lines[n - 1][:100]}" for n in hits[:10]]
        if len(hits) > 10:
            shown.append(f"  ... and {len(hits) - 10} more")
        raise NvtourError(
            EXIT_BAD_FILE,
            f"text '{start}' matches {len(hits)} lines in {name}; choose one with @N or ~L:\n" + "\n".join(shown))
    else:
        l1 = hits[0]
    l2 = l1
    if q.get("end"):
        found = next((n for n in range(l1, len(lines) + 1) if q["end"] in lines[n - 1]), None)
        if found is None:
            raise NvtourError(EXIT_BAD_FILE, f"end text '{q['end']}' not found in {name} after line {l1} ({len(lines)} lines)")
        l2 = found
    return l1, l2 + (q.get("plus") or 0)


def read_lines(path: str) -> list[str]:
    """Lines of a text file like nvim shows them (a final newline adds no empty line)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_range_or_query(text: str) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """Parse ``L1[-L2]`` or a query into (l1, l2, query); one side is None."""
    if text.startswith("/"):
        try:
            return None, None, parse_query(text)
        except ValueError as exc:
            raise NvtourError(EXIT_BAD_FILE, f"bad text range {text!r}: {exc}") from None
    l1, l2 = parse_range(text)
    return l1, l2, None


def parse_spec(spec: str, cwd: str | None = None) -> Spec:
    """Parse ``FILE:L1[-L2]`` (also ``FILE:L1,L2``, ``FILE:L:COL``) or ``FILE:/TEXT/...`` into a Spec.

    A text query starts at the first ``:/`` whose rest is a well formed query, so TEXT may contain
    ``:`` (``a.cpp:/Cache::get/``) and a path may contain ``:/``. Otherwise the last ``:`` splits."""
    pos, first_err = spec.find(":/"), None
    while pos > 0:
        try:
            q = parse_query(spec[pos + 1:])
        except ValueError as exc:
            first_err = first_err or exc
            pos = spec.find(":/", pos + 1)
            continue
        return Spec(resolve_path(spec[:pos], cwd), None, None, q)
    m = _COL_RE.match(spec)
    if m:
        with_col = resolve_path(m.group(1), cwd)
        # Only a file literally named "x.cpp:12" makes "x.cpp:12:5" mean line 5 of it.
        if os.path.isfile(with_col) or not os.path.isfile(resolve_path(spec.rpartition(":")[0], cwd)):
            l1, l2 = parse_range(m.group(2))
            return Spec(with_col, l1, l2, None)
    path, sep, rng = spec.rpartition(":")
    if not sep or not path:
        raise NvtourError(EXIT_BAD_FILE, f"bad spec {spec!r}: expected FILE:L1[-L2] or FILE:/TEXT/")
    try:
        l1, l2 = parse_range(rng)
    except NvtourError:
        if first_err:  # it looked like a text query: say what is wrong with it
            raise NvtourError(EXIT_BAD_FILE, f"bad text range in {spec!r}: {first_err}") from None
        raise
    return Spec(resolve_path(path, cwd), l1, l2, None)


def parse_file_range(spec: str, cwd: str | None = None) -> tuple[str, int, int]:
    """Parse ``path:L1[-L2]`` (or ``path:L1,L2``, ``path:L:COL``) into (absolute path, l1, l2)."""
    path, l1, l2, query = parse_spec(spec, cwd)
    if query is not None or l1 is None or l2 is None:
        raise NvtourError(EXIT_BAD_FILE, f"bad spec {spec!r}: expected FILE:L1[-L2]")
    return path, l1, l2


def require_file(path: str) -> None:
    """Raise a bad-file error unless ``path`` is a readable regular file."""
    if not os.path.isfile(path):
        raise NvtourError(EXIT_BAD_FILE, f"file not found: {path}")
    if not os.access(path, os.R_OK):
        raise NvtourError(EXIT_BAD_FILE, f"file not readable: {path}")

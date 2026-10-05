"""Parsing of ``FILE:L1[-L2]`` specs (also ``FILE:L1,L2`` and ``FILE:L:COL``) and path resolution."""

from __future__ import annotations

import os
import re

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


def parse_file_range(spec: str, cwd: str | None = None) -> tuple[str, int, int]:
    """Parse ``path:L1[-L2]`` (or ``path:L1,L2``, ``path:L:COL``) into (absolute path, l1, l2)."""
    m = _COL_RE.match(spec)
    if m:
        with_col = resolve_path(m.group(1), cwd)
        # Only a file literally named "x.cpp:12" makes "x.cpp:12:5" mean line 5 of it.
        if os.path.isfile(with_col) or not os.path.isfile(resolve_path(spec.rpartition(":")[0], cwd)):
            l1, l2 = parse_range(m.group(2))
            return with_col, l1, l2
    path, sep, rng = spec.rpartition(":")
    if not sep or not path:
        raise NvtourError(EXIT_BAD_FILE, f"bad spec {spec!r}: expected FILE:L1[-L2]")
    l1, l2 = parse_range(rng)
    return resolve_path(path, cwd), l1, l2


def require_file(path: str) -> None:
    """Raise a bad-file error unless ``path`` is a readable regular file."""
    if not os.path.isfile(path):
        raise NvtourError(EXIT_BAD_FILE, f"file not found: {path}")
    if not os.access(path, os.R_OK):
        raise NvtourError(EXIT_BAD_FILE, f"file not readable: {path}")

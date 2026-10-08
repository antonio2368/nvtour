"""Rendering of diagram sources to text for ``nvtour diagram``, and the ``--link`` targets."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from typing import Any

from . import ranges
from .errors import EXIT_BAD_FILE, EXIT_USAGE, NvtourError

FORMATS = ["mermaid", "dot", "easy", "text"]
# The Lua runtime names the buffer of diagram NAME the same way.
BUFFER_PREFIX = "nvtour://diagram/"
RENDER_TIMEOUT = 30.0

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_EXTENSIONS = {".mmd": "mermaid", ".mermaid": "mermaid", ".dot": "dot", ".gv": "dot", ".txt": "text"}
# format -> (program, environment variable that overrides it, install hint)
_TOOLS = {
    "mermaid": ("mermaid-ascii", "NVTOUR_MERMAID_ASCII",
                "download it from https://github.com/AlexanderGrooff/mermaid-ascii/releases"),
    "dot": ("graph-easy", "NVTOUR_GRAPH_EASY", "install it with: sudo apt install libgraph-easy-perl"),
    "easy": ("graph-easy", "NVTOUR_GRAPH_EASY", "install it with: sudo apt install libgraph-easy-perl"),
}


def check_name(name: str) -> str:
    """Return ``name`` if it can name a diagram buffer, else raise a usage error."""
    if not _NAME_RE.match(name):
        raise NvtourError(EXIT_USAGE, f"bad diagram name {name!r}: use letters, digits, '.', '_' and '-'")
    return name


def buffer_name(name: str) -> str:
    return BUFFER_PREFIX + name


def guess_format(path: str | None) -> str:
    """Format from the file extension of ``path``; mermaid for stdin and unknown extensions."""
    if not path:
        return "mermaid"
    return _EXTENSIONS.get(os.path.splitext(path)[1].lower(), "mermaid")


def tool_command(fmt: str) -> list[str] | None:
    """Command of the renderer of ``fmt`` (the environment variable may hold a command with arguments)."""
    program, var = _TOOLS[fmt][:2]
    override = os.environ.get(var)
    if override:
        return shlex.split(override)
    found = shutil.which(program)
    return [found] if found else None


def tool_status() -> list[tuple[str, str | None]]:
    """(program, command or None) for each renderer, for ``doctor``."""
    out = []
    for fmt in ("mermaid", "dot"):
        cmd = tool_command(fmt)
        out.append((_TOOLS[fmt][0], " ".join(cmd) if cmd else None))
    return out


def clean(text: str) -> list[str]:
    """Lines of rendered text: no colour codes, no tabs, no trailing blanks, no blank first or last lines."""
    lines = [_ANSI_RE.sub("", line).expandtabs(8).rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def render(source: str, fmt: str, ascii_only: bool) -> list[str]:
    """Render ``source`` to text lines with the renderer of ``fmt`` ("text" is used as it is)."""
    if fmt == "text":
        lines = clean(source)
    else:
        program, var, hint = _TOOLS[fmt]
        cmd = tool_command(fmt)
        if not cmd:
            raise NvtourError(EXIT_USAGE, f"{program} not found for --format {fmt}; {hint}, or set {var}")
        if fmt == "mermaid":
            cmd = cmd + ["-f", "-"] + (["--ascii"] if ascii_only else [])
        else:
            cmd = cmd + [f"--from={'dot' if fmt == 'dot' else 'txt'}", f"--as={'ascii' if ascii_only else 'boxart'}"]
        try:
            proc = subprocess.run(cmd, input=source, capture_output=True, text=True, timeout=RENDER_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise NvtourError(EXIT_BAD_FILE, f"{program} did not finish in {RENDER_TIMEOUT:g} s") from None
        except OSError as exc:
            raise NvtourError(EXIT_USAGE, f"cannot run {program}: {exc}") from None
        lines = clean(proc.stdout)
        if proc.returncode != 0 or not lines:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            raise NvtourError(EXIT_BAD_FILE, f"{program} could not render the diagram: {detail}")
    if not lines:
        raise NvtourError(EXIT_BAD_FILE, "the diagram is empty")
    return lines


def parse_link(spec: str, lines: list[str]) -> dict[str, Any]:
    """Parse ``TEXT=FILE:L1[-L2]``; TEXT must occur in the diagram ``lines`` and the range in FILE."""
    text, sep, target = spec.rpartition("=")
    if not sep or not text:
        raise NvtourError(EXIT_USAGE, f"bad --link {spec!r}: expected TEXT=FILE:L1[-L2]")
    if not any(text in line for line in lines):
        raise NvtourError(EXIT_BAD_FILE, f"--link text {text!r} is not in the diagram")
    path, l1, l2 = ranges.parse_file_range(target)
    ranges.require_file(path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        count = sum(1 for _ in fh)
    if l2 > count:
        raise NvtourError(EXIT_BAD_FILE, f"--link {text!r}: range {l1}-{l2} is beyond end of file ({count} lines): {path}")
    return {"text": text, "file": path, "l1": l1, "l2": l2}

"""Small git helpers (toplevel lookup, ``git show``)."""

from __future__ import annotations

import os
import subprocess

from .errors import EXIT_BAD_FILE, NvtourError


def toplevel(directory: str) -> str | None:
    """Return the git toplevel of ``directory`` or None when not in a repository."""
    try:
        out = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return os.path.realpath(out.stdout.strip())


def show(path: str, ref: str) -> str:
    """Return the content of ``path`` at ``ref`` (``git show ref:relpath``)."""
    directory = os.path.dirname(path) or "."
    top = toplevel(directory)
    if top is None:
        raise NvtourError(EXIT_BAD_FILE, f"{path} is not inside a git repository")
    rel = os.path.relpath(path, top)
    try:
        out = subprocess.run(
            ["git", "-C", top, "show", f"{ref}:{rel}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NvtourError(EXIT_BAD_FILE, f"git show failed: {exc}") from exc
    if out.returncode != 0:
        msg = out.stderr.decode("utf-8", "replace").strip()
        raise NvtourError(EXIT_BAD_FILE, f"git show {ref}:{rel} failed: {msg}")
    return out.stdout.decode("utf-8", "replace")

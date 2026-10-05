"""Small git helpers (toplevel lookup, file content at a ref)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

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


def _git(top: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", top, *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NvtourError(EXIT_BAD_FILE, f"git failed: {exc}") from exc


def _err(out: subprocess.CompletedProcess) -> str:
    return out.stderr.decode("utf-8", "replace").strip()


def show(path: str, ref: str) -> str:
    """Return the content of ``path`` at ``ref`` (``git show ref:relpath``)."""
    directory = os.path.dirname(path) or "."
    top = toplevel(directory)
    if top is None:
        raise NvtourError(EXIT_BAD_FILE, f"{path} is not inside a git repository")
    rel = os.path.relpath(path, top)
    out = _git(top, "show", f"{ref}:{rel}")
    if out.returncode != 0:
        raise NvtourError(EXIT_BAD_FILE, f"git show {ref}:{rel} failed: {_err(out)}")
    return out.stdout.decode("utf-8", "replace")


@dataclass
class Blob:
    """A file as it is at a commit."""

    rel: str  # path relative to the git toplevel
    sha: str  # the commit the ref pointed to
    text: str


def blob_at(path: str, ref: str) -> Blob:
    """Content of ``path`` at ``ref``, bound to the commit ``ref`` points to now.

    ``path`` does not have to exist in the working tree: the toplevel is found from its nearest
    existing directory.
    """
    directory = os.path.dirname(path) or "."
    while not os.path.isdir(directory) and os.path.dirname(directory) != directory:
        directory = os.path.dirname(directory)
    top = toplevel(directory)
    if top is None:
        raise NvtourError(EXIT_BAD_FILE, f"{path} is not inside a git repository")
    rel = os.path.relpath(path, top)
    if rel == os.curdir or rel.startswith(os.pardir + os.sep):
        raise NvtourError(EXIT_BAD_FILE, f"{path} is not inside the git repository {top}")
    out = _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    if out.returncode != 0:
        raise NvtourError(EXIT_BAD_FILE, f"unknown git ref: {ref}")
    sha = out.stdout.decode().strip()
    out = _git(top, "cat-file", "blob", f"{sha}:{rel}")
    if out.returncode != 0:
        raise NvtourError(EXIT_BAD_FILE, f"{rel} is not a file at {ref}: {_err(out)}")
    return Blob(rel=rel, sha=sha, text=out.stdout.decode("utf-8", "replace"))

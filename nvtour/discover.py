"""Neovim instance discovery, scoring, selection and the per-workspace pin cache."""

from __future__ import annotations

import getpass
import glob
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .client import HINT, Client
from .errors import EXIT_NO_MATCH, EXIT_NO_NVIM, EXIT_RPC, NvtourError

PROBE_LUA = """
local bufs = {}
for _, b in ipairs(vim.api.nvim_list_bufs()) do
  if vim.bo[b].buflisted then
    local n = vim.api.nvim_buf_get_name(b)
    if n ~= "" and n:sub(1, 1) == "/" and #bufs < 200 then bufs[#bufs + 1] = n end
  end
end
local v = vim.version()
return { pid = vim.fn.getpid(), cwd = vim.fn.getcwd(), version = { v.major, v.minor, v.patch },
         buffers = bufs, tabpages = #vim.api.nvim_list_tabpages() }
"""

SOCKET_RE = re.compile(r"nvim\.(\d+)\.\d+$")


@dataclass
class Instance:
    """One discovered nvim socket."""

    socket: str
    pid: int | None
    state: str  # live | stale | unresponsive
    cwd: str = ""
    buffers: list[str] = field(default_factory=list)
    tabpages: int = 0
    version: str = ""
    score: int = 0
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def runtime_dirs(env: Mapping[str, str]) -> list[str]:
    """Glob patterns scanned for sockets, in order."""
    pats: list[str] = []
    xdg = env.get("XDG_RUNTIME_DIR")
    if xdg:
        pats.append(os.path.join(xdg, "nvim.*.0"))
    if not env.get("NVTOUR_RUNTIME_ONLY"):
        user = env.get("USER") or getpass.getuser()
        pats.append(f"/tmp/nvim.{user}/*/nvim.*.0")
        pats.append("/tmp/nvim.*.0")
    return pats


def scan_socket_paths(env: Mapping[str, str]) -> list[str]:
    """Return candidate socket paths, de-duplicated, in scan order."""
    seen: dict[str, None] = {}
    for pat in runtime_dirs(env):
        for p in sorted(glob.glob(pat)):
            if SOCKET_RE.search(p):
                seen.setdefault(p, None)
    return list(seen)


def pid_of(path: str) -> int | None:
    """Parse the pid out of ``nvim.<pid>.0``."""
    m = SOCKET_RE.search(os.path.basename(path))
    return int(m.group(1)) if m else None


def pid_alive(pid: int | None) -> bool:
    """True if ``/proc/<pid>`` exists."""
    return pid is not None and os.path.exists(f"/proc/{pid}")


def _under(path: str, root: str) -> bool:
    return Path(path).is_relative_to(root)


def score_instance(cwd: str, workspace: str, buffers: list[str]) -> tuple[int, str]:
    """Score an instance against workspace ``W`` (see DESIGN.md section 3)."""
    cwd_r = os.path.realpath(cwd)
    if cwd_r == workspace:
        return 100, "exact match"
    if _under(cwd_r, workspace):
        return 80, "subdir"
    if _under(workspace, cwd_r):
        return 60, "parent"
    if any(_under(b, workspace) for b in buffers):
        return 40, "buffers"
    return 0, ""


def probe(path: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """Probe a socket; returns the info table or None when unresponsive."""
    client = Client(path, timeout)
    try:
        client.connect()
        info = client.exec_lua(PROBE_LUA)
    except NvtourError:
        return None
    finally:
        client.close()
    if not isinstance(info, dict):
        return None
    if not info.get("buffers"):
        info["buffers"] = []
    return info


def inspect_all(workspace: str, env: Mapping[str, str], timeout: float = 2.0) -> list[Instance]:
    """Enumerate and probe every socket, scoring live instances against ``workspace``."""
    out: list[Instance] = []
    for path in scan_socket_paths(env):
        pid = pid_of(path)
        if not pid_alive(pid):
            out.append(Instance(path, pid, "stale"))
            continue
        info = probe(path, timeout)
        if info is None:
            out.append(Instance(path, pid, "unresponsive"))
            continue
        cwd = str(info.get("cwd", ""))
        bufs = [str(b) for b in info.get("buffers", [])]
        score, kind = score_instance(cwd, workspace, bufs)
        ver = ".".join(str(x) for x in info.get("version", []))
        out.append(
            Instance(path, int(info.get("pid", pid or 0)), "live", cwd, bufs, int(info.get("tabpages", 0)), ver, score, kind)
        )
    return out


def inspect_socket(path: str, workspace: str, timeout: float = 2.0) -> Instance:
    """Probe one explicitly given socket; raise exit 5 when it does not answer."""
    info = probe(path, timeout)
    if info is None:
        raise NvtourError(EXIT_RPC, f"cannot connect to {path}; {HINT}")
    cwd = str(info.get("cwd", ""))
    bufs = [str(b) for b in info.get("buffers", [])]
    score, kind = score_instance(cwd, workspace, bufs)
    ver = ".".join(str(x) for x in info.get("version", []))
    return Instance(path, int(info.get("pid", 0)), "live", cwd, bufs, int(info.get("tabpages", 0)), ver, score, kind or "manual")


def format_instances(instances: list[Instance]) -> str:
    """One line per instance: ``pid  state  score  cwd (#buffers)``."""
    lines = []
    for i in instances:
        cwd = i.cwd or "-"
        lines.append(f"{i.pid}  {i.state}  {i.score}  {cwd} ({len(i.buffers)})")
    return "\n".join(lines)


def select(instances: list[Instance], workspace: str) -> Instance:
    """Pick the unique best live instance or raise exit 3 / 4."""
    live = [i for i in instances if i.state == "live"]
    if not live:
        raise NvtourError(EXIT_NO_NVIM, f"no running nvim found; open nvim in {workspace} and retry")
    top = max(i.score for i in live)
    best = [i for i in live if i.score == top]
    if top > 0 and len(best) == 1:
        return best[0]
    table = "\n".join(f"  pid {i.pid}  score {i.score}  {i.cwd}" for i in live)
    reason = "several instances match equally" if top > 0 else "no instance matches this workspace"
    raise NvtourError(
        EXIT_NO_MATCH,
        f"{reason}; live nvim instances:\n{table}\nopen nvim in {workspace} or run: nvtour attach <pid>",
    )


# ---------------------------------------------------------------------------
# Pin cache
# ---------------------------------------------------------------------------


def pin_path(workspace: str, env: Mapping[str, str]) -> Path:
    """Location of the pin file for ``workspace``."""
    digest = hashlib.sha256(workspace.encode()).hexdigest()[:16]
    xdg = env.get("XDG_RUNTIME_DIR")
    base = Path(xdg) / "nvtour" if xdg else Path.home() / ".cache" / "nvtour"
    return base / digest


def write_pin(workspace: str, socket: str, env: Mapping[str, str], pid: int | None = None) -> None:
    """Remember ``socket`` for ``workspace`` (atomically).

    The pid is stored only when it is visible here: a socket with a custom name, or one that belongs
    to an nvim in another pid namespace (a container), is then checked by existence alone.
    """
    if pid is None:
        pid = pid_of(socket)
    p = pin_path(workspace, env)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"socket": socket, "pid": pid if pid_alive(pid) else None}))
    os.replace(tmp, p)


def read_pin(workspace: str, env: Mapping[str, str]) -> str | None:
    """Return the pinned socket if it is still alive; otherwise drop the stale pin."""
    p = pin_path(workspace, env)
    try:
        raw = p.read_text().strip()
    except OSError:
        return None
    try:
        data = json.loads(raw)
        sock, pid = str(data.get("socket") or ""), data.get("pid")
    except (ValueError, AttributeError):  # plain-text pin written by nvtour 0.1
        sock, pid = raw, pid_of(raw)
    if sock and os.path.exists(sock) and (pid is None or pid_alive(int(pid))):
        return sock
    clear_pin(workspace, env)
    return None


def clear_pin(workspace: str, env: Mapping[str, str]) -> bool:
    """Remove the pin; returns True if one existed."""
    try:
        pin_path(workspace, env).unlink()
        return True
    except OSError:
        return False


def explicit_socket(
    socket_arg: str | None, workspace: str, env: Mapping[str, str], use_pin: bool = True
) -> tuple[str, str] | None:
    """Apply precedence rules 1-4; returns (socket, source) or None to fall back to discovery."""
    if socket_arg:
        return socket_arg, "--socket"
    for var in ("NVTOUR_SOCKET", "NVIM"):
        val = env.get(var)
        if val and os.path.exists(val):  # a stale value left in the environment is ignored
            return val, f"${var}"
    if use_pin:
        pinned = read_pin(workspace, env)
        if pinned:
            return pinned, "pin"
    return None

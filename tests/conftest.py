"""Shared fixtures: a private headless nvim (never the user's real instances)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pynvim
import pytest

REPO = Path(__file__).resolve().parent.parent


def clean_env(runtime: Path) -> dict[str, str]:
    """Environment that cannot reach the real nvim instances."""
    env = {k: v for k, v in os.environ.items() if k not in ("NVIM", "NVTOUR_SOCKET", "NVIM_LISTEN_ADDRESS")}
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["NVTOUR_RUNTIME_ONLY"] = "1"
    env["PYTHONPATH"] = str(REPO)
    return env


def wait_for_socket(runtime: Path, pid: int, timeout: float = 15.0) -> str:
    sock = runtime / f"nvim.{pid}.0"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sock.exists():
            return str(sock)
        time.sleep(0.05)
    raise RuntimeError("nvim socket did not appear")


@pytest.fixture(scope="session")
def sandbox(tmp_path_factory: pytest.TempPathFactory):
    """Temp runtime dir + workspace with two source files and a git repo."""
    root = tmp_path_factory.mktemp("nvtour")
    runtime = root / "run"
    ws = root / "ws"
    runtime.mkdir()
    ws.mkdir()
    ws = ws.resolve()
    (ws / "a.txt").write_text("".join(f"line {i} of a\n" for i in range(1, 61)))
    (ws / "b.txt").write_text("".join(f"line {i} of b\n" for i in range(1, 31)))
    git = ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "init"], check=True)
    return SimpleNamespace(root=root, runtime=runtime.resolve(), ws=ws)


@pytest.fixture(scope="session")
def nvim_proc(sandbox):
    """Headless nvim started with cwd = workspace and its own runtime dir."""
    env = clean_env(sandbox.runtime)
    proc = subprocess.Popen(
        ["nvim", "--headless", "--clean"],
        cwd=sandbox.ws,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sock = wait_for_socket(sandbox.runtime, proc.pid)
        yield SimpleNamespace(proc=proc, socket=sock, pid=proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def pristine(sandbox):
    """Byte-exact snapshot of the source files, checked at the end of the session."""
    snap = {p: p.read_bytes() for p in (sandbox.ws / "a.txt", sandbox.ws / "b.txt")}
    yield snap


@pytest.fixture()
def nv(nvim_proc, pristine):
    """pynvim handle on the private instance; the tour is cleared before each test."""
    nvim = pynvim.attach("socket", path=nvim_proc.socket)
    nvim.exec_lua("if _G.nvtour then _G.nvtour.dispatch('clear', {}) end")
    nvim.command("silent! %bwipeout!")
    yield nvim
    nvim.close()


@pytest.fixture()
def cli(sandbox, nvim_proc):
    """Run the real CLI against the private instance and return the CompletedProcess."""

    def run(*args: str, stdin: str | None = None, socket: bool = True, env_extra: dict | None = None):
        env = clean_env(sandbox.runtime)
        env.update(env_extra or {})
        cmd = [sys.executable, "-m", "nvtour.cli"]
        if socket:
            cmd += ["--socket", nvim_proc.socket]
        cmd += list(args)
        return subprocess.run(cmd, cwd=sandbox.ws, env=env, input=stdin, capture_output=True, text=True, timeout=60)

    return run

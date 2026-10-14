"""Connection to nvim with hard timeouts, and Lua runtime loading."""

from __future__ import annotations

import hashlib
import time
from functools import cache
from importlib import resources
from typing import Any, Callable

from .errors import EXIT_RPC, NvtourError
from .rpc import Connection, RpcTimeout

HINT = "nvim may be waiting at a prompt; press <Enter> in nvim"


@cache
def lua_source() -> str:
    """Return the packaged Lua runtime source."""
    return resources.files("nvtour").joinpath("lua/nvtour.lua").read_text(encoding="utf-8")


def lua_version() -> str:
    """Version of the packaged runtime: a hash of its source, so any edit reloads it in nvim."""
    return hashlib.sha1(lua_source().encode()).hexdigest()[:12]


class Client:
    """A connection to one nvim instance; every request has a timeout."""

    def __init__(self, socket: str, timeout: float = 5.0) -> None:
        self.socket = socket
        self.timeout = timeout
        self.deadline: float | None = None  # one budget for the whole command, set by connect()
        self.conn: Connection | None = None

    def _guard(self, fn: Callable[[float], Any], phase: str) -> Any:
        """Run ``fn(deadline)``; a timeout or a failure becomes an NvtourError that names ``phase``."""
        deadline = time.monotonic() + self.timeout if self.deadline is None else self.deadline
        try:
            if deadline <= time.monotonic():
                raise RpcTimeout()
            return fn(deadline)
        except RpcTimeout:
            later = "; the command may still run when nvim is free" if phase == "dispatch" else ""
            raise NvtourError(EXIT_RPC, f"timeout after {self.timeout:g}s during {phase} on {self.socket}; {HINT}{later}")
        except NvtourError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NvtourError(EXIT_RPC, f"RPC failure during {phase} on {self.socket}: {exc}") from exc

    def connect(self) -> "Client":
        self.deadline = time.monotonic() + self.timeout
        self.conn = self._guard(lambda deadline: Connection(self.socket, deadline), "connect")
        return self

    def request(self, method: str, args: list[Any], phase: str) -> Any:
        """Call the nvim API function ``method`` with a timeout."""
        return self._guard(lambda deadline: self.conn.request(method, args, deadline), phase)

    def exec_lua(self, code: str, *args: Any, phase: str = "request") -> Any:
        """Run Lua code in nvim with a timeout."""
        return self.request("nvim_exec_lua", [code, list(args)], phase)

    def mode(self) -> dict[str, Any]:
        """``nvim_get_mode()``: answered at once even when nvim waits for input."""
        res = self.request("nvim_get_mode", [], "mode check")
        return res if isinstance(res, dict) else {}

    def check_not_blocked(self) -> None:
        """Refuse to send a request that nvim would queue until the user answers a prompt."""
        m = self.mode()
        if m.get("blocking"):
            raise NvtourError(
                EXIT_RPC,
                f"nvim is waiting for input (mode {m.get('mode')!r}: a prompt, a pending key or an operator); "
                "the command was not run. Ask the user to press <Esc> or <Enter> in nvim, then retry",
            )

    def loaded_version(self) -> str | None:
        """Version of the Lua runtime currently loaded in nvim, if any."""
        return self.exec_lua("return _G.nvtour and _G.nvtour.VERSION", phase="version check")

    def ensure_lua_loaded(self) -> None:
        """Send the Lua runtime when it is missing or has a different version."""
        version = lua_version()
        if self.loaded_version() != version:
            # Wrap the runtime so the chunk returns a plain string, not the module table; the version
            # is passed as the chunk's first vararg.
            self.exec_lua("local m = (function(...)\n" + lua_source() + "\nend)(...)\nreturn m.VERSION", version,
                          phase="runtime load")

    def call(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch one command and return the result table."""
        clean = {k: v for k, v in (args or {}).items() if v is not None}
        res = self.exec_lua("return _G.nvtour.dispatch(...)", cmd, clean, phase="dispatch")
        if not isinstance(res, dict):
            raise NvtourError(EXIT_RPC, f"unexpected reply from nvim: {res!r}")
        return res

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

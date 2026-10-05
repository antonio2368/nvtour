"""pynvim connection with hard timeouts and Lua runtime loading."""

from __future__ import annotations

import hashlib
import threading
from functools import cache
from importlib import resources
from typing import Any, Callable

from .errors import EXIT_RPC, NvtourError

HINT = "nvim may be waiting at a prompt; press <Enter> in nvim"


class RpcTimeout(Exception):
    """An RPC call did not finish in time."""


def run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run ``fn`` in a daemon thread; raise RpcTimeout if it takes longer than ``timeout``."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise RpcTimeout()
    if "error" in box:
        raise box["error"]
    return box.get("value")


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
        self.nvim: Any = None

    def _guard(self, fn: Callable[[], Any]) -> Any:
        try:
            return run_with_timeout(fn, self.timeout)
        except RpcTimeout:
            raise NvtourError(EXIT_RPC, f"timeout after {self.timeout:g}s talking to {self.socket}; {HINT}")
        except NvtourError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NvtourError(EXIT_RPC, f"RPC failure on {self.socket}: {exc}; {HINT}") from exc

    def connect(self) -> "Client":
        try:
            import pynvim
        except ImportError:
            raise NvtourError(EXIT_RPC, "pynvim is not installed; install with: pip install pynvim") from None

        self.nvim = self._guard(lambda: pynvim.attach("socket", path=self.socket))
        return self

    def exec_lua(self, code: str, *args: Any) -> Any:
        """Run Lua code in nvim with a timeout."""
        return self._guard(lambda: self.nvim.exec_lua(code, *args))

    def loaded_version(self) -> str | None:
        """Version of the Lua runtime currently loaded in nvim, if any."""
        return self.exec_lua("return _G.nvtour and _G.nvtour.VERSION")

    def ensure_lua_loaded(self) -> None:
        """Send the Lua runtime when it is missing or has a different version."""
        version = lua_version()
        if self.loaded_version() != version:
            # Wrap the runtime so the chunk returns a plain string, not the module table; the version
            # is passed as the chunk's first vararg.
            self.exec_lua("local m = (function(...)\n" + lua_source() + "\nend)(...)\nreturn m.VERSION", version)

    def call(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch one command and return the result table."""
        clean = {k: v for k, v in (args or {}).items() if v is not None}
        res = self.exec_lua("return _G.nvtour.dispatch(...)", cmd, clean)
        if not isinstance(res, dict):
            raise NvtourError(EXIT_RPC, f"unexpected reply from nvim: {res!r}")
        return res

    def close(self) -> None:
        if self.nvim is not None:
            try:
                run_with_timeout(self.nvim.close, 1.0)
            except Exception:  # noqa: BLE001 - best effort
                pass

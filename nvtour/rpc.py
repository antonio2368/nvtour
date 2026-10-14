"""A small msgpack-RPC client for the nvim API over a unix socket (standard library only).

nvtour sends a few requests per command, so the cost that counts is the start of the process: this
module replaces pynvim, whose import (asyncio, greenlet) took more time than the rest of a command.
Strings use UTF-8 with ``surrogateescape`` both ways, as pynvim does, so a name that is not valid
UTF-8 goes back to nvim unchanged.
"""

from __future__ import annotations

import socket
import struct
import time
from typing import Any

REQUEST, RESPONSE, NOTIFICATION = 0, 1, 2
ENCODING = ("utf-8", "surrogateescape")


class RpcTimeout(Exception):
    """A request did not finish before the deadline."""


class RpcError(Exception):
    """nvim answered a request with an error, or the connection broke."""


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _pack(obj: Any, out: list[bytes]) -> None:
    if obj is None:
        out.append(b"\xc0")
    elif obj is True:
        out.append(b"\xc3")
    elif obj is False:
        out.append(b"\xc2")
    elif isinstance(obj, int):
        if 0 <= obj < 0x80 or -32 <= obj < 0:
            out.append(struct.pack(">b", obj) if obj < 0 else bytes((obj,)))
        elif obj < 0:
            out.append(b"\xd3" + struct.pack(">q", obj))
        else:
            out.append(b"\xcf" + struct.pack(">Q", obj))
    elif isinstance(obj, float):
        out.append(b"\xcb" + struct.pack(">d", obj))
    elif isinstance(obj, str):
        data = obj.encode(*ENCODING)
        out.append(b"\xdb" + struct.pack(">I", len(data)) + data)
    elif isinstance(obj, (bytes, bytearray)):
        out.append(b"\xc6" + struct.pack(">I", len(obj)) + bytes(obj))
    elif isinstance(obj, (list, tuple)):
        out.append(b"\xdd" + struct.pack(">I", len(obj)))
        for item in obj:
            _pack(item, out)
    elif isinstance(obj, dict):
        out.append(b"\xdf" + struct.pack(">I", len(obj)))
        for key, value in obj.items():
            _pack(key, out)
            _pack(value, out)
    else:
        raise TypeError(f"cannot send a {type(obj).__name__} to nvim")


def pack(obj: Any) -> bytes:
    """Encode ``obj`` (None, bool, int, float, str, bytes, list, tuple, dict)."""
    out: list[bytes] = []
    _pack(obj, out)
    return b"".join(out)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

# Fixed-size values: type byte -> struct format.
_FIXED = {
    0xCA: ">f", 0xCB: ">d",
    0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q",
    0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q",
}
# Values with a length field: type byte -> (kind, struct format of the length).
_SIZED = {
    0xD9: ("str", ">B"), 0xDA: ("str", ">H"), 0xDB: ("str", ">I"),
    0xC4: ("bin", ">B"), 0xC5: ("bin", ">H"), 0xC6: ("bin", ">I"),
    0xC7: ("ext", ">B"), 0xC8: ("ext", ">H"), 0xC9: ("ext", ">I"),
    0xDC: ("array", ">H"), 0xDD: ("array", ">I"),
    0xDE: ("map", ">H"), 0xDF: ("map", ">I"),
}
_FIXEXT = {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}


class Reader:
    """Decodes values from a socket; ``read`` blocks until one whole value is there."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.pos = 0
        self.deadline: float | None = None

    def _fill(self, n: int) -> None:
        while len(self.buf) - self.pos < n:
            if self.deadline is not None:
                left = self.deadline - time.monotonic()
                if left <= 0:
                    raise RpcTimeout()
                self.sock.settimeout(left)
            try:
                data = self.sock.recv(65536)
            except TimeoutError:
                raise RpcTimeout() from None
            if not data:
                raise RpcError("nvim closed the connection")
            self.buf = self.buf[self.pos:] + data
            self.pos = 0

    def _take(self, n: int) -> bytes:
        self._fill(n)
        data = self.buf[self.pos:self.pos + n]
        self.pos += n
        return data

    def _unpack(self, fmt: str) -> Any:
        return struct.unpack(fmt, self._take(struct.calcsize(fmt)))[0]

    def _ext(self, n: int) -> Any:
        self._unpack(">b")  # the type: Buffer, Window or Tabpage; nvtour only needs the handle
        data = self._take(n)
        sub = Reader(self.sock)
        sub.buf = data
        return sub.read()

    def read(self) -> Any:
        """Decode the next value."""
        t = self._take(1)[0]
        if t < 0x80:
            return t
        if t >= 0xE0:
            return t - 0x100
        if t <= 0x8F:
            return {self.read(): self.read() for _ in range(t & 0x0F)}
        if t <= 0x9F:
            return [self.read() for _ in range(t & 0x0F)]
        if t <= 0xBF:
            return self._take(t & 0x1F).decode(*ENCODING)
        if t == 0xC0:
            return None
        if t in (0xC2, 0xC3):
            return t == 0xC3
        if t in _FIXED:
            return self._unpack(_FIXED[t])
        if t in _FIXEXT:
            return self._ext(_FIXEXT[t])
        if t in _SIZED:
            kind, fmt = _SIZED[t]
            n = self._unpack(fmt)
            if kind == "str":
                return self._take(n).decode(*ENCODING)
            if kind == "bin":
                return self._take(n)
            if kind == "ext":
                return self._ext(n)
            if kind == "array":
                return [self.read() for _ in range(n)]
            return {self.read(): self.read() for _ in range(n)}
        raise RpcError(f"bad msgpack type byte 0x{t:02x} from nvim")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class Connection:
    """One socket to nvim; requests are sent one at a time."""

    def __init__(self, path: str, deadline: float) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.reader = Reader(self.sock)
        self.msgid = 0
        try:
            self.sock.settimeout(max(deadline - time.monotonic(), 0.001))
            self.sock.connect(path)
        except TimeoutError:
            self.sock.close()
            raise RpcTimeout() from None
        except OSError:
            self.sock.close()
            raise

    def request(self, method: str, args: list[Any], deadline: float) -> Any:
        """Send ``method(args)`` and wait for its response until ``deadline`` (time.monotonic)."""
        self.msgid += 1
        msgid = self.msgid
        left = deadline - time.monotonic()
        if left <= 0:
            raise RpcTimeout()
        self.sock.settimeout(left)
        try:
            self.sock.sendall(pack([REQUEST, msgid, method, args]))
        except TimeoutError:
            raise RpcTimeout() from None
        self.reader.deadline = deadline
        while True:
            msg = self.reader.read()
            if not isinstance(msg, list) or not msg:
                raise RpcError(f"bad message from nvim: {msg!r}")
            if msg[0] == RESPONSE and len(msg) == 4 and msg[1] == msgid:
                err = msg[2]
                if err is not None:
                    raise RpcError(err[1] if isinstance(err, list) and len(err) > 1 else str(err))
                return msg[3]
            if msg[0] == REQUEST and len(msg) == 4:
                # nvim asks this client something (rpcrequest): say no, so nvim does not wait for it.
                self.sock.sendall(pack([RESPONSE, msg[1], "nvtour does not serve requests", None]))
            # A notification, or a response to an older request that timed out: skip it.

    def close(self) -> None:
        self.sock.close()

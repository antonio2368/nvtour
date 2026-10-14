"""The msgpack-RPC client: the codec against the msgpack package, and requests to a private nvim."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time

import msgpack
import pytest

from nvtour.client import Client
from nvtour.errors import EXIT_RPC, NvtourError
from nvtour.rpc import Reader, pack

from conftest import REPO

VALUES = [
    None, True, False, 0, 1, 127, 128, 255, 256, 65535, 65536, 2**32, 2**64 - 1,
    -1, -32, -33, -128, -129, -32768, -32769, -(2**31) - 1, -(2**63),
    0.0, -1.5, 1e300, "", "a", "x" * 31, "x" * 32, "x" * 300, "x" * 70000, "ünïcode ✓",
    b"", b"\x00\xff", [], [1, [2, [3]]], list(range(20)), {}, {"a": 1, "b": [None, {"c": "d"}]},
    {str(i): i for i in range(20)},
]


class BytesSock:
    """Feeds ``data`` to a Reader in small pieces, as a socket can."""

    def __init__(self, data: bytes, piece: int = 3) -> None:
        self.data, self.piece = data, piece

    def settimeout(self, _t: float) -> None:
        pass

    def recv(self, _n: int) -> bytes:
        out, self.data = self.data[:self.piece], self.data[self.piece:]
        return out


@pytest.mark.parametrize("value", VALUES, ids=lambda v: type(v).__name__ + str(len(v) if hasattr(v, "__len__") else v)[:12])
def test_codec_matches_msgpack(value):
    assert msgpack.unpackb(pack(value), raw=False, strict_map_key=False) == value
    assert Reader(BytesSock(msgpack.packb(value, use_bin_type=True))).read() == value


def test_tuple_is_sent_as_array_and_ext_decodes_to_its_handle():
    assert msgpack.unpackb(pack((1, "a"))) == [1, "a"]
    ext = msgpack.packb(msgpack.ExtType(0, msgpack.packb(7)))  # a Buffer handle
    assert Reader(BytesSock(ext)).read() == 7


def test_invalid_utf8_round_trips():
    raw = b"name-\xff.txt"  # nvim sends a buffer name as a msgpack str, valid UTF-8 or not
    value = Reader(BytesSock(msgpack.packb(raw, use_bin_type=False))).read()
    assert value.encode("utf-8", "surrogateescape") == raw
    assert msgpack.unpackb(pack(value), raw=True) == raw


def test_requests_to_nvim(nvim_proc, nv):
    c = Client(nvim_proc.socket, 5).connect()
    try:
        assert c.exec_lua("return ...", 1, "two", [3], {"k": None}) == 1
        assert c.exec_lua("return { ... }", "ü", -5, 2.5, True) == ["ü", -5, 2.5, True]
        assert c.exec_lua("return vim.api.nvim_get_current_buf()") == nv.current.buffer.handle
        big = "y" * 200_000  # more than one recv in both directions
        assert c.exec_lua("return ...", big) == big
        assert c.mode()["blocking"] is False
        with pytest.raises(NvtourError) as e:
            c.exec_lua("error('boom')", phase="probe")
        assert e.value.code == EXIT_RPC and "during probe" in e.value.message and "boom" in e.value.message
        assert c.exec_lua("return 42") == 42  # the connection is still usable after an error
    finally:
        c.close()


def test_rpcrequest_from_nvim_is_refused(nvim_proc, nv):
    c = Client(nvim_proc.socket, 5).connect()
    try:
        chan = c.request("nvim_get_api_info", [], "test")[0]
        # nvim asks this client while the client waits for the answer to exec_lua: no deadlock.
        res = c.exec_lua("local ok, err = pcall(vim.rpcrequest, ..., 'x') return { ok, tostring(err) }", chan)
        assert res[0] is False and "does not serve requests" in res[1]
    finally:
        c.close()


def test_timeout_when_nvim_never_answers(tmp_path):
    path = str(tmp_path / "silent.sock")
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
    try:
        c = Client(path, 0.3).connect()
        t0 = time.monotonic()
        with pytest.raises(NvtourError) as e:
            c.call("status")
        assert time.monotonic() - t0 < 2
        assert e.value.code == EXIT_RPC and "timeout after 0.3s during dispatch" in e.value.message
        c.close()
    finally:
        server.close()


def test_connect_to_dead_socket(tmp_path):
    path = str(tmp_path / "dead.sock")
    s = socket.socket(socket.AF_UNIX)
    s.bind(path)
    s.close()  # the file stays, nothing listens
    with pytest.raises(NvtourError) as e:
        Client(path, 1).connect()
    assert e.value.code == EXIT_RPC and "during connect" in e.value.message


def test_cli_does_not_import_pynvim():
    code = "import sys, nvtour.cli; nvtour.cli.build_parser(); print('pynvim' in sys.modules, 'asyncio' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True).stdout
    assert out.split() == ["False", "False"]

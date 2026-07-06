"""Coverage for TestBridge teardown when a client disconnects while a
handler is parked in a nested event loop.

Eval'd code can open a modal (``QDialog.exec``), parking ``_handle_line``
mid-request. If the client times out and disconnects during that park,
``_on_disconnected`` used to ``deleteLater()`` the socket immediately —
the nested loop then destroyed the C++ object under the parked frame,
and the response write after unwind hit a deleted QLocalSocket
(RuntimeError, then a real SIGSEGV — live-crash 2026-07-05). The bridge
now defers doomed sockets until the outer drain unwinds and skips the
write when the client is gone. These tests drive the state machine
directly — no real sockets needed.
"""

from __future__ import annotations

from jellytoast.test_bridge import TestBridge as _Bridge  # alias: pytest must not collect it


class _FakeSock:
    def __init__(self):
        self.deleted = False
        self.written = b""

    def deleteLater(self):  # noqa: N802 — Qt naming
        self.deleted = True

    def write(self, data: bytes):
        self.written += data

    def flush(self):
        pass


def _bridge(qapp) -> _Bridge:
    return _Bridge(qapp, dict)


def test_disconnect_while_handling_defers_deletion(qapp):
    b = _bridge(qapp)
    sock = _FakeSock()
    b._buffers[id(sock)] = bytearray()
    b._socks[id(sock)] = sock

    b._handling = True  # a handler is parked somewhere up-stack
    b._on_disconnected(sock)
    assert not sock.deleted, "deleteLater during a parked handler is the segfault"
    assert id(sock) not in b._socks

    # The drain tail sweeps doomed sockets once the stack is clear.
    b._handling = False
    for doomed in b._doomed:
        doomed.deleteLater()
    b._doomed.clear()
    assert sock.deleted


def test_disconnect_idle_deletes_immediately(qapp):
    b = _bridge(qapp)
    sock = _FakeSock()
    b._buffers[id(sock)] = bytearray()
    b._socks[id(sock)] = sock
    b._on_disconnected(sock)
    assert sock.deleted


def test_handle_line_skips_write_for_disconnected_client(qapp):
    b = _bridge(qapp)
    sock = _FakeSock()
    # Not registered in _socks — the client already disconnected.
    b._handle_line(sock, b'{"op": "ping"}')
    assert sock.written == b"", "response written to a dead client"

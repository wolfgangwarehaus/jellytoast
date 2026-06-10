"""
Dev-only remote-control bridge for live end-to-end testing.

OFF by default. Only stood up when the environment variable
``JT_TEST_BRIDGE=1`` is set at launch (wired in ``jellytoast.main``).

It opens a per-user ``QLocalServer`` (a Unix-domain socket, distinct
from the single-instance socket) that accepts newline-delimited JSON
requests and returns newline-delimited JSON responses. Because the
server lives on the GUI thread, each request is evaluated *on the GUI
thread* — so it can safely touch any Qt object, emit ``PlayerBus``
signals, call window methods, and read back state.

This is the deterministic control path for driving the app on KDE
Wayland, where synthetic pointer/key input is unreliable: instead of
clicking blind, a test harness emits the same signals the UI emits and
reads the same state the UI reads.

Wire protocol (one JSON object per line, UTF-8):

  request:   {"op": "ping"}
             {"op": "eval", "code": "<python expression>"}
             {"op": "exec", "code": "<python statements>"}
  response:  {"ok": true,  "result": <json-coerced value>}
             {"ok": false, "error": "<repr>", "traceback": "<str>"}

SECURITY: this executes arbitrary Python in-process. It binds to a
user-private local socket and ONLY listens when JT_TEST_BRIDGE=1. It
must never be enabled in a shipped/packaged build.
"""

import getpass
import json
import logging
import traceback

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer

logger = logging.getLogger(__name__)


def socket_name() -> str:
    """The per-user local-socket name. Shared by server and client so
    Qt resolves both to the same on-disk socket path."""
    return f"jellytoast-test-bridge-{getpass.getuser()}"


def _coerce(value):
    """Best-effort JSON-safe coercion. Natively serialisable values
    pass through; everything else degrades to its ``repr`` (recursing
    into containers first so a dict/list of objects stays structured)."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _coerce(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_coerce(v) for v in value]
        return repr(value)


class TestBridge(QObject):
    """A GUI-thread eval/exec socket. See the module docstring.

    ``namespace_factory`` is a zero-arg callable returning the dict used
    as the eval/exec globals. It is invoked fresh per request so
    late-bound objects (e.g. ``win.mpv_ctrl``, built after first paint)
    resolve to their current value.
    """

    def __init__(self, app, namespace_factory):
        super().__init__(app)
        self._ns_factory = namespace_factory
        self._server: "QLocalServer | None" = None
        self._buffers: dict[int, bytearray] = {}
        self._socks: dict[int, object] = {}
        self._socket_name = socket_name()
        # Re-entrancy guard. If an eval/exec'd command spins a nested
        # event loop (app.processEvents(), QTest.qWait()), Qt can deliver
        # another socket's readyRead on top of the current handler.
        # Writing to / tearing down a QLocalSocket from that nested stack
        # is a use-after-free (the documented SIGSEGV class). We refuse to
        # process re-entrantly: the bytes stay buffered and are drained
        # when the outer handler unwinds. A loop-spinning command thus
        # won't get a nested RPC serviced until it returns — by design.
        self._handling = False

    def start(self) -> bool:
        QLocalServer.removeServer(self._socket_name)
        self._server = QLocalServer(self)
        # This socket evals arbitrary code on the GUI thread — restrict
        # it to the owning user so other local accounts on a shared box
        # can't connect while the dev flag is on (0600 on the Unix
        # socket; ACL-equivalent on Windows named pipes).
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(self._socket_name):
            logger.warning(
                "test bridge listen failed: %s", self._server.errorString()
            )
            return False
        logger.warning(
            "TEST BRIDGE listening on '%s' — remote eval socket is OPEN (dev only)",
            self._socket_name,
        )
        return True

    def _on_new_connection(self):
        if self._server is None:
            return
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        self._buffers[id(sock)] = bytearray()
        self._socks[id(sock)] = sock
        sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
        sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))

    def _on_disconnected(self, sock):
        self._buffers.pop(id(sock), None)
        self._socks.pop(id(sock), None)
        sock.deleteLater()

    def _on_ready_read(self, sock):
        buf = self._buffers.get(id(sock))
        if buf is None:
            return
        buf += bytes(sock.readAll())
        if self._handling:
            # Re-entrant delivery from a nested event loop spun by user
            # code. Leave the bytes buffered; the outer handler drains all
            # sockets when it unwinds (see the guard in __init__).
            return
        self._handling = True
        try:
            self._drain()
        finally:
            self._handling = False

    def _drain(self):
        """Process every buffered complete line across all live sockets,
        looping until no buffer holds a newline. Called only from the
        non-re-entrant outer handler."""
        progressed = True
        while progressed:
            progressed = False
            for sid in list(self._buffers.keys()):
                buf = self._buffers.get(sid)
                sock = self._socks.get(sid)
                while buf is not None and sock is not None and b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf[:] = rest
                    self._handle_line(sock, bytes(line))
                    progressed = True
                    buf = self._buffers.get(sid)

    def _handle_line(self, sock, raw: bytes):
        resp = self._evaluate(raw)
        try:
            sock.write((json.dumps(resp) + "\n").encode())
            sock.flush()
        except Exception:
            logger.exception("test bridge failed to write response")

    def _evaluate(self, raw: bytes) -> dict:
        try:
            req = json.loads(raw.decode())
        except Exception as e:
            return {"ok": False, "error": f"bad request: {e!r}"}
        op = req.get("op")
        code = req.get("code", "")
        if op == "ping":
            return {"ok": True, "result": "pong"}
        if op not in ("eval", "exec"):
            return {"ok": False, "error": f"unknown op: {op!r}"}
        try:
            ns = self._ns_factory()
        except Exception as e:
            return {"ok": False, "error": f"namespace error: {e!r}"}
        try:
            if op == "eval":
                return {"ok": True, "result": _coerce(eval(code, ns))}
            exec(code, ns)
            return {"ok": True, "result": None}
        except Exception as e:
            return {
                "ok": False,
                "error": repr(e),
                "traceback": traceback.format_exc(),
            }

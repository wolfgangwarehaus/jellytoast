"""Reusable client library for the jellytoast test bridge.

Unlike ``jt_ctl.py`` (one-shot CLI), this lets a scenario script fire
many eval/exec calls in a single process: it reuses one
``QCoreApplication`` and opens a fresh ``QLocalSocket`` per RPC.

Usage in a scenario script:

    import os, sys
    sys.path.insert(0, "/path/to/jellytoast")  # the repo checkout
    from dev.jt_drive import Bridge
    b = Bridge()                      # needs TMPDIR=/tmp to reach the live socket
    print(b.e("get_now_playing().title"))
    b.x("bus.pause_toggled.emit()")
    b.shot("/tmp/jt_shots/x.png")
"""

import json
import os
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jellytoast.test_bridge import socket_name  # noqa: E402


class BridgeError(RuntimeError):
    pass


class Bridge:
    def __init__(self, timeout_ms: int = 15000):
        self._app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        self._timeout = timeout_ms

    def _rpc(self, op: str, code: str = "") -> dict:
        s = QLocalSocket()
        s.connectToServer(socket_name())
        if not s.waitForConnected(self._timeout):
            raise BridgeError(f"connect failed: {s.errorString()} (TMPDIR={os.environ.get('TMPDIR')})")
        s.write((json.dumps({"op": op, "code": code}) + "\n").encode())
        s.flush()
        s.waitForBytesWritten(self._timeout)
        buf = bytearray()
        while b"\n" not in buf:
            if not s.waitForReadyRead(self._timeout):
                break
            buf += bytes(s.readAll())
        s.disconnectFromServer()
        line = bytes(buf).split(b"\n", 1)[0].decode(errors="replace")
        return json.loads(line) if line else {"ok": False, "error": "no response"}

    def e(self, code: str):
        """eval; raises BridgeError on failure."""
        r = self._rpc("eval", code)
        if not r.get("ok"):
            raise BridgeError(f"{r.get('error')}\n{r.get('traceback', '')}")
        return r["result"]

    def x(self, code: str):
        """exec; raises BridgeError on failure."""
        r = self._rpc("exec", code)
        if not r.get("ok"):
            raise BridgeError(f"{r.get('error')}\n{r.get('traceback', '')}")
        return None

    def try_e(self, code: str):
        """eval that never raises; returns (ok, value_or_error_str)."""
        r = self._rpc("eval", code)
        return (bool(r.get("ok")), r.get("result") if r.get("ok") else r.get("error"))

    def pump(self, seconds: float = 0.4):
        """Let the app's OWN event loop settle deferred builds/loads by
        sleeping CLIENT-SIDE.

        Do NOT call app.processEvents() (or QTest.qWait) inside the bridge:
        spinning a nested event loop from within an eval/exec tears down a
        QLocalSocket on a nested stack and crashes the app (use-after-free
        SIGSEGV). The app keeps running its own event loop while we sleep
        here, so deferred QTimer.singleShot builds and async fetches settle
        on their own between RPCs."""
        import time

        time.sleep(seconds)

    def shot(self, path: str) -> str:
        """Grab the main window's own painting (blur-blind) to a PNG the
        app process writes; returns the path. Use spectacle for blur."""
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        ok = self.e(f"win.grab().save({path!r})")
        if not ok:
            raise BridgeError(f"grab().save failed for {path}")
        return path

    def surface(self) -> str:
        return self.e("win.content_stack.currentWidget().objectName()")

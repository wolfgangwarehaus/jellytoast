#!/usr/bin/env python3
"""CLI client for the jellytoast test bridge (see modules/test_bridge.py).

The bridge only listens when the app was launched with JT_TEST_BRIDGE=1.
This client connects to that per-user local socket, sends one JSON
request, prints the JSON response line, and exits non-zero if the call
errored.

Usage:
    python dev/jt_ctl.py ping
    python dev/jt_ctl.py eval "win.content_stack.currentWidget().objectName()"
    python dev/jt_ctl.py exec "bus.pause_toggled.emit()"

Uses QLocalSocket (not raw sockets) so it resolves the socket name the
same way QLocalServer does, regardless of where Qt places the socket
file on this platform.
"""

import json

# Allow running from anywhere: ensure the repo root is importable.
import os
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.test_bridge import socket_name  # noqa: E402

_CONNECT_MS = 3000
_IO_MS = 10000


def call(op: str, code: str = "") -> dict:
    QCoreApplication(sys.argv)
    sock = QLocalSocket()
    sock.connectToServer(socket_name())
    if not sock.waitForConnected(_CONNECT_MS):
        return {
            "ok": False,
            "error": f"connect failed ({sock.errorString()}) — is the app running "
            f"with JT_TEST_BRIDGE=1?",
        }
    sock.write((json.dumps({"op": op, "code": code}) + "\n").encode())
    sock.flush()
    sock.waitForBytesWritten(_IO_MS)
    buf = bytearray()
    while b"\n" not in buf:
        if not sock.waitForReadyRead(_IO_MS):
            break
        buf += bytes(sock.readAll())
    sock.disconnectFromServer()
    line = bytes(buf).split(b"\n", 1)[0].decode(errors="replace")
    if not line:
        return {"ok": False, "error": "no response from bridge"}
    try:
        return json.loads(line)
    except Exception as e:
        return {"ok": False, "error": f"unparseable response: {e!r}: {line!r}"}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    op = sys.argv[1]
    code = sys.argv[2] if len(sys.argv) > 2 else ""
    resp = call(op, code)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

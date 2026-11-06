"""The ``jellytoast`` command: hand off to a running instance BEFORE
loading Qt, else start the app.

Importing ``jellytoast.app`` (Qt, PySide6, the whole UI) takes seconds on a
phone. A second tap on the launcher only has to tell the running app to come
forward, so try that first with nothing but the standard library: connect
to the single-instance socket (``single_instance.SingleInstance`` listens
on ``$TMPDIR/jellytoast-<user>-<key>``, Qt's ``QDir::tempPath()``) and send
the launch message with the launcher's activation token. Anything unusual —
Windows (a named pipe, not a file), no socket, a stale one — falls through
to the full start, whose own single-instance gate still applies.
"""

from __future__ import annotations

import os
import sys


def _socket_path() -> str:
    import getpass

    key = os.environ.get("JT_INSTANCE_KEY", "jellytoast")
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, f"jellytoast-{getpass.getuser()}-{key}")


def relay_to_running() -> bool:
    """True when a running jellytoast took the "come forward" message."""
    if sys.platform.startswith("win"):
        return False
    try:
        import socket

        from jellytoast.single_instance_msg import launch_message

        path = _socket_path()
        if not os.path.exists(path):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(path)
            s.sendall(launch_message())
        return True
    except Exception:
        return False


def main() -> None:
    if relay_to_running():
        sys.exit(0)
    # Process setup that must precede the first Qt import (either path).
    from jellytoast import (
        boot_env,  # noqa: F401
        quick,
    )

    if quick.wanted():
        # The phone UI's own entry: never imports jellytoast.app (the
        # whole desktop window, mpv, D-Bus — a second of a phone's start).
        quick.main()
        return
    from jellytoast.app import main as app_main

    app_main()

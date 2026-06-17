"""
Single-instance enforcement.

Standard Qt pattern: QSharedMemory acts as the lock (its existence
proves an instance is running), QLocalServer accepts an out-of-band
"raise me" message from any subsequent launch attempts. Second launch
detects the lock, sends the message via QLocalSocket, exits cleanly.

Why both:
- QSharedMemory alone would let us detect duplicates but not signal
  the running instance to come forward.
- QLocalServer alone would work on a clean exit but races on startup
  (two simultaneous launches both try to create the server).
- Together: the shared-memory create() is atomic and acts as the
  arbiter, the local server is just the message channel.

Stale-segment recovery: if the previous instance was SIGKILL'd, the
shared-memory segment may persist on Linux. We detect this by trying
to *connect* to the local server when attach() succeeds — if no one's
listening, the holder is dead and we force-recover.
"""

import logging

from PySide6.QtCore import QObject, QSharedMemory, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)


def force_foreground(window) -> None:
    """Bring ``window`` to the actual foreground.

    On Windows, ``raise_()`` / ``activateWindow()`` only *flash* the
    taskbar button for a background process — the OS blocks
    ``SetForegroundWindow`` from a non-foreground process. The standard
    workaround is to temporarily attach our GUI thread's input queue to
    the current foreground window's thread, which makes the call honored.
    No-op on other platforms, where Qt's ``activateWindow()`` already
    foregrounds. Best-effort — never raises.
    """
    from jellytoast.platform_compat import IS_WINDOWS

    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        # Pin the HWND/HANDLE signatures so a 64-bit handle isn't truncated
        # to a 32-bit c_int (ctypes' default) on the way in or back out — the
        # classic ctypes-on-Windows footgun (mirrors win_frameless._user32).
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        for _fn in ("BringWindowToTop", "SetForegroundWindow"):
            getattr(user32, _fn).argtypes = [wintypes.HWND]
        hwnd = int(window.winId())
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        our_tid = user32.GetWindowThreadProcessId(hwnd, None)
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        if fg_tid and fg_tid != our_tid:
            user32.AttachThreadInput(fg_tid, our_tid, True)
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                user32.AttachThreadInput(fg_tid, our_tid, False)
        else:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
    except Exception as e:  # pragma: no cover — Windows-only
        logger.debug("force_foreground failed: %s", e)


class SingleInstance(QObject):
    """Acquire a per-key application-wide lock. If another instance
    already holds it, signal that instance to raise its window and tell
    the caller to exit. Holds the lock for the lifetime of this object,
    so keep it alive (don't let it be GC'd before the app quits)."""

    # Emitted on the GUI thread when another launch attempt connects
    # to our server. Wire this to the host window's show / raise /
    # activateWindow trio.
    raise_requested = Signal()

    # Per-key timeouts. Sub-second so a stale-segment retry doesn't
    # noticeably delay launch, but generous enough to absorb a busy
    # system's scheduler hiccups on a real running instance.
    _CONNECT_TIMEOUT_MS = 500
    _WRITE_TIMEOUT_MS = 500

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self._key = key
        # Per-user socket name avoids collisions across user accounts
        # on a multi-user box (LocalServer's default abstract socket
        # is system-global on Linux).
        import getpass

        self._socket_name = f"jellytoast-{getpass.getuser()}-{key}"
        # The shared-memory segment key must be per-user TOO. On Linux a
        # QSharedMemory key maps to a system-global ftok id, so a bare
        # key collides across user accounts: user B would attach() to
        # user A's segment, fail to reach A's per-user socket (treating
        # it as stale), then fail to create() the still-live segment —
        # and acquire() returns False, so B exits without ever showing a
        # window. Mirror the socket's per-user namespacing.
        self._mem_key = f"{self._socket_name}-shm"
        self._mem: "QSharedMemory | None" = None
        self._server: "QLocalServer | None" = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if we're the first
        instance (caller should proceed to build + show the window) or
        False if another instance was found and signaled (caller should
        exit cleanly). On True, also installs the QLocalServer listener
        so future launch attempts can ping us."""
        mem = QSharedMemory(self._mem_key)
        if mem.attach():
            # Segment exists. Probe the holder via QLocalSocket — if
            # it's reachable, it's a real running instance; if not,
            # it's a stale segment from a crashed previous run.
            if self._signal_existing():
                mem.detach()
                return False
            # Stale. Detach so the OS releases the segment, then fall
            # through to create a fresh one.
            mem.detach()

        if not mem.create(1):
            # Race — another launch attempt won between our attach
            # check and our create. Treat as "another instance" and
            # signal it to come forward.
            self._signal_existing()
            return False

        # We hold the lock. Stand up the listener.
        self._mem = mem
        QLocalServer.removeServer(self._socket_name)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(self._socket_name):
            # Listener failed (path conflict, perms, …). The lock is
            # still ours so duplicate launches will be detected, but
            # they won't be able to reach us. Best-effort log, then
            # carry on.
            logger.warning(
                "listener failed: %s", self._server.errorString()
            )
        return True

    def _signal_existing(self) -> bool:
        """Try to ping the running instance. Returns True on success
        (real instance reachable), False if the socket is unreachable
        (likely a stale shared-memory segment)."""
        sock = QLocalSocket()
        sock.connectToServer(self._socket_name)
        if not sock.waitForConnected(self._CONNECT_TIMEOUT_MS):
            return False
        sock.write(b"raise")
        sock.flush()
        sock.waitForBytesWritten(self._WRITE_TIMEOUT_MS)
        sock.disconnectFromServer()
        return True

    def _on_new_connection(self):
        if self._server is None:
            return
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        # We don't actually need the message contents — any connection
        # means "another launch happened, please come forward". Drain
        # whatever was sent so the socket closes cleanly.
        try:
            sock.waitForReadyRead(200)
            sock.readAll()
        finally:
            sock.disconnectFromServer()
            sock.deleteLater()
        self.raise_requested.emit()

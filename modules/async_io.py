"""Async I/O helpers — Qt-native replacements for `threading.Thread` +
`requests` patterns scattered through the codebase.

Two facilities live here:

- ``get_qnam()`` — an app-wide ``QNetworkAccessManager``. QNAM is the
  Qt-idiomatic way to do HTTP from a GUI app: it runs on the calling
  thread's event loop, never blocks, fires ``finished`` per reply, and
  internally pools connections + caps parallelism per host. The image
  loader in ``ui_helpers`` uses it.

- ``run_async(fn, *args, on_result=…, on_error=…)`` — runs a blocking
  callable on a shared ``QThreadPool`` and dispatches the result onto
  the GUI thread via Qt signals. Used for the still-sync ``requests``
  paths in ``jellyfin_api`` (lyrics, library shuffle, favorite toggle).
  Preferred over raw ``threading.Thread`` because workers are bounded
  (no thread explosion on bursts) and lifetimes are managed by Qt.

Both helpers lazy-construct on first use so tests can import the module
without a live ``QApplication``.
"""

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager


# ── QNetworkAccessManager singleton ─────────────────────────────────────────

_qnam: Optional[QNetworkAccessManager] = None


def get_qnam() -> QNetworkAccessManager:
    """Return the app-wide QNetworkAccessManager.

    QNAM must be created and used from a single thread (the GUI thread
    in our case). It pools connections, supports HTTP/2 transparently,
    and caps concurrent requests per host at 6 — the 5-parallel-GET
    figure cited in some Qt docs is for the legacy synchronous path.

    Lazy: first call constructs it. Subsequent calls return the same
    instance. Module import remains side-effect-free.
    """
    global _qnam
    if _qnam is None:
        _qnam = QNetworkAccessManager()
    return _qnam


# ── Shared thread pool for blocking work ────────────────────────────────────

_pool: Optional[QThreadPool] = None


def get_thread_pool() -> QThreadPool:
    """Return the app-wide QThreadPool.

    Bounded at 4 workers so a click-storm (e.g. mashing the shuffle
    button) can't spawn one thread per click. Anything that doesn't
    fit in 4 slots queues, which is exactly what we want.
    """
    global _pool
    if _pool is None:
        _pool = QThreadPool()
        _pool.setMaxThreadCount(4)
    return _pool


# ── run_async: blocking callable → GUI-thread callback ──────────────────────

class _Signaler(QObject):
    """Signal carrier for cross-thread completion. Lives on the thread
    that constructed it (GUI thread); slots auto-dispatch via queued
    connection from the pool worker."""

    completed = Signal(object)
    failed = Signal(object)


# Pin live signalers across the cross-thread emit. Without this, PySide6
# garbage-collects the QObject between `signal.emit()` (worker thread)
# and the GUI-thread slot dispatch — slot never runs. Same pattern as
# the `_pending_loaders` set the old image loader needed; centralising
# it here lets us delete that one.
_pending_signalers: set = set()


class _AsyncTask(QRunnable):
    def __init__(self, fn, args, kwargs, signaler: _Signaler):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._signaler = signaler
        self.setAutoDelete(True)

    def run(self):  # noqa: D401  (Qt override)
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001
            self._signaler.failed.emit(exc)
            return
        self._signaler.completed.emit(result)


def run_async(fn: Callable[..., Any], *args,
              on_result: Optional[Callable[[Any], None]] = None,
              on_error: Optional[Callable[[Exception], None]] = None,
              **kwargs) -> None:
    """Run ``fn(*args, **kwargs)`` on the shared pool; dispatch result
    or exception back to the GUI thread.

    Either callback may be omitted. Exceptions raised by ``fn`` are
    routed to ``on_error`` if given, otherwise swallowed silently —
    the caller is responsible for surfacing failures they care about.
    """
    sig = _Signaler()
    _pending_signalers.add(sig)

    def _drop(_=None):
        _pending_signalers.discard(sig)

    if on_result is not None:
        sig.completed.connect(on_result)
    if on_error is not None:
        sig.failed.connect(on_error)
    sig.completed.connect(_drop)
    sig.failed.connect(_drop)

    get_thread_pool().start(_AsyncTask(fn, args, kwargs, sig))

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

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtNetwork import QNetworkAccessManager

logger = logging.getLogger(__name__)

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

    Bounded at 8 workers so a click-storm (e.g. mashing the shuffle
    button) can't spawn one thread per click. Anything that doesn't
    fit queues, which is exactly what we want.

    Pool size sits at 8 (not 4) to keep the bulk-download path from
    starving: ``library_sync`` runs *on* this pool while phase 2 fires
    one ``_plan`` job per album onto the same pool. With 4 slots a
    long-running ``sync_library`` plus 3 plannings could shut out the
    first ``_download_track`` job (also pool-scheduled) for minutes.
    Eight slots leaves comfortable headroom for ``_MAX_CONCURRENT=2``
    downloads + several plannings + ``sync_library`` itself, and the
    ``_planning_in_flight`` cap in ``offline.manager`` keeps phase 2
    from flooding the queue past that.
    """
    global _pool
    if _pool is None:
        _pool = QThreadPool()
        _pool.setMaxThreadCount(8)
    return _pool


# ── run_async: blocking callable → GUI-thread callback ──────────────────────


class _Signaler(QObject):
    """Signal carrier for cross-thread completion.

    User callbacks are invoked through the QObject methods
    ``_dispatch_result`` / ``_dispatch_error`` rather than connecting
    them directly to the signals. The reason is subtle: PySide6 wraps
    a plain Python slot in a temporary QObject that lives on the
    thread of the ``.connect()`` call. With the auto-connection rule
    that routes signals to the **slot's** owning thread, a plain
    Python slot connected from a pool worker would route the
    completion event back onto that worker's event loop — and pool
    workers don't have event loops, so the callback would never fire.
    Empirically observed 2026-05-18 as the "phase 2 stuck at 12
    planning_in_flight" library-walk bug. Routing through methods of
    the signaler itself (a QObject we pin to the GUI thread before
    connecting) lets Qt route correctly to the GUI event loop.
    """

    completed = Signal(object)
    failed = Signal(object)

    def __init__(self, on_result=None, on_error=None):
        super().__init__()
        self._on_result = on_result
        self._on_error = on_error

    def _dispatch_result(self, result):  # GUI-thread slot
        if self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:  # noqa: BLE001
                # Keep swallowing — the dispatcher must stay robust — but
                # surface the traceback so a bug in a user on_result
                # callback isn't completely invisible.
                logger.exception("async on_result callback failed")

    def _dispatch_error(self, exc):  # GUI-thread slot
        if self._on_error is not None:
            try:
                self._on_error(exc)
            except Exception:  # noqa: BLE001
                logger.exception("async on_error callback failed")


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
            self._emit(self._signaler.failed, exc)
            return
        self._emit(self._signaler.completed, result)

    @staticmethod
    def _emit(signal, payload) -> None:
        # The signaler's C++ object can be gone by the time a pool worker
        # finishes: at app/interpreter shutdown the QApplication is torn
        # down while this daemon thread is still running its fn, so the
        # emit hits a deleted QObject. Swallow that — the result has
        # nowhere to go and there's nothing to recover. Without this, a
        # job in flight at shutdown spews "RuntimeError: Signal source has
        # been deleted" to stderr (harmless but noisy, and it surfaces a
        # lot under random test order).
        try:
            signal.emit(payload)
        except RuntimeError:
            pass


def run_async(
    fn: Callable[..., Any],
    *args,
    on_result: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
    **kwargs,
) -> None:
    """Run ``fn(*args, **kwargs)`` on the shared pool; dispatch result
    or exception back to the GUI thread.

    Either callback may be omitted. Exceptions raised by ``fn`` are
    routed to ``on_error`` if given, otherwise swallowed silently —
    the caller is responsible for surfacing failures they care about.

    **Caller-thread independence:** the signaler is pinned to the GUI
    thread regardless of which thread called ``run_async``. Without
    this, a call site running on a pool worker (e.g.
    ``library_sync.sync_library`` invoking ``offline.download`` for each
    album) would create the signaler with worker-thread affinity. Qt
    ``AutoConnection`` would then queue the completion event onto that
    worker's event loop — which doesn't exist — and the callback would
    never fire. Symptom: plannings ran but their ``_planned`` callback
    silently dropped, so no track ever dispatched. Pinning the signaler
    to the GUI thread guarantees the callback lands on the GUI event
    loop where the listeners live.
    """
    sig = _Signaler(on_result=on_result, on_error=on_error)
    # Pin signaler to GUI thread before connecting so the QObject-method
    # slots (``_dispatch_result`` / ``_dispatch_error``) route correctly
    # via QueuedConnection back to the GUI event loop, even when the
    # caller is itself on a pool worker (e.g. library_sync phase 2
    # firing per-album ``offline.download`` calls).
    try:
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        if app is not None and sig.thread() is not app.thread():
            sig.moveToThread(app.thread())
    except Exception:
        pass
    _pending_signalers.add(sig)

    def _drop(_=None):
        _pending_signalers.discard(sig)

    sig.completed.connect(sig._dispatch_result)
    sig.failed.connect(sig._dispatch_error)
    sig.completed.connect(_drop)
    sig.failed.connect(_drop)

    get_thread_pool().start(_AsyncTask(fn, args, kwargs, sig))


# ── call_on_gui: run a callback on the GUI thread from any thread ────────────


class _GuiInvoker(QObject):
    """One-shot trampoline that runs a zero-arg callable on the GUI
    thread. Pinned to the GUI thread (same mechanism as ``_Signaler``)
    so a ``fire`` emit from a worker / asyncio-loop thread is delivered
    via QueuedConnection onto the GUI event loop, never inline on the
    caller's thread."""

    fire = Signal(object)

    def _run(self, fn):  # GUI-thread slot
        _pending_signalers.discard(self)
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.exception("call_on_gui callback failed")


def call_on_gui(fn: Callable[[], Any]) -> None:
    """Invoke ``fn()`` on the GUI thread, callable from any thread.

    Use when a callback fires on a non-GUI thread (e.g. snapcast's
    asyncio loop via ``concurrent.futures.Future.add_done_callback``)
    but needs to touch widgets — which must only happen on the GUI
    thread. With no QApplication (headless unit tests) there is no GUI
    event loop to marshal onto, so ``fn`` runs inline on the caller.
    """
    try:
        from PySide6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
    except Exception:  # noqa: BLE001
        app = None
    if app is None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.exception("call_on_gui callback failed (headless)")
        return
    inv = _GuiInvoker()
    if inv.thread() is not app.thread():
        inv.moveToThread(app.thread())
    _pending_signalers.add(inv)
    inv.fire.connect(inv._run)
    inv.fire.emit(fn)

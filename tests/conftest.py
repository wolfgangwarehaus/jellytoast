"""Shared pytest setup for jellytoast tests.

Goals:
- Make `import jellytoast.…` work whether tests are invoked from the repo
  root or from inside `tests/`.
- Redirect QSettings + QStandardPaths to a temp dir so the user's real
  ~/.config/jellytoast/ is never touched by a test run.
- Avoid pulling in heavy Qt subsystems (QApplication, QtWebEngine) — the
  modules under test don't need them.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Under pytest-xdist every worker is a separate PROCESS, but
# QStandardPaths.setTestModeEnabled(True) resolves the test sandbox off
# $HOME (~/.qttest) — so all workers would otherwise share ONE QSettings
# file and race/leak across each other. That collision is the source of
# the core-count-dependent failures in `pytest -n auto` (the CI command):
# more cores -> more workers -> more contention on the shared file. Give
# each worker its own HOME, set BEFORE any QStandardPaths lookup, so its
# test-mode config/data tree is private. A non-xdist run leaves HOME
# untouched and behaves exactly as before.
_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER")
if _xdist_worker:
    import tempfile

    _worker_home = os.path.join(tempfile.gettempdir(), f"jellytoast-test-{_xdist_worker}")
    os.makedirs(_worker_home, exist_ok=True)
    os.environ["HOME"] = _worker_home
    os.environ["XDG_CONFIG_HOME"] = os.path.join(_worker_home, ".config")
    os.environ["XDG_DATA_HOME"] = os.path.join(_worker_home, ".local", "share")
    os.environ["XDG_CACHE_HOME"] = os.path.join(_worker_home, ".cache")

# Tests must never reach the OS secret store. The HOME/XDG split above does
# not move it: on macOS `keyring` talks to the real login Keychain, so a
# `pytest -n auto` run has every worker × every credentials read (which
# retries 5×) hitting Keychain — a locked/missing login keychain turns that
# into an unbounded prompt loop on the user's desktop. python-keyring reads
# this env var at import time; credentials.py imports keyring lazily, so
# setting it here (before any test body runs) is early enough.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

from PySide6.QtCore import QStandardPaths  # noqa: E402

# Per-process redirect: every QStandardPaths.writableLocation(...) call
# now resolves under a tmpfs-style "test mode" path that QtCore picks up,
# isolating QSettings and the queue.json file used by Settings.save_queue.
QStandardPaths.setTestModeEnabled(True)

if sys.platform in ("win32", "darwin"):
    # Windows and macOS need their own QSettings redirect — the HOME/XDG env
    # vars above are Linux-only, and test mode does NOT move the native store.
    # Windows: the default backend IS the registry (NativeFormat) and the qapp
    # fixture sets no organizationName, so a bare QSettings() has NO valid
    # registry path at all: status() latches AccessError and every setValue is
    # silently dropped. That's why e.g. test_switch_family_applies_and_tints_body
    # failed only on Windows — color_tokens persisted the preset palette into a
    # black hole and refresh_theme()'s load_persisted_overrides() re-read
    # nothing, wiping the tint.
    # macOS: NativeFormat is CFPreferences — cfprefsd caches per-domain and
    # writes asynchronously, so a sync() often leaves QSettings.fileName()'s
    # plist nonexistent (test_settings_migration asserted on it) and reads
    # after writes race the daemon; worse, the daemon is keyed to the REAL
    # user, so test writes can leak outside the sandboxed $HOME.
    # Same fix both places: force the INI backend into a per-worker temp dir
    # (mirroring the $HOME split above) and give QSettings a real org/app name.
    import tempfile

    from PySide6.QtCore import QCoreApplication, QSettings

    _qs_dir = os.path.join(
        tempfile.gettempdir(),
        f"jellytoast-test-{_xdist_worker or 'main'}",
        "qsettings",
    )
    # Wipe the previous RUN's leftovers before anything constructs a QSettings:
    # jellytoast modules stamp theme state at import time, so a stale INI from
    # an earlier run would leak into this one before isolated_settings clears.
    import shutil

    shutil.rmtree(_qs_dir, ignore_errors=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, _qs_dir)
    QCoreApplication.setOrganizationName("jellytoast-tests")
    QCoreApplication.setApplicationName("jellytoast-tests")


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """A `Settings` instance whose queue.json lives in `tmp_path`.

    Use this instead of `get_settings()` in tests that exercise
    save_queue/load_queue or any other path that writes to disk.

    Also installs this instance AS the ``get_settings()`` module
    singleton for the duration of the test. Under
    ``setTestModeEnabled(True)`` every ``QSettings`` shares one file, but
    two *different* ``Settings`` objects keep two independent in-memory
    QSettings caches that don't reliably see each other's writes. So a
    test reading via this fixture while production code writes via
    ``get_settings()`` (e.g. ``open_create_smart_playlist._persist``)
    would diverge depending on which test created the singleton first —
    the order-dependent failures pytest-randomly surfaced. Pinning the
    singleton to this same object means there is exactly ONE cache, so
    reader and writer always agree, in any test order. The QSettings
    store is cleared on setup so each test starts known-empty, and
    monkeypatch restores the previous singleton automatically at
    teardown.
    """
    from jellytoast import settings as _settings_mod

    s = _settings_mod.Settings()
    monkeypatch.setattr(s, "_config_dir", tmp_path)
    monkeypatch.setattr(_settings_mod, "_settings", s)
    # Start each test from an empty QSettings store. Under
    # setTestModeEnabled(True) every QSettings shares one process-wide
    # file, so keys written by an earlier test (or via this pinned
    # singleton) would otherwise leak in — clear before yielding so
    # reader and writer always see a coherent, known-empty state.
    s._s.clear()
    s._s.sync()
    return s


@pytest.fixture(scope="session")
def qapp():
    """A process-wide QApplication for tests that build QPixmaps /
    QImages, or instantiate widgets (QLabel + friends in the badge
    tests). QApplication subclasses QGuiApplication, so callers that
    only needed the QImage/QPixmap subsystem keep working unchanged.
    Session-scoped because Qt only allows one application instance per
    process."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QCoreApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def offline_db(tmp_path, monkeypatch):
    """A fresh, empty offline ``downloads.db`` + blob dir rooted in
    ``tmp_path``. Yields the blob-dir path.

    ``db.connect()`` resolves ``locations.db_path()`` unbound precisely
    so a test can redirect it — patch that *and* ``_DOWNLOADS_DIR``,
    and reset the module-global connection on both sides so neither
    this run nor the next sees a stale handle pointing at the shared
    QStandardPaths test-mode DB.
    """
    from jellytoast.offline import db as _db
    from jellytoast.offline import locations as _loc

    monkeypatch.setattr(_loc, "db_path", lambda: tmp_path / "downloads.db")
    monkeypatch.setattr(_loc, "_DOWNLOADS_DIR", tmp_path)

    def _reset_conn():
        if _db._conn is not None:
            try:
                _db._conn.close()
            except Exception:
                pass
            _db._conn = None

    _reset_conn()
    _db.connect()  # runs migrations against the tmp DB
    yield tmp_path
    _reset_conn()


@pytest.fixture(autouse=True)
def _drain_async_and_stop_cast_singletons():
    """Make every test clean up the background work it spun up, so none
    of it leaks into a later test and aborts the process under random
    order.

    Two leak sources, drained in this order (the order matters):

    1. **The shared async_io ``QThreadPool``.** Cast discovery
       (``discover_dlna``/``_sonos``) submits a blocking network sweep
       via ``run_async``; the pool worker outlives the test. We
       ``waitForDone`` so every in-flight worker finishes FIRST — before
       step 2 tears down the asyncio loop those workers are still using
       (stopping the loop mid ``submit_blocking`` segfaults the worker).

    2. **Cast loop/server threads** — the DLNA asyncio loop thread and
       the cast-proxy HTTP server. A live loop/server thread
       torn down (or whose owned objects get GC'd) while an unrelated
       LATER test pumps a Qt event loop aborts the process. Cast tests
       build these singletons but never call ``CastManager.cleanup()``
       (production's teardown), so the threads outlive the test.

    Reads module globals straight out of ``sys.modules`` — never imports
    a module the test didn't already load, never *creates* a singleton —
    so it is a genuine no-op for the ~2000 non-cast tests. After stopping,
    each global is reset to None so the next test builds a fresh instance
    (also fixes cross-test cast-proxy state bleed)."""
    yield
    import gc
    import sys

    # Pin CPython's generational GC OFF for the whole teardown drain. The
    # drain pumps the Qt event loop (step 0) and waits on pool / loop
    # threads (steps 1-2); if an automatic GC pass fires MID Qt
    # event-dispatch it can reap a Qt C++ object while Qt is still
    # delivering an event to it -> a main-thread SIGSEGV inside
    # ``processEvents``. GC scheduling is timing- and interpreter-version
    # sensitive, which is why that crash reproduced only on the 3.11 CI
    # runner, intermittently (~1 push in 3, always at the step-0
    # processEvents). Holding auto-GC off makes the reaping deterministic:
    # we collect ONCE, explicitly, in the finally (step 3) when every
    # thread is quiesced and no event is in flight.
    _gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        aio = sys.modules.get("jellytoast.async_io")

        # 0. Flush deferred Qt callbacks this test scheduled but that never
        #    fired — most importantly a SmartPlaylistEditor's construction-time
        #    ``QTimer.singleShot(0, self._refresh_preview)``. That callback
        #    holds a ref to its widget (so GC can't reap it) and, if unfired,
        #    SURVIVES into a later randomly-ordered test, where it fires against
        #    the real ``run_async`` and spawns a pool worker that builds a real
        #    provider (``get_provider`` → keyring import). GC firing on that
        #    non-GUI worker mid-import then reaps a stale Qt object and the
        #    cross-thread ``~QObject`` SIGSEGVs the worker — crashing whatever
        #    unrelated test happened to be running on it. We pump the event loop
        #    with ``run_async`` neutralised, so any flushed dispatch is a no-op
        #    and the widget becomes collectable; step 3 then reaps it HERE, in
        #    its owning test, instead of leaking the crash into another.
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
        except Exception:
            app = None
        if app is not None and aio is not None:
            real_run_async = getattr(aio, "run_async", None)
            try:
                aio.run_async = lambda *a, **k: None
                for _ in range(3):
                    app.processEvents()
            except Exception:
                pass
            finally:
                if real_run_async is not None:
                    aio.run_async = real_run_async

        # 1. Drain in-flight pool workers before touching the loops they use.
        pool = getattr(aio, "_pool", None) if aio is not None else None
        if pool is not None:
            try:
                pool.waitForDone(15000)
            except Exception:
                pass

        # 2. Stop the long-lived cast loop / server threads.
        for modname, attr, method in (
            ("jellytoast.cast.dlna.controller", "_CONTROLLER", "stop"),
            ("jellytoast.cast_proxy", "_PROXY", "stop"),
        ):
            mod = sys.modules.get(modname)
            if mod is None:
                continue
            inst = getattr(mod, attr, None)
            if inst is None:
                continue
            try:
                getattr(inst, method)()
            except Exception:
                pass
            try:
                setattr(mod, attr, None)
            except Exception:
                pass
    finally:
        # 3. Reap any now-unreferenced Qt objects on THIS (main) thread,
        #    explicitly — now that the loop is quiesced and auto-GC was held
        #    off for the whole drain. This both keeps a later test's pool
        #    worker from tripping a cross-thread ``~QObject`` during its own
        #    GC AND guarantees no automatic collection fired mid
        #    event-dispatch above. Re-enable auto-GC for the next test only
        #    if it was on when we entered (don't clobber a caller that
        #    disabled it deliberately).
        gc.collect()
        if _gc_was_enabled:
            gc.enable()


@pytest.fixture(autouse=True)
def _connectivity_leak_guard(request):
    """Keep ``jellytoast.offline.connectivity``'s module globals from
    leaking across tests on a worker.

    Production API wrappers (jellyfin_api / subsonic) feed the REAL
    ``note_request_failure`` counter on simulated network errors, so any
    two failure-exercising tests whose runtimes span the 4 s hysteresis
    window can trip auto-offline (threshold is 2) — and then every later
    ``LibraryGrid.load_items`` on that worker takes the offline
    short-circuit and renders an EMPTY model. That's the recurring
    3.12-CI flake (2026-06-09: double_load/stale_cache victims;
    2026-06-11: alphabet-rail victims — ``assert ([])``); it never
    reproduced locally in 6 randomized full-suite runs because the trip
    needs CI's slower wall-clock to span the window.

    Two-tier guard, teardown-only (zero setup cost):
    - always: zero the consecutive-failure counter so it can't
      accumulate across unrelated tests;
    - if the offline/unreachable flags actually flipped: restore them,
      stop the auto-probe, and WARN with the culprit's nodeid so the
      polluting test is named in the CI log instead of an innocent
      downstream victim failing.

    Deliberately does NOT call ``_reset_for_tests()`` — that helper also
    installs a fake auto-advancing clock, which would silently change
    burst-immunity behavior for every later connectivity test."""
    yield
    conn = sys.modules.get("jellytoast.offline.connectivity")
    if conn is None:
        return
    try:
        flipped = conn.is_offline_mode() or not conn._server_reachable
        if flipped:
            import warnings

            warnings.warn(
                f"connectivity globals leaked by {request.node.nodeid}: "
                f"offline={conn.is_offline_mode()} "
                f"source={conn._offline_source} "
                f"reachable={conn._server_reachable} "
                f"fails={conn._consecutive_failures} — restored",
                stacklevel=1,
            )
            conn._server_reachable = True
            conn._offline_mode = False
            conn._offline_source = None
            try:
                conn._stop_auto_probe()
            except Exception:
                pass
        conn._consecutive_failures = 0
        conn._first_failure_ts = 0.0
    except Exception:
        pass


def force_sync_render(grid):
    """Pin a ``LibraryGrid``'s item-render signals to ``DirectConnection`` so
    ``emit()`` runs the slot *inline*.

    ``_items_loaded`` / ``_refresh_loaded`` are wired with Qt's default
    ``AutoConnection``, which resolves direct-vs-queued by thread affinity
    **at emit time**. On the GUI thread that's Direct (synchronous) — which is
    what the library_grid tests assume when they assert the model right after
    ``load_items`` (or after draining their hand-driven ``run_async`` /
    ``QTimer`` queues; the cache-hit render emits ``_items_loaded`` straight
    out of ``load_items``). But under ``pytest -n auto`` scheduling the same
    emit can resolve to **QUEUED**, and since these tests never pump the Qt
    event loop the render never lands → the rare ``assert 0 == 150`` flake
    (``test_library_grid_stale_cache_tail``, CI 2026-06-03). Forcing Direct
    makes the render synchronous *by construction*, killing the timing
    dependency rather than papering over it. Production is unaffected: it only
    emits these on the GUI thread, where AutoConnection is already Direct."""
    from PySide6.QtCore import Qt

    for sig, slot in (
        (grid._items_loaded, grid._on_items_loaded),
        (grid._refresh_loaded, grid._on_refresh_loaded),
    ):
        try:
            sig.disconnect(slot)
        except (RuntimeError, TypeError):
            pass
        sig.connect(slot, Qt.ConnectionType.DirectConnection)

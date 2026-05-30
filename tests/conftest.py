"""Shared pytest setup for jellytoast tests.

Goals:
- Make `import modules.…` work whether tests are invoked from the repo
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

from PySide6.QtCore import QStandardPaths  # noqa: E402

# Per-process redirect: every QStandardPaths.writableLocation(...) call
# now resolves under a tmpfs-style "test mode" path that QtCore picks up,
# isolating QSettings and the queue.json file used by Settings.save_queue.
QStandardPaths.setTestModeEnabled(True)


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
    from modules import settings as _settings_mod

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
    from modules.offline import db as _db
    from modules.offline import locations as _loc

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

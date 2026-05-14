"""Shared pytest setup for JellyToast tests.

Goals:
- Make `import modules.…` work whether tests are invoked from the repo
  root or from inside `tests/`.
- Redirect QSettings + QStandardPaths to a temp dir so the user's real
  ~/.config/JellyToast/ is never touched by a test run.
- Avoid pulling in heavy Qt subsystems (QApplication, QtWebEngine) — the
  modules under test don't need them.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
    """
    from modules.settings import Settings

    s = Settings()
    monkeypatch.setattr(s, "_config_dir", tmp_path)
    return s


@pytest.fixture(scope="session")
def qapp():
    """A process-wide QGuiApplication for tests that build QPixmaps /
    QImages. Lighter than QApplication (no widgets subsystem), still
    enough to satisfy the GUI thread requirement of the QImage and
    QPixmap constructors. Session-scoped because Qt only allows one
    application instance per process."""
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication([])
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

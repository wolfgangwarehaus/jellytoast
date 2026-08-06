"""Structured file logging — opt-in, env-driven, rotation-capped.

``install()`` gives the root logger two sinks:

  * a ``RotatingFileHandler`` (~1 MB × 3 backups) under the app data dir —
    ``<AppDataLocation>/logs/jellytoast.log``, the same root ``downloads.db``
    and the view cache live in — so a bug report can carry a bounded, recent
    log instead of an unbounded one (or, as was the case before this module,
    nothing at all once the launch terminal is gone);
  * a console handler pinned at WARNING, so a terminal launch stays quiet
    unless something is actually wrong. The INFO-level standing diagnostics
    (``JT_BOOT_TIMING``, ``JT_COVER_DIAG``, ``JT_BLUR_DIAG``) keep flowing —
    into the FILE, which is where a field report wants them anyway. Setting
    ``JT_LOG`` / ``JT_LOG_LEVEL`` explicitly lowers the console back down, so
    the documented ``JT_LOG_LEVEL=DEBUG jellytoast`` QA workflow is unchanged.

``app.py`` runs ``logging.basicConfig`` at import (long before a QApplication
exists), so a console ``StreamHandler`` is already attached by the time we get
here — we re-level THAT one rather than stacking a second, or every warning
would print twice.

Level comes from ``JT_LOG`` (``debug`` / ``info`` / ``warning`` / ``error``),
falling back to the older ``JT_LOG_LEVEL`` and then ``info``. Nothing is
persisted in Settings: a support request is "relaunch with JT_LOG=debug", not
a toggle hunt. ``app.main()`` calls ``install()`` right after the Qt identity
names are set (so ``AppDataLocation`` resolves to the per-app dir) and before
the heavy boot work, so the log captures the boot it's usually asked about.
Idempotent, best-effort, never fatal.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MAX_BYTES = 1_000_000  # ~1 MB per file …
_BACKUP_COUNT = 3  # … × 3 rotated backups ≈ 4 MB worst-case on disk
_installed = False
_file_path: Path | None = None


def _env_level() -> str:
    """The raw level name the user asked for, or "" when they asked for
    nothing. ``JT_LOG`` is the new spelling; ``JT_LOG_LEVEL`` is the one the
    QA docs and install_doctor already tell people to use."""
    for var in ("JT_LOG", "JT_LOG_LEVEL"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val.upper()
    return ""


def _level_from_env() -> int:
    """Root level from ``JT_LOG`` / ``JT_LOG_LEVEL``, default INFO."""
    return getattr(logging, _env_level() or "INFO", logging.INFO)


def log_dir() -> Path:
    """The app's log directory — ``<AppDataLocation>/logs``. Falls back to a
    hand-built per-identity path when Qt's location resolves without the app
    segment (i.e. before the QApplication names are set)."""
    from PySide6.QtCore import QStandardPaths

    from jellytoast.settings_migration import _QSETTINGS_APP, _QSETTINGS_ORG

    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    p = Path(base) if base else Path.home()
    # Before setApplicationName/setOrganizationName, AppDataLocation is the
    # bare platform root (~/.local/share) — append the identity pair so the
    # logs never land outside the app's own tree.
    if _QSETTINGS_APP not in p.parts:
        p = p / _QSETTINGS_ORG / _QSETTINGS_APP
    return p / "logs"


def log_file_path() -> Path | None:
    """The active log file, or None while ``install()`` hasn't run/succeeded.
    (``jellytoast.diagnostics`` tails this for the support report.)"""
    return _file_path


def install() -> bool:
    """Attach the rotating file handler and pin the console to WARNING.
    Returns True when the file handler landed. Idempotent — a second call is a
    no-op — and never raises (an unwritable app data dir just means no file
    log, never a failed launch)."""
    global _installed, _file_path
    if _installed:
        return _file_path is not None
    _installed = True

    root = logging.getLogger()
    root.setLevel(_level_from_env())

    # Console: re-level basicConfig's handler when it's there, else add one.
    console_level = getattr(logging, _env_level(), logging.WARNING) if _env_level() else logging.WARNING
    existing = [h for h in root.handlers if type(h) is logging.StreamHandler]
    if existing:
        for h in existing:
            h.setLevel(console_level)
    else:
        console = logging.StreamHandler()
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)

    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        _file_path = d / "jellytoast.log"
        fh = RotatingFileHandler(
            _file_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(fh)
        return True
    except Exception:
        _file_path = None
        return False


def open_logs_dir() -> bool:
    """Open the log directory in the platform file manager. Returns False when
    there's nothing to open (install() never ran / no writable dir)."""
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        d = log_dir()
        if not d.is_dir():
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(d))))
    except Exception:
        return False

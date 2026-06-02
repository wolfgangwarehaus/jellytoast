"""Run-once boot migrations: the JellyToast→jellytoast org-name rename
and the nested-AppDataLocation recovery.

Extracted from ``settings.py`` (2026-06-02). Self-contained boot-time
helpers run once from ``Settings.__init__``. ``settings`` re-imports the
names it calls (``_migrate_legacy_org_name`` / ``_recover_nested_appdata``
+ ``_QSETTINGS_ORG`` / ``_QSETTINGS_APP``), so a monkeypatch on
``settings._migrate_legacy_org_name`` still reaches the bare call in
``Settings.__init__``. The keyring entry copy in the org rename uses the
constants that now live in ``credentials``.
"""

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QSettings

from modules.credentials import (
    _KEYRING_SERVICE,
    _KEYRING_USERNAME,
    _LEGACY_KEYRING_SERVICE,
)

logger = logging.getLogger(__name__)


# QSettings org/app names. Pre-2026-05-15 these were "JellyToast" /
# "JellyToast"; the migration helper copies keys forward on first
# launch and sets a marker so it doesn't re-run.
_QSETTINGS_ORG = "jellytoast"
_QSETTINGS_APP = "jellytoast"
_LEGACY_QSETTINGS_ORG = "JellyToast"
_LEGACY_QSETTINGS_APP = "JellyToast"
_MIGRATION_MARKER = "_migrated_to_lowercase"
# Separate marker for the nested-AppDataLocation recovery (2026-05-16).
# The original lowercase migration only renamed the OUTER org directory,
# leaving the INNER app subdir (Qt's two-level org/app layout) at its
# legacy name — so the post-migration app read from an empty path while
# the real data sat one folder over. This recovery runs ONCE regardless
# of whether the legacy migration already fired, so installs that landed
# in the broken intermediate state heal themselves on next launch.
_NESTED_RECOVERY_MARKER = "_nested_appdata_recovered"


def _pick_richer_downloads_db(legacy_db: Path, new_db: Path) -> Path:
    """Return whichever ``downloads.db`` has more rows in the ``nodes``
    table. On a tie or an unreadable file, prefer the legacy one — it's
    the copy the user has been operating on longest, and an unreadable
    new file (most likely a half-initialised SQLite header from the
    "fresh start" the broken app did) shouldn't get to win.

    Counts -1 for unreadable; legacy ≥ new comparison means -1 only
    wins for the new file when the legacy is also unreadable, in which
    case we still return legacy (safe default)."""
    import sqlite3

    def _count(p: Path) -> int:
        try:
            with sqlite3.connect(str(p)) as c:
                row = c.execute("SELECT COUNT(*) FROM nodes").fetchone()
                return int(row[0]) if row else -1
        except Exception:
            return -1

    legacy_n = _count(legacy_db)
    new_n = _count(new_db)
    if legacy_n >= new_n:
        return legacy_db
    return new_db


def _rename_inner_app_subdir(new_root: Path) -> None:
    """Inside ``new_root`` (a lowercase org dir like
    ``~/.local/share/jellytoast/``), rename the legacy inner app subdir
    (``JellyToast/``) to its lowercase form (``jellytoast/``). If only
    the legacy exists, this is a plain move. If both exist, merge:
    moving any entry from legacy that isn't already at the destination,
    with a downloads.db tiebreaker that prefers whichever DB has more
    rows in ``nodes`` (the broken-state app may have created a near-
    empty DB at the new path while the rich one sat at the legacy)."""
    if not new_root.exists():
        return
    legacy_inner = new_root / _LEGACY_QSETTINGS_APP
    new_inner = new_root / _QSETTINGS_APP
    if not legacy_inner.exists() or not legacy_inner.is_dir():
        return
    # Same-dir on case-insensitive filesystems (macOS HFS+ default,
    # Windows). Skip — there's nothing to rename and a move would error.
    try:
        if new_inner.exists() and legacy_inner.samefile(new_inner):
            return
    except OSError:
        pass

    import shutil

    if not new_inner.exists():
        try:
            shutil.move(str(legacy_inner), str(new_inner))
            logger.info("inner-app rename %s → %s", legacy_inner, new_inner)
        except OSError as e:
            logger.warning(
                "inner-app rename %s → %s failed: %s",
                legacy_inner,
                new_inner,
                e,
            )
        return

    # Both exist: merge. First settle the downloads.db tiebreaker so
    # the richer copy ends up at the new path before the generic
    # "skip if exists" loop runs.
    legacy_db = legacy_inner / "downloads.db"
    new_db = new_inner / "downloads.db"
    if legacy_db.exists() and new_db.exists():
        keep = _pick_richer_downloads_db(legacy_db, new_db)
        if keep == legacy_db:
            try:
                new_db.unlink()
                shutil.move(str(legacy_db), str(new_db))
                logger.info(
                    "kept richer downloads.db from legacy inner app dir"
                )
            except OSError as e:
                logger.warning("downloads.db merge failed: %s", e)

    for entry in list(legacy_inner.iterdir()):
        dst = new_inner / entry.name
        if dst.exists():
            continue
        try:
            shutil.move(str(entry), str(dst))
        except OSError as e:
            logger.warning(
                "inner-merge %s → %s failed: %s", entry, dst, e
            )
    # Try cleaning up the legacy inner dir if it's now empty. Leaving
    # a non-empty husk is fine — the user can inspect manually.
    try:
        legacy_inner.rmdir()
    except OSError:
        pass


def _recover_nested_appdata():
    """Run-once recovery for users stuck on the nested-AppDataLocation
    bug. The original lowercase migration left the inner app subdir
    (Qt's two-level ``<org>/<app>/`` layout) at its legacy name, so
    after the org rename the app read from
    ``~/.local/share/jellytoast/jellytoast/`` (empty) while the real
    data sat at ``~/.local/share/jellytoast/JellyToast/``.

    Runs INDEPENDENTLY of ``_MIGRATION_MARKER`` — users who already
    "migrated" but hit this bug need recovery anyway. Sets its own
    marker so it's idempotent."""
    new_qs = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    if new_qs.value(_NESTED_RECOVERY_MARKER, False, type=bool):
        return
    if sys.platform != "linux":
        new_qs.setValue(_NESTED_RECOVERY_MARKER, True)
        new_qs.sync()
        return

    home = Path.home()
    new_config_dir = home / ".config" / _QSETTINGS_ORG
    new_data_dir = home / ".local" / "share" / _QSETTINGS_ORG
    new_cache_dir = home / ".cache" / _QSETTINGS_ORG

    any_legacy_inner = any(
        (root / _LEGACY_QSETTINGS_APP).exists()
        for root in (new_data_dir, new_cache_dir, new_config_dir)
    )
    if any_legacy_inner:
        for root in (new_data_dir, new_cache_dir, new_config_dir):
            _rename_inner_app_subdir(root)
        logger.info("nested-AppDataLocation recovery complete")

    new_qs.setValue(_NESTED_RECOVERY_MARKER, True)
    new_qs.sync()


def _migrate_legacy_org_name():
    """One-shot migration from the legacy CamelCase brand ("JellyToast")
    to the lowercase form ("jellytoast"). Runs once per install at
    Settings construction time and sets ``_MIGRATION_MARKER`` so it
    never repeats.

    Three things move forward:

    1. **QSettings keys** — every key under the old org/app is copied
       to the new org/app (when the new path doesn't already have it).
       The legacy ``JellyToast.conf`` file is left in place as a
       belt-and-braces backup.
    2. **Data + cache directories** — ``~/.local/share/JellyToast/``
       and ``~/.cache/JellyToast/`` are filesystem-moved to their
       lowercase equivalents. Filesystem move (not copy) so a large
       downloads tree doesn't double on disk.
    3. **Keyring entry** — the access token under
       ``keyring.get_password("JellyToast", "access_token")`` is
       copied to the new service name. The old entry is left alone
       so a rollback is non-destructive.

    Linux-only for now — the user base on macOS / Windows is
    effectively zero (the rename pre-dates first ship there). Other
    platforms get a fresh-install experience under the new name."""
    new_qs = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    if new_qs.value(_MIGRATION_MARKER, False, type=bool):
        return
    if sys.platform != "linux":
        # Mark migrated so we don't keep checking on platforms with no
        # legacy footprint. New installs land under the lowercase
        # name from the start.
        new_qs.setValue(_MIGRATION_MARKER, True)
        new_qs.sync()
        return

    old_qs = QSettings(_LEGACY_QSETTINGS_ORG, _LEGACY_QSETTINGS_APP)
    legacy_keys = old_qs.allKeys()

    home = Path.home()
    old_config_dir = home / ".config" / _LEGACY_QSETTINGS_ORG
    new_config_dir = home / ".config" / _QSETTINGS_ORG
    old_data_dir = home / ".local" / "share" / _LEGACY_QSETTINGS_ORG
    new_data_dir = home / ".local" / "share" / _QSETTINGS_ORG
    old_cache_dir = home / ".cache" / _LEGACY_QSETTINGS_ORG
    new_cache_dir = home / ".cache" / _QSETTINGS_ORG

    has_legacy_anything = bool(legacy_keys) or any(
        p.exists() for p in (old_config_dir, old_data_dir, old_cache_dir)
    )
    if not has_legacy_anything:
        new_qs.setValue(_MIGRATION_MARKER, True)
        new_qs.sync()
        return

    # 1. Copy QSettings keys. Don't clobber anything the new path
    #    already has — the user might have started a fresh install
    #    on the new code before noticing the old config was around.
    for k in legacy_keys:
        if not new_qs.contains(k):
            new_qs.setValue(k, old_qs.value(k))

    # 2. Filesystem-move the data + cache trees. Move (not copy) so a
    #    multi-GB downloads/ tree doesn't double on disk.
    import shutil

    for src, dst in (
        (old_data_dir, new_data_dir),
        (old_cache_dir, new_cache_dir),
    ):
        if src.exists() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            except OSError as e:
                logger.warning(
                    "migrate %s → %s failed: %s", src, dst, e
                )

    # 2b. Rename the nested inner app subdir. Qt's AppDataLocation is
    #     ~/.local/share/<org>/<app>/ — a TWO-level layout — so after
    #     moving the outer org dir above, the user's data is still under
    #     a "JellyToast/" subfolder inside the new lowercase root. Rename
    #     that inner subdir too, or the app reads from an empty path.
    for new_root in (new_data_dir, new_cache_dir):
        _rename_inner_app_subdir(new_root)

    # 3. Move other config files (queue.json, scrobble_queue.json, …).
    #    The legacy .conf file stays put as a rollback safety net —
    #    its keys are already in new_qs from step 1.
    if old_config_dir.exists():
        try:
            new_config_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        for entry in old_config_dir.iterdir():
            if entry.name == f"{_LEGACY_QSETTINGS_APP}.conf":
                continue
            dst = new_config_dir / entry.name
            if not dst.exists():
                try:
                    shutil.move(str(entry), str(dst))
                except OSError as e:
                    logger.warning(
                        "migrate %s → %s failed: %s", entry, dst, e
                    )

    # 4. Keyring — copy the access token under the new service name.
    try:
        import keyring

        old_token = keyring.get_password(_LEGACY_KEYRING_SERVICE, _KEYRING_USERNAME)
        if old_token:
            existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
            if not existing:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, old_token)
    except Exception as e:
        logger.warning("keyring migration failed: %s", e)

    new_qs.setValue(_MIGRATION_MARKER, True)
    new_qs.sync()
    logger.info("org-name migration complete (JellyToast → jellytoast)")

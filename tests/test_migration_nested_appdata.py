"""Tests for the nested-AppDataLocation recovery (``_recover_nested_appdata``).

Background: the original ``JellyToast → jellytoast`` migration only
renamed the OUTER org directory under ``~/.local/share/``, leaving the
INNER app subdir (Qt's two-level ``<org>/<app>/`` layout) at its legacy
name. Result: after migrating, the app read from
``~/.local/share/jellytoast/jellytoast/`` (empty) while the user's real
data sat at ``~/.local/share/jellytoast/JellyToast/``.

These tests pin the inner-rename + merge behaviour:

  - clean rename when only the legacy inner exists
  - merge when both exist (richer downloads.db wins; other unique files
    are migrated over)
  - idempotent marker prevents re-run
  - no-op when there's no legacy inner subdir
  - data + cache + config dirs all handled

We monkeypatch ``Path.home`` + ``QSettings`` so each test owns a fresh
fake home and a fresh marker. No Qt event loop is needed.
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from modules import settings as settings_mod
from modules.settings import (
    _LEGACY_QSETTINGS_APP,
    _NESTED_RECOVERY_MARKER,
    _QSETTINGS_APP,
    _QSETTINGS_ORG,
    _pick_richer_downloads_db,
    _recover_nested_appdata,
    _rename_inner_app_subdir,
)


def _seed_downloads_db(path: Path, n_nodes: int) -> None:
    """Write a real SQLite file at ``path`` with a ``nodes`` table holding
    ``n_nodes`` placeholder rows. Mirrors the real schema closely enough
    that ``SELECT COUNT(*) FROM nodes`` returns the expected count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as c:
        c.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, value TEXT)"
        )
        c.executemany(
            "INSERT INTO nodes (id, value) VALUES (?, ?)",
            [(f"n{i}", f"row-{i}") for i in range(n_nodes)],
        )


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Fresh fake home dir per test, plus a fresh QSettings marker store.

    Forces ``sys.platform == "linux"`` so the recovery doesn't take the
    short-circuit on macOS/Windows test runs.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    # Reset the QSettings marker so each test starts with recovery
    # un-fired. Test mode redirects QSettings under tmpfs already, so
    # we just remove the key (clear() would wipe unrelated tests'
    # state in the same xdist worker).
    qs = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    qs.remove(_NESTED_RECOVERY_MARKER)
    qs.sync()
    yield tmp_path
    qs = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    qs.remove(_NESTED_RECOVERY_MARKER)
    qs.sync()


def _data_root(home: Path) -> Path:
    return home / ".local" / "share" / _QSETTINGS_ORG


def _cache_root(home: Path) -> Path:
    return home / ".cache" / _QSETTINGS_ORG


def _config_root(home: Path) -> Path:
    return home / ".config" / _QSETTINGS_ORG


# ── _rename_inner_app_subdir ────────────────────────────────────────────────


def test_rename_when_only_legacy_inner_exists(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    (legacy_inner / "a.txt").write_text("hello")
    _seed_downloads_db(legacy_inner / "downloads.db", n_nodes=5)

    _rename_inner_app_subdir(_data_root(fake_home))

    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    assert new_inner.exists()
    assert not legacy_inner.exists()
    assert (new_inner / "a.txt").read_text() == "hello"
    with sqlite3.connect(str(new_inner / "downloads.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 5


def test_rename_noop_when_no_legacy_inner(fake_home):
    # Only the (currently empty) new dir exists.
    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    new_inner.mkdir(parents=True)
    (new_inner / "x.txt").write_text("preserved")

    _rename_inner_app_subdir(_data_root(fake_home))

    assert (new_inner / "x.txt").read_text() == "preserved"


def test_rename_noop_when_root_missing(fake_home):
    # Should not crash if the org dir doesn't exist yet (fresh install).
    _rename_inner_app_subdir(_data_root(fake_home))
    assert not _data_root(fake_home).exists()


def test_merge_keeps_richer_downloads_db(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    new_inner.mkdir(parents=True)
    _seed_downloads_db(legacy_inner / "downloads.db", n_nodes=5)
    _seed_downloads_db(new_inner / "downloads.db", n_nodes=1)
    # Each side has a unique companion file too, to prove the generic
    # merge runs in addition to the downloads.db tiebreak.
    (legacy_inner / "legacy_only.txt").write_text("L")
    (new_inner / "new_only.txt").write_text("N")

    _rename_inner_app_subdir(_data_root(fake_home))

    with sqlite3.connect(str(new_inner / "downloads.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 5
    assert (new_inner / "legacy_only.txt").read_text() == "L"
    assert (new_inner / "new_only.txt").read_text() == "N"


def test_merge_keeps_new_downloads_db_when_richer(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    new_inner.mkdir(parents=True)
    _seed_downloads_db(legacy_inner / "downloads.db", n_nodes=2)
    _seed_downloads_db(new_inner / "downloads.db", n_nodes=20)

    _rename_inner_app_subdir(_data_root(fake_home))

    with sqlite3.connect(str(new_inner / "downloads.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 20


def test_merge_skips_existing_files_at_destination(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    new_inner.mkdir(parents=True)
    # Same filename, different content. The merge must NOT clobber
    # the new path (it's the authoritative one from the app's view
    # post-migration).
    (legacy_inner / "settings.json").write_text("legacy-content")
    (new_inner / "settings.json").write_text("new-content")

    _rename_inner_app_subdir(_data_root(fake_home))

    assert (new_inner / "settings.json").read_text() == "new-content"


def test_merge_removes_empty_legacy_dir(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    new_inner.mkdir(parents=True)
    (legacy_inner / "moved.txt").write_text("x")

    _rename_inner_app_subdir(_data_root(fake_home))

    assert (new_inner / "moved.txt").exists()
    assert not legacy_inner.exists()


def test_merge_keeps_legacy_dir_when_collision_blocks_move(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    new_inner.mkdir(parents=True)
    # Same-named file on both sides → the legacy copy stays put
    # (skip-if-exists), and the now-non-empty legacy dir survives.
    (legacy_inner / "conflict.txt").write_text("legacy")
    (new_inner / "conflict.txt").write_text("new")

    _rename_inner_app_subdir(_data_root(fake_home))

    assert legacy_inner.exists()
    assert (legacy_inner / "conflict.txt").read_text() == "legacy"


# ── _pick_richer_downloads_db ───────────────────────────────────────────────


def test_pick_richer_db_legacy_wins(fake_home):
    legacy_db = fake_home / "legacy.db"
    new_db = fake_home / "new.db"
    _seed_downloads_db(legacy_db, n_nodes=10)
    _seed_downloads_db(new_db, n_nodes=3)
    assert _pick_richer_downloads_db(legacy_db, new_db) == legacy_db


def test_pick_richer_db_new_wins(fake_home):
    legacy_db = fake_home / "legacy.db"
    new_db = fake_home / "new.db"
    _seed_downloads_db(legacy_db, n_nodes=2)
    _seed_downloads_db(new_db, n_nodes=50)
    assert _pick_richer_downloads_db(legacy_db, new_db) == new_db


def test_pick_richer_db_tie_prefers_legacy(fake_home):
    legacy_db = fake_home / "legacy.db"
    new_db = fake_home / "new.db"
    _seed_downloads_db(legacy_db, n_nodes=7)
    _seed_downloads_db(new_db, n_nodes=7)
    assert _pick_richer_downloads_db(legacy_db, new_db) == legacy_db


def test_pick_richer_db_unreadable_new_prefers_legacy(fake_home):
    legacy_db = fake_home / "legacy.db"
    new_db = fake_home / "new.db"
    _seed_downloads_db(legacy_db, n_nodes=1)
    new_db.write_bytes(b"not a sqlite file at all")
    assert _pick_richer_downloads_db(legacy_db, new_db) == legacy_db


# ── _recover_nested_appdata ─────────────────────────────────────────────────


def test_recover_moves_data_when_only_legacy_inner_exists(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    _seed_downloads_db(legacy_inner / "downloads.db", n_nodes=42)
    (legacy_inner / "queue.json").write_text("[]")

    _recover_nested_appdata()

    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    assert (new_inner / "queue.json").exists()
    with sqlite3.connect(str(new_inner / "downloads.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 42
    assert not legacy_inner.exists()


def test_recover_merges_both_dirs_keeping_richer_db(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    new_inner.mkdir(parents=True)
    _seed_downloads_db(legacy_inner / "downloads.db", n_nodes=5)
    _seed_downloads_db(new_inner / "downloads.db", n_nodes=1)

    _recover_nested_appdata()

    with sqlite3.connect(str(new_inner / "downloads.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 5


def test_recover_marker_prevents_rerun(fake_home):
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    (legacy_inner / "first.txt").write_text("v1")

    _recover_nested_appdata()
    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    assert (new_inner / "first.txt").exists()

    # Simulate a *new* legacy dir appearing after the marker was set
    # (would happen only if someone manually re-created it). The second
    # call must NOT touch it — that's what the marker is for.
    legacy_inner.mkdir(parents=True, exist_ok=True)
    (legacy_inner / "second.txt").write_text("v2")

    _recover_nested_appdata()

    assert (legacy_inner / "second.txt").exists()  # untouched
    assert not (new_inner / "second.txt").exists()


def test_recover_noop_when_no_legacy_inner_anywhere(fake_home):
    # Fresh install: just the lowercase inner dirs, nothing legacy.
    (_data_root(fake_home) / _QSETTINGS_APP).mkdir(parents=True)
    (_cache_root(fake_home) / _QSETTINGS_APP).mkdir(parents=True)

    _recover_nested_appdata()

    # Marker still gets set so we don't keep re-checking.
    qs = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    assert qs.value(_NESTED_RECOVERY_MARKER, False, type=bool) is True


def test_recover_handles_cache_and_config_dirs(fake_home):
    # All three roots have a legacy inner subdir with one file each.
    for root in (
        _data_root(fake_home),
        _cache_root(fake_home),
        _config_root(fake_home),
    ):
        legacy_inner = root / _LEGACY_QSETTINGS_APP
        legacy_inner.mkdir(parents=True)
        (legacy_inner / "marker.txt").write_text(root.name)

    _recover_nested_appdata()

    for root in (
        _data_root(fake_home),
        _cache_root(fake_home),
        _config_root(fake_home),
    ):
        assert (root / _QSETTINGS_APP / "marker.txt").exists()
        assert not (root / _LEGACY_QSETTINGS_APP).exists()


def test_recover_skips_on_non_linux(fake_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    (legacy_inner / "a.txt").write_text("untouched")

    _recover_nested_appdata()

    # macOS path is short-circuited — marker set, filesystem untouched.
    assert (legacy_inner / "a.txt").exists()
    qs = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    assert qs.value(_NESTED_RECOVERY_MARKER, False, type=bool) is True


def test_settings_init_runs_recovery(fake_home, monkeypatch):
    # End-to-end: constructing Settings() fires both the legacy-org
    # migration AND the nested-appdata recovery in order. Stub out the
    # legacy-org migration so we're isolating the new recovery step.
    monkeypatch.setattr(settings_mod, "_migrate_legacy_org_name", lambda: None)

    legacy_inner = _data_root(fake_home) / _LEGACY_QSETTINGS_APP
    legacy_inner.mkdir(parents=True)
    (legacy_inner / "queue.json").write_text("[]")

    settings_mod.Settings()

    new_inner = _data_root(fake_home) / _QSETTINGS_APP
    assert (new_inner / "queue.json").exists()

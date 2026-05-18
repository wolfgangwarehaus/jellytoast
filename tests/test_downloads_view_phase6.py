"""Tests for the Phase 6 DownloadsView wiring.

Covers the three new UI affordances:

- The queue-level pause/resume button — click toggles the offline
  manager flag and the label flips; an externally-fired
  ``download_queue_paused`` bus signal flips it without a click.
- The per-row Re-sync button — kicks ``offline.resync`` through
  ``async_io.run_async``; the in-flight sub-line uses the ACCENT
  treatment, the result re-reads node state from the index.
- The new stale clause in ``_DownloadRow.update_state`` — shows
  "Stale · {size}" with the WARN_FG colour; the unknown-state
  fallthrough still works.

``run_async`` is monkeypatched to run synchronously so we don't have to
spin up a Qt thread pool in unit tests; the production code path is the
same shape (the result callback fires on the GUI thread, our fake fires
it inline).
"""

from __future__ import annotations

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


# Pre-import the view here so ``ui_helpers`` reads the real settings
# once at module load (it caches the active theme at import time).
# Replacing settings with a thin fake before this import explodes on
# missing attributes like ``theme_mode``.
from modules import downloads_view as _dv_mod  # noqa: E402  (intentional ordering)
DownloadsView = _dv_mod.DownloadsView
_DownloadRow = _dv_mod._DownloadRow


@pytest.fixture(autouse=True)
def _reset_manager():
    """The download manager keeps a module-level paused flag — reset
    around each test so order can't leak persistence."""
    from modules.offline import manager as _mgr

    _mgr._reset_for_tests()
    yield
    _mgr._reset_for_tests()


@pytest.fixture
def fake_settings(monkeypatch):
    """Drop-in settings stub for the manager's ``downloads_paused``
    flag. The DownloadsView also reads ``auto_offline_mode``,
    ``prefer_server_when_online``, and ``download_quality`` at
    construction — those are accessed via ``get_settings()`` directly,
    so the stub instance only needs to satisfy the manager. Other
    settings calls fall through to the real (test-mode-isolated) one."""
    import modules.settings as settings_mod

    real_get = settings_mod.get_settings
    real = real_get()

    class _Stub:
        downloads_paused = False

        def __getattr__(self, name):
            return getattr(real, name)

        def __setattr__(self, name, value):
            if name == "downloads_paused":
                object.__setattr__(self, name, value)
            else:
                setattr(real, name, value)

    stub = _Stub()
    monkeypatch.setattr(settings_mod, "get_settings", lambda: stub)
    return stub


@pytest.fixture
def fake_offline(monkeypatch):
    """Stub out the offline package surface DownloadsView reads at
    construction time — ``list_downloads``, ``storage_usage``,
    ``item_size``, ``is_offline_mode``. Tests append node dicts onto
    ``nodes`` to populate the row list."""
    state = {
        "nodes": [],
        "size": 1234,
        "offline_mode": False,
    }

    import modules.offline as offline_pkg

    monkeypatch.setattr(offline_pkg, "list_downloads", lambda kind=None: state["nodes"])
    monkeypatch.setattr(offline_pkg, "storage_usage", lambda: {"total": state["size"]})
    monkeypatch.setattr(offline_pkg, "item_size", lambda item_id: state["size"])
    monkeypatch.setattr(offline_pkg, "is_offline_mode", lambda: state["offline_mode"])
    return state


@pytest.fixture
def sync_run_async(monkeypatch):
    """Run ``run_async(fn, *args, on_result, on_error)`` synchronously
    — invoke ``fn`` inline and route the result / exception to the same
    callbacks the production path uses. Keeps tests deterministic."""

    def _fake_run_async(fn, *args, on_result=None, on_error=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(exc)
            return
        if on_result is not None:
            on_result(result)

    import modules.async_io as async_io_mod

    monkeypatch.setattr(async_io_mod, "run_async", _fake_run_async)
    return _fake_run_async


# ── Pause / Resume button ──────────────────────────────────────────────────


class TestPauseResumeButton:
    def test_click_pauses_and_label_flips(
        self, qapp, fake_settings, fake_offline, sync_run_async, monkeypatch
    ):
        from modules.downloads_view import DownloadsView
        from modules.offline import manager as _mgr

        view = DownloadsView()
        assert view._pause_btn.text() == "Pause downloads"

        # Stub _emit_paused so we don't need a live Qt signal connection
        # — the click handler reads is_paused() directly to flip the label.
        view._pause_btn.click()
        assert _mgr.is_paused() is True
        assert view._pause_btn.text() == "Resume downloads"

    def test_click_when_paused_resumes(
        self, qapp, fake_settings, fake_offline, sync_run_async
    ):
        from modules.downloads_view import DownloadsView
        from modules.offline import manager as _mgr

        _mgr.pause()
        view = DownloadsView()
        assert view._pause_btn.text() == "Resume downloads"

        view._pause_btn.click()
        assert _mgr.is_paused() is False
        assert view._pause_btn.text() == "Pause downloads"

    def test_bus_signal_flips_label_without_click(
        self, qapp, fake_settings, fake_offline, sync_run_async
    ):
        from modules.downloads_view import DownloadsView
        from modules.offline import manager as _mgr
        from modules.player_state import PlayerBus

        view = DownloadsView()
        assert view._pause_btn.text() == "Pause downloads"

        # Flip the flag out-of-band (as if another window or the manager
        # itself paused us), then fire the bus signal the view listens
        # for. The label must follow.
        _mgr._paused_loaded = True
        _mgr._paused = True
        PlayerBus.get().download_queue_paused.emit()
        assert view._pause_btn.text() == "Resume downloads"

        _mgr._paused = False
        PlayerBus.get().download_queue_resumed.emit()
        assert view._pause_btn.text() == "Pause downloads"


# ── Per-row Re-sync button ─────────────────────────────────────────────────


class TestResyncButton:
    def test_click_calls_offline_resync_and_updates_state(
        self, qapp, fake_settings, fake_offline, sync_run_async, monkeypatch
    ):
        fake_offline["nodes"] = [
            {"item_id": "tx1", "kind": "track", "name": "Track One", "state": "complete"}
        ]

        # Patch the underlying snapshot.resync so the package-level
        # offline.resync (which delegates to it) returns the canned
        # success result without a provider round-trip.
        calls = []

        from modules.offline import snapshot as _snap

        def _fake_resync(item_id):
            calls.append(item_id)
            return {
                "updated": True,
                "marked_stale": False,
                "deleted_server_side": False,
                "error": None,
            }

        monkeypatch.setattr(_snap, "resync", _fake_resync)

        # The index lookup post-resync should return a node so the row
        # can refresh into the "complete" state.
        from modules.offline import _index

        monkeypatch.setattr(
            _index,
            "get_node",
            lambda iid: {"item_id": iid, "kind": "track", "state": "complete"},
        )

        from modules.downloads_view import DownloadsView

        view = DownloadsView()
        row = view._rows["tx1"]
        row._resync_btn.click()

        assert calls == ["tx1"]
        # Result handler ran synchronously (sync_run_async) → row is no
        # longer in the resyncing state and the buttons are re-enabled.
        assert row._resyncing is False
        assert row._resync_btn.isEnabled()
        assert row._remove_btn.isEnabled()

    def test_in_flight_sub_line_shows_resyncing(self, qapp, fake_settings, fake_offline):
        from modules.downloads_view import _DownloadRow

        row = _DownloadRow(
            {"item_id": "tx1", "kind": "album", "name": "Some Album", "state": "complete"}
        )
        row.set_resyncing(True)
        assert "Re-syncing" in row._sub.text()
        assert "Album" in row._sub.text()
        assert not row._resync_btn.isEnabled()
        assert not row._remove_btn.isEnabled()

    def test_failure_shows_resync_failed(self, qapp, fake_settings, fake_offline, sync_run_async, monkeypatch):
        fake_offline["nodes"] = [
            {"item_id": "tx1", "kind": "track", "name": "Track One", "state": "complete"}
        ]

        from modules.offline import snapshot as _snap

        monkeypatch.setattr(
            _snap,
            "resync",
            lambda iid: {
                "updated": False,
                "marked_stale": False,
                "deleted_server_side": False,
                "error": "connection refused",
            },
        )

        from modules.downloads_view import DownloadsView

        view = DownloadsView()
        row = view._rows["tx1"]
        row._resync_btn.click()

        assert "Re-sync failed" in row._sub.text()
        assert row._resync_btn.isEnabled()


# ── Stale badge ────────────────────────────────────────────────────────────


class TestStaleBadge:
    def test_update_state_stale_shows_badge(self, qapp, fake_settings, fake_offline):
        from modules.downloads_view import _DownloadRow

        row = _DownloadRow(
            {"item_id": "tx1", "kind": "track", "name": "Track One", "state": "complete"}
        )
        row.update_state("stale", 1.0)
        text = row._sub.text()
        assert "Stale" in text
        # Size string from fake_offline.item_size (1234 bytes -> "1 KB").
        assert "KB" in text or "B" in text

    def test_update_state_unknown_state_does_not_crash(self, qapp, fake_settings, fake_offline):
        from modules.downloads_view import _DownloadRow

        row = _DownloadRow(
            {"item_id": "tx1", "kind": "track", "name": "Track One", "state": "complete"}
        )
        # Anything not in the recognised set falls through to the
        # catch-all — shows what we have, doesn't raise.
        row.update_state("weirdstate", 1.0)
        assert "Track" in row._sub.text()

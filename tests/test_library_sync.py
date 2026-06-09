"""Tests for the library-sync orchestrator.

Covers:

- ``sync_library`` paginates through the provider and enqueues only
  the albums that aren't already downloaded.
- Already-complete albums are skipped (idempotent re-run).
- Last short page terminates the walk.
- Provider failures inside one album don't abort the whole walk.
- ``start_periodic_sync`` / ``stop_periodic_sync`` are idempotent and
  the headless no-op path doesn't crash.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_state():
    from modules.offline import library_sync as ls
    from modules.offline import manager as mgr
    ls._reset_for_tests()
    mgr._reset_for_tests()
    yield
    ls._reset_for_tests()
    mgr._reset_for_tests()


class FakeProvider:
    """Minimal provider stand-in. ``pages`` is a list-of-lists; one
    inner list per ``get_items`` call."""

    def __init__(self, pages: List[List[Dict[str, Any]]]):
        self._pages = pages
        self._idx = 0
        self.calls: List[Dict[str, Any]] = []

    def get_items(self, item_type="", limit=100, start_index=0, **_):
        self.calls.append(
            {"item_type": item_type, "limit": limit, "start_index": start_index}
        )
        if self._idx >= len(self._pages):
            return {"Items": [], "TotalRecordCount": 0}
        page = self._pages[self._idx]
        self._idx += 1
        return {"Items": page, "TotalRecordCount": len(page)}


def _album(item_id: str) -> Dict[str, Any]:
    return {"Id": item_id, "Name": f"Album {item_id}", "Type": "MusicAlbum"}


def _album_with_count(item_id: str, child_count: int) -> Dict[str, Any]:
    return {
        "Id": item_id,
        "Name": f"Album {item_id}",
        "Type": "MusicAlbum",
        "ChildCount": child_count,
    }


def test_walk_enqueues_non_downloaded(monkeypatch):
    from modules.offline import library_sync as ls

    provider = FakeProvider(pages=[[_album("a1"), _album("a2"), _album("a3")]])
    downloaded_ids = {"a2"}
    enqueued: List[str] = []

    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr(
        "modules.offline.is_downloaded", lambda i: i in downloaded_ids
    )
    monkeypatch.setattr(
        "modules.offline.download",
        lambda album: enqueued.append(album["Id"]),
    )

    total, new = ls.sync_library()
    assert total == 3
    assert new == 2
    assert enqueued == ["a1", "a3"]
    # MusicAlbum was the item_type filter on the provider call.
    assert provider.calls[0]["item_type"] == "MusicAlbum"


def test_all_downloaded_walk_clears_session_ghost(qapp, monkeypatch):
    # A walk that registers an expected total (ChildCount) but dispatches
    # nothing — every album already downloaded — must reset the session
    # counters. Otherwise _session_expected_total stays clamping
    # total_session at N forever, and DownloadsView (which only clears on
    # total_session == 0) leaves the aggregate "0 of N" block, the 1 Hz
    # stats timer, and the persisted library_download_in_progress flag
    # stuck for the whole session.
    from modules.offline import library_sync as ls
    from modules.offline import manager as mgr

    # Headless: no real QTimer (mirrors test_offline_manager_stats.no_timer).
    monkeypatch.setattr(mgr, "_ensure_stats_timer", lambda: None)
    monkeypatch.setattr(mgr, "_stop_stats_timer", lambda: None)

    provider = FakeProvider(
        pages=[[
            _album_with_count("a1", 10),
            _album_with_count("a2", 10),
            _album_with_count("a3", 10),
        ]]
    )
    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda _i: True)
    monkeypatch.setattr("modules.offline.download", lambda album: None)

    total, new = ls.sync_library()
    assert (total, new) == (3, 0)
    # Pre-fix: _session_expected_total stays 30 and total_session is
    # clamped at 30. Post-fix the anti-ghost reset zeroes both.
    assert mgr._session_expected_total == 0
    assert mgr.get_queue_stats()[:2] == (0, 0)


def test_partial_walk_keeps_real_session_total(qapp, monkeypatch):
    # The anti-ghost reset must NOT stomp a walk that dispatched real
    # work — only the dispatched-nothing case clears.
    from modules.offline import library_sync as ls
    from modules.offline import manager as mgr

    monkeypatch.setattr(mgr, "_ensure_stats_timer", lambda: None)
    monkeypatch.setattr(mgr, "_stop_stats_timer", lambda: None)

    provider = FakeProvider(
        pages=[[_album_with_count("a1", 10), _album_with_count("a2", 10)]]
    )
    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda i: i == "a1")
    monkeypatch.setattr("modules.offline.download", lambda album: None)

    total, new = ls.sync_library()
    assert (total, new) == (2, 1)
    # a2 was dispatched (enqueued == 1) -> expected total survives.
    assert mgr._session_expected_total == 20


def test_pagination_walks_full_pages_until_short(monkeypatch):
    from modules.offline import library_sync as ls

    full_page = [_album(f"x{i}") for i in range(100)]
    partial_page = [_album("last")]
    provider = FakeProvider(pages=[full_page, partial_page])
    enqueued: List[str] = []

    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda _i: False)
    monkeypatch.setattr(
        "modules.offline.download",
        lambda album: enqueued.append(album["Id"]),
    )

    total, new = ls.sync_library()
    assert total == 101
    assert new == 101
    assert enqueued[-1] == "last"
    # start_index of second call should be PAGE_SIZE.
    assert provider.calls[1]["start_index"] == 100


def test_empty_provider_returns_zeros(monkeypatch):
    from modules.offline import library_sync as ls

    provider = FakeProvider(pages=[])
    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda _i: False)
    monkeypatch.setattr("modules.offline.download", lambda _a: None)

    total, new = ls.sync_library()
    assert total == 0
    assert new == 0


def test_download_failure_skips_one_album_not_walk(monkeypatch):
    from modules.offline import library_sync as ls

    provider = FakeProvider(pages=[[_album("a1"), _album("a2"), _album("a3")]])
    enqueued: List[str] = []

    def _download(album):
        if album["Id"] == "a2":
            raise RuntimeError("simulated provider hiccup")
        enqueued.append(album["Id"])

    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda _i: False)
    monkeypatch.setattr("modules.offline.download", _download)

    total, new = ls.sync_library()
    assert total == 3
    assert new == 2
    assert enqueued == ["a1", "a3"]


def test_on_progress_fires_per_page(monkeypatch):
    from modules.offline import library_sync as ls

    provider = FakeProvider(pages=[[_album(f"a{i}") for i in range(100)],
                                    [_album("a100")]])
    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda _i: False)
    monkeypatch.setattr("modules.offline.download", lambda _a: None)

    seen_args: List[tuple] = []
    ls.sync_library(on_progress=lambda s, e: seen_args.append((s, e)))
    # Two-phase: enumerate fires (seen, 0) once per page during the
    # walk, then one final (seen, enqueued) after the enqueue phase.
    assert seen_args == [(100, 0), (101, 0), (101, 101)]


def test_periodic_sync_lifecycle_headless():
    """In a non-Qt context, start/stop should no-op cleanly."""
    from modules.offline import library_sync as ls

    # No QApplication needed — start_periodic_sync imports QTimer
    # successfully in test env but the headless safety path returns
    # silently if the import fails. Idempotence is the contract.
    ls.start_periodic_sync()
    ls.start_periodic_sync()  # idempotent
    ls.stop_periodic_sync()
    ls.stop_periodic_sync()


def test_init_starts_timer_when_setting_on(monkeypatch):
    from modules.offline import library_sync as ls

    called = {"start": 0}
    monkeypatch.setattr(ls, "start_periodic_sync", lambda: called.__setitem__("start", called["start"] + 1))

    fake_settings = mock.Mock()
    fake_settings.library_sync_enabled = True
    monkeypatch.setattr("modules.settings.get_settings", lambda: fake_settings)

    ls.init()
    assert called["start"] == 1


def test_init_does_not_start_timer_when_setting_off(monkeypatch):
    from modules.offline import library_sync as ls

    called = {"start": 0}
    monkeypatch.setattr(ls, "start_periodic_sync", lambda: called.__setitem__("start", called["start"] + 1))

    fake_settings = mock.Mock()
    fake_settings.library_sync_enabled = False
    monkeypatch.setattr("modules.settings.get_settings", lambda: fake_settings)

    ls.init()
    assert called["start"] == 0


def test_phase2_backpressures_on_planning_in_flight(monkeypatch):
    """Phase 2 must not flood the QThreadPool with planning jobs faster
    than they complete. With FIFO scheduling, an unbacked-pressured loop
    queues N plannings ahead of any ``_download_track`` job and starves
    actual downloads for minutes. The loop checks
    ``manager._planning_in_flight`` and waits whenever it hits the cap."""
    from modules.offline import library_sync as ls
    from modules.offline import manager as mgr

    monkeypatch.setattr(ls, "_PLAN_INFLIGHT_CAP", 3)
    monkeypatch.setattr(ls, "_PLAN_POLL_S", 0.001)

    # 6 albums in one page → cap of 3 means the loop must pause and
    # drain once partway through.
    provider = FakeProvider(pages=[[_album(f"a{i}") for i in range(6)]])
    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda _i: False)

    download_order: List[str] = []
    in_flight_at_call: List[int] = []

    def _fake_download(album):
        # Capture the counter at the moment download() is invoked so we
        # can assert the loop never exceeded the cap.
        in_flight_at_call.append(mgr._planning_in_flight)
        download_order.append(album["Id"])
        # Simulate "planning kicked off" by bumping the counter.
        mgr._planning_in_flight += 1

    monkeypatch.setattr("modules.offline.download", _fake_download)

    # Drive completions from a tiny background thread so the polling
    # loop in phase 2 actually has something to observe.
    import threading
    import time as _time

    stop = threading.Event()

    def _drain():
        while not stop.is_set():
            if mgr._planning_in_flight > 0:
                mgr._planning_in_flight = max(0, mgr._planning_in_flight - 1)
            _time.sleep(0.002)

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    try:
        total, new = ls.sync_library()
    finally:
        stop.set()
        drainer.join(timeout=1.0)
        mgr._planning_in_flight = 0

    assert total == 6
    assert new == 6
    assert download_order == [f"a{i}" for i in range(6)]
    # The cap must never be exceeded at the moment of the download()
    # call — the in-flight count is checked *before* incrementing.
    assert max(in_flight_at_call) < 3, in_flight_at_call


def test_cancel_walk_short_circuits_phase2(monkeypatch):
    """``cancel_walk`` flips a cooperative flag the phase-2 loop polls
    each iteration. After cancel, no more albums get enqueued even
    though the loop hasn't reached the end of ``all_albums``."""
    from modules.offline import library_sync as ls

    provider = FakeProvider(pages=[[_album(f"a{i}") for i in range(10)]])
    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda _i: False)

    enqueued: List[str] = []

    def _fake_download(album):
        enqueued.append(album["Id"])
        # Cancel after the 3rd enqueue so subsequent iterations bail.
        if len(enqueued) == 3:
            ls.cancel_walk()

    monkeypatch.setattr("modules.offline.download", _fake_download)

    total, new = ls.sync_library()
    assert total == 10
    # Only the first 3 made it through before cancel.
    assert new == 3
    assert enqueued == ["a0", "a1", "a2"]


def test_sync_library_resets_cancel_flag_on_entry(monkeypatch):
    """A stale cancel from a previous walk must not poison the next
    one. ``sync_library`` resets the flag at the top."""
    from modules.offline import library_sync as ls

    # Pre-set the cancel flag as if a prior walk had been cancelled.
    ls._cancel_requested = True

    provider = FakeProvider(pages=[[_album("a1"), _album("a2")]])
    monkeypatch.setattr("modules.providers.get_provider", lambda: provider)
    monkeypatch.setattr("modules.offline.is_downloaded", lambda _i: False)
    enqueued: List[str] = []
    monkeypatch.setattr(
        "modules.offline.download", lambda a: enqueued.append(a["Id"])
    )

    total, new = ls.sync_library()
    # Cancel flag should have been reset → walk completed normally.
    assert total == 2
    assert new == 2
    assert enqueued == ["a1", "a2"]
    assert ls._cancel_requested is False

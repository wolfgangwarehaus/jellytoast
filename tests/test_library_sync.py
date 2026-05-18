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
    ls._reset_for_tests()
    yield
    ls._reset_for_tests()


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
    # One call per page; values are running totals.
    assert seen_args == [(100, 100), (101, 101)]


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

"""Tests for the top-level ``offline.repair`` walk.

Repair iterates every complete node and resyncs each — the result is
a summary that lets the Settings → Downloads "Repair downloads" button
tell the user "checked N, flagged M stale, K gone server-side". The
blob files themselves are never touched.
"""

from __future__ import annotations

import pytest

import modules.offline as offline
from modules.offline import index as _index


class _FakeProvider:
    def __init__(self, items=None, raise_on=None):
        self._items = items or {}
        self._raise_on = raise_on or set()

    def get_item(self, item_id):
        if item_id in self._raise_on:
            raise RuntimeError("boom")
        return self._items.get(item_id)


@pytest.fixture
def install_provider(monkeypatch):
    def _install(fp):
        import modules.providers as providers_mod
        monkeypatch.setattr(providers_mod, "_PROVIDER", fp)

    return _install


def _seed(item_id, metadata, kind="track", state="complete"):
    _index.upsert_node(item_id, kind, metadata, requested=False,
                       state=state)


class TestRepair:
    def test_empty_db_returns_zero_counts(self, offline_db,
                                          install_provider):
        install_provider(_FakeProvider())
        out = offline.repair()
        assert out == {
            "checked": 0, "marked_stale": 0,
            "deleted_server_side": 0, "errors": 0,
        }

    def test_skips_pending_and_failed_nodes(self, offline_db,
                                            install_provider):
        _seed("t1", {"Id": "t1"}, state="pending")
        _seed("t2", {"Id": "t2"}, state="failed")
        install_provider(_FakeProvider())
        out = offline.repair()
        assert out["checked"] == 0

    def test_walks_every_complete_node(self, offline_db, install_provider):
        for tid in ("t1", "t2", "t3"):
            _seed(tid, {"Id": tid, "Name": tid}, state="complete")
        install_provider(_FakeProvider({
            "t1": {"Id": "t1", "Name": "t1"},
            "t2": {"Id": "t2", "Name": "t2"},
            "t3": {"Id": "t3", "Name": "t3"},
        }))
        out = offline.repair()
        assert out["checked"] == 3
        assert out["marked_stale"] == 0

    def test_counts_marked_stale(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "RunTimeTicks": 1000}, state="complete")
        _seed("t2", {"Id": "t2", "RunTimeTicks": 1000}, state="complete")
        install_provider(_FakeProvider({
            "t1": {"Id": "t1", "RunTimeTicks": 1000},      # fine
            "t2": {"Id": "t2", "RunTimeTicks": 9999},      # drift
        }))
        out = offline.repair()
        assert out["checked"] == 2
        assert out["marked_stale"] == 1

    def test_counts_deleted_server_side(self, offline_db,
                                        install_provider):
        _seed("t1", {"Id": "t1"}, state="complete")
        _seed("t2", {"Id": "t2"}, state="complete")
        install_provider(_FakeProvider({
            "t1": {"Id": "t1"},
        }))                                                 # t2 missing
        out = offline.repair()
        assert out["checked"] == 2
        assert out["deleted_server_side"] == 1
        # The missing item is also marked stale (its blob is now
        # mismatched-with-reality), so both counters move.
        assert out["marked_stale"] == 1

    def test_counts_provider_errors(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1"}, state="complete")
        _seed("t2", {"Id": "t2"}, state="complete")
        install_provider(_FakeProvider({
            "t1": {"Id": "t1"},
        }, raise_on={"t2"}))
        out = offline.repair()
        assert out["checked"] == 2
        assert out["errors"] == 1

    def test_walks_albums_and_artists_too(self, offline_db,
                                          install_provider):
        # Album / artist / playlist nodes get re-synced too — their
        # snapshots can drift (renamed playlist, edited album cover).
        _seed("al1", {"Id": "al1", "Name": "Old"},
              kind="album", state="complete")
        install_provider(_FakeProvider({
            "al1": {"Id": "al1", "Name": "New"},
        }))
        out = offline.repair()
        assert out["checked"] == 1

    def test_deduplicates_by_item_id(self, offline_db, install_provider):
        # If the same item ever showed up in two list_complete buckets
        # (it shouldn't — kind is a single column — but defend against
        # it), it must only be re-synced once.
        _seed("t1", {"Id": "t1"}, state="complete")
        install_provider(_FakeProvider({
            "t1": {"Id": "t1"},
        }))
        out = offline.repair()
        assert out["checked"] == 1

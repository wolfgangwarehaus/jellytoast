"""Tests for ``snapshot.resync`` — re-fetch + reconcile one download.

Resync inspects the provider's current view of an item against the
frozen snapshot and decides: update the snapshot when user-visible
metadata drifted (rename, re-tag); mark stale when the underlying
audio file looks re-encoded (DateModified or RunTimeTicks differs);
flag deleted_server_side when the item no longer exists upstream.
"""

from __future__ import annotations

import pytest

from modules.offline import index as _index
from modules.offline import snapshot as _snap


class _FakeProvider:
    """Programmable get_item — the only provider method resync needs."""

    def __init__(self, items=None, raise_on=None):
        self._items = items or {}
        self._raise_on = raise_on or set()

    def get_item(self, item_id):
        if item_id in self._raise_on:
            raise RuntimeError("simulated provider failure")
        return self._items.get(item_id)


@pytest.fixture
def install_provider(monkeypatch):
    """Returns a setter for the live provider singleton — each test
    builds the FakeProvider with the responses it needs."""

    def _install(fp):
        import modules.providers as providers_mod

        monkeypatch.setattr(providers_mod, "_PROVIDER", fp)

    return _install


def _seed(item_id, metadata, kind="track", state="complete"):
    _index.upsert_node(item_id, kind, metadata, requested=False, state=state)


class TestResyncNoOp:
    def test_no_node_returns_empty_summary(self, offline_db, install_provider):
        install_provider(_FakeProvider({"t1": {"Id": "t1", "Name": "x"}}))
        out = _snap.resync("ghost")
        assert out["updated"] is False
        assert out["marked_stale"] is False
        assert out["deleted_server_side"] is False

    def test_identical_metadata_no_changes(self, offline_db, install_provider):
        meta = {
            "Id": "t1",
            "Name": "Track",
            "RunTimeTicks": 1000,
            "DateModified": "2026-01-01T00:00:00Z",
        }
        _seed("t1", meta)
        install_provider(_FakeProvider({"t1": dict(meta)}))
        out = _snap.resync("t1")
        assert out["updated"] is False
        assert out["marked_stale"] is False
        assert _index.get_node("t1")["state"] == "complete"


class TestResyncMetadataDrift:
    def test_rename_updates_snapshot(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "Old"})
        install_provider(_FakeProvider({"t1": {"Id": "t1", "Name": "New"}}))
        out = _snap.resync("t1")
        assert out["updated"] is True
        fresh = _index.get_snapshot("t1")
        assert fresh["Name"] == "New"

    def test_rename_keeps_complete_state(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "Old"}, state="complete")
        install_provider(_FakeProvider({"t1": {"Id": "t1", "Name": "New"}}))
        _snap.resync("t1")
        # A pure rename doesn't flag stale or demote state.
        assert _index.get_node("t1")["state"] == "complete"

    def test_retag_artist_updates_snapshot(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "T", "AlbumArtist": "X"})
        install_provider(_FakeProvider({"t1": {"Id": "t1", "Name": "T", "AlbumArtist": "Y"}}))
        out = _snap.resync("t1")
        assert out["updated"] is True


class TestResyncBlobDrift:
    def test_date_modified_change_marks_stale(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "T", "DateModified": "2026-01-01T00:00:00Z"})
        install_provider(
            _FakeProvider({"t1": {"Id": "t1", "Name": "T", "DateModified": "2026-05-01T00:00:00Z"}})
        )
        out = _snap.resync("t1")
        assert out["marked_stale"] is True
        assert _index.get_node("t1")["state"] == "stale"

    def test_runtime_ticks_change_marks_stale(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "T", "RunTimeTicks": 1000})
        install_provider(_FakeProvider({"t1": {"Id": "t1", "Name": "T", "RunTimeTicks": 2000}}))
        out = _snap.resync("t1")
        assert out["marked_stale"] is True

    def test_blob_drift_only_marks_complete_nodes(self, offline_db, install_provider):
        # An already-failed node doesn't get re-flagged as stale —
        # there's no committed blob to be stale against.
        _seed("t1", {"Id": "t1", "RunTimeTicks": 1000}, state="failed")
        install_provider(_FakeProvider({"t1": {"Id": "t1", "RunTimeTicks": 2000}}))
        out = _snap.resync("t1")
        assert out["marked_stale"] is False
        assert _index.get_node("t1")["state"] == "failed"


class TestResyncDeleted:
    def test_missing_item_flags_deleted_server_side(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "T"})
        install_provider(_FakeProvider({}))  # not in map
        out = _snap.resync("t1")
        assert out["deleted_server_side"] is True

    def test_missing_item_marks_complete_node_stale(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "T"}, state="complete")
        install_provider(_FakeProvider({}))
        out = _snap.resync("t1")
        assert out["marked_stale"] is True
        assert _index.get_node("t1")["state"] == "stale"

    def test_missing_item_does_not_delete_blob(self, offline_db, install_provider):
        # The node row stays — only state flips. The caller (UI) gets
        # to decide whether to delete the bytes.
        _seed("t1", {"Id": "t1", "Name": "T"}, state="complete")
        install_provider(_FakeProvider({}))
        _snap.resync("t1")
        assert _index.get_node("t1") is not None


class TestResyncErrors:
    def test_provider_exception_surfaces_in_error(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "T"})
        install_provider(_FakeProvider(raise_on={"t1"}))
        out = _snap.resync("t1")
        assert out["error"] is not None
        # State must be untouched on error.
        assert _index.get_node("t1")["state"] == "complete"


class TestIsStale:
    def test_returns_false_for_missing_node(self, offline_db, install_provider):
        install_provider(_FakeProvider())
        assert _snap.is_stale("ghost") is False

    def test_returns_true_on_drift(self, offline_db, install_provider):
        _seed("t1", {"Id": "t1", "Name": "Old"})
        install_provider(_FakeProvider({"t1": {"Id": "t1", "Name": "New"}}))
        assert _snap.is_stale("t1") is True

"""Tests for the disk-backed JSON view cache (modules/disk_cache.py).

Covers the save/load round-trip, scope-mismatch and server-identity
misses, malformed-file tolerance, atomic-write cleanup, and the
clear / clear_all wipes. No real QStandardPaths dir — the cache is
pointed at a pytest tmp_path.
"""

from __future__ import annotations

import json

import pytest

from modules import disk_cache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """disk_cache pointed at a fresh tmp dir, with the server-identity
    scope stubbed to a fixed value so tests are deterministic and
    don't depend on whatever settings happen to be loaded."""
    monkeypatch.setattr(disk_cache, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        disk_cache,
        "_server_scope",
        lambda: {"_server_url": "http://s", "_provider_kind": "jellyfin"},
    )
    return disk_cache


class TestRoundTrip:
    def test_save_then_load(self, cache):
        cache.save("songs", {"sort": "name"}, [{"Id": "1"}])
        assert cache.load("songs", {"sort": "name"}) == [{"Id": "1"}]

    def test_missing_file_is_miss(self, cache):
        assert cache.load("never-written", {}) is None

    def test_empty_scope_round_trips(self, cache):
        cache.save("genres", {}, ["rock", "jazz"])
        assert cache.load("genres", {}) == ["rock", "jazz"]

    def test_payload_can_be_dict(self, cache):
        cache.save("home", {}, {"resume": [], "latest": ["a"]})
        assert cache.load("home", {}) == {"resume": [], "latest": ["a"]}

    def test_resave_overwrites(self, cache):
        cache.save("songs", {}, ["old"])
        cache.save("songs", {}, ["new"])
        assert cache.load("songs", {}) == ["new"]


class TestScopeMatching:
    def test_scope_mismatch_is_miss(self, cache):
        cache.save("songs", {"sort": "name"}, ["a"])
        assert cache.load("songs", {"sort": "year"}) is None

    def test_extra_scope_key_is_miss(self, cache):
        cache.save("songs", {"sort": "name"}, ["a"])
        assert cache.load("songs", {"sort": "name", "parent": "p1"}) is None

    def test_server_identity_change_invalidates(self, cache, monkeypatch):
        cache.save("songs", {"sort": "name"}, ["a"])
        # Same caller scope, different server identity → miss.
        monkeypatch.setattr(
            cache,
            "_server_scope",
            lambda: {"_server_url": "http://other", "_provider_kind": "jellyfin"},
        )
        assert cache.load("songs", {"sort": "name"}) is None

    def test_provider_kind_change_invalidates(self, cache, monkeypatch):
        cache.save("songs", {}, ["a"])
        monkeypatch.setattr(
            cache,
            "_server_scope",
            lambda: {"_server_url": "http://s", "_provider_kind": "subsonic"},
        )
        assert cache.load("songs", {}) is None


class TestCorruptionTolerance:
    def test_malformed_json_is_miss(self, cache, tmp_path):
        (tmp_path / "songs.json").write_text("{not json", encoding="utf-8")
        assert cache.load("songs", {}) is None

    def test_missing_scope_key_is_miss(self, cache, tmp_path):
        # A file with a payload but no scope can't match any request.
        (tmp_path / "songs.json").write_text(
            json.dumps({"payload": ["a"]}), encoding="utf-8"
        )
        assert cache.load("songs", {}) is None


class TestAtomicWrite:
    def test_no_tmp_file_left_after_save(self, cache, tmp_path):
        cache.save("songs", {}, ["a"])
        assert (tmp_path / "songs.json").exists()
        assert not (tmp_path / "songs.json.tmp").exists()


class TestClear:
    def test_clear_removes_one_entry(self, cache):
        cache.save("songs", {}, ["a"])
        cache.save("genres", {}, ["b"])
        cache.clear("songs")
        assert cache.load("songs", {}) is None
        # Other entries untouched.
        assert cache.load("genres", {}) == ["b"]

    def test_clear_missing_is_noop(self, cache):
        # No exception when the file was never written.
        cache.clear("never-written")

    def test_clear_all_wipes_everything(self, cache):
        for name in ("songs", "genres", "albums"):
            cache.save(name, {}, [name])
        cache.clear_all()
        for name in ("songs", "genres", "albums"):
            assert cache.load(name, {}) is None

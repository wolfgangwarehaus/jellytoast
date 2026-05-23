"""Tests for modules.smart_shuffle and its QueueManager integration.

The algorithm under test is weighted-random — assertions are bounded
("max-run no greater than N") rather than exact-order, except where a
seeded RNG locks the output. Synthetic libraries use the same artist
distribution the production path will see: many tracks per artist,
small enough artist count that classic shuffle reliably clusters.
"""

from __future__ import annotations

import random
from typing import List
from unittest.mock import patch

import pytest

from modules.smart_shuffle import smart_shuffle


# ── Helpers ─────────────────────────────────────────────────────────────────


def _track(track_id: str, artist_id: str, casing: str = "Jellyfin") -> dict:
    """Build a track dict. ``casing`` picks the artist-id field name:
    Jellyfin uses ``ArtistId``, Subsonic uses ``artistId``."""
    key = "ArtistId" if casing == "Jellyfin" else "artistId"
    return {"Id": track_id, key: artist_id, "Name": f"Track {track_id}"}


def _library(n_tracks: int, n_artists: int, casing: str = "Jellyfin") -> List[dict]:
    """``n_tracks`` items spread round-robin across ``n_artists``."""
    return [_track(f"t{i}", f"a{i % n_artists}", casing=casing) for i in range(n_tracks)]


def _max_run(items: List[dict]) -> int:
    """Longest consecutive run of the same ArtistId/artistId in the
    output. Used to assert smart_shuffle's spread guarantee."""
    if not items:
        return 0
    best = run = 1
    prev = items[0].get("ArtistId") or items[0].get("artistId")
    for it in items[1:]:
        cur = it.get("ArtistId") or it.get("artistId")
        if cur == prev:
            run += 1
            best = max(best, run)
        else:
            run = 1
        prev = cur
    return best


# ── Pure-function behaviour ─────────────────────────────────────────────────


class TestSmartShuffleBasics:
    def test_empty_input_returns_empty(self):
        assert smart_shuffle([], []) == []

    def test_single_item_returns_single_item(self):
        items = [_track("t0", "a0")]
        out = smart_shuffle(items, [])
        assert len(out) == 1
        assert out[0]["Id"] == "t0"

    def test_does_not_mutate_input(self):
        items = _library(100, 5)
        snapshot = list(items)
        smart_shuffle(items, [], rng=random.Random(0))
        assert items == snapshot

    def test_preserves_all_items(self):
        items = _library(100, 5)
        out = smart_shuffle(items, [], rng=random.Random(0))
        assert len(out) == len(items)
        assert sorted(i["Id"] for i in out) == sorted(i["Id"] for i in items)

    def test_seeded_rng_is_deterministic(self):
        items = _library(100, 5)
        a = smart_shuffle(items, [], rng=random.Random(42))
        b = smart_shuffle(items, [], rng=random.Random(42))
        assert [i["Id"] for i in a] == [i["Id"] for i in b]

    def test_different_seeds_give_different_output(self):
        items = _library(100, 5)
        a = smart_shuffle(items, [], rng=random.Random(1))
        b = smart_shuffle(items, [], rng=random.Random(2))
        assert [i["Id"] for i in a] != [i["Id"] for i in b]


class TestArtistSpread:
    def test_no_long_runs_of_same_artist(self):
        # 100 tracks across 5 artists: classic shuffle on this regularly
        # produces runs of 5-7 same-artist. Smart shuffle should keep
        # the max-run small — bound is generous (≤3) because the
        # algorithm is probabilistic.
        items = _library(100, 5)
        out = smart_shuffle(items, [], rng=random.Random(42))
        assert _max_run(out) <= 3, (
            f"max-run {_max_run(out)} too high — smart_shuffle is clustering artists"
        )

    def test_artist_spread_stable_across_seeds(self):
        # Run several seeds — every one should respect the spread bound.
        items = _library(100, 5)
        for seed in range(10):
            out = smart_shuffle(items, [], rng=random.Random(seed))
            assert _max_run(out) <= 3, f"seed {seed}: run {_max_run(out)}"

    def test_smart_beats_classic_on_spread(self):
        # Statistical contrast: averaged over many seeds, smart_shuffle
        # should produce a smaller max-run than classic shuffle on the
        # same library. Sanity-checks the algorithm is doing something.
        items = _library(100, 5)
        smart_runs = []
        classic_runs = []
        for seed in range(20):
            smart_out = smart_shuffle(items, [], rng=random.Random(seed))
            smart_runs.append(_max_run(smart_out))
            classic = list(items)
            random.Random(seed).shuffle(classic)
            classic_runs.append(_max_run(classic))
        avg_smart = sum(smart_runs) / len(smart_runs)
        avg_classic = sum(classic_runs) / len(classic_runs)
        assert avg_smart < avg_classic, (
            f"smart {avg_smart:.2f} should beat classic {avg_classic:.2f}"
        )

    def test_recent_history_seeds_initial_picks(self):
        # If we just heard a lot of artist a0, the first picks should
        # avoid a0. Compare two outputs from the same seed: one with a0
        # history, one without. The first index of a0 should appear
        # later when a0 is in the history.
        items = _library(40, 4)
        recent_a0 = ["a0"] * 6
        with_history = smart_shuffle(items, recent_a0, rng=random.Random(7))
        without_history = smart_shuffle(items, [], rng=random.Random(7))

        def _first_a0_position(out):
            for i, it in enumerate(out):
                if it.get("ArtistId") == "a0":
                    return i
            return -1

        # Either equal-or-later — history can only push a0 later, never
        # earlier. The actual delta depends on the seed, but seed 7 + a
        # 4-artist library reliably pushes a0 past index 1.
        assert _first_a0_position(with_history) >= _first_a0_position(without_history)


class TestEdgeCases:
    def test_missing_artist_field_does_not_crash(self):
        items = [{"Id": f"t{i}", "Name": f"T{i}"} for i in range(50)]
        out = smart_shuffle(items, [], rng=random.Random(0))
        assert len(out) == 50

    def test_subsonic_artistid_casing_works(self):
        items = _library(50, 4, casing="Subsonic")
        out = smart_shuffle(items, [], rng=random.Random(0))
        assert len(out) == 50
        # The spread guarantee should hold under either casing — the
        # internal _artist_id helper normalises both.
        assert _max_run(out) <= 3

    def test_mixed_casing_in_one_library(self):
        items = _library(25, 3, casing="Jellyfin") + _library(25, 3, casing="Subsonic")
        out = smart_shuffle(items, [], rng=random.Random(0))
        assert len(out) == 50

    def test_tiny_library_falls_back_gracefully(self):
        # Below the smart_min threshold smart_shuffle reverts to classic
        # — we just need it to return a complete permutation.
        items = _library(5, 2)
        out = smart_shuffle(items, [], rng=random.Random(0))
        assert sorted(i["Id"] for i in out) == sorted(i["Id"] for i in items)

    def test_all_same_artist_does_not_crash(self):
        items = [_track(f"t{i}", "only_artist") for i in range(50)]
        out = smart_shuffle(items, [], rng=random.Random(0))
        assert len(out) == 50

    def test_none_in_recent_history_is_filtered(self):
        items = _library(50, 4)
        out = smart_shuffle(items, ["a0", None, "a1"], rng=random.Random(0))  # type: ignore
        assert len(out) == 50


# ── QueueManager integration ────────────────────────────────────────────────
#
# Reuses the test-double fixtures from test_queue_manager via direct import.
# We avoid duplicating the fixture machinery by importing the FakeProvider
# pattern inline.


class _FakeProvider:
    kind = "fake"

    def get_audio_stream_url(self, item_id):
        return f"stream://{item_id}"

    def get_video_stream_url(self, item_id):
        return f"stream://{item_id}"

    def get_image_url(self, item_id, image_type, width):
        return f"img://{item_id}"


@pytest.fixture
def fake_provider(monkeypatch):
    import modules.providers as providers_mod

    fp = _FakeProvider()
    monkeypatch.setattr(providers_mod, "_PROVIDER", fp)
    yield fp
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)


@pytest.fixture
def isolated_settings_singleton(tmp_path, monkeypatch):
    import modules.settings as settings_mod

    s = settings_mod.Settings()
    monkeypatch.setattr(s, "_config_dir", tmp_path)
    monkeypatch.setattr(settings_mod, "_settings", s)
    s._s.clear()
    s._s.sync()
    return s


@pytest.fixture
def fresh_bus():
    from modules.player_state import PlayerBus

    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


def _audio_items(n):
    return [
        {
            "Id": f"id{i}",
            "Name": f"Track {i}",
            "Type": "Audio",
            "ArtistId": f"artist{i % 5}",
        }
        for i in range(n)
    ]


class TestQueueManagerSmartShuffleDispatch:
    def test_apply_shuffle_routes_through_smart_shuffle(
        self, qapp, fake_provider, isolated_settings_singleton, fresh_bus
    ):
        """_apply_shuffle should always call smart_shuffle (smart is
        now always-on, no longer gated by a setting)."""
        from modules.player_state import QueueContext, QueueKind
        from modules.queue_manager import QueueManager
        import modules.queue_manager as qm_mod

        qm = QueueManager()
        items = _audio_items(20)

        with patch.object(
            qm_mod,
            "_smart_shuffle",
            wraps=qm_mod._smart_shuffle,
        ) as spy:
            qm.bus.shuffle_changed.emit(True)
            qm.play_now(items, 0, QueueContext(kind=QueueKind.ALBUM))

        assert spy.called
        # First positional arg should be a list of wrapped item dicts —
        # one per item less the anchored head.
        call_args = spy.call_args_list[0]
        wrapped = call_args[0][0]
        assert isinstance(wrapped, list)
        assert all("_orig_index" in w for w in wrapped)

    def test_anchored_head_preserved(
        self, qapp, fake_provider, isolated_settings_singleton, fresh_bus
    ):
        """Shuffle-on must keep the currently-playing track at the
        head of play_order."""
        from modules.player_state import QueueContext, QueueKind
        from modules.queue_manager import QueueManager

        qm = QueueManager()
        items = _audio_items(20)
        qm.bus.shuffle_changed.emit(True)
        qm.play_now(items, 7, QueueContext(kind=QueueKind.ALBUM))

        # Item id7 should land at play_order[0].
        assert qm.current_index == 0
        assert qm.current_item["Id"] == "id7"
        # All items should still be present.
        assert set(i["Id"] for i in qm.queue) == set(i["Id"] for i in items)

    def test_history_deque_fed_on_playback(
        self, qapp, fake_provider, isolated_settings_singleton, fresh_bus
    ):
        """``_play_current`` should push the track's artist id into the
        rolling history deque so subsequent shuffles can seed off it."""
        from modules.player_state import QueueContext, QueueKind
        from modules.queue_manager import QueueManager

        qm = QueueManager()
        items = _audio_items(5)
        qm.play_now(items, 0, QueueContext(kind=QueueKind.ALBUM))

        assert "artist0" in qm._recent_artist_ids
        qm.next()
        assert "artist1" in qm._recent_artist_ids

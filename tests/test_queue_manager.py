"""Tests for QueueManager — navigation, peek, shuffle, resume-position.

The Queue dataclass tests in test_player_state.py cover the data layer
(permutations, serialization). These tests cover the controller layer:
how QueueManager walks `play_order`, honors RepeatMode, mutates the
queue under add_next / add_to_end, preserves the playing track across
shuffle toggles, and restores saved position on __init__.

Signals are exercised by capturing emissions on a connected list rather
than running a Qt event loop — direct connections fire synchronously.
"""

from typing import List

import pytest

from jellytoast.player_state import (
    NowPlaying,
    PlayerBus,
    QueueContext,
    QueueKind,
    RepeatMode,
)

# ── Test doubles & fixtures ─────────────────────────────────────────────────


class FakeProvider:
    """Minimum surface QueueManager._build_now_playing exercises."""

    kind = "fake"

    def get_audio_stream_url(self, item_id: str) -> str:
        return f"stream://{item_id}"

    def get_video_stream_url(self, item_id: str) -> str:
        return f"stream://{item_id}"

    def get_image_url(self, item_id: str, image_type: str, width: int) -> str:
        return f"img://{item_id}"


@pytest.fixture
def fake_provider(monkeypatch):
    fp = FakeProvider()
    import jellytoast.providers as providers_mod

    monkeypatch.setattr(providers_mod, "_PROVIDER", fp)
    yield fp
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)


@pytest.fixture
def isolated_settings_singleton(isolated_settings):
    """tmp_path-backed Settings pinned as the get_settings() singleton
    with its QSettings cleared, so save_queue / saved_position_ms /
    shuffle writes don't pollute the real config or leak across tests.
    Delegates to the canonical conftest fixture — see tests/conftest.py."""
    return isolated_settings


@pytest.fixture
def fresh_bus():
    """Ensure each QueueManager test gets a fresh PlayerBus — connected
    slots from prior tests would otherwise still fire."""
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def qm(qapp, fake_provider, isolated_settings_singleton, fresh_bus):
    """A QueueManager wired to fake provider + isolated settings + fresh
    bus. `qapp` provides the QGuiApplication required for QObject signals."""
    from jellytoast.queue_manager import QueueManager

    return QueueManager()


def _items(n, base="id"):
    return [{"Id": f"{base}{i}", "Name": f"Track {i}", "Type": "Audio"} for i in range(n)]


def _capture(signal) -> List:
    """Collect every emission of `signal` into a list. Returns the list."""
    out: List = []
    signal.connect(lambda *args: out.append(args))
    return out


# ── _peek_next_item ─────────────────────────────────────────────────────────


class TestPeekNextItem:
    def test_empty_queue_returns_none(self, qm):
        assert qm._peek_next_item() is None

    def test_off_mid_queue_returns_next(self, qm):
        items = _items(3)
        qm.play_now(items, 0, QueueContext(kind=QueueKind.ALBUM))
        # current is items[0]; next is items[1]
        peeked = qm._peek_next_item()
        assert peeked is not None
        assert peeked["Id"] == "id1"

    def test_off_last_returns_none(self, qm):
        items = _items(3)
        qm.play_now(items, 2, QueueContext(kind=QueueKind.ALBUM))
        assert qm._peek_next_item() is None

    def test_repeat_all_last_wraps_to_first(self, qm):
        items = _items(3)
        qm.play_now(items, 2, QueueContext(kind=QueueKind.ALBUM))
        qm.bus.repeat_changed.emit(RepeatMode.ALL.value)
        peeked = qm._peek_next_item()
        assert peeked is not None
        assert peeked["Id"] == "id0"

    def test_repeat_one_returns_current(self, qm):
        items = _items(3)
        qm.play_now(items, 1, QueueContext(kind=QueueKind.ALBUM))
        qm.bus.repeat_changed.emit(RepeatMode.ONE.value)
        peeked = qm._peek_next_item()
        assert peeked is not None
        # RepeatMode.ONE replays the current track
        assert peeked["Id"] == "id1"

    def test_repeat_one_at_last_still_returns_current(self, qm):
        items = _items(3)
        qm.play_now(items, 2, QueueContext(kind=QueueKind.ALBUM))
        qm.bus.repeat_changed.emit(RepeatMode.ONE.value)
        peeked = qm._peek_next_item()
        assert peeked is not None
        assert peeked["Id"] == "id2"


# ── next() / previous() ─────────────────────────────────────────────────────


class TestNext:
    def test_off_mid_advances_index(self, qm):
        items = _items(3)
        qm.play_now(items, 0, QueueContext(kind=QueueKind.ALBUM))
        qm.next()
        assert qm.current_index == 1
        assert qm.current_item["Id"] == "id1"

    def test_off_last_emits_stop(self, qm):
        items = _items(3)
        qm.play_now(items, 2, QueueContext(kind=QueueKind.ALBUM))
        stops = _capture(qm.bus.stop_requested)
        qm.next()
        assert len(stops) == 1
        # current_index unchanged (the queue stops, doesn't reset)
        assert qm.current_index == 2

    def test_repeat_all_last_wraps_to_zero(self, qm):
        items = _items(3)
        qm.play_now(items, 2, QueueContext(kind=QueueKind.ALBUM))
        qm.bus.repeat_changed.emit(RepeatMode.ALL.value)
        qm.next()
        assert qm.current_index == 0
        assert qm.current_item["Id"] == "id0"

    def test_repeat_one_replays_current(self, qm):
        items = _items(3)
        qm.play_now(items, 1, QueueContext(kind=QueueKind.ALBUM))
        qm.bus.repeat_changed.emit(RepeatMode.ONE.value)
        plays = _capture(qm.bus.play_requested)
        qm.next()
        assert qm.current_index == 1
        assert len(plays) == 1
        np: NowPlaying = plays[0][0]
        assert np.item_id == "id1"


class TestPrevious:
    def test_position_over_3s_seeks_to_zero(self, qm):
        from jellytoast.player_state import set_now_playing

        items = _items(3)
        qm.play_now(items, 1, QueueContext(kind=QueueKind.ALBUM))
        # Mid-track, beyond the seek-vs-prev threshold.
        np = NowPlaying(item_id="id1", position=5000)
        set_now_playing(np)
        seeks = _capture(qm.bus.seek_requested)
        qm.previous()
        # Index unchanged; track restarts via seek_requested(0)
        assert qm.current_index == 1
        assert seeks == [(0,)]

    def test_position_under_3s_steps_back(self, qm):
        from jellytoast.player_state import set_now_playing

        items = _items(3)
        qm.play_now(items, 2, QueueContext(kind=QueueKind.ALBUM))
        np = NowPlaying(item_id="id2", position=1000)
        set_now_playing(np)
        qm.previous()
        assert qm.current_index == 1

    def test_first_off_seeks_to_zero(self, qm):
        from jellytoast.player_state import set_now_playing

        items = _items(3)
        qm.play_now(items, 0, QueueContext(kind=QueueKind.ALBUM))
        np = NowPlaying(item_id="id0", position=500)
        set_now_playing(np)
        seeks = _capture(qm.bus.seek_requested)
        qm.previous()
        assert qm.current_index == 0
        assert seeks == [(0,)]

    def test_first_repeat_all_wraps_to_last(self, qm):
        from jellytoast.player_state import set_now_playing

        items = _items(3)
        qm.play_now(items, 0, QueueContext(kind=QueueKind.ALBUM))
        qm.bus.repeat_changed.emit(RepeatMode.ALL.value)
        np = NowPlaying(item_id="id0", position=500)
        set_now_playing(np)
        qm.previous()
        assert qm.current_index == 2


# ── play_now / add_next / add_to_end ────────────────────────────────────────


class TestPlayNow:
    def test_installs_queue_and_emits_play(self, qm):
        items = _items(3)
        plays = _capture(qm.bus.play_requested)
        qm.play_now(items, 1, QueueContext(kind=QueueKind.ALBUM))
        assert qm.current_index == 1
        assert qm.current_item["Id"] == "id1"
        assert len(plays) == 1
        assert plays[0][0].item_id == "id1"

    def test_empty_items_noops(self, qm):
        plays = _capture(qm.bus.play_requested)
        qm.play_now([], 0, QueueContext(kind=QueueKind.ALBUM))
        assert plays == []
        assert qm.current_item is None

    def test_clamps_out_of_range_start(self, qm):
        items = _items(3)
        qm.play_now(items, 99, QueueContext(kind=QueueKind.ALBUM))
        # Clamped to last index, not crashed
        assert qm.current_index == 2

    def test_shuffle_on_keeps_start_at_head(self, qm):
        # With shuffle enabled going in, play_now's keep_at_start should
        # put the requested item at play_order[0] regardless of start_index.
        qm.bus.shuffle_changed.emit(True)
        items = _items(8)
        qm.play_now(items, 5, QueueContext(kind=QueueKind.ALBUM))
        assert qm.current_index == 0
        # Item id5 should be the one at play_order[0]
        assert qm.current_item["Id"] == "id5"


class TestAddNext:
    def test_on_empty_becomes_manual_queue(self, qm):
        items = _items(2)
        qm.add_next(items)
        assert qm.context.kind is QueueKind.MANUAL
        assert qm.queue[0]["Id"] == "id0"

    def test_inserts_after_current_index(self, qm):
        existing = _items(3)
        qm.play_now(existing, 0, QueueContext(kind=QueueKind.ALBUM))
        qm.add_next(_items(2, base="new"))
        # Order in play_ordered: id0, new0, new1, id1, id2
        ids = [i["Id"] for i in qm.queue]
        assert ids == ["id0", "new0", "new1", "id1", "id2"]
        # current_index unchanged — the active track is still id0
        assert qm.current_index == 0


class TestAddToEnd:
    def test_appends(self, qm):
        existing = _items(3)
        qm.play_now(existing, 0, QueueContext(kind=QueueKind.ALBUM))
        qm.add_to_end(_items(2, base="new"))
        ids = [i["Id"] for i in qm.queue]
        assert ids == ["id0", "id1", "id2", "new0", "new1"]


class TestClear:
    def test_empties_queue(self, qm):
        qm.play_now(_items(3), 0, QueueContext(kind=QueueKind.ALBUM))
        qm.clear()
        assert qm.current_item is None
        assert qm.queue == []

    def test_clear_resets_now_playing(self, qm):
        # Clearing the queue (e.g. on sign-out) must stop playback and reset
        # the global NowPlaying so a poller of get_now_playing() doesn't read
        # the previously-playing track.
        from jellytoast.player_state import get_now_playing

        qm.play_now(_items(3), 0, QueueContext(kind=QueueKind.ALBUM))
        assert get_now_playing().item_id == "id0"
        stops = _capture(qm.bus.stop_requested)
        qm.clear()
        assert get_now_playing().item_id == ""
        assert qm.current_item is None
        assert len(stops) == 1


# ── Shuffle ─────────────────────────────────────────────────────────────────


class TestShuffleToggle:
    def test_on_keeps_current_at_head(self, qm):
        items = _items(10)
        qm.play_now(items, 4, QueueContext(kind=QueueKind.ALBUM))
        qm.bus.shuffle_changed.emit(True)
        # Current track must be the same after shuffle, at index 0
        assert qm.current_item["Id"] == "id4"
        assert qm.current_index == 0
        # All original items still present in play_ordered
        assert set(i["Id"] for i in qm.queue) == set(i["Id"] for i in items)

    def test_off_restores_sequential_order(self, qm):
        items = _items(10)
        qm.play_now(items, 4, QueueContext(kind=QueueKind.ALBUM))
        qm.bus.shuffle_changed.emit(True)
        qm.bus.shuffle_changed.emit(False)
        # Sequential order restored
        ids = [i["Id"] for i in qm.queue]
        assert ids == [f"id{i}" for i in range(10)]
        # Current track preserved at its original position
        assert qm.current_item["Id"] == "id4"
        assert qm.current_index == 4


# ── Resume on __init__ ──────────────────────────────────────────────────────
#
# Constructed via the same pattern as the `qm` fixture but with saved-queue
# and saved-position state pre-written to settings, so we can assert on the
# restore behavior.


class TestResumeOnInit:
    def _save_album_queue(self, settings, items, start_index=0):
        """Persist `items` as a v2 album-context queue at start_index.
        This mirrors what QueueManager._save would write after play_now."""
        from jellytoast.player_state import Queue

        q = Queue(
            context=QueueContext(kind=QueueKind.ALBUM, source_id="album1"),
            original_items=items,
            play_order=list(range(len(items))),
            current_index=start_index,
        )
        settings.save_queue(q)

    def test_no_saved_queue_does_nothing(
        self, qapp, fake_provider, isolated_settings_singleton, fresh_bus
    ):
        from jellytoast.queue_manager import QueueManager

        qm = QueueManager()
        assert qm.current_item is None

    def test_saved_queue_loads_without_position(
        self, qapp, fake_provider, isolated_settings_singleton, fresh_bus
    ):
        items = _items(3)
        self._save_album_queue(isolated_settings_singleton, items, start_index=1)
        from jellytoast.queue_manager import QueueManager

        qm = QueueManager()
        # Queue restored, but no playback_restored emit since no saved
        # position. current_item should still be valid.
        assert qm.current_item is not None
        assert qm.current_item["Id"] == "id1"

    def test_matching_saved_position_emits_restored(
        self, qapp, fake_provider, isolated_settings_singleton, fresh_bus
    ):
        items = _items(3)
        self._save_album_queue(isolated_settings_singleton, items, start_index=1)
        isolated_settings_singleton.saved_position_ms = 27488
        isolated_settings_singleton.saved_position_item_id = "id1"

        # Subscribe to playback_restored BEFORE constructing the manager
        # (the manager defers the emit via QTimer.singleShot(0)).
        from jellytoast.queue_manager import QueueManager

        bus = PlayerBus.get()
        restored = _capture(bus.playback_restored)

        qm = QueueManager()  # noqa: F841 — kept alive for the timer to fire

        # Drain the Qt event loop once so the deferred emit lands.
        qapp.processEvents()

        assert len(restored) == 1
        np: NowPlaying = restored[0][0]
        assert np.item_id == "id1"
        assert np.position == 27488
        assert np.is_paused is True

    def test_mismatched_saved_position_id_skips_restore(
        self, qapp, fake_provider, isolated_settings_singleton, fresh_bus
    ):
        items = _items(3)
        self._save_album_queue(isolated_settings_singleton, items, start_index=1)
        # Saved position belongs to a track that's no longer the queue's
        # current item — guard against starting a different track mid-way
        # through.
        isolated_settings_singleton.saved_position_ms = 27488
        isolated_settings_singleton.saved_position_item_id = "id99"

        from jellytoast.queue_manager import QueueManager

        bus = PlayerBus.get()
        restored = _capture(bus.playback_restored)

        qm = QueueManager()  # noqa: F841
        qapp.processEvents()

        # No restore emission — the id mismatch is the safety gate
        assert restored == []


# ── _audio_stream_url — offline-aware URL gating (Phase 4) ───────────────────


class _FakeBlob:
    """Stand-in for offline._store.Blob — only exists() / as_uri() are
    exercised by QueueManager._audio_stream_url."""

    def __init__(self, exists: bool = True, uri: str = "file:///blobs/t1.flac"):
        self._exists = exists
        self._uri = uri

    def exists(self) -> bool:
        return self._exists

    def as_uri(self) -> str:
        return self._uri


class TestAudioStreamUrl:
    """QueueManager prefers a downloaded local blob over the server
    stream — gated by offline mode, server reachability, and the
    prefer_server_when_online setting (design doc §5.4)."""

    def _patch_offline(self, monkeypatch, *, blob, offline_mode=False, server_reachable=True):
        import jellytoast.offline as off

        monkeypatch.setattr(off, "local_blob", lambda _id: blob)
        monkeypatch.setattr(off, "is_offline_mode", lambda: offline_mode)
        monkeypatch.setattr(off, "is_server_reachable", lambda: server_reachable)

    def test_not_downloaded_uses_server(self, qm, monkeypatch):
        self._patch_offline(monkeypatch, blob=None)
        url, is_local = qm._audio_stream_url("t1")
        assert url == "stream://t1"
        assert is_local is False

    def test_blob_with_missing_file_uses_server(self, qm, monkeypatch):
        self._patch_offline(monkeypatch, blob=_FakeBlob(exists=False))
        url, is_local = qm._audio_stream_url("t1")
        assert url == "stream://t1"
        assert is_local is False

    def test_offline_mode_forces_local_even_with_prefer_server(self, qm, monkeypatch):
        qm.settings.prefer_server_when_online = True
        self._patch_offline(monkeypatch, blob=_FakeBlob(), offline_mode=True)
        url, is_local = qm._audio_stream_url("t1")
        assert url == "file:///blobs/t1.flac"
        assert is_local is True

    def test_default_prefers_local_when_online(self, qm, monkeypatch):
        # prefer_server_when_online defaults False → local wins.
        self._patch_offline(monkeypatch, blob=_FakeBlob(), server_reachable=True)
        url, is_local = qm._audio_stream_url("t1")
        assert url == "file:///blobs/t1.flac"
        assert is_local is True

    def test_prefer_server_when_online_and_reachable_uses_server(self, qm, monkeypatch):
        qm.settings.prefer_server_when_online = True
        self._patch_offline(
            monkeypatch, blob=_FakeBlob(), offline_mode=False, server_reachable=True
        )
        url, is_local = qm._audio_stream_url("t1")
        assert url == "stream://t1"
        assert is_local is False

    def test_prefer_server_but_unreachable_falls_back_to_local(self, qm, monkeypatch):
        qm.settings.prefer_server_when_online = True
        self._patch_offline(
            monkeypatch, blob=_FakeBlob(), offline_mode=False, server_reachable=False
        )
        url, is_local = qm._audio_stream_url("t1")
        assert url == "file:///blobs/t1.flac"
        assert is_local is True

    def test_offline_lookup_error_falls_back_to_server(self, qm, monkeypatch):
        import jellytoast.offline as off

        def _boom(_id):
            raise RuntimeError("offline db not ready")

        monkeypatch.setattr(off, "local_blob", _boom)
        url, is_local = qm._audio_stream_url("t1")
        assert url == "stream://t1"
        assert is_local is False

    def test_build_now_playing_propagates_is_local(self, qm, monkeypatch):
        self._patch_offline(monkeypatch, blob=_FakeBlob(), offline_mode=True)
        np = qm._build_now_playing({"Id": "t1", "Name": "Track", "Type": "Audio"})
        assert np.is_local is True
        assert np.stream_url == "file:///blobs/t1.flac"

    def test_build_now_playing_is_local_false_when_streaming(self, qm, monkeypatch):
        self._patch_offline(monkeypatch, blob=None)
        np = qm._build_now_playing({"Id": "t1", "Name": "Track", "Type": "Audio"})
        assert np.is_local is False
        assert np.stream_url == "stream://t1"


class TestRadioEmbeddedStream:
    """Internet-radio stations carry the live HTTP/Icecast/HLS URL
    inline on the queue item (no provider round-trip to resolve it);
    QueueManager._build_now_playing must honour it and skip the
    offline-blob lookup entirely (no local copy of a live feed)."""

    def test_embedded_stream_url_used_directly(self, qm):
        np = qm._build_now_playing(
            {
                "Id": "station-1",
                "Name": "SomaFM Groove Salad",
                "Type": "Audio",
                "streamUrl": "https://ice2.somafm.com/groovesalad-128-aac",
                "RunTimeTicks": 0,
            }
        )
        assert np.stream_url == "https://ice2.somafm.com/groovesalad-128-aac"
        assert np.is_local is False
        assert np.duration == 0

    def test_embedded_stream_skips_offline_lookup(self, qm, monkeypatch):
        # If the offline lookup ran for a radio item it would either
        # raise (no DB) or return None — either way the streamUrl would
        # be lost in favour of get_audio_stream_url. Verify the lookup
        # is bypassed by raising on it.
        import jellytoast.offline as off

        def _boom(_id):
            raise AssertionError("offline lookup must not run for radio items")

        monkeypatch.setattr(off, "local_blob", _boom)
        np = qm._build_now_playing(
            {
                "Id": "station-1",
                "Name": "Radio",
                "Type": "Audio",
                "streamUrl": "https://example.com/stream",
            }
        )
        assert np.stream_url == "https://example.com/stream"

    def test_embedded_stream_clears_thumb_url(self, qm):
        # Stations don't have a cover-art id; the thumb URL must come
        # back empty so the bar / mini player fall through to their
        # placeholder instead of asking the provider for art at the
        # station's id (which would 404).
        np = qm._build_now_playing(
            {
                "Id": "station-1",
                "Name": "Radio",
                "Type": "Audio",
                "streamUrl": "https://example.com/stream",
            }
        )
        assert np.thumb_url == ""
        assert np.image_id == ""


# ── remove_at — queue item removal + index reindexing ───────────────────────
# Backs the right-click "remove from queue" action. Zero prior coverage; the
# play_order reindex (`p > orig_idx` shift) and current_index adjustment are
# exactly the off-by-one arithmetic a refactor breaks silently.


class TestRemoveAt:
    def test_remove_before_current_keeps_track_decrements_index(self, qm):
        qm.play_now(_items(4), 2, QueueContext(kind=QueueKind.ALBUM))
        assert qm.current_item["Id"] == "id2"
        qm.remove_at(0)  # drop id0, ahead of current
        # Same track still playing, index shifted down by one.
        assert qm.current_item["Id"] == "id2"
        assert qm.current_index == 1
        assert [i["Id"] for i in qm.queue] == ["id1", "id2", "id3"]

    def test_remove_current_advances_to_next(self, qm):
        qm.play_now(_items(4), 1, QueueContext(kind=QueueKind.ALBUM))
        plays = _capture(qm.bus.play_requested)
        qm.remove_at(1)  # drop the playing track (id1)
        # Lands on what was the next track and requests playback of it.
        assert qm.current_item["Id"] == "id2"
        assert plays  # play_requested fired for the new current
        assert [i["Id"] for i in qm.queue] == ["id0", "id2", "id3"]

    def test_remove_current_when_last_stops(self, qm):
        qm.play_now(_items(3), 2, QueueContext(kind=QueueKind.ALBUM))
        stops = _capture(qm.bus.stop_requested)
        qm.remove_at(2)  # drop the playing track, which is the tail
        assert qm.current_index == -1
        assert stops  # stop_requested fired — nothing left to advance to
        assert [i["Id"] for i in qm.queue] == ["id0", "id1"]

    def test_remove_last_current_clears_now_playing(self, qm):
        # Removing the playing tail track stops playback; the global
        # NowPlaying must clear too so get_now_playing() doesn't return it.
        from jellytoast.player_state import get_now_playing

        qm.play_now(_items(3), 2, QueueContext(kind=QueueKind.ALBUM))
        assert get_now_playing().item_id == "id2"
        qm.remove_at(2)
        assert qm.current_index == -1
        assert get_now_playing().item_id == ""

    def test_remove_out_of_range_is_noop(self, qm):
        qm.play_now(_items(3), 0, QueueContext(kind=QueueKind.ALBUM))
        qm.remove_at(99)
        assert [i["Id"] for i in qm.queue] == ["id0", "id1", "id2"]
        assert qm.current_index == 0


# ── move_item — drag-to-reorder + current-track survival ────────────────────
# Backs the now-playing page's drag-reorder. Zero prior coverage; current_index
# is recomputed by identity (play_order.index(cur_orig)) so playback follows
# the moved/displaced track instead of jumping.


class TestMoveItem:
    def test_move_other_item_keeps_current_track(self, qm):
        qm.play_now(_items(4), 0, QueueContext(kind=QueueKind.ALBUM))
        qm.move_item(2, 0)  # pull id2 to the front
        assert qm.current_item["Id"] == "id0"  # still playing id0
        assert [i["Id"] for i in qm.queue] == ["id2", "id0", "id1", "id3"]

    def test_move_current_item_follows_it(self, qm):
        qm.play_now(_items(4), 0, QueueContext(kind=QueueKind.ALBUM))
        qm.move_item(0, 2)  # drag the playing track back two slots
        assert qm.current_item["Id"] == "id0"  # playback follows the move
        assert qm.current_index == 2
        assert [i["Id"] for i in qm.queue] == ["id1", "id2", "id0", "id3"]

    def test_move_noop_guards(self, qm):
        qm.play_now(_items(3), 0, QueueContext(kind=QueueKind.ALBUM))
        before = [i["Id"] for i in qm.queue]
        qm.move_item(1, 1)  # src == dest
        qm.move_item(0, 99)  # dest out of range
        assert [i["Id"] for i in qm.queue] == before
        assert qm.current_index == 0

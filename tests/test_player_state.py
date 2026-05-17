"""Tests for the Queue / QueueContext / NowPlaying dataclasses.

These are pure-logic; no Qt event loop, no network. The Queue model is
the foundation of the Strawberry-style permutation invariants — if these
break, shuffle/repeat/now-playing all break.
"""

from modules.player_state import (
    NowPlaying,
    Queue,
    QueueContext,
    QueueKind,
)


def _items(n):
    return [{"Id": f"id{i}", "Name": f"Track {i}"} for i in range(n)]


# ── QueueContext ────────────────────────────────────────────────────────────


class TestQueueContext:
    def test_default_is_manual(self):
        ctx = QueueContext()
        assert ctx.kind == QueueKind.MANUAL
        assert ctx.source_id == ""
        assert ctx.source_label == ""
        assert ctx.source_icon == ""

    def test_explicit_album(self):
        ctx = QueueContext(
            kind=QueueKind.ALBUM,
            source_id="a1",
            source_label="Kid A",
            source_icon="http://x/y.jpg",
        )
        assert ctx.kind is QueueKind.ALBUM
        assert ctx.source_label == "Kid A"


# ── Queue ───────────────────────────────────────────────────────────────────


class TestQueue:
    def test_empty(self):
        q = Queue()
        assert q.is_empty
        assert q.length == 0
        assert q.current_item is None
        assert q.play_ordered() == []

    def test_play_ordered_respects_permutation(self):
        items = _items(4)
        q = Queue(
            original_items=items,
            play_order=[2, 0, 3, 1],
            current_index=0,
        )
        assert q.length == 4
        assert q.current_item == items[2]
        names = [it["Name"] for it in q.play_ordered()]
        assert names == ["Track 2", "Track 0", "Track 3", "Track 1"]

    def test_current_item_walks_play_order(self):
        items = _items(3)
        q = Queue(
            original_items=items,
            play_order=[1, 2, 0],
            current_index=2,
        )
        # play_order[2] = 0 → original_items[0]
        assert q.current_item == items[0]

    def test_current_item_out_of_range_returns_none(self):
        q = Queue(
            original_items=_items(2),
            play_order=[0, 1],
            current_index=99,
        )
        assert q.current_item is None

    def test_play_ordered_filters_invalid_indices(self):
        # If play_order somehow has stale indices (eg. items were dropped
        # from original_items), play_ordered shouldn't crash — just skip.
        q = Queue(
            original_items=_items(2),
            play_order=[0, 5, 1],
            current_index=0,
        )
        assert len(q.play_ordered()) == 2


class TestQueueRoundTrip:
    def test_to_from_dict(self):
        items = _items(3)
        ctx = QueueContext(
            kind=QueueKind.ALBUM,
            source_id="alb1",
            source_label="Foo",
        )
        original = Queue(
            context=ctx,
            original_items=items,
            play_order=[2, 0, 1],
            current_index=1,
            manual_overlay=[{"Id": "overlay1"}],
        )
        restored = Queue.from_dict(original.to_dict())
        assert restored.context.kind is QueueKind.ALBUM
        assert restored.context.source_id == "alb1"
        assert restored.original_items == items
        assert restored.play_order == [2, 0, 1]
        assert restored.current_index == 1
        assert restored.manual_overlay == [{"Id": "overlay1"}]

    def test_from_dict_unknown_kind_falls_back_to_manual(self):
        # Forward-compat: a future QueueKind in the file shouldn't crash
        # an older client; treat it as MANUAL.
        data = {
            "context": {
                "kind": "telepathic",
                "source_id": "",
                "source_label": "",
                "source_icon": "",
            },
            "original_items": _items(2),
            "play_order": [0, 1],
            "current_index": 0,
            "manual_overlay": [],
        }
        q = Queue.from_dict(data)
        assert q.context.kind is QueueKind.MANUAL

    def test_from_dict_normalises_out_of_range_index(self):
        data = {
            "context": {"kind": "manual", "source_id": "", "source_label": "", "source_icon": ""},
            "original_items": _items(2),
            "play_order": [0, 1],
            "current_index": 99,
        }
        q = Queue.from_dict(data)
        assert q.current_index == -1

    def test_from_dict_default_play_order_is_sequential(self):
        data = {
            "context": {"kind": "album", "source_id": "", "source_label": "", "source_icon": ""},
            "original_items": _items(3),
            # play_order intentionally missing
            "current_index": 0,
        }
        q = Queue.from_dict(data)
        assert q.play_order == [0, 1, 2]


# ── NowPlaying ──────────────────────────────────────────────────────────────


class TestNowPlaying:
    def test_audio_video_predicates(self):
        assert NowPlaying(item_type="Audio").is_audio
        assert not NowPlaying(item_type="Audio").is_video
        assert NowPlaying(item_type="Movie").is_video
        assert NowPlaying(item_type="Episode").is_video
        assert not NowPlaying(item_type="Episode").is_audio

    def test_ticks_conversion(self):
        # Jellyfin REST uses 100-nanosecond ticks; ms * 10_000.
        np = NowPlaying(position=1500, duration=240_000)
        assert np.position_ticks == 15_000_000
        assert np.duration_ticks == 2_400_000_000

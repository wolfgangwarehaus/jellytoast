"""Drag-reorder maps to the right play-order index in source-order display.

Bug-hunt HIGH: in a pristine SHUFFLED album/playlist the track list renders
in SOURCE order, so the model's play_index is the SOURCE index. The drag
emitted those straight to QueueManager.move_item (which treats them as
play-order) → the WRONG track moved. The page now re-maps by Id (src by
its own Id; destination = the track the drop landed after), mirroring the
context-menu remove fix. Play-order display still passes the indices
through unchanged.

The handler is self-contained, so it's exercised on a stub self.
"""

import types

from jellytoast.now_playing_page import NowPlayingPage


def _page(kind, play_order_ids):
    calls = {"move": [], "jump": []}
    play = [{"Id": i} for i in play_order_ids]
    page = types.SimpleNamespace(
        _displayed_items_kind=kind,
        queue_mgr=types.SimpleNamespace(queue=play),
        bus=types.SimpleNamespace(
            queue_move_item=types.SimpleNamespace(
                emit=lambda s, d: calls["move"].append((s, d))
            ),
            track_jumped=types.SimpleNamespace(
                emit=lambda i: calls["jump"].append(i)
            ),
        ),
    )
    return page, calls


def _reorder(page, src_play_orig, dest_play, src_id, anchor_id):
    NowPlayingPage._on_reorder_requested(
        page, src_play_orig, dest_play, src_id, anchor_id
    )


class TestSourceOrderRemap:
    # Display (source) order: a b c d e. Play order (shuffled): a d b e c.
    PLAY = ["a", "d", "b", "e", "c"]

    def test_drag_to_bottom_maps_by_id(self):
        # Drag "a" (play idx 0) to land after "c" (play idx 4).
        page, calls = _page("source", self.PLAY)
        # src_play_orig/dest_play are SOURCE indices (0 and 4) — must be ignored.
        _reorder(page, 0, 4, src_id="a", anchor_id="c")
        # pop a → [d,b,e,c], insert at 4 → a ends up right after c.
        assert calls["move"] == [(0, 4)]
        assert calls["jump"] == []

    def test_drag_below_target_inserts_after_anchor(self):
        # Drag "b" (play idx 2) to after "a" (play idx 0).
        page, calls = _page("source", self.PLAY)
        _reorder(page, 1, 0, src_id="b", anchor_id="a")
        # src(2) > anchor(0) → dest = anchor + 1 = 1 → [a,b,d,e,c].
        assert calls["move"] == [(2, 1)]

    def test_drop_at_top_jumps(self):
        # Drag "c" (play idx 4) to the very top (no anchor above it).
        page, calls = _page("source", self.PLAY)
        _reorder(page, 4, 0, src_id="c", anchor_id="")
        assert calls["move"] == [(4, 0)]
        assert calls["jump"] == [0]

    def test_unknown_src_id_is_ignored(self):
        page, calls = _page("source", self.PLAY)
        _reorder(page, 0, 1, src_id="zzz", anchor_id="a")
        assert calls["move"] == []


class TestPlayOrderPassThrough:
    def test_indices_used_directly(self):
        # Play-order display: the play_index values ARE play-order indices.
        page, calls = _page("play", ["a", "b", "c"])
        _reorder(page, 2, 0, src_id="c", anchor_id="")
        assert calls["move"] == [(2, 0)]
        assert calls["jump"] == [0]

    def test_noop_when_src_equals_dest(self):
        page, calls = _page("play", ["a", "b", "c"])
        _reorder(page, 1, 1, src_id="b", anchor_id="a")
        assert calls["move"] == []

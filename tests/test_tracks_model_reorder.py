"""``_TracksModel`` — the now-playing track list's drag-reorder model
(``jellytoast/np_track_list.py``), the authoritative layout the QueueManager
commits moves against.

Pins the subtle index math that had no direct coverage: the disc-divider
interleaving in ``set_state`` (play_index runs over the flat track list, not
the dividers) and ``move_track``'s beginMoveRows off-by-one (downward moves
use ``target + 1``) + the post-move play_index renumber. A regression here
silently corrupts which track a drag actually moves.
"""

import pytest

from jellytoast.np_track_list import _TracksModel


def _items(n=0, disc=None):
    """n plain tracks, or pass ``disc`` (a list of ParentIndexNumber per
    track) to build a multi-disc item list."""
    if disc is not None:
        return [
            {"Id": f"t{i}", "Name": f"T{i}", "ParentIndexNumber": disc[i]}
            for i in range(len(disc))
        ]
    return [{"Id": f"t{i}", "Name": f"T{i}"} for i in range(n)]


@pytest.fixture
def model(qapp):
    return _TracksModel()


def _kinds(m):
    return [e["kind"] for e in m._entries]


def _track_ids(m):
    return [e["item"]["Id"] for e in m._entries if e["kind"] == "track"]


def _play_indices(m):
    return [e["play_index"] for e in m._entries if e["kind"] == "track"]


class TestSetStateDiscDividers:
    def test_single_disc_has_no_dividers(self, model):
        model.set_state(_items(3), 0, True, True, multi_disc=False)
        assert _kinds(model) == ["track", "track", "track"]
        assert _play_indices(model) == [0, 1, 2]

    def test_multi_disc_interleaves_dividers(self, model):
        # discs 1,1,2,2,2 → a divider before disc 1 and before disc 2.
        model.set_state(_items(disc=[1, 1, 2, 2, 2]), 0, True, True, multi_disc=True)
        assert _kinds(model) == [
            "disc", "track", "track", "disc", "track", "track", "track",
        ]
        # play_index runs over the flat track list — dividers don't count.
        assert _play_indices(model) == [0, 1, 2, 3, 4]
        # disc_info = (disc_number, track_count_on_that_disc)
        discs = [e["disc_info"] for e in model._entries if e["kind"] == "disc"]
        assert discs == [(1, 2), (2, 3)]

    def test_multi_disc_off_stays_flat_even_with_parentindex(self, model):
        model.set_state(_items(disc=[1, 2]), 0, True, True, multi_disc=False)
        assert _kinds(model) == ["track", "track"]

    def test_items_returns_tracks_in_play_order_skipping_dividers(self, model):
        model.set_state(_items(disc=[1, 2, 2]), 0, True, True, multi_disc=True)
        assert [it["Id"] for it in model.items()] == ["t0", "t1", "t2"]


class TestMoveTrack:
    def test_move_down_lands_at_target_and_renumbers(self, model):
        model.set_state(_items(3), 0, True, True)  # [t0,t1,t2] play_index 0,1,2
        post = model.move_track(0, 2)
        assert post == 2
        assert _track_ids(model) == ["t1", "t2", "t0"]
        assert _play_indices(model) == [0, 1, 2]  # renumbered in the new order

    def test_move_up_lands_at_target_and_renumbers(self, model):
        model.set_state(_items(3), 0, True, True)
        post = model.move_track(2, 0)
        assert post == 0
        assert _track_ids(model) == ["t2", "t0", "t1"]
        assert _play_indices(model) == [0, 1, 2]

    def test_noop_move_returns_src_unchanged(self, model):
        model.set_state(_items(3), 0, True, True)
        assert model.move_track(1, 1) == 1
        assert _track_ids(model) == ["t0", "t1", "t2"]

    def test_out_of_range_is_rejected(self, model):
        model.set_state(_items(3), 0, True, True)
        assert model.move_track(-1, 0) == -1
        assert model.move_track(5, 0) == -1
        assert _track_ids(model) == ["t0", "t1", "t2"]

    def test_non_track_source_is_rejected(self, model):
        # entries: disc(row0), t0(1), t1(2), disc(3), t2(4)
        model.set_state(_items(disc=[1, 1, 2]), 0, True, True, multi_disc=True)
        assert model.move_track(0, 1) == -1  # row 0 is a divider

    def test_landing_on_a_divider_is_rejected(self, model):
        model.set_state(_items(disc=[1, 1, 2]), 0, True, True, multi_disc=True)
        assert model.move_track(1, 3) == -1  # row 3 is a divider
        assert _track_ids(model) == ["t0", "t1", "t2"]  # untouched

    def test_play_index_of_entry_reads_current_row(self, model):
        model.set_state(_items(3), 0, True, True)
        assert model.play_index_of_entry(2) == 2
        assert model.play_index_of_entry(-1) == -1
        model.move_track(2, 0)  # [t2,t0,t1] renumbered 0,1,2
        assert model.play_index_of_entry(0) == 0  # t2 is now play_index 0

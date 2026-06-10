"""Tests for Settings.save_queue / load_queue with v1 → v2 migration.

`isolated_settings` (in conftest.py) gives us a Settings whose
`_config_dir` is a fresh tmp_path, so each test owns its queue.json.
"""

import json

from jellytoast.player_state import Queue, QueueContext, QueueKind


def _items(n):
    return [{"Id": f"id{i}", "Name": f"Track {i}"} for i in range(n)]


def test_load_returns_none_when_file_missing(isolated_settings):
    assert isolated_settings.load_queue() is None


def test_v2_round_trip(isolated_settings):
    items = _items(3)
    q = Queue(
        context=QueueContext(
            kind=QueueKind.ALBUM,
            source_id="a1",
            source_label="Kid A",
        ),
        original_items=items,
        play_order=[2, 0, 1],
        current_index=1,
    )
    isolated_settings.save_queue(q)
    restored = isolated_settings.load_queue()
    assert restored is not None
    assert restored.context.kind is QueueKind.ALBUM
    assert restored.context.source_label == "Kid A"
    assert restored.original_items == items
    assert restored.play_order == [2, 0, 1]
    assert restored.current_index == 1


def test_v2_payload_is_versioned(isolated_settings, tmp_path):
    q = Queue(
        original_items=_items(2),
        play_order=[0, 1],
        current_index=0,
    )
    isolated_settings.save_queue(q)
    raw = json.loads((tmp_path / "queue.json").read_text())
    assert raw.get("version") == 2
    assert "context" in raw
    assert "play_order" in raw


def test_v1_legacy_file_migrates_to_manual(isolated_settings, tmp_path):
    # A v1 file as written by older builds: flat queue + index, no
    # version tag, no context, no play_order.
    items = _items(3)
    (tmp_path / "queue.json").write_text(
        json.dumps(
            {
                "queue": items,
                "index": 1,
            }
        )
    )
    restored = isolated_settings.load_queue()
    assert restored is not None
    assert restored.context.kind is QueueKind.MANUAL
    assert restored.original_items == items
    # Sequential play_order is the right v1 → v2 default since v1 had
    # no notion of shuffle permutation.
    assert restored.play_order == [0, 1, 2]
    assert restored.current_index == 1


def test_v1_legacy_with_empty_queue_returns_none(isolated_settings, tmp_path):
    (tmp_path / "queue.json").write_text(json.dumps({"queue": [], "index": -1}))
    assert isolated_settings.load_queue() is None


def test_v1_legacy_clamps_out_of_range_index(isolated_settings, tmp_path):
    (tmp_path / "queue.json").write_text(
        json.dumps(
            {
                "queue": _items(2),
                "index": 99,
            }
        )
    )
    restored = isolated_settings.load_queue()
    assert restored is not None
    assert restored.current_index == -1


def test_load_returns_none_on_corrupt_json(isolated_settings, tmp_path):
    (tmp_path / "queue.json").write_text("not even close to json {{{ ")
    assert isolated_settings.load_queue() is None

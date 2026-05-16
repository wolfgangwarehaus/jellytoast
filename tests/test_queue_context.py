"""Tests for the seeded-radio additions to ``QueueContext``.

The broader ``QueueContext`` / ``Queue`` round-trip behavior is
covered by ``test_player_state.py``; this file targets just the
``seed_kind`` + ``radio_played_ids`` fields added for A9 (seeded radio
provider methods). Pure-logic — no Qt, no network.
"""

from modules.player_state import (
    Queue,
    QueueContext,
    QueueKind,
)


# ── Defaults ────────────────────────────────────────────────────────────────


class TestQueueContextRadioDefaults:
    def test_default_seed_kind_is_none(self):
        ctx = QueueContext()
        assert ctx.seed_kind is None

    def test_default_radio_played_ids_is_empty_list(self):
        ctx = QueueContext()
        assert ctx.radio_played_ids == []

    def test_each_instance_gets_its_own_radio_list(self):
        # The default-mutable footgun: without field(default_factory=list)
        # every QueueContext would share the same list and appending to
        # one would corrupt the next. Verify the factory pattern holds.
        a = QueueContext()
        b = QueueContext()
        assert a.radio_played_ids is not b.radio_played_ids
        a.radio_played_ids.append("track-1")
        assert b.radio_played_ids == []


# ── Construction with radio fields ──────────────────────────────────────────


class TestQueueContextRadioConstruction:
    def test_populated_radio_context(self):
        ctx = QueueContext(
            kind=QueueKind.INSTANT_MIX,
            source_id="seed-1",
            source_label="Radio: Artist X",
            seed_kind="similar",
            radio_played_ids=["track-1", "track-2"],
        )
        assert ctx.kind is QueueKind.INSTANT_MIX
        assert ctx.seed_kind == "similar"
        assert ctx.radio_played_ids == ["track-1", "track-2"]

    def test_all_three_seed_kind_values_accepted(self):
        # The dataclass doesn't validate the literal — it's a plain str.
        # We document the accepted values via tests rather than an Enum
        # so radio_played_ids stays trivially JSON-serializable.
        for kind in ("similar", "instant_mix", "genre"):
            ctx = QueueContext(seed_kind=kind)
            assert ctx.seed_kind == kind


# ── Queue round-trip with radio fields ──────────────────────────────────────


class TestQueueRoundTripWithRadio:
    def test_radio_fields_survive_to_from_dict(self):
        ctx = QueueContext(
            kind=QueueKind.INSTANT_MIX,
            source_id="seed-1",
            source_label="Radio: Foo",
            seed_kind="instant_mix",
            radio_played_ids=["t1", "t2", "t3"],
        )
        q = Queue(
            context=ctx,
            original_items=[{"Id": "t1"}, {"Id": "t2"}],
            play_order=[0, 1],
            current_index=0,
        )
        restored = Queue.from_dict(q.to_dict())
        assert restored.context.seed_kind == "instant_mix"
        assert restored.context.radio_played_ids == ["t1", "t2", "t3"]

    def test_legacy_dict_without_radio_fields_loads(self):
        # Pre-A9 saved session JSON has no seed_kind / radio_played_ids
        # in the context dict. The loader must default them in so the
        # user's queue still restores on first launch after upgrade.
        data = {
            "context": {
                "kind": "album",
                "source_id": "alb1",
                "source_label": "Album",
                "source_icon": "",
            },
            "original_items": [{"Id": "t1"}],
            "play_order": [0],
            "current_index": 0,
        }
        q = Queue.from_dict(data)
        assert q.context.seed_kind is None
        assert q.context.radio_played_ids == []

    def test_empty_string_seed_kind_normalizes_to_none(self):
        # Future-proofing: some serializers might write "" instead of
        # null for an Optional[str]. Treat that as None so call sites
        # can rely on `if ctx.seed_kind:` checks.
        data = {
            "context": {
                "kind": "manual",
                "seed_kind": "",
                "radio_played_ids": [],
            },
            "original_items": [],
            "play_order": [],
            "current_index": -1,
        }
        q = Queue.from_dict(data)
        assert q.context.seed_kind is None

    def test_radio_played_ids_is_copied_not_shared(self):
        # to_dict shouldn't expose the live list — mutating the dict
        # output mustn't poison the context. Catches a "return self.x"
        # regression that would let queue.json saves race with the
        # RadioFeeder appending new played ids.
        ctx = QueueContext(radio_played_ids=["t1"])
        q = Queue(context=ctx)
        serialized = q.to_dict()
        serialized["context"]["radio_played_ids"].append("WRONG")
        assert ctx.radio_played_ids == ["t1"]

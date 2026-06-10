"""Tests for the seeded-radio queue feeder.

Covers ``QueueManager._maybe_refill_radio`` and its result/error paths:
within-5-of-end refill trigger, dedupe vs played-set + existing queue,
200-cap trim of played items, played-set bookkeeping on track change,
skip-heavy reseed (advances offset), kind-gating, in-flight idempotency,
empty-batch fallback, and the ``radio_extended`` / ``radio_exhausted``
bus signals.

``jellytoast.async_io.run_async`` is monkeypatched to run synchronously on
the calling thread so the tests don't have to drive a Qt event loop;
the queue manager itself stays untouched. ``PlayerBus`` is reset between
tests via the ``fresh_bus`` fixture (same pattern as test_queue_manager).
"""

from typing import Dict, List
from unittest.mock import MagicMock

import pytest

from jellytoast.player_state import (
    PlayerBus,
    QueueContext,
    QueueKind,
)

# ── Test doubles & fixtures ─────────────────────────────────────────────────


class FakeProvider:
    """Stand-in for the provider singleton. Each radio method is a
    MagicMock so tests can pre-program return values and inspect call
    args. The non-radio methods are just enough for
    ``QueueManager._build_now_playing``."""

    kind = "fake"

    def __init__(self) -> None:
        self.get_instant_mix = MagicMock(return_value=[])
        self.get_similar_songs = MagicMock(return_value=[])
        self.get_genre_radio = MagicMock(return_value=[])
        self.get_random_audio_items = MagicMock(return_value=[])

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
    # QueueManager calls ``get_provider()`` at refill time (not at
    # construction) per the singleton-refs rule, so a single setattr
    # is enough — every read inside the feeder picks this up.
    yield fp
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)


@pytest.fixture
def isolated_settings_singleton(isolated_settings):
    """tmp_path-backed Settings pinned as the get_settings() singleton
    with QSettings cleared. Delegates to the canonical conftest fixture."""
    return isolated_settings


@pytest.fixture
def fresh_bus():
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def sync_run_async(monkeypatch):
    """Make ``jellytoast.async_io.run_async`` run inline. The real
    implementation dispatches to a QThreadPool + queues the result back
    to the GUI thread via Qt signals — we don't want either in unit
    tests. Run the worker on the calling thread and pump the callback
    directly so refill effects land before the test assertion."""
    import jellytoast.async_io as async_io

    def _inline(fn, *args, on_result=None, on_error=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
            return
        if on_result is not None:
            on_result(result)

    monkeypatch.setattr(async_io, "run_async", _inline)
    # The queue manager imports run_async via ``from jellytoast import
    # async_io``; the call site is ``async_io.run_async(...)``. Patching
    # the attribute on the module suffices — no need to also patch the
    # queue_manager namespace.
    return _inline


@pytest.fixture
def qm(qapp, fake_provider, isolated_settings_singleton, fresh_bus, sync_run_async):
    """A QueueManager wired to the fake provider, isolated settings, a
    fresh bus, and inline run_async."""
    from jellytoast.queue_manager import QueueManager

    return QueueManager()


def _items(n: int, base: str = "id") -> List[Dict]:
    return [{"Id": f"{base}{i}", "Name": f"Track {i}", "Type": "Audio"} for i in range(n)]


def _radio_ctx(seed_id: str = "seed-1", seed_kind: str = "track") -> QueueContext:
    return QueueContext(
        kind=QueueKind.INSTANT_MIX,
        source_id=seed_id,
        source_label=f"Radio: {seed_id}",
        seed_kind=seed_kind,
    )


def _install_radio_queue(
    qm,
    items: List[Dict],
    start_index: int,
    seed_kind: str = "track",
    played_ids: List[str] = None,
) -> None:
    """Bypass ``play_now`` to install a fully-controlled radio queue.
    Going through play_now would trigger the very feeder under test
    on the inaugural ``_play_current`` call and pollute assertions
    about provider call count. We instead poke the internal queue
    directly so the test can fire ``_play_current`` once at the
    desired index from a clean state."""
    from jellytoast.player_state import Queue

    qm._q = Queue(
        context=QueueContext(
            kind=QueueKind.INSTANT_MIX,
            source_id="seed-1",
            source_label="Radio: seed-1",
            seed_kind=seed_kind,
            radio_played_ids=list(played_ids or []),
        ),
        original_items=list(items),
        play_order=list(range(len(items))),
        current_index=start_index,
    )


def _capture(signal) -> List:
    out: List = []
    signal.connect(lambda *args: out.append(args))
    return out


# ── Refill trigger ──────────────────────────────────────────────────────────


class TestRefillTrigger:
    def test_within_5_of_end_fires_feeder(self, qm, fake_provider):
        items = _items(10)
        _install_radio_queue(qm, items, start_index=5)
        # Pre-program a 25-track refill batch.
        batch = _items(25, base="r")
        fake_provider.get_instant_mix.return_value = batch

        qm._play_current()

        assert fake_provider.get_instant_mix.call_count == 1
        _, kwargs = fake_provider.get_instant_mix.call_args
        assert kwargs["count"] == 25
        # All 25 land (no dedupe collisions in this setup).
        ids = [it["Id"] for it in qm.original_items]
        assert ids[10:] == [it["Id"] for it in batch]

    def test_cushion_skips_refill(self, qm, fake_provider):
        items = _items(10)
        _install_radio_queue(qm, items, start_index=0)
        qm._play_current()
        assert fake_provider.get_instant_mix.call_count == 0

    def test_no_refill_on_non_instant_mix(self, qm, fake_provider):
        # Manual queue, within-5-of-end: feeder must stay silent.
        items = _items(10)
        qm.play_now(items, 9, QueueContext(kind=QueueKind.MANUAL))
        assert fake_provider.get_instant_mix.call_count == 0


# ── Dedupe ──────────────────────────────────────────────────────────────────


class TestDedupe:
    def test_dedupe_against_played_ids(self, qm, fake_provider):
        items = _items(10)
        played = [f"r{i}" for i in range(10)]
        _install_radio_queue(qm, items, start_index=5, played_ids=played)
        # 25 candidates, first 10 overlap with played set → 15 land.
        fake_provider.get_instant_mix.return_value = _items(25, base="r")

        qm._play_current()

        appended = qm.original_items[10:]
        assert len(appended) == 15
        assert [it["Id"] for it in appended] == [f"r{i}" for i in range(10, 25)]

    def test_dedupe_against_existing_queue(self, qm, fake_provider):
        # Queue contains r0..r4 already; provider returns r0..r24 →
        # 20 net additions.
        items = _items(5) + _items(5, base="r")
        _install_radio_queue(qm, items, start_index=5)
        fake_provider.get_instant_mix.return_value = _items(25, base="r")

        qm._play_current()

        # Original count was 10; expect 10 + 20 = 30.
        assert len(qm.original_items) == 30


# ── 200-cap trim ────────────────────────────────────────────────────────────


class TestCapTrim:
    def test_cap_200_trims_played(self, qm, fake_provider):
        # 200 in queue, current_index = 199 (last). Provider returns
        # 30 new items → final length must be ≤ 200 and the trimmed
        # entries must all have been played (play_order index <
        # current_index *before* trim).
        items = _items(200, base="q")
        # Mark first 50 as played so the trim has candidates ahead
        # of the current track. current_index = 199 means
        # play_order positions 0..198 are "played"; we set
        # radio_played_ids for the first 50 to mirror that bookkeeping
        # but the trim contract is "position < current_index", which
        # is enough.
        played = [f"q{i}" for i in range(50)]
        _install_radio_queue(qm, items, start_index=199, played_ids=played)
        fake_provider.get_instant_mix.return_value = _items(30, base="r")

        # Snapshot the would-survive future items (just the current
        # track at index 199 — there's nothing past it before refill).
        current_before = qm.current_item["Id"]

        qm._play_current()

        assert len(qm.original_items) <= 200
        # The trimmed entries should all have been from the played-set
        # head (q0..q49 are candidates). Surviving queue must still
        # contain the currently-playing track.
        surviving_ids = {it["Id"] for it in qm.original_items}
        assert current_before in surviving_ids
        # All refill entries (r0..r29) must be present — those were
        # added after current_index, never trimmable.
        for i in range(30):
            assert f"r{i}" in surviving_ids

    def test_trim_preserves_current_index(self, qm, fake_provider):
        items = _items(200, base="q")
        _install_radio_queue(qm, items, start_index=199)
        fake_provider.get_instant_mix.return_value = _items(30, base="r")
        current_id = qm.current_item["Id"]

        qm._play_current()

        # The current track should remain the current track after
        # trim — its play_order index may have shifted down, but
        # current_item resolves through play_order so we just
        # assert the id is preserved.
        assert qm.current_item["Id"] == current_id


# ── Played-set bookkeeping ──────────────────────────────────────────────────


class TestPlayedSet:
    def test_plays_push_ids_in_order(self, qm, fake_provider):
        items = _items(20)
        _install_radio_queue(qm, items, start_index=0)
        # Cushion away from end so refill doesn't fire and pollute
        # the call counts.
        qm._play_current()  # id0
        qm._q.current_index = 1
        qm._play_current()  # id1
        qm._q.current_index = 2
        qm._play_current()  # id2

        assert qm.context.radio_played_ids == ["id0", "id1", "id2"]


# ── Skip-heavy reseed ───────────────────────────────────────────────────────


class TestSkipHeavy:
    def test_three_skips_in_five_advances_offset(self, qm, fake_provider):
        # Genre radio so we can exercise the offset param meaningfully
        # — instant_mix doesn't accept offset in the provider signature,
        # but the genre path does and the feeder branches on seed_kind.
        items = _items(10)
        _install_radio_queue(qm, items, start_index=5, seed_kind="genre")
        # Pre-load radio_played_ids so offset advancement has a non-zero
        # length to advance to. _play_current appends the current track
        # id ("id5") before computing offset, so the expected value is
        # 40 + 1 = 41.
        qm._q.context.radio_played_ids = [f"p{i}" for i in range(40)]
        # Drive 3 skips + 2 naturals across the rolling window.
        qm._skip_history.extend([True, False, True, False, True])

        fake_provider.get_genre_radio.return_value = _items(25, base="g")

        qm._play_current()

        assert fake_provider.get_genre_radio.call_count == 1
        _, kwargs = fake_provider.get_genre_radio.call_args
        assert kwargs["offset"] == 41

    def test_two_skips_in_five_does_not_advance(self, qm, fake_provider):
        items = _items(10)
        _install_radio_queue(qm, items, start_index=5, seed_kind="genre")
        qm._q.context.radio_played_ids = [f"p{i}" for i in range(40)]
        qm._skip_history.extend([True, False, True, False, False])
        fake_provider.get_genre_radio.return_value = _items(25, base="g")

        qm._play_current()

        _, kwargs = fake_provider.get_genre_radio.call_args
        assert kwargs["offset"] == 0


# ── In-flight idempotency ───────────────────────────────────────────────────


class TestIdempotency:
    def test_two_play_currents_fire_one_provider_call(self, qm, fake_provider, monkeypatch):
        """Two _play_current invocations while a refill is in flight
        must only produce one provider call. We disable run_async's
        inline result-dispatch and re-enable it manually so the second
        _play_current happens while ``_refilling`` is still True."""
        import jellytoast.async_io as async_io

        pending: list = []

        def _deferred(fn, *args, on_result=None, on_error=None, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                pending.append((on_error, exc, "err"))
                return
            pending.append((on_result, result, "ok"))

        monkeypatch.setattr(async_io, "run_async", _deferred)

        items = _items(10)
        _install_radio_queue(qm, items, start_index=5)
        fake_provider.get_instant_mix.return_value = _items(25, base="r")

        qm._play_current()  # fires refill; result is queued in `pending`
        # Second play_current while refill is "in flight" (no on_result yet).
        qm._q.current_index = 6
        qm._play_current()

        # Only the first call hit the provider.
        assert fake_provider.get_instant_mix.call_count == 1

        # Drain the deferred callback so the flag clears for cleanliness.
        cb, payload, kind = pending.pop(0)
        if cb is not None:
            cb(payload)


# ── Empty result fallback ───────────────────────────────────────────────────


class TestEmptyResult:
    def test_empty_primary_falls_back_to_random(self, qm, fake_provider):
        items = _items(10)
        _install_radio_queue(qm, items, start_index=5)
        fake_provider.get_instant_mix.return_value = []
        fake_provider.get_random_audio_items.return_value = _items(15, base="f")

        qm._play_current()

        assert fake_provider.get_random_audio_items.call_count == 1
        # Fallback items got appended.
        appended = qm.original_items[10:]
        assert [it["Id"] for it in appended] == [f"f{i}" for i in range(15)]

    def test_empty_everywhere_fires_radio_exhausted(self, qm, fake_provider):
        items = _items(10)
        _install_radio_queue(qm, items, start_index=5)
        fake_provider.get_instant_mix.return_value = []
        fake_provider.get_random_audio_items.return_value = []

        exhausted = _capture(qm.bus.radio_exhausted)
        qm._play_current()

        assert len(exhausted) == 1
        # Queue untouched — no crash, just stops extending.
        assert len(qm.original_items) == 10


# ── Bus signal carries append count ─────────────────────────────────────────


class TestRadioExtendedSignal:
    def test_extended_carries_post_dedupe_count(self, qm, fake_provider):
        # 25 returned, 10 already in played set → 15 net additions.
        items = _items(10)
        played = [f"r{i}" for i in range(10)]
        _install_radio_queue(qm, items, start_index=5, played_ids=played)
        fake_provider.get_instant_mix.return_value = _items(25, base="r")

        extended = _capture(qm.bus.radio_extended)
        qm._play_current()

        assert len(extended) == 1
        # _capture stores args as a tuple; pull the int payload.
        assert extended[0][0] == 15

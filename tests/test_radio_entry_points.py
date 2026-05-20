"""Tests for the album / artist / genre seeded-radio entry points.

Covers ``ui_helpers.start_seed_radio`` — the shared launcher behind the
LibraryGrid / GenresView right-click menus.

The seed-kind contract under test:

  * ``album``  → ``get_instant_mix(album_id)``,  ctx.seed_kind == "album"
  * ``artist`` → ``get_similar_songs(artist_id)``, ctx.seed_kind == "artist"
  * ``genre``  → ``get_genre_radio(genre_name)``,  ctx.seed_kind == "genre"

In every case the emitted ``QueueContext`` carries ``kind=INSTANT_MIX``
and the right ``source_id`` / ``source_label`` so the RadioFeeder
auto-extends from the correct seed. Empty / failed fetches emit nothing.

``modules.async_io.run_async`` is monkeypatched inline (same pattern as
test_radio_feeder); ``PlayerBus`` is reset per test via ``fresh_bus``.
"""

from unittest.mock import MagicMock

import pytest

from modules.player_state import PlayerBus, QueueKind


# ── Test doubles & fixtures ─────────────────────────────────────────────────


class FakeProvider:
    """Provider stand-in: each radio method is a MagicMock so tests can
    pre-program return values and inspect call args."""

    kind = "fake"

    def __init__(self):
        self.get_instant_mix = MagicMock(return_value=[])
        self.get_similar_songs = MagicMock(return_value=[])
        self.get_genre_radio = MagicMock(return_value=[])


@pytest.fixture
def fake_provider(monkeypatch):
    fp = FakeProvider()
    import modules.providers as providers_mod

    monkeypatch.setattr(providers_mod, "_PROVIDER", fp)
    yield fp
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)


@pytest.fixture
def fresh_bus():
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def sync_run_async(monkeypatch):
    """Run ``async_io.run_async`` inline on the calling thread so the
    provider fetch + result callback land before the assertion."""
    import modules.async_io as async_io

    def _inline(fn, *args, on_result=None, on_error=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(exc)
            return
        if on_result is not None:
            on_result(result)

    monkeypatch.setattr(async_io, "run_async", _inline)
    return _inline


@pytest.fixture
def captured_emits(fresh_bus):
    """Connect a spy to ``PlayerBus.queue_play_now`` and collect every
    ``(items, start_index, context)`` emission."""
    bus = PlayerBus.get()
    seen = []
    bus.queue_play_now.connect(lambda items, idx, ctx: seen.append((items, idx, ctx)))
    return seen


# ── Album radio ─────────────────────────────────────────────────────────────


def test_album_radio_calls_instant_mix_with_album_id(
    fake_provider, sync_run_async, captured_emits
):
    from modules.ui_helpers import start_seed_radio

    fake_provider.get_instant_mix.return_value = [
        {"Id": "t1", "Name": "Track 1"},
        {"Id": "t2", "Name": "Track 2"},
    ]
    start_seed_radio("album", "album-99", "Greatest Hits")

    fake_provider.get_instant_mix.assert_called_once_with("album-99")
    fake_provider.get_similar_songs.assert_not_called()
    fake_provider.get_genre_radio.assert_not_called()

    assert len(captured_emits) == 1
    items, idx, ctx = captured_emits[0]
    assert [i["Id"] for i in items] == ["t1", "t2"]
    assert idx == 0
    assert ctx.kind == QueueKind.INSTANT_MIX
    assert ctx.seed_kind == "album"
    assert ctx.source_id == "album-99"
    assert ctx.source_label == "Greatest Hits"


# ── Artist radio ────────────────────────────────────────────────────────────


def test_artist_radio_calls_similar_songs_with_artist_id(
    fake_provider, sync_run_async, captured_emits
):
    from modules.ui_helpers import start_seed_radio

    fake_provider.get_similar_songs.return_value = [{"Id": "s1", "Name": "Song 1"}]
    start_seed_radio("artist", "artist-42", "The Band")

    fake_provider.get_similar_songs.assert_called_once_with("artist-42")
    fake_provider.get_instant_mix.assert_not_called()
    fake_provider.get_genre_radio.assert_not_called()

    assert len(captured_emits) == 1
    items, idx, ctx = captured_emits[0]
    assert [i["Id"] for i in items] == ["s1"]
    assert ctx.kind == QueueKind.INSTANT_MIX
    assert ctx.seed_kind == "artist"
    assert ctx.source_id == "artist-42"
    assert ctx.source_label == "The Band"


# ── Genre radio ─────────────────────────────────────────────────────────────


def test_genre_radio_calls_genre_radio_with_genre_name(
    fake_provider, sync_run_async, captured_emits
):
    from modules.ui_helpers import start_seed_radio

    fake_provider.get_genre_radio.return_value = [{"Id": "g1", "Name": "Jazz Song"}]
    # Genre radio keys off the *name* (source_label), not the id.
    start_seed_radio("genre", "genre-id-7", "Jazz")

    fake_provider.get_genre_radio.assert_called_once_with("Jazz")
    fake_provider.get_instant_mix.assert_not_called()
    fake_provider.get_similar_songs.assert_not_called()

    assert len(captured_emits) == 1
    items, idx, ctx = captured_emits[0]
    assert [i["Id"] for i in items] == ["g1"]
    assert ctx.kind == QueueKind.INSTANT_MIX
    assert ctx.seed_kind == "genre"
    assert ctx.source_label == "Jazz"


# ── Empty / failure paths ───────────────────────────────────────────────────


def test_empty_batch_emits_nothing(fake_provider, sync_run_async, captured_emits):
    from modules.ui_helpers import start_seed_radio

    fake_provider.get_instant_mix.return_value = []
    start_seed_radio("album", "album-1", "Empty Album")

    fake_provider.get_instant_mix.assert_called_once_with("album-1")
    assert captured_emits == []


def test_provider_failure_emits_nothing(fake_provider, sync_run_async, captured_emits):
    from modules.ui_helpers import start_seed_radio

    fake_provider.get_similar_songs.side_effect = RuntimeError("network down")
    start_seed_radio("artist", "artist-1", "Doomed")

    assert captured_emits == []


def test_missing_id_skips_album_fetch(fake_provider, sync_run_async, captured_emits):
    from modules.ui_helpers import start_seed_radio

    # No album id → no fetch, no emit (guard fires before run_async).
    start_seed_radio("album", "", "No Id")
    fake_provider.get_instant_mix.assert_not_called()
    assert captured_emits == []


def test_missing_genre_name_skips_genre_fetch(
    fake_provider, sync_run_async, captured_emits
):
    from modules.ui_helpers import start_seed_radio

    # Genre radio needs the name; an empty label short-circuits.
    start_seed_radio("genre", "genre-id-only", "")
    fake_provider.get_genre_radio.assert_not_called()
    assert captured_emits == []

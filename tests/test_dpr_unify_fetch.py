"""Coverage for the DPR-unify cover-fetch contract.

After 2026-05-26 every cover-fetch site requests a **fixed
worst-case-DPR source size** from the server, so the L2 raw image
cache (modules.ui_helpers._raw_image_cache, modules.image_cache's
raw tier) holds one entry per item regardless of which DPR happened
to be live the first time the item loaded. Wayland's fractional-DPR
drift across launches would otherwise pin a fresh raw per session.

These tests pin the contract by monkey-patching ``screen_dpr`` to a
sequence of values and ``get_image_url`` to a recorder, then firing
each site's cover-load entry point and asserting the recorded size
argument is constant. See ``docs/research/dpr_cache_keys.md`` for
the per-site recommendations.
"""

from __future__ import annotations

from typing import List

import pytest

# ── Helpers ─────────────────────────────────────────────────────────────


class _FakeProvider:
    """Records every ``get_image_url`` call so a test can assert that
    the size argument was identical across DPR changes."""

    def __init__(self):
        self.size_calls: List[int] = []
        self.user_id = "u"
        self.token = "t"
        self.server_url = "http://test"

    def get_image_url(self, item_id, name, size):
        self.size_calls.append(int(size))
        return f"http://test/Items/{item_id}/Images/{name}?fillWidth={size}"

    # Some sites consult is_authenticated; keep it cheap and truthy.
    def is_authenticated(self) -> bool:
        return True


@pytest.fixture
def fake_provider(monkeypatch):
    """Stub `get_provider` everywhere the under-test modules use it.

    Returned object exposes ``size_calls`` (a list of every size arg
    passed to ``get_image_url``). Re-patches per-module so the import
    resolves the fake even when callers do ``from modules.providers
    import get_provider``."""
    fp = _FakeProvider()
    monkeypatch.setattr(
        "modules.providers.get_provider", lambda: fp, raising=False
    )
    # Each module typically binds at top-level with ``from modules.providers
    # import get_provider`` — patch every consumer's local name too.
    for modname in (
        "modules.search_view",
        "modules.songs_view",
        "modules.artist_page",
        "modules.now_playing_bar",
    ):
        try:
            mod = __import__(modname, fromlist=["get_provider"])
            if hasattr(mod, "get_provider"):
                monkeypatch.setattr(mod, "get_provider", lambda: fp)
        except Exception:
            pass
    return fp


def _stub_load_image_async(monkeypatch, modname: str):
    """No-op out the network fetch so the test never touches QNAM."""
    mod = __import__(modname, fromlist=["load_image_async"])
    if hasattr(mod, "load_image_async"):
        monkeypatch.setattr(mod, "load_image_async", lambda *a, **k: None)


# ── search_view._SongsSection.set_items ────────────────────────────────


def test_search_view_fetches_at_fixed_size_across_dprs(qapp, fake_provider, monkeypatch):
    from modules import search_view as _sv
    from modules.songs_view import _SongRowDelegate as _SRD

    _stub_load_image_async(monkeypatch, "modules.search_view")

    section = _sv._SongsSection()
    items = [{"Id": f"item-{i}", "AlbumId": f"alb-{i}", "Name": f"x{i}"} for i in range(3)]

    for dpr in (1.0, 1.5, 2.0):
        fake_provider.size_calls.clear()
        monkeypatch.setattr(_sv, "screen_dpr", lambda _w=None, _d=dpr: _d)
        section.set_items(items)
        # Each item should have requested the same fixed source size.
        expected = _SRD.THUMB_SIZE * 3
        assert fake_provider.size_calls == [expected] * len(items), (
            f"search_view fetch size drifted at dpr={dpr}: "
            f"{fake_provider.size_calls}"
        )


# ── songs_view._SongsListView._load_visible_covers ─────────────────────


def test_songs_view_fetches_at_fixed_size_across_dprs(qapp, fake_provider, monkeypatch):
    """Songs view's cover prefetch fires off the model's set_items; we
    drive it directly via the list view's _load_visible_covers helper."""
    from modules import songs_view as _songs

    _stub_load_image_async(monkeypatch, "modules.songs_view")

    view = _songs.SongsView()
    items = [
        {"Id": f"s-{i}", "AlbumId": f"a-{i}", "Name": f"track{i}", "Album": "x"}
        for i in range(2)
    ]
    view._model.set_items(items)

    for dpr in (1.0, 1.5, 2.0):
        fake_provider.size_calls.clear()
        monkeypatch.setattr(_songs, "screen_dpr", lambda _w=None, _d=dpr: _d)
        # Force the view to re-attempt every cover by clearing the
        # "already loaded" set; otherwise it short-circuits after the
        # first pass.
        view._covers_loaded.clear()
        view._load_visible_covers()
        expected = _songs._SongRowDelegate.THUMB_SIZE * 3
        # Some items may be skipped (no AlbumId etc.); the contract is
        # that EVERY size call we did make used the fixed size.
        assert fake_provider.size_calls, f"no calls at dpr={dpr}"
        assert all(sz == expected for sz in fake_provider.size_calls), (
            f"songs_view fetch size drifted at dpr={dpr}: "
            f"{fake_provider.size_calls}"
        )


# ── artist_page album-tile fetch ────────────────────────────────────────


def test_artist_page_tile_fetches_at_fixed_size_across_dprs(
    qapp, fake_provider, monkeypatch
):
    """Artist's discography tile grid: _on_albums_loaded fires the
    per-album cover fetches. Verifies the size arg is fixed across
    DPRs at the worst-case source value (library_grid's COVER_SIZE × 3)."""
    from modules import artist_page as _ap
    from modules.library_grid import _TileDelegate

    _stub_load_image_async(monkeypatch, "modules.artist_page")

    page = _ap.ArtistPage()
    page._artist_id = "artist-1"
    page._artist_meta = {}
    albums = [{"Id": f"album-{i}", "Name": f"a{i}"} for i in range(2)]

    for dpr in (1.0, 1.5, 2.0):
        fake_provider.size_calls.clear()
        monkeypatch.setattr(_ap, "screen_dpr", lambda _w=None, _d=dpr: _d)
        page._on_albums_loaded("artist-1", albums)
        expected = _TileDelegate.COVER_SIZE * 3
        assert all(sz == expected for sz in fake_provider.size_calls), (
            f"artist_page tile fetch size drifted at dpr={dpr}: "
            f"{fake_provider.size_calls}"
        )


# ── artist_page header cover fetch ──────────────────────────────────────


def test_artist_page_header_fetches_at_fixed_size_across_dprs(
    qapp, fake_provider, monkeypatch
):
    """Header artist-photo fetch — runs in _on_meta_loaded."""
    from modules import artist_page as _ap

    _stub_load_image_async(monkeypatch, "modules.artist_page")

    page = _ap.ArtistPage()
    page._artist_id = "artist-2"
    meta = {"Name": "Test", "Genres": ["rock"]}

    for dpr in (1.0, 1.5, 2.0):
        fake_provider.size_calls.clear()
        monkeypatch.setattr(_ap, "screen_dpr", lambda _w=None, _d=dpr: _d)
        page._on_meta_loaded("artist-2", meta)
        expected = page.HEADER_COVER * 3
        assert fake_provider.size_calls == [expected], (
            f"artist_page header fetch size drifted at dpr={dpr}: "
            f"{fake_provider.size_calls}"
        )


# ── now_playing_bar live + prefetch cover fetches ──────────────────────


def test_now_playing_bar_live_fetch_fixed_size(qapp, fake_provider, monkeypatch):
    """_on_started's cover fetch — should always request `_BAR_SOURCE_PX`."""
    from modules import now_playing_bar as _npb
    from modules.player_state import NowPlaying

    _stub_load_image_async(monkeypatch, "modules.now_playing_bar")

    bar = _npb.NowPlayingBar()
    np = NowPlaying(
        item_id="track-1",
        image_id="album-1",
        title="x",
        subtitle="y",
        album="z",
        item_type="Audio",
        stream_url="http://stream",
    )

    for dpr in (1.0, 1.5, 2.0):
        fake_provider.size_calls.clear()
        monkeypatch.setattr(_npb, "screen_dpr", lambda _w=None, _d=dpr: _d)
        bar._on_started(np)
        # Exactly one cover-fetch URL per started callback.
        assert fake_provider.size_calls == [_npb._BAR_SOURCE_PX], (
            f"now_playing_bar live fetch size drifted at dpr={dpr}: "
            f"{fake_provider.size_calls}"
        )


def test_now_playing_bar_prefetch_fixed_size(qapp, fake_provider, monkeypatch):
    """_prefetch_cover should match the live fetch's source size so the
    L2 raw entry it warms is reusable by the live fetch."""
    from modules import now_playing_bar as _npb
    from modules.player_state import NowPlaying

    _stub_load_image_async(monkeypatch, "modules.now_playing_bar")

    bar = _npb.NowPlayingBar()
    np = NowPlaying(
        item_id="track-2",
        image_id="album-2",
        title="x",
        subtitle="y",
        album="z",
        item_type="Audio",
        stream_url="http://stream",
    )

    for dpr in (1.0, 1.5, 2.0):
        fake_provider.size_calls.clear()
        monkeypatch.setattr(_npb, "screen_dpr", lambda _w=None, _d=dpr: _d)
        bar._prefetch_cover(np)
        assert fake_provider.size_calls == [_npb._BAR_SOURCE_PX], (
            f"now_playing_bar prefetch size drifted at dpr={dpr}: "
            f"{fake_provider.size_calls}"
        )

"""Regression tests for the #234 audit second pass (findings 6–8; finding 9's
byte-budgeted cover LRU is pinned in test_library_grid_cover_cache_cap.py).

6. Subsonic smart-playlist server rules paginate past the API's 500-cap
   (getSongsByGenre count/offset, getAlbumList2 size/offset).
7. An image load that fails to even START fans the failure out to its
   subscribers and forgets the key — previously the stale waiter list
   wedged that cache_key (and its widgets) for the whole session — and
   the deferred queue keeps draining.
8. Favorite toggles roll back on a failed server write: providers raise,
   and toggle_favorite_async restores + re-broadcasts the old state.
"""

from __future__ import annotations

from typing import List

import pytest

from jellytoast.providers.subsonic import SubsonicProvider

# ── Finding 6: server-rule pagination ───────────────────────────────────────


@pytest.fixture
def provider(monkeypatch):
    p = SubsonicProvider()
    p._username = "test"
    p._password = "test"
    p._server_url = "https://example.invalid"
    p.calls = []
    # endpoint → callable(params) → response (so pages can differ)
    p.responders = {}

    def _fake_request(path, params=None, server_url=None):
        params = dict(params or {})
        p.calls.append((path, params))
        fn = p.responders.get(path)
        return fn(params) if fn else {}

    monkeypatch.setattr(p, "_request", _fake_request)
    return p


def _songs(start, count):
    return [{"id": f"s{i}", "title": f"T{i}"} for i in range(start, start + count)]


class TestGenreRulePagination:
    def test_fetches_past_500(self, provider):
        # 1200 matching songs → 3 pages (500 + 500 + 200).
        def _by_genre(params):
            off = params["offset"]
            n = max(0, min(500, 1200 - off))
            return {"songsByGenre": {"song": _songs(off, n)}}

        provider.responders["getSongsByGenre"] = _by_genre
        out = provider._query_single_native(
            {"field": "genre", "op": "equals", "value": "Jazz"}, [""]
        )
        assert len(out) == 1200
        offsets = [pa["offset"] for path, pa in provider.calls]
        assert offsets == [0, 500, 1000]

    def test_short_first_page_stops_immediately(self, provider):
        provider.responders["getSongsByGenre"] = lambda pa: {
            "songsByGenre": {"song": _songs(0, 40)}
        }
        out = provider._query_single_native(
            {"field": "genre", "op": "equals", "value": "Jazz"}, [""]
        )
        assert len(out) == 40
        assert len(provider.calls) == 1


class TestYearRulePagination:
    def test_album_walk_pages_past_500(self, provider):
        # 700 albums in range → 2 getAlbumList2 pages; tracks fan out per
        # album via getAlbumTracks-equivalent (stubbed empty here — the
        # pagination of the ALBUM list is what finding 6 pins).
        def _album_list(params):
            off = params.get("offset", 0)
            n = max(0, min(500, 700 - off))
            return {"albumList2": {"album": [{"id": f"al{i}"} for i in range(off, off + n)]}}

        provider.responders["getAlbumList2"] = _album_list
        provider.responders["getAlbum"] = lambda pa: {"album": {"song": []}}
        provider._query_single_native({"field": "year", "op": "equals", "value": 1999}, [""])
        pages = [pa.get("offset", 0) for path, pa in provider.calls if path == "getAlbumList2"]
        assert pages == [0, 500]


# ── Finding 7: start-failure fan-out + deferred drain ───────────────────────


@pytest.fixture(autouse=True)
def _clean_pipeline_globals():
    """The cover pipeline keeps module-level state (gate counter, the two
    deferred queues, the in-flight subscriber map). These tests drive it
    directly, so they must START from a known-empty state — inheriting a
    stray deferred entry from an earlier test made
    test_promote_skips_a_bad_deferred_and_fires_the_next pop the wrong
    entry and fail under random ordering."""
    from jellytoast import ui_helpers as uh

    def _reset():
        uh._gated_in_flight = 0
        uh._deferred_normal.clear()
        uh._deferred_low.clear()
        uh._inflight_subscribers.clear()

    _reset()
    yield
    _reset()


class TestImageStartFailure:
    def test_fail_inflight_fans_out_and_forgets(self, qapp):
        from jellytoast import ui_helpers as uh

        errors: List[bool] = []
        pixmaps: List[object] = []
        uh._inflight_subscribers["k1"] = [
            (pixmaps.append, None),  # legacy: gets a placeholder pixmap
            (lambda _p: None, lambda: errors.append(True)),  # on_error path
        ]
        uh._fail_inflight("k1", 64, 64, 0)
        assert "k1" not in uh._inflight_subscribers  # key forgotten, not wedged
        assert errors == [True]
        assert len(pixmaps) == 1 and not pixmaps[0].isNull()

    def test_promote_skips_a_bad_deferred_and_fires_the_next(self, qapp):
        from jellytoast import ui_helpers as uh

        fired: List[str] = []
        failed_errors: List[bool] = []
        uh._inflight_subscribers["bad"] = [(lambda _p: None, lambda: failed_errors.append(True))]

        def _boom():
            raise RuntimeError("QNAM teardown")

        gate_before = uh._gated_in_flight
        # Queues are cache_key-addressable OrderedDicts now (art audit).
        uh._deferred_normal["bad"] = (64, 64, 0, _boom)
        uh._deferred_normal["good"] = (64, 64, 0, lambda: fired.append("good"))
        try:
            uh._promote_next_deferred()
            # The bad deferred failed its subscribers and the queue kept
            # draining to the good one — which now holds the gate slot.
            assert failed_errors == [True]
            assert "bad" not in uh._inflight_subscribers
            assert fired == ["good"]
            assert uh._gated_in_flight == gate_before + 1
        finally:
            uh._gated_in_flight = gate_before
            uh._deferred_normal.clear()
            uh._deferred_low.clear()


# ── Finding 8: favorite rollback ────────────────────────────────────────────


@pytest.fixture
def fresh_bus():
    from jellytoast.player_state import PlayerBus

    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


class _FlakyProvider:
    def __init__(self, fail: bool):
        self.fail = fail
        self.calls: List[tuple] = []

    def toggle_favorite(self, item_id, state):
        self.calls.append((item_id, state))
        if self.fail:
            raise RuntimeError("server said no")


@pytest.fixture
def inline_async(monkeypatch):
    import jellytoast.async_io as aio

    def _inline(fn, *args, on_result=None, on_error=None, **_kw):
        try:
            res = fn(*args)
        except Exception as e:  # noqa: BLE001
            if on_error is not None:
                on_error(e)
            return
        if on_result is not None:
            on_result(res)

    monkeypatch.setattr(aio, "run_async", _inline)


class TestFavoriteRollback:
    def _install(self, monkeypatch, fail):
        import jellytoast.providers as providers_mod

        p = _FlakyProvider(fail)
        monkeypatch.setattr(providers_mod, "_PROVIDER", p)
        return p

    def test_failure_rolls_back_np_and_rebroadcasts(
        self, qapp, fresh_bus, inline_async, monkeypatch
    ):
        from jellytoast.player_state import PlayerBus, get_now_playing
        from jellytoast.ui_helpers import toggle_favorite_async

        self._install(monkeypatch, fail=True)
        np = get_now_playing()
        np.item_id = "song-1"
        np.is_favorite = True  # the optimistic flip already happened
        emissions: List[tuple] = []
        PlayerBus.get().favorite_toggled.connect(lambda *a: emissions.append(a))
        rolled: List[bool] = []

        toggle_favorite_async("song-1", True, on_rollback=lambda: rolled.append(True))

        assert np.is_favorite is False  # shared state restored
        assert rolled == [True]  # call-site restore ran
        assert emissions == [("song-1", False)]  # surfaces flip back

    def test_success_touches_nothing(self, qapp, fresh_bus, inline_async, monkeypatch):
        from jellytoast.player_state import PlayerBus, get_now_playing
        from jellytoast.ui_helpers import toggle_favorite_async

        p = self._install(monkeypatch, fail=False)
        np = get_now_playing()
        np.item_id = "song-1"
        np.is_favorite = True
        emissions: List[tuple] = []
        PlayerBus.get().favorite_toggled.connect(lambda *a: emissions.append(a))

        toggle_favorite_async("song-1", True)

        assert p.calls == [("song-1", True)]
        assert np.is_favorite is True
        assert emissions == []  # no rollback broadcast

    def test_subsonic_toggle_reraises(self, provider, monkeypatch):
        def _fail(path, params=None, server_url=None):
            raise RuntimeError("503")

        monkeypatch.setattr(provider, "_request", _fail)
        with pytest.raises(RuntimeError):
            provider.toggle_favorite("id1", True)

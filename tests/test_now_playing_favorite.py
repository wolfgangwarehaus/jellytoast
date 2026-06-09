"""Tests for the now-playing page's source-collection favourite CTA
(modules.now_playing_page.NowPlayingPage._on_favorite_cta).

Regression: in LIVE (non-preview) mode the handler used to read the
favourite state from ``_preview_meta`` — which is ``{}`` outside preview
mode — so ``cur_fav`` was always ``False``, the CTA always sent
"favorite", and it could never un-favorite. The live branch now reads +
writes ``self._live_source_fav`` (kept current by the favorite_toggled
bus signal), so toggling from favourited yields an UN-favorite request.

We drive the pure handler logic on a bare page (``__new__`` to skip the
heavy widget build) with stubbed provider / icon / run_async, so no Qt
painting or network happens. The on-screen heart-glyph confirm is a GUI
eyeball left for the manual test plan.
"""

import modules.now_playing_page as npp_mod
from modules.now_playing_page import NowPlayingPage


def _fake_run_async(fn, *args, on_result=None, on_error=None, **_kw):
    """Synchronous stand-in for modules.async_io.run_async: run ``fn``
    inline and route its result/exception to the callbacks, so the
    GUI-thread dispatch the real helper does is collapsed to a direct
    call in tests."""
    try:
        res = fn(*args)
    except Exception as e:
        if on_error is not None:
            on_error(e)
        return None
    if on_result is not None:
        on_result(res)
    return res


class _RecordingProvider:
    def __init__(self):
        self.calls = []
        self.get_item_calls = []
        self.item_meta = {}  # item_id -> meta dict returned by get_item

    def toggle_favorite(self, item_id, state):
        self.calls.append((item_id, state))

    def get_item(self, item_id):
        self.get_item_calls.append(item_id)
        return self.item_meta.get(item_id, {})

    def get_image_url(self, item_id, kind="Primary", size=512):
        return f"http://img/{item_id}"


class _Ctx:
    def __init__(self, source_id):
        self.source_id = source_id


class _QueueMgr:
    def __init__(self, source_id):
        self.context = _Ctx(source_id)


class _SignalRecorder:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _Bus:
    def __init__(self):
        self.favorite_toggled = _SignalRecorder()


class _FakeFavBtn:
    def __init__(self):
        self.icon_set = None

    def setIcon(self, ic):
        self.icon_set = ic


def _bare_page(monkeypatch, *, live_source="album-7", live_fav=False):
    """A NowPlayingPage with only the attributes _on_favorite_cta touches,
    and module-level run_async / icon helpers neutralised."""
    monkeypatch.setattr(npp_mod, "run_async", _fake_run_async)
    monkeypatch.setattr(npp_mod, "accent_icon", lambda name: ("accent", name))
    monkeypatch.setattr(npp_mod, "icon", lambda name: ("plain", name))

    page = NowPlayingPage.__new__(NowPlayingPage)
    page.api = _RecordingProvider()
    page.bus = _Bus()
    page.queue_mgr = _QueueMgr(live_source)
    page._preview_id = ""
    page._preview_meta = {}
    page._live_source_fav = live_fav
    page._fav_cta = _FakeFavBtn()
    return page


def test_live_favorited_toggles_to_unfavorite(monkeypatch):
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=True)
    page._on_favorite_cta()
    # Starting favourited → the request must UN-favorite (the bug: it
    # always sent True because cur_fav read the empty _preview_meta).
    assert page.api.calls == [("album-7", False)]
    assert page._live_source_fav is False
    assert page.bus.favorite_toggled.emitted == [("album-7", False)]
    # Heart goes to the outline glyph.
    assert page._fav_cta.icon_set == ("plain", "favorite_outline")


def test_live_not_favorited_toggles_to_favorite(monkeypatch):
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=False)
    page._on_favorite_cta()
    assert page.api.calls == [("album-7", True)]
    assert page._live_source_fav is True
    assert page.bus.favorite_toggled.emitted == [("album-7", True)]
    assert page._fav_cta.icon_set == ("accent", "favorite_filled")


def test_live_double_toggle_round_trips(monkeypatch):
    # fav → unfav → fav: state flips each time, proving the source is
    # read back (not always-False as in the bug).
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=True)
    page._on_favorite_cta()
    page._on_favorite_cta()
    assert [c[1] for c in page.api.calls] == [False, True]
    assert page._live_source_fav is True


def test_preview_branch_uses_preview_meta(monkeypatch):
    # The preview branch is unchanged: it reads/writes _preview_meta.
    page = _bare_page(monkeypatch)
    page._preview_id = "playlist-3"
    page._preview_meta = {"UserData": {"IsFavorite": True}}
    page._on_favorite_cta()
    assert page.api.calls == [("playlist-3", False)]
    assert page._preview_meta["UserData"]["IsFavorite"] is False


def test_no_target_id_is_a_noop(monkeypatch):
    page = _bare_page(monkeypatch, live_source="", live_fav=False)
    page._on_favorite_cta()
    assert page.api.calls == []
    assert page.bus.favorite_toggled.emitted == []


def test_bus_signal_seeds_live_source_fav(monkeypatch):
    # An external favourite (phone / web) for the live source must seed
    # the page's authoritative state so the next CTA read is correct.
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=False)
    page._preview_id = ""
    # Stand in for QListContainer.is_dragging via the real handler path:
    # _on_favorite_toggled doesn't touch the list, only the fav state.
    page._on_favorite_toggled("album-7", True)
    assert page._live_source_fav is True
    # A non-matching id leaves it alone.
    page._on_favorite_toggled("other-album", False)
    assert page._live_source_fav is True


# --- load-time seeding of the live-source fav state (the HIGH's finish) ---
# The CTA fix alone left a residual gap: _live_source_fav defaulted False
# and was only corrected by a toggle or an external event, so an already-
# favourited album/playlist showed an UNfilled heart on fresh live load.
# _on_context_changed now fetches the real state (staleness-guarded).


def test_apply_live_source_fav_sets_state_and_icon(monkeypatch):
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=False)
    page._apply_live_source_fav("album-7", {"UserData": {"IsFavorite": True}})
    assert page._live_source_fav is True
    assert page._fav_cta.icon_set == ("accent", "favorite_filled")


def test_apply_live_source_fav_staleness_guard(monkeypatch):
    # A reply for a source the user has since moved off must be dropped,
    # so it can't clobber the current source's heart.
    page = _bare_page(monkeypatch, live_source="album-NEW", live_fav=False)
    page._apply_live_source_fav("album-OLD", {"UserData": {"IsFavorite": True}})
    assert page._live_source_fav is False  # unchanged
    assert page._fav_cta.icon_set is None  # icon untouched


def test_apply_live_source_fav_during_preview_keeps_preview_glyph(monkeypatch):
    # The live state is recorded for when the user returns to live, but
    # the visible heart keeps the previewed item's glyph while previewing.
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=False)
    page._preview_id = "playlist-3"
    page._preview_meta = {"UserData": {"IsFavorite": False}}
    page._apply_live_source_fav("album-7", {"UserData": {"IsFavorite": True}})
    assert page._live_source_fav is True  # recorded for live
    assert page._fav_cta.icon_set == ("plain", "favorite_outline")  # preview glyph


def _stub_context_refreshers(page):
    page._list_container = type("_LC", (), {"is_dragging": lambda self: False})()
    page._refresh_track_list = lambda: None


def test_context_change_fetches_real_fav_state(monkeypatch):
    # An already-favourited source must load with a filled heart.
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=False)
    page.api.item_meta = {"album-7": {"UserData": {"IsFavorite": True}}}
    _stub_context_refreshers(page)
    page._on_context_changed(_Ctx("album-7"))
    assert page.api.get_item_calls == ["album-7"]
    assert page._live_source_fav is True
    assert page._fav_cta.icon_set == ("accent", "favorite_filled")


def test_context_change_no_source_skips_fetch(monkeypatch):
    page = _bare_page(monkeypatch, live_source="", live_fav=False)
    _stub_context_refreshers(page)
    page._on_context_changed(_Ctx(""))
    assert page.api.get_item_calls == []
    assert page._live_source_fav is False


def test_refresh_fav_cta_icon_reads_authoritative_state(monkeypatch):
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=True)
    page._refresh_fav_cta_icon()
    assert page._fav_cta.icon_set == ("accent", "favorite_filled")
    # Preview overrides the live state for the visible glyph.
    page._preview_id = "playlist-3"
    page._preview_meta = {"UserData": {"IsFavorite": False}}
    page._refresh_fav_cta_icon()
    assert page._fav_cta.icon_set == ("plain", "favorite_outline")


def test_clear_preview_restamps_heart_from_live_state(monkeypatch):
    # Returning to live (no context change) must re-stamp the heart from
    # _live_source_fav, not leave the previewed item's filled glyph.
    page = _bare_page(monkeypatch, live_source="album-7", live_fav=False)
    page._preview_id = "playlist-3"
    page._preview_meta = {"UserData": {"IsFavorite": True}}
    page._preview_tracks = []
    page._refresh_now_playing = lambda *_a, **_k: None
    page._refresh_track_list = lambda: None
    page._refresh_meta_line = lambda: None
    page._update_lyrics_visibility = lambda: None
    page._update_cta_visibility = lambda: None
    page.preview_changed = _SignalRecorder()
    monkeypatch.setattr(npp_mod, "get_now_playing", lambda: object())
    page.clear_preview()
    assert page._preview_id == ""
    assert page._fav_cta.icon_set == ("plain", "favorite_outline")


# --- _on_dpr_changed preserves the preview KIND (audit 2026-06-01 §1.6) ---
# A DPR change during a preview must NOT relabel the preview kind. The old
# code round-tripped through load_preview to refresh the cover, which both
# (a) no-op'd — load_preview early-returns on the unchanged id+kind+meta, so
# the cover was never refetched at the new DPR — and (b) risked resetting a
# PLAYLIST preview to ALBUM. The handler now re-issues ONLY the cover at the
# new physical target and never touches _preview_kind, so the kind is
# trivially preserved AND the cover actually refreshes.


def test_on_dpr_changed_preserves_preview_kind(qapp, monkeypatch):
    from modules.player_state import QueueKind

    captured = []
    monkeypatch.setattr(npp_mod, "load_image_async", lambda key, *a, **k: captured.append(key))
    monkeypatch.setattr(npp_mod, "screen_dpr", lambda _w: 2.0)

    page = _bare_page(monkeypatch, live_source="album-7")
    page.load_preview = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("load_preview must not be called by _on_dpr_changed")
    )
    page._preview_id = "pl-1"
    page._preview_meta = {"Name": "PL", "UserData": {}}

    page._preview_kind = QueueKind.PLAYLIST
    page._on_dpr_changed()
    assert page._preview_kind == QueueKind.PLAYLIST  # preserved, not reset
    assert captured == ["pl-1|nppage"]  # cover re-issued at the new DPR

    captured.clear()
    page._preview_kind = QueueKind.ALBUM
    page._on_dpr_changed()
    assert page._preview_kind == QueueKind.ALBUM
    assert captured == ["pl-1|nppage"]

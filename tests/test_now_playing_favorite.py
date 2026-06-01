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


class _RecordingProvider:
    def __init__(self):
        self.calls = []

    def toggle_favorite(self, item_id, state):
        self.calls.append((item_id, state))


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
    monkeypatch.setattr(npp_mod, "run_async", lambda fn, *a, **k: fn(*a, **k))
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

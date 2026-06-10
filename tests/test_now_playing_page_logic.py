"""Unit coverage for three NowPlayingPage logic paths the audit flagged
as untested or dead:

- ``_on_dpr_changed`` preview branch re-issues the preview COVER at the
  new physical target (it used to round-trip ``load_preview``, whose
  early-return guard no-ops on the unchanged id+kind+meta — so the cover
  never refetched at the new DPR).
- ``_on_row_clicked`` source→play index mapping (the click-path sibling
  of the tested Remove-path mapping in test_np_context_remove).
- ``_items_span_multiple_artists`` (pure staticmethod driving the per-row
  artist sub-line).

Driven on a bare page (``__new__`` to skip the heavy widget build) with
stubbed collaborators — no Qt paint, no audio, no server.
"""

from types import SimpleNamespace

import pytest

from jellytoast.now_playing_page import NowPlayingPage

# ── _on_dpr_changed preview-cover re-issue ──────────────────────────────────


def test_dpr_change_reissues_preview_cover(qapp, monkeypatch):
    import jellytoast.now_playing_page as npp

    captured = []
    monkeypatch.setattr(
        npp, "load_image_async", lambda key, *a, **k: captured.append(key)
    )
    # screen_dpr(self) would touch the (uninitialised) QWidget — stub it.
    monkeypatch.setattr(npp, "screen_dpr", lambda _w: 2.0)

    page = NowPlayingPage.__new__(NowPlayingPage)
    page._preview_id = "alb1"
    page._preview_kind = npp.QueueKind.ALBUM
    page._preview_meta = {"Name": "X", "UserData": {}}
    page.api = SimpleNamespace(get_image_url=lambda i, t, s: f"http://x/{i}")

    NowPlayingPage._on_dpr_changed(page)
    # Pre-fix: load_preview early-returned, so nothing loaded. Post-fix the
    # cover is re-issued directly at the standard nppage cache key.
    assert captured == ["alb1|nppage"]


def test_dpr_change_no_preview_meta_is_noop(qapp, monkeypatch):
    import jellytoast.now_playing_page as npp

    captured = []
    monkeypatch.setattr(
        npp, "load_image_async", lambda key, *a, **k: captured.append(key)
    )
    monkeypatch.setattr(npp, "screen_dpr", lambda _w: 2.0)

    page = NowPlayingPage.__new__(NowPlayingPage)
    page._preview_id = "alb1"
    page._preview_kind = npp.QueueKind.ALBUM
    page._preview_meta = {}  # no meta yet → nothing to re-issue
    page.api = SimpleNamespace(get_image_url=lambda i, t, s: f"http://x/{i}")

    NowPlayingPage._on_dpr_changed(page)
    assert captured == []


# ── _on_row_clicked source→play index mapping ───────────────────────────────


class _Sig:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def _click_page(kind, original_items, queue):
    page = NowPlayingPage.__new__(NowPlayingPage)
    page._preview_id = ""
    page._displayed_items_kind = kind
    page.queue_mgr = SimpleNamespace(original_items=original_items, queue=queue)
    page.bus = SimpleNamespace(track_jumped=_Sig())
    return page


def test_row_click_source_maps_to_play_index(qapp):
    # Source order [a, b, c]; play order [c, a, b]. Displayed row 2 == "c",
    # which sits at play-order index 0 → track_jumped(0), NOT (2).
    page = _click_page(
        "source",
        [{"Id": "a"}, {"Id": "b"}, {"Id": "c"}],
        [{"Id": "c"}, {"Id": "a"}, {"Id": "b"}],
    )
    NowPlayingPage._on_row_clicked(page, 2)
    assert page.bus.track_jumped.emitted == [(0,)]


def test_row_click_play_mode_passthrough(qapp):
    queue = [{"Id": "c"}, {"Id": "a"}, {"Id": "b"}]
    page = _click_page("play", queue, queue)
    NowPlayingPage._on_row_clicked(page, 2)
    assert page.bus.track_jumped.emitted == [(2,)]


def test_row_click_unknown_id_is_noop(qapp):
    # Source row resolves to an Id absent from the play queue → no emit.
    page = _click_page(
        "source",
        [{"Id": "a"}, {"Id": "b"}, {"Id": "z"}],
        [{"Id": "a"}, {"Id": "b"}],
    )
    NowPlayingPage._on_row_clicked(page, 2)
    assert page.bus.track_jumped.emitted == []


# ── _items_span_multiple_artists (pure staticmethod) ────────────────────────


@pytest.mark.parametrize(
    "items, expected",
    [
        ([], False),
        ([{"AlbumArtist": "Bjork"}, {"AlbumArtist": "Bjork"}], False),
        ([{"AlbumArtist": "Bjork"}, {"AlbumArtist": "Aphex Twin"}], True),
        # empty AlbumArtist falls back to Artists[0]
        ([{"AlbumArtist": "", "Artists": ["VA"]}, {"AlbumArtist": "VA"}], False),
        ([{"Artists": ["A"]}, {"Artists": ["B"]}], True),
        # strip().lower() collapses to the same key
        ([{"AlbumArtist": " Bjork "}, {"AlbumArtist": "bjork"}], False),
    ],
)
def test_items_span_multiple_artists(items, expected):
    assert NowPlayingPage._items_span_multiple_artists(items) is expected

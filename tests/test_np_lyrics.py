"""Tests for the lyrics content pipeline extracted into ``jellytoast.np_lyrics``.

The lyrics subsystem (fetch / cache / render per-line widgets / synced
highlight + auto-scroll) was moved verbatim out of ``now_playing_page.py``
into a ``_LyricsMixin`` that ``NowPlayingPage`` mixes in. These tests pin:

- the extraction wiring (NowPlayingPage IS-A _LyricsMixin; the helper
  classes re-export from the old module for back-compat),
- the ``_LyricsCache`` LRU semantics, and
- the moved render path running correctly on a real instance that mixes
  in ``_LyricsMixin`` (the production scenario), driven offscreen.

Before this extraction the lyrics code had ZERO direct coverage.
"""

from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from jellytoast.np_lyrics import _LyricsCache, _LyricsMixin

# ── Extraction wiring ───────────────────────────────────────────────────────


def test_now_playing_page_mixes_in_lyrics_mixin():
    from jellytoast.now_playing_page import NowPlayingPage

    assert issubclass(NowPlayingPage, _LyricsMixin)
    # Single Qt base — the mixin must be a plain object, not a QObject,
    # so NowPlayingPage(QWidget) keeps exactly one Qt ancestor.
    assert _LyricsMixin.__bases__ == (object,)


def test_helper_classes_reexported_from_old_module():
    # now_playing_page re-imports these (used in its __init__/_build_left_pane);
    # keep them addressable on the old module for any back-compat reference.
    import jellytoast.now_playing_page as npp
    from jellytoast import np_lyrics

    assert npp._LyricsCache is np_lyrics._LyricsCache
    assert npp._ScrollbarFader is np_lyrics._ScrollbarFader


# ── _LyricsCache (LRU keyed by item_id) ─────────────────────────────────────


def test_lyrics_cache_miss_then_hit():
    c = _LyricsCache()
    assert c.get("a") == (False, None)
    c.put("a", {"Lyrics": [{"Text": "hi"}]})
    hit, data = c.get("a")
    assert hit is True
    assert data == {"Lyrics": [{"Text": "hi"}]}


def test_lyrics_cache_stores_none_as_a_real_hit():
    # A track with no lyrics caches ``None`` — a subsequent lookup must
    # report a HIT (so we don't re-fetch), distinct from a miss.
    c = _LyricsCache()
    c.put("inst", None)
    assert c.get("inst") == (True, None)


def test_lyrics_cache_lru_eviction_respects_recency():
    c = _LyricsCache(capacity=2)
    c.put("a", None)
    c.put("b", None)
    # Touch "a" so "b" becomes the least-recently-used.
    assert c.get("a")[0] is True
    c.put("c", None)  # over capacity → evict LRU ("b")
    assert c.get("b")[0] is False
    assert c.get("a")[0] is True
    assert c.get("c")[0] is True


# ── Render path on a real mixin host (offscreen) ────────────────────────────


class _LyricsHost(_LyricsMixin, QWidget):
    """Minimal stand-in for NowPlayingPage: provides exactly the widgets +
    state the moved render path touches, and stubs the two left-pane-mode
    hooks the methods call back into (which stay on the real page)."""

    def __init__(self):
        super().__init__()
        self._lyrics_scroll = QScrollArea(self)
        self._lyrics_container = QWidget()
        self._lyrics_layout = QVBoxLayout(self._lyrics_container)
        self._lyrics_layout.addStretch(1)  # the trailing stretch the code preserves
        self._lyrics_scroll.setWidget(self._lyrics_container)
        self._lyrics_anim = QPropertyAnimation(
            self._lyrics_scroll.verticalScrollBar(), b"value", self
        )
        self._lyrics_widgets = []
        self._lyrics_starts_ms = []
        self._lyrics_synced = False
        self._active_line_idx = -1
        self._user_off_live = False
        self._lyric_scroll_is_auto = False
        self.hook_calls = []

    def _update_lyrics_visibility(self):
        self.hook_calls.append("visibility")

    def _update_live_btn_visibility(self):
        self.hook_calls.append("live_btn")


def _lines(*pairs):
    # pairs of (text, start_ms) → Jellyfin payload shape (Start is 100-ns ticks)
    return {"Lyrics": [{"Text": t, "Start": ms * 10_000} for t, ms in pairs]}


def test_render_synced_lyrics_builds_one_widget_per_line(qapp):
    host = _LyricsHost()
    host._render_lyrics_payload(_lines(("one", 0), ("two", 1000), ("three", 2000)))
    assert len(host._lyrics_widgets) == 3
    assert host._lyrics_synced is True
    assert host._lyrics_starts_ms == [0, 1000, 2000]
    # The render path pings the left-pane-mode hooks (kept on the page).
    assert "visibility" in host.hook_calls


def test_unsynced_lyrics_render_but_dont_drive_scroll(qapp):
    host = _LyricsHost()
    host._render_lyrics_payload(_lines(("a", 0), ("b", 0)))
    assert len(host._lyrics_widgets) == 2
    assert host._lyrics_synced is False


def test_empty_payload_clears_to_status_line(qapp):
    host = _LyricsHost()
    host._render_lyrics_payload(_lines(("a", 0)))
    assert host._lyrics_widgets  # populated
    host._render_lyrics_payload(None)  # "No lyrics available" status
    assert host._lyrics_widgets == []
    assert host._lyrics_synced is False


def test_update_active_lyric_bisects_to_current_line(qapp):
    host = _LyricsHost()
    host._render_lyrics_payload(_lines(("one", 0), ("two", 1000), ("three", 2000)))
    host._active_line_idx = -1
    host._update_active_lyric(1500)  # between line two (1000) and three (2000)
    assert host._active_line_idx == 1
    host._update_active_lyric(2500)  # past the last start
    assert host._active_line_idx == 2
    host._update_active_lyric(0)  # before the first non-zero start → line 0
    assert host._active_line_idx == 0

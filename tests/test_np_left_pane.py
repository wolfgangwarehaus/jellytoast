"""Tests for the left-pane mode controller extracted into
``jellytoast.np_left_pane``.

The cover/lyrics/visualizer mode state machine was moved verbatim out of
``now_playing_page.py`` into a ``_LeftPaneMixin`` that ``NowPlayingPage``
mixes in alongside ``_LyricsMixin``. These pin the extraction wiring (incl.
the MRO that keeps ``eventFilter``'s ``super()`` chaining to ``QWidget``),
the pure mode-cycle logic, and the mode-application path on a real host.

Before this extraction the mode controller had ZERO direct coverage.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QWidget

from jellytoast.np_left_pane import _LeftPaneMixin

# ── Extraction wiring ───────────────────────────────────────────────────────


def test_now_playing_page_mixes_in_left_pane_mixin():
    from jellytoast.now_playing_page import NowPlayingPage

    assert issubclass(NowPlayingPage, _LeftPaneMixin)
    assert _LeftPaneMixin.__bases__ == (object,)
    # MRO order matters: _LeftPaneMixin.eventFilter calls super().eventFilter,
    # which must chain past _LyricsMixin (no eventFilter) to QWidget.
    mro = [c.__name__ for c in NowPlayingPage.__mro__]
    assert mro.index("_LeftPaneMixin") < mro.index("_LyricsMixin") < mro.index("QWidget")
    assert NowPlayingPage.eventFilter.__qualname__ == "_LeftPaneMixin.eventFilter"


# ── Pure mode-cycle logic (staticmethod) ────────────────────────────────────


def test_mode_cycle_is_the_lyrics_visualizer_pair():
    nxt = _LeftPaneMixin._next_left_pane_mode
    assert nxt("lyrics") == "visualizer"
    assert nxt("visualizer") == "lyrics"
    assert nxt("cover") == "lyrics"  # cover is reachable only via saved setting


# ── Mode application on a real host (offscreen) ─────────────────────────────


class _PaneHost(_LeftPaneMixin, QWidget):
    """Minimal NowPlayingPage stand-in providing the widgets/state the mode
    controller touches. ``_build_visualizer_widget`` (real one spins up the
    FFT engine) and the lyrics-mixin re-snap are stubbed."""

    def __init__(self):
        super().__init__()
        self._lyrics_scroll = QScrollArea(self)
        self._lyrics_toggle_btn = QPushButton(self)
        self._live_btn = QPushButton(self)
        self._cover = QLabel(self)
        self._visualizer_widget = None
        self._np_left_pane_mode = "lyrics"
        self._preview_id = ""
        self._lyrics_widgets = []
        self._lyrics_starts_ms = []
        self._lyrics_synced = False
        self._user_off_live = False
        self._lyrics_toggle_eligible = False
        self._lyrics_toggle_hovered = False
        self.built_viz = 0

    def _build_visualizer_widget(self):
        self.built_viz += 1
        self._visualizer_widget = QWidget(self)

    def _scroll_to_active_lyric(self, idx):  # sibling lyrics-mixin method
        pass


def test_show_lyrics_property_tracks_mode(qapp):
    host = _PaneHost()
    host._np_left_pane_mode = "lyrics"
    assert host._show_lyrics is True
    host._np_left_pane_mode = "cover"
    assert host._show_lyrics is False
    host._np_left_pane_mode = "visualizer"
    assert host._show_lyrics is False


def test_lyrics_scroll_hidden_without_lyrics(qapp):
    host = _PaneHost()
    host._np_left_pane_mode = "lyrics"
    host._lyrics_widgets = []  # instrumental track — nothing to show
    host._update_lyrics_visibility()
    assert host._lyrics_scroll.isVisible() is False
    # The toggle stays eligible even when the current track has no lyrics.
    assert host._lyrics_toggle_eligible is True


def test_switch_to_visualizer_lazy_builds_once(qapp):
    host = _PaneHost()
    host._set_left_pane_mode("visualizer")
    assert host._np_left_pane_mode == "visualizer"
    assert host.built_viz == 1  # built once, not re-built by _update_lyrics_visibility
    assert host._visualizer_widget is not None


def test_preview_mode_hides_the_whole_left_pane(qapp):
    host = _PaneHost()
    host._preview_id = "album-9"
    host._update_lyrics_visibility()
    assert host._lyrics_scroll.isVisible() is False
    assert host._lyrics_toggle_btn.isVisible() is False
    assert host._live_btn.isVisible() is False
    assert host._lyrics_toggle_eligible is False

"""Left-pane mode controller for the now-playing page.

The cover ↔ lyrics ↔ visualizer mode state machine, extracted from
``now_playing_page.py``: which surface the left pane shows, the lazy
visualizer build, the lyrics-toggle button + its hover gate
(``eventFilter``), the "Live" pill visibility, and the mode-cycle logic.

``_LeftPaneMixin`` is mixed into ``NowPlayingPage`` alongside
``_LyricsMixin`` (see ``np_lyrics``) — it is *not* a standalone widget.
Its methods reference page-owned widgets/state (``self._lyrics_scroll``,
``self._cover``, ``self._live_btn``, ``self._visualizer_widget``,
``self._np_left_pane_mode`` …) set up by ``NowPlayingPage.__init__`` /
``_build_left_pane``, and call into the sibling lyrics mixin
(``_scroll_to_active_lyric``). All of that resolves on the combined
instance.

``eventFilter`` is a Qt override: with
``NowPlayingPage(_LeftPaneMixin, _LyricsMixin, QWidget)`` its
``super().eventFilter(...)`` chains past ``_LyricsMixin`` (which has none)
to ``QWidget.eventFilter`` — identical to before the split.
"""

from PySide6.QtCore import QEvent
from PySide6.QtGui import QCursor


class _LeftPaneMixin:
    """Left-pane cover/lyrics/visualizer mode controller, mixed into
    ``NowPlayingPage``. Plain-``object`` mixin so the page keeps a single
    Qt base (``QWidget``)."""

    # ── Left-pane mode helpers ───────────────────────────────────────────

    @property
    def _show_lyrics(self) -> bool:
        """Back-compat: lyrics are "shown" only when the left-pane
        mode is ``lyrics``. Cover-only and visualizer modes both
        hide the lyrics scroll."""
        return self._np_left_pane_mode == "lyrics"

    def _set_left_pane_mode(self, mode: str) -> None:
        """Persist + apply a new left-pane mode. Lazy-builds the
        visualizer widget on first switch into visualizer mode."""
        if mode not in ("cover", "lyrics", "visualizer"):
            mode = "lyrics"
        if mode == self._np_left_pane_mode:
            return
        self._np_left_pane_mode = mode
        try:
            from modules.settings import get_settings

            get_settings().np_left_pane_mode = mode
        except Exception:
            pass
        if mode == "visualizer" and self._visualizer_widget is None:
            self._build_visualizer_widget()
        self._update_lyrics_visibility()

    def _build_visualizer_widget(self) -> None:
        """Lazy-construct the visualizer + slot it into the lyrics-
        scroll's parent layout so swapping between modes is a single
        setVisible call instead of a layout rebuild. Also lazy-starts
        the FFT engine so the bars receive real band data; the engine
        is parented to the page so it tears down cleanly when the
        page is destroyed at app shutdown."""
        from modules.visualizer import VisualizerEngine
        from modules.visualizer_widget import VisualizerWidget

        widget = VisualizerWidget(self)
        widget.hide()
        # Insert into the same layout slot as the lyrics scroll so
        # they swap visibly. The parent layout is the page's main v;
        # take the lyrics scroll's index so the visualizer occupies
        # the exact vertical band.
        parent_layout = self._lyrics_scroll.parentWidget().layout()
        idx = parent_layout.indexOf(self._lyrics_scroll)
        if idx >= 0:
            parent_layout.insertWidget(idx, widget, 100)
        self._visualizer_widget = widget
        # Same hover gate as the lyrics scroll — moving the cursor over
        # the visualizer surface keeps the toggle button reachable.
        widget.installEventFilter(self)
        # Spin up the FFT engine on first widget build. On Linux this
        # spawns a parec subprocess that reads the default sink's
        # monitor source; on other OSes the default tap stays the
        # silence stub (per-OS backends are P4). Engine is parented to
        # the widget so Qt's cleanup chain stops it at app shutdown,
        # and we also connect destroyed → stop explicitly so the parec
        # subprocess is reaped promptly rather than waiting for the
        # Python finaliser.
        if self._visualizer_engine is None:
            self._visualizer_engine = VisualizerEngine(parent=widget)
            self._visualizer_engine.start()
            widget.destroyed.connect(self._visualizer_engine.stop)

    def _update_live_btn_visibility(self):
        # Live button only makes sense when lyrics are visible, synced,
        # and the user has actively scrolled away from the active line.
        show = (
            self._show_lyrics
            and self._lyrics_synced
            and self._user_off_live
            and not self._preview_id
        )
        self._live_btn.setVisible(show)

    def eventFilter(self, obj, event):
        # Hover gate for the lyrics toggle button — visible whenever the
        # cursor is over any left-pane content surface (lyrics scroll,
        # cover label, visualizer widget) or the toggle button itself.
        # Leave fires a short grace timer so flicking up to click the
        # button doesn't snap-hide it mid-motion.
        hover_targets = (
            self._lyrics_scroll,
            self._lyrics_toggle_btn,
            self._cover,
            self._visualizer_widget,
        )
        if obj in hover_targets:
            et = event.type()
            if et == QEvent.Type.Enter:
                self._lyrics_toggle_hovered = True
                self._lyrics_toggle_hide_timer.stop()
                self._sync_lyrics_toggle_visibility()
            elif et == QEvent.Type.Leave:
                self._lyrics_toggle_hide_timer.start()
        return super().eventFilter(obj, event)

    def _on_lyrics_hover_grace_done(self):
        # Re-check current cursor position — Qt's Leave fires when the
        # cursor moves to a child too. Geometric hit-test against every
        # hover target covers that case.
        gpos = QCursor.pos()

        def _over(widget):
            if widget is None or not widget.isVisible():
                return False
            return widget.rect().contains(widget.mapFromGlobal(gpos))

        self._lyrics_toggle_hovered = (
            _over(self._lyrics_scroll)
            or _over(self._lyrics_toggle_btn)
            or _over(self._cover)
            or _over(self._visualizer_widget)
        )
        self._sync_lyrics_toggle_visibility()

    def _sync_lyrics_toggle_visibility(self):
        # Always-visible-when-eligible. Earlier versions hover-gated this
        # to keep the surface minimal, but hiding it on Leave collapsed
        # the toggle row's height and shifted the visualizer up/down by
        # a few pixels every time the cursor crossed the pane boundary.
        # The button is already styled TEXT_FAINT → TEXT on hover so the
        # default appearance is subtle enough to live always-on.
        self._lyrics_toggle_btn.setVisible(self._lyrics_toggle_eligible)

    def _toggle_lyrics(self):
        """Flip the left-pane mode along the lyrics ↔ visualizer pair.
        Cover-only mode is reachable via the saved ``np_left_pane_mode``
        setting but isn't in the quick-toggle rotation; from cover the
        toggle lands on lyrics so the user can fall back into the main
        pair with one click. The lyrics scroll is hidden inside lyrics
        mode on instrumental tracks (no lyrics to show) — the pane sits
        empty under the cover, which lets the user pre-set lyrics mode
        before the next track that does have them."""
        cur = self._np_left_pane_mode
        nxt = self._next_left_pane_mode(cur)
        self._set_left_pane_mode(nxt)
        # Re-snap to active line when lyrics come back so the user
        # doesn't have to find the now-moment manually.
        if self._show_lyrics and self._lyrics_synced:
            self._user_off_live = False
            if 0 <= self._active_line_idx < len(self._lyrics_widgets):
                self._scroll_to_active_lyric(self._active_line_idx)
        self._update_live_btn_visibility()

    def _update_lyrics_visibility(self):
        # Preview mode forces lyrics + visualizer off — the user is
        # browsing, not listening.
        if self._preview_id:
            self._lyrics_scroll.hide()
            if self._visualizer_widget is not None:
                self._visualizer_widget.hide()
            self._lyrics_toggle_eligible = False
            self._lyrics_toggle_btn.hide()
            self._live_btn.hide()
            return

        mode = self._np_left_pane_mode

        # Lazy-build the visualizer the first time the page enters
        # visualizer mode. Without this, a saved "visualizer" setting
        # would land with self._visualizer_widget = None on the first
        # _update_lyrics_visibility call and the user would see an
        # empty pane.
        if mode == "visualizer" and self._visualizer_widget is None:
            self._build_visualizer_widget()

        # Lyrics scroll visibility — only when the mode is "lyrics"
        # and the active track actually has lyrics. The toggle stays
        # eligible in every live mode (we want it visible in cover /
        # visualizer mode too so the user can cycle back).
        has_lyrics = bool(self._lyrics_widgets) or bool(self._lyrics_starts_ms)
        self._lyrics_scroll.setVisible(mode == "lyrics" and has_lyrics)

        # Visualizer visibility — track the mode regardless of
        # whether lyrics happen to exist for this track.
        if self._visualizer_widget is not None:
            self._visualizer_widget.setVisible(mode == "visualizer")

        # Toggle is eligible whenever there's a non-trivial next
        # state to land on. In cover mode the next press flips to
        # lyrics (only useful if lyrics exist) — but we still want it
        # available, so eligibility is True whenever something
        # meaningful is reachable (visualizer is always meaningful).
        self._lyrics_toggle_eligible = True
        nxt = self._next_left_pane_mode(mode)
        next_label = {
            "lyrics": "Show lyrics",
            "visualizer": "Show visualizer",
            "cover": "Hide pane",
        }.get(nxt, "Show lyrics")
        self._lyrics_toggle_btn.setText(next_label)
        self._sync_lyrics_toggle_visibility()

    @staticmethod
    def _next_left_pane_mode(current: str) -> str:
        """Pick the next mode for a single toggle press.

        Cycle is the lyrics ↔ visualizer pair. Cover-only mode is
        reachable only via a saved setting; from cover the toggle
        lands on lyrics so the user falls back into the main pair
        with one click. Lyrics availability for the *current* track
        does not gate the cycle — pre-setting lyrics mode on an
        instrumental track is a valid intent (the next track may
        have them).
        """
        if current == "lyrics":
            return "visualizer"
        if current == "visualizer":
            return "lyrics"
        return "lyrics"  # cover → lyrics

"""Lyrics pane for the now-playing page.

The lyrics *content pipeline*, extracted from ``now_playing_page.py``:
fetching (per-track, with a small LRU), rendering per-line widgets,
synced-lyrics highlight + predictive auto-scroll, font-size restyling,
and the user-scroll / "Live" pill detection.

``_LyricsMixin`` is mixed into ``NowPlayingPage`` — it is *not* a
standalone widget. Its methods reference page-owned state and widgets
(``self._lyrics_scroll``, ``self._lyrics_layout``, ``self._lyrics_anim``,
``self.api``, ``self._lyrics_loaded`` …) that ``NowPlayingPage.__init__``
and ``_build_left_pane`` set up, and call back into the page's left-pane
mode controller (``_update_lyrics_visibility`` / ``_update_live_btn_visibility``).
Both directions resolve on the combined instance.

The left-pane *mode* state machine (cover ↔ lyrics ↔ visualizer, the
toggle button + hover chrome, ``eventFilter``) deliberately stays on the
page — it spans the visualizer + cover too and is a separate concern.
"""

import bisect
from collections import OrderedDict
from typing import Dict, List, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    Slot,
)
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QLabel,
    QScrollArea,
)

from jellytoast.async_io import run_async
from jellytoast.design_tokens import TYPE_BODY, type_qss
from jellytoast.player_state import get_now_playing
from jellytoast.ui_helpers import TEXT_DIM, TEXT_FAINT, ink_alpha


class _ScrollbarFader(QObject):
    """Drives a QScrollBar's `QGraphicsOpacityEffect` so the bar fades
    out after a short idle window and fades back in on scroll or hover.
    Layout is unaffected — the bar still occupies its slot, it's just
    invisible when not in use.

    Wakes on:
      - value changes (the user scrolled, or content scrolled them)
      - range changes (content size changed, e.g. queue swap)
      - mouse-enter on the bar itself or its parent scroll area's viewport
    """

    IDLE_MS = 900  # how long after last activity before fading
    FADE_MS = 220  # fade animation duration

    def __init__(self, scroll_area: QScrollArea):
        super().__init__(scroll_area)
        self._area = scroll_area
        self._bar = scroll_area.verticalScrollBar()
        self._effect = QGraphicsOpacityEffect(self._bar)
        self._effect.setOpacity(0.0)
        self._bar.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(self.FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._fade_out)

        self._bar.valueChanged.connect(self._wake)
        self._bar.rangeChanged.connect(self._wake)
        # Hover anywhere over the scroll area's viewport (including the
        # bar itself) keeps the bar awake.
        self._bar.installEventFilter(self)
        viewport = scroll_area.viewport()
        if viewport is not None:
            viewport.installEventFilter(self)

    def eventFilter(self, obj, event):
        t = event.type()
        if t in (QEvent.Type.Enter, QEvent.Type.MouseMove, QEvent.Type.Wheel):
            self._wake()
        return False

    def _wake(self, *_):
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._idle_timer.start(self.IDLE_MS)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()


class _LyricsCache:
    """Tiny LRU keyed by item_id. Avoids re-fetching when the user
    rapidly hops back and forth across the queue. Capacity matches a
    typical album side; bigger caches just hold memory."""

    def __init__(self, capacity: int = 32):
        self.capacity = capacity
        self._d: "OrderedDict[str, Optional[Dict]]" = OrderedDict()

    def get(self, item_id: str) -> "tuple[bool, Optional[Dict]]":
        if item_id in self._d:
            self._d.move_to_end(item_id)
            return True, self._d[item_id]
        return False, None

    def put(self, item_id: str, data: Optional[Dict]):
        self._d[item_id] = data
        self._d.move_to_end(item_id)
        while len(self._d) > self.capacity:
            self._d.popitem(last=False)


class _LyricsMixin:
    """Lyrics content pipeline, mixed into ``NowPlayingPage``.

    Plain-``object`` mixin: ``NowPlayingPage(_LyricsMixin, QWidget)`` keeps
    a single Qt base (``QWidget``). Signals (e.g. ``_lyrics_loaded``) stay
    declared on ``NowPlayingPage`` because they require ``QObject`` ancestry;
    these methods emit/connect them via ``self`` at runtime.
    """

    @Slot(str)
    def _on_lyrics_font_size_changed(self, _key: str):
        # Restyle every existing line with the new tier so the change is
        # visible immediately, no track skip required. Delegate to the
        # shared restyle helper so the settings read is hoisted once and
        # the no-op-CSS diff guard applies here too.
        self._restyle_lyrics_around(self._active_line_idx)
        # Re-snap so the active line lands at its proper anchor under
        # the new line spacing.
        if 0 <= self._active_line_idx < len(self._lyrics_widgets):
            self._scroll_to_active_lyric(self._active_line_idx)

    # ── Lyrics ──────────────────────────────────────────────────────────────

    def _fetch_lyrics(self, item_id: str):
        if not item_id:
            self._set_lyrics_text("")
            return
        hit, cached = self._lyrics_cache.get(item_id)
        if hit:
            self._render_lyrics_payload(cached)
            return
        if self._lyrics_loading_for == item_id:
            return  # already in flight
        # Offline mode: lyrics live only on the server (the offline store
        # doesn't carry them), so don't fire a live request — that honours
        # the "leave the network alone" contract and keeps the track-
        # transition path free of in-flight server calls (the 0.2.0 Windows
        # QA crash rode exactly that mix). Cache hits above still render.
        try:
            from jellytoast import offline as _offline

            if _offline.is_offline_mode():
                self._set_lyrics_text("Lyrics unavailable offline", muted=True)
                return
        except Exception:
            pass
        self._lyrics_loading_for = item_id
        self._set_lyrics_text("Loading lyrics…", muted=True)
        # Fetch on the shared QThreadPool; `_lyrics_loaded` is wired to
        # `_on_lyrics_loaded` and dispatches on the GUI thread.
        run_async(
            self.api.get_lyrics,
            item_id,
            on_result=lambda payload, iid=item_id: self._lyrics_loaded.emit(iid, payload),
            on_error=lambda _e, iid=item_id: self._lyrics_loaded.emit(iid, None),
        )

    @Slot(str, object)
    def _on_lyrics_loaded(self, item_id: str, payload):
        self._lyrics_cache.put(item_id, payload)
        if self._lyrics_loading_for == item_id:
            self._lyrics_loading_for = ""
        # Only render if this is still the active item — the user may
        # have skipped tracks while we were fetching.
        np = get_now_playing()
        if np.item_id == item_id:
            self._render_lyrics_payload(payload)

    def _render_lyrics_payload(self, payload: Optional[Dict]):
        if not payload:
            self._set_lyrics_text("No lyrics available", muted=True)
            return
        lines = payload.get("Lyrics") or []
        if not lines:
            self._set_lyrics_text("No lyrics available", muted=True)
            return

        # Synced if any line carries a non-zero `Start` (Jellyfin returns
        # 100-ns ticks). Build per-line widgets either way so we can
        # later highlight; for unsynced we just don't drive scroll.
        any_timed = any(int(ln.get("Start") or 0) > 0 for ln in lines)
        starts_ms: List[int] = []
        widgets: List[QLabel] = []
        for ln in lines:
            text = (ln.get("Text") or "").strip()
            start_ticks = int(ln.get("Start") or 0)
            start_ms = start_ticks // 10_000
            # Parent at construction: a parentless QLabel that gets styled
            # below realizes a transient top-level native window — on Windows
            # that flashes as a tiny "jellytoast" titlebar'd box on every track
            # change until insertWidget reparents it (see app.py boot note).
            label = QLabel(text or "♪", self._lyrics_container)  # blank → beat marker
            label.setWordWrap(True)
            # Left-align lyrics on a wide desktop pane — reads as verse
            # the way Apple Music macOS does. iOS centers; desktop is
            # different ergonomics.
            label.setAlignment(Qt.AlignmentFlag.AlignLeft)
            # Initial style: dim / falloff. The first synced position
            # tick will recolor by distance from the active line.
            label.setStyleSheet(self._lyric_line_css(distance=99))
            starts_ms.append(start_ms)
            widgets.append(label)

        self._install_lyrics_widgets(widgets, starts_ms, synced=any_timed)
        # If we're rendering mid-track (e.g. user opened the page after
        # playback already started), prime the highlight to the current
        # position straight away.
        if self._lyrics_synced:
            self._update_active_lyric(get_now_playing().position)

    def _install_lyrics_widgets(self, widgets: List[QLabel], starts_ms: List[int], synced: bool):
        # Wipe everything before the trailing stretch.
        while self._lyrics_layout.count() > 1:
            it = self._lyrics_layout.takeAt(0)
            w = it.widget() if it else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._lyrics_widgets = widgets
        self._lyrics_starts_ms = starts_ms
        self._lyrics_synced = synced
        self._active_line_idx = -1
        # Real per-line lyrics replace any status fallback label, so a
        # later theme re-stamp shouldn't try to recolor a stale label.
        self._status_label = None
        # Each new track resets the user's "off live" state — the
        # tracking auto-scroll picks up from the new track's first line.
        self._user_off_live = False
        for i, w in enumerate(widgets):
            self._lyrics_layout.insertWidget(i, w)
        self._lyrics_scroll.verticalScrollBar().setValue(0)
        self._update_lyrics_visibility()
        self._update_live_btn_visibility()

    def _set_lyrics_text(self, text: str, muted: bool = False):
        """Single-paragraph fallback used for status messages ("Loading…",
        "No lyrics available"). Clears any previously built per-line
        widgets so the synced highlight doesn't try to address them."""
        self._lyrics_widgets = []
        self._lyrics_starts_ms = []
        self._lyrics_synced = False
        self._active_line_idx = -1
        self._user_off_live = False
        self._status_label = None
        while self._lyrics_layout.count() > 1:
            it = self._lyrics_layout.takeAt(0)
            w = it.widget() if it else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not text:
            return
        color = TEXT_FAINT if muted else TEXT_DIM
        # Parent at construction — this status label ("Loading lyrics…") is
        # built+styled on EVERY track change; left parentless it flashes a
        # transient top-level "jellytoast" window on Windows before insertWidget
        # reparents it (the per-track popup). _lyrics_container owns the layout.
        label = QLabel(text, self._lyrics_container)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # Qt's stylesheet parser doesn't support line-height — drop it
        # rather than letting it warn at runtime. Lyrics labels rely on
        # default leading; spacing between successive lines is handled
        # by _lyrics_layout.spacing.
        label.setStyleSheet(f"color: {color}; {type_qss(TYPE_BODY)}")
        # Remember the fallback label + its muted intent so a live
        # theme flip can re-color it (the baked TEXT_FAINT/TEXT_DIM are
        # frozen at import).
        self._status_label = label
        self._status_muted = muted
        self._lyrics_layout.insertWidget(0, label)
        self._lyrics_scroll.verticalScrollBar().setValue(0)
        self._update_lyrics_visibility()
        self._update_live_btn_visibility()

    # Distance-from-active opacity falloff. Apple Music's lyrics view
    # is the genre reference: the active line is the loudest object on
    # the page, with surrounding lines fading by distance so the eye
    # naturally tracks the now-moment without losing the few lines
    # ahead. Index by absolute distance from the active line.
    _FALLOFF = (0.95, 0.70, 0.45, 0.28, 0.18)

    # Per-key (active_size, active_weight, active_pad, inactive_size,
    # inactive_weight, inactive_pad). Bookended by the smallest comfortable
    # readable size and a roomy desktop comfort size; "default" matches the
    # baseline shipped post-Phase-3.
    _LYRICS_SIZE_TABLE = {
        "small": (16, 600, 4, 12, 400, 2),
        "default": (18, 600, 6, 13, 400, 3),
        "large": (20, 600, 8, 14, 400, 4),
        "largest": (22, 700, 10, 16, 600, 5),
    }

    def _lyric_line_css(self, distance: int, size_key: str = "") -> str:
        # ``size_key`` is the lyrics_font_size value at the start of the
        # current restyle pass (hoisted out of the per-line loop). When
        # called from outside the loop the empty default falls back to
        # a settings read.
        if not size_key:
            from jellytoast.settings import get_settings

            size_key = get_settings().lyrics_font_size
        a_size, a_weight, a_pad, i_size, i_weight, i_pad = self._LYRICS_SIZE_TABLE.get(
            size_key, self._LYRICS_SIZE_TABLE["default"]
        )
        if distance == 0:
            return (
                f"color: {ink_alpha(0.95)}; "
                f"font-size: {a_size}px; font-weight: {a_weight}; "
                f"padding: {a_pad}px 0;"
            )
        idx = min(distance, len(self._FALLOFF) - 1)
        opacity = self._FALLOFF[idx]
        return (
            f"color: {ink_alpha(opacity)}; "
            f"font-size: {i_size}px; font-weight: {i_weight}; "
            f"padding: {i_pad}px 0;"
        )

    def _restyle_lyrics_around(self, active: int):
        # Recolor every line by its distance from `active`. Runs on every
        # active-line tick (~1 Hz for synced lyrics), so the per-line
        # work has to stay tight: hoist the settings read out of the
        # loop, build at most ``len(_FALLOFF)`` unique CSS strings (the
        # distance buckets), and skip setStyleSheet entirely when the
        # incoming string matches what's already on the widget — Qt
        # otherwise re-parses the QSS and re-cascades on every call.
        from jellytoast.settings import get_settings

        size_key = get_settings().lyrics_font_size
        cache: dict[int, str] = {}
        for i, w in enumerate(self._lyrics_widgets):
            distance = abs(i - active)
            css = cache.get(distance)
            if css is None:
                css = self._lyric_line_css(distance, size_key)
                cache[distance] = css
            if w.styleSheet() != css:
                w.setStyleSheet(css)

    def _restamp_lyrics_theme(self) -> None:
        """Re-color the lyrics on a live ``theme_changed`` flip. Called
        from ``now_playing_page._reapply_theme``.

        Synced lyrics self-correct on the next ~1 Hz position tick via
        ``_restyle_lyrics_around``, but the *unsynced* per-line widgets
        (built once at ``_lyric_line_css(distance=99)``) and the status
        fallback label ("Loading…", "No lyrics available") bake their
        ink at construction and would otherwise stay in the old palette
        — worst case white-on-light = invisible."""
        from jellytoast import ui_helpers as _u

        widgets = getattr(self, "_lyrics_widgets", None) or []
        if widgets:
            if self._lyrics_synced:
                # Re-color around the active line immediately rather than
                # waiting for the next tick.
                self._restyle_lyrics_around(self._active_line_idx)
            else:
                # Unsynced lines render at a uniform faint falloff — re-apply
                # that exact style so the flip is instant and stays uniform.
                css = self._lyric_line_css(distance=99)
                for w in widgets:
                    w.setStyleSheet(css)
        lbl = getattr(self, "_status_label", None)
        if lbl is not None:
            color = _u.TEXT_FAINT if getattr(self, "_status_muted", False) else _u.TEXT_DIM
            lbl.setStyleSheet(f"color: {color}; {type_qss(TYPE_BODY)}")

    @Slot(int)
    def _on_position_updated(self, ms: int):
        if self._lyrics_synced:
            self._update_active_lyric(ms)

    def _update_active_lyric(self, ms: int):
        if not self._lyrics_starts_ms:
            return
        # Find the latest line whose start <= ms. bisect_right gives the
        # insertion point for `ms+1`, so the active index is one less.
        idx = bisect.bisect_right(self._lyrics_starts_ms, ms) - 1
        if idx < 0:
            idx = 0
        if idx == self._active_line_idx:
            return
        self._active_line_idx = idx
        self._restyle_lyrics_around(idx)
        self._scroll_to_active_lyric(idx)

    def _scroll_to_active_lyric(self, idx: int):
        """Anchor the active line at ~38% from the top of the lyrics
        viewport so 2-3 upcoming lines stay visible — feels predictive
        rather than reactive (Apple Music pattern). Smooth 300ms ease-out
        via QPropertyAnimation on the vertical scroll bar."""
        if not (0 <= idx < len(self._lyrics_widgets)):
            return
        active = self._lyrics_widgets[idx]
        viewport = self._lyrics_scroll.viewport()
        if viewport is None or viewport.height() == 0:
            return
        # Each line widget's direct parent is the lyrics container, so
        # `pos()` is already container-relative — using mapTo() here
        # double-counts and overshoots, which knocked the active line
        # well above the visible area.
        active_y = active.pos().y()
        # Anchor a touch lower than 38%: at 0.42 the active line sits
        # comfortably near the eye-line of the viewport with the next
        # 2-3 lines below still visible. 38% on a tall pane was placing
        # the line just inside the top edge.
        target = active_y - int(viewport.height() * 0.42)
        bar = self._lyrics_scroll.verticalScrollBar()
        target = max(bar.minimum(), min(bar.maximum(), target))
        if abs(target - bar.value()) < 8:
            return  # < 8px move — skip to avoid jitter on short consecutive lines
        # Mark this scroll as auto so the valueChanged listener can tell
        # it apart from a manual user scroll. Cleared by the animation's
        # finished signal (wired in _build_left_pane).
        self._lyric_scroll_is_auto = True
        self._lyrics_anim.stop()
        self._lyrics_anim.setStartValue(bar.value())
        self._lyrics_anim.setEndValue(target)
        self._lyrics_anim.start()

    # ── Lyrics toggle + Live button ────────────────────────────────────

    def _on_lyrics_scrolled(self, _value: int):
        # If the scroll was triggered by our auto-anchor animation,
        # ignore — only user-initiated scrolls flip the off-live state.
        if self._lyric_scroll_is_auto:
            return
        if not self._lyrics_synced or not self._lyrics_widgets:
            return
        self._user_off_live = True
        self._update_live_btn_visibility()

    def _resnap_to_live(self):
        self._user_off_live = False
        self._update_live_btn_visibility()
        if 0 <= self._active_line_idx < len(self._lyrics_widgets):
            self._scroll_to_active_lyric(self._active_line_idx)

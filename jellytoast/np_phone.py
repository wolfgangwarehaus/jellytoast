"""Phone Now Playing layout (a mobile shell at compact width, live playback).

The desktop page splits cover / lyrics (left) and the track list (right), and
the transport lives in the bar under it. On a phone that read as three
competing strips, so the live page gets its own arrangement instead:

* the middle shows ONE surface — the big cover (with title, artist and a
  heart / ⋮ row under it), the lyrics, the visualizer or the queue; for all
  but the cover a slim header (thumb · title / artist · heart · ⋮) keeps the
  track in view;
* under it, always: a full-width seek bar with elapsed / format / remaining,
  shuffle · previous · play · next · repeat, and a row of surface toggles
  (cast · lyrics · visualizer · queue · sleep) — the active one is lit.

The bar hides while this is up (it would repeat the transport). The page's
own cover label, lyrics scroll, visualizer and track-list pane are MOVED in
here on entry and back on exit, so loading, sync and drag-reorder keep
working unchanged. Shuffle / repeat / sleep / cast and the track favorite
drive the transport bar's own controls — one source of truth.

``_PhonePlayerMixin`` is mixed into ``NowPlayingPage``; it relies on the
page's widgets and on ``attach_transport_bar`` having run. Strings use
``QCoreApplication.translate("NowPlayingPage", …)``, not ``self.tr``: at
runtime ``tr`` looks up the instance's class (NowPlayingPage) while lupdate
files mixin strings under the mixin's name, so they'd never translate.
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QSize, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from jellytoast import ui_helpers as _u
from jellytoast.design_tokens import (
    BTN_ICON,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_TINY,
    TYPE_TITLE,
    button_qss,
    font,
    rad,
    type_qss,
)
from jellytoast.icon_button import IconButton
from jellytoast.icons import accent_icon, icon
from jellytoast.player_state import get_now_playing


def _phone_btn(glyph: str, side: int, icon_px: int, tip: str) -> IconButton:
    b = IconButton()
    b.setIcon(icon(glyph))
    b.setProperty("_jt_icon", glyph)
    b.setIconSize(QSize(icon_px, icon_px))
    b.setFixedSize(side, side)
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setStyleSheet(button_qss(BTN_ICON))
    return b


class _PhonePlayerMixin:
    _phone_on = False
    _phone_built = False
    _phone_surface = "cover"  # cover | lyrics | visualizer | queue
    _phone_seeking = False
    _phone_playing = False
    _PHONE_THUMB = 52
    _SURFACES = ("lyrics", "visualizer", "queue")

    # ── Build ──────────────────────────────────────────────────────────────

    def _build_phone_pane(self) -> QWidget:
        pane = QWidget()
        pane.setStyleSheet("background: transparent;")
        v = QVBoxLayout(pane)
        v.setContentsMargins(4, 0, 4, 6)
        v.setSpacing(0)
        self._phone_v = v

        # Slim header for the lyrics / visualizer / queue surfaces.
        self._phone_header = QWidget()
        h = QHBoxLayout(self._phone_header)
        h.setContentsMargins(0, 4, 0, 12)
        h.setSpacing(12)
        self._phone_thumb = QLabel()
        self._phone_thumb.setFixedSize(self._PHONE_THUMB, self._PHONE_THUMB)
        self._phone_thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._phone_thumb.installEventFilter(self)  # tap → back to the cover
        h.addWidget(self._phone_thumb)
        col = QVBoxLayout()
        col.setSpacing(2)
        self._phone_h_title = _u.MarqueeLabel("")
        self._phone_h_artist = _u.MarqueeLabel("")
        col.addStretch(1)
        col.addWidget(self._phone_h_title)
        col.addWidget(self._phone_h_artist)
        col.addStretch(1)
        h.addLayout(col, 1)
        self._phone_h_heart = _phone_btn("favorite_outline", 44, 22, QCoreApplication.translate("NowPlayingPage", "Favorite"))
        self._phone_h_more = _phone_btn("more_vert", 44, 22, QCoreApplication.translate("NowPlayingPage", "More"))
        h.addWidget(self._phone_h_heart)
        h.addWidget(self._phone_h_more)
        v.addWidget(self._phone_header)

        # Middle: one surface at a time. The cover block is ours; lyrics,
        # visualizer and the queue pane are borrowed from the page on entry.
        self._phone_slot = QVBoxLayout()
        self._phone_slot.setContentsMargins(0, 0, 0, 0)
        self._phone_slot.setSpacing(0)
        v.addLayout(self._phone_slot, 1)

        self._phone_cover_block = QWidget()
        cb = QVBoxLayout(self._phone_cover_block)
        cb.setContentsMargins(0, 8, 0, 0)
        cb.setSpacing(0)
        self._phone_cover_row = QHBoxLayout()
        self._phone_cover_row.addStretch(1)
        self._phone_cover_row.addStretch(1)
        cb.addLayout(self._phone_cover_row)
        cb.addSpacing(18)
        # Heart · title / artist · ⋮ — the actions flank the track name
        # instead of taking a row of their own.
        self._phone_title = QLabel("")
        self._phone_title.setFont(font(TYPE_TITLE))
        self._phone_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._phone_title.setWordWrap(True)
        self._phone_artist = QLabel("")
        self._phone_artist.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._phone_artist_full = ""  # elided to one line in _phone_fit_cover
        names = QVBoxLayout()
        names.setContentsMargins(0, 0, 0, 0)
        names.setSpacing(4)
        names.addWidget(self._phone_title)
        names.addWidget(self._phone_artist)
        names.addStretch(1)
        # A FIXED-height box (a 2-line title + a 1-line artist): the cover is
        # sized against it, so a long title wrapping no longer shrinks / moves
        # the art on every track change.
        self._phone_meta = QWidget()
        meta = QHBoxLayout(self._phone_meta)
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(8)
        self._phone_heart = _phone_btn("favorite_outline", 44, 24, QCoreApplication.translate("NowPlayingPage", "Favorite"))
        self._phone_more = _phone_btn("more_vert", 44, 24, QCoreApplication.translate("NowPlayingPage", "More"))
        meta.addWidget(self._phone_heart, 0, Qt.AlignmentFlag.AlignVCenter)
        meta.addLayout(names, 1)
        meta.addWidget(self._phone_more, 0, Qt.AlignmentFlag.AlignVCenter)
        cb.addWidget(self._phone_meta)
        cb.addStretch(1)
        self._phone_slot.addWidget(self._phone_cover_block, 1)
        # Lyrics surface on a track without lyrics: say so, don't leave a
        # blank middle.
        self._phone_no_lyrics = QLabel(QCoreApplication.translate("NowPlayingPage", "No lyrics for this track"))
        self._phone_no_lyrics.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._phone_no_lyrics.hide()
        self._phone_slot.addWidget(self._phone_no_lyrics, 1)

        # "● Live" re-snap for scrolled-away lyrics lives under the slot.
        self._phone_live_row = QHBoxLayout()
        self._phone_live_row.setContentsMargins(0, 0, 0, 0)
        self._phone_live_row.addStretch(1)
        v.addLayout(self._phone_live_row)

        # ── Fixed controls ──
        self._phone_controls = QWidget()
        c = QVBoxLayout(self._phone_controls)
        c.setContentsMargins(0, 6, 0, 0)
        c.setSpacing(0)
        self._phone_seek = _u.ScrubbableSlider(Qt.Orientation.Horizontal)
        self._phone_seek.setRange(0, 1000)
        self._phone_seek.setMinimumHeight(32)
        self._phone_seek.sliderPressed.connect(self._phone_seek_pressed)
        self._phone_seek.sliderReleased.connect(self._phone_seek_released)
        self._phone_seek.valueChanged.connect(self._phone_seek_moved)
        c.addWidget(self._phone_seek)
        times = QHBoxLayout()
        times.setContentsMargins(2, 0, 2, 0)
        self._phone_cur = QLabel("0:00")
        self._phone_rem = QLabel("-0:00")
        self._phone_rem.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        times.addWidget(self._phone_cur)
        times.addStretch(1)
        times.addWidget(self._phone_rem)
        c.addLayout(times)
        c.addSpacing(8)  # breathing room between the times and the format line
        # The format line gets its own centred line: "Streaming · Bit
        # Perfect · FLAC · 1724 kbps" is too long to sit between the times
        # (it ran into them in a monospace font).
        self._phone_info = QLabel("")
        self._phone_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._phone_info.setWordWrap(True)
        c.addWidget(self._phone_info)
        c.addSpacing(16)

        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        self._phone_shuffle = _phone_btn("shuffle", 48, 22, QCoreApplication.translate("NowPlayingPage", "Shuffle"))
        self._phone_prev = _phone_btn("prev", 56, 30, QCoreApplication.translate("NowPlayingPage", "Previous"))
        self._phone_play = _phone_btn("play", 72, 44, QCoreApplication.translate("NowPlayingPage", "Play / Pause"))
        self._phone_next = _phone_btn("next", 56, 30, QCoreApplication.translate("NowPlayingPage", "Next"))
        self._phone_repeat = _phone_btn("repeat", 48, 22, QCoreApplication.translate("NowPlayingPage", "Repeat"))
        for i, b in enumerate(
            (
                self._phone_shuffle,
                self._phone_prev,
                self._phone_play,
                self._phone_next,
                self._phone_repeat,
            )
        ):
            if i:
                transport.addStretch(1)
            transport.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        c.addLayout(transport)
        c.addSpacing(10)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        self._phone_toggles = {}
        for i, (key, glyph, tip) in enumerate(
            (
                ("cast", "cast", QCoreApplication.translate("NowPlayingPage", "Cast")),
                ("lyrics", "lyrics", QCoreApplication.translate("NowPlayingPage", "Lyrics")),
                ("visualizer", "visualizer", QCoreApplication.translate("NowPlayingPage", "Visualizer")),
                ("queue", "queue", QCoreApplication.translate("NowPlayingPage", "Queue")),
                ("sleep", "moon", QCoreApplication.translate("NowPlayingPage", "Sleep timer")),
            )
        ):
            if i:
                bottom.addStretch(1)
            b = _phone_btn(glyph, 44, 22, tip)
            bottom.addWidget(b)
            self._phone_toggles[key] = b
        c.addLayout(bottom)
        v.addWidget(self._phone_controls)

        # Wiring.
        bus = self.bus
        self._phone_play.clicked.connect(lambda: bus.pause_toggled.emit())
        self._phone_prev.clicked.connect(lambda: bus.prev_track.emit())
        self._phone_next.clicked.connect(lambda: bus.next_track.emit())
        self._phone_shuffle.clicked.connect(lambda: self._phone_bar_click("shuffle"))
        self._phone_repeat.clicked.connect(lambda: self._phone_bar_click("repeat"))
        t = self._phone_toggles
        t["cast"].clicked.connect(lambda: self._phone_bar_click("cast"))
        t["sleep"].clicked.connect(lambda: self._phone_bar_click("sleep"))
        for key in self._SURFACES:
            t[key].clicked.connect(lambda _c=False, k=key: self._phone_toggle_surface(k))
        t["cast"].setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t["cast"].customContextMenuRequested.connect(
            lambda pos: self._transport_bar is not None
            and self._transport_bar.cast_context_requested.emit(t["cast"].mapToGlobal(pos))
        )
        for heart in (self._phone_heart, self._phone_h_heart):
            heart.clicked.connect(self._phone_toggle_favorite)
        for more in (self._phone_more, self._phone_h_more):
            more.clicked.connect(lambda _c=False, b=more: self._phone_more_menu(b))

        bus.position_updated.connect(self._phone_on_position)
        bus.duration_set.connect(lambda _ms: self._phone_on_position(get_now_playing().position))
        bus.playback_started.connect(self._phone_on_started)
        bus.playback_restored.connect(self._phone_on_restored)
        bus.playback_paused.connect(lambda: self._phone_set_playing(False))
        bus.playback_resumed.connect(lambda: self._phone_set_playing(True))
        bus.playback_stopped.connect(self._phone_on_stopped)
        bus.favorite_toggled.connect(lambda *_a: self._phone_refresh_heart())
        # The bar formats the "Streaming · FLAC · 1726 kbps" line; mirror it
        # once it has handled the update.
        for sig in (bus.streaming_info_updated, bus.cast_started, bus.cast_stopped):
            sig.connect(lambda *_a: QTimer.singleShot(0, self._phone_refresh_info))
        for sig in (
            bus.shuffle_changed,
            bus.repeat_changed,
            bus.sleep_timer_started,
            bus.sleep_timer_cancelled,
            bus.sleep_timer_fired,
            bus.cast_started,
            bus.cast_stopped,
        ):
            sig.connect(lambda *_a: self._phone_refresh_toggles())

        self._phone_restyle()
        pane.hide()
        self._phone_pane = pane
        self._phone_built = True
        return pane

    def _phone_restyle(self) -> None:
        self._phone_title.setStyleSheet(f"color: {_u.TEXT}; font-weight: 700;")
        self._phone_artist.setStyleSheet(f"color: {_u.TEXT_DIM}; {type_qss(TYPE_BODY)}")
        self._phone_h_title.setStyleSheet(
            f"color: {_u.TEXT}; {type_qss(TYPE_BODY)} font-weight: 600;"
        )
        self._phone_h_artist.setStyleSheet(f"color: {_u.TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self._phone_no_lyrics.setStyleSheet(f"color: {_u.TEXT_FAINT}; {type_qss(TYPE_BODY)}")
        for lbl in (self._phone_cur, self._phone_rem, self._phone_info):
            lbl.setStyleSheet(f"color: {_u.TEXT_FAINT}; {type_qss(TYPE_TINY)}")
        bar = getattr(self, "_transport_bar", None)
        if bar is not None:
            self._phone_seek.setStyleSheet(bar._slider_qss())
        self._phone_refresh_toggles()

    @property
    def _show_lyrics(self) -> bool:
        # The phone player picks its own surface (not the saved desktop
        # left-pane mode) — the Live re-snap button follows that.
        if self._phone_on:
            return self._phone_surface == "lyrics"
        return self._np_left_pane_mode == "lyrics"

    # ── Mode switch ────────────────────────────────────────────────────────

    def phone_player_active(self) -> bool:
        return self._phone_on

    def _phone_wanted(self) -> bool:
        from jellytoast.platform_compat import is_mobile_shell

        return (
            self._phone_built
            and getattr(self, "_is_compact", False)
            and is_mobile_shell()
            and not self._preview_id
        )

    def _sync_phone_mode(self) -> None:
        want = self._phone_wanted()
        if want == self._phone_on:
            if want:
                self._phone_apply_surface()
            return
        if want:
            self._phone_enter()
        else:
            self._phone_exit()
        self.phone_player_changed.emit(want)

    def _phone_enter(self) -> None:
        self._phone_on = True
        self._compact_tabs.hide()
        self._left_pane.hide()
        # Borrow the page's surfaces.
        self._phone_cover_row.insertWidget(1, self._cover)
        for overlay in (self._fav_cta, self._download_cta):
            self._cover.removeEventFilter(overlay)
            overlay.hide()
        slot = self._phone_slot
        slot.addWidget(self._lyrics_scroll, 1)
        if self._visualizer_widget is not None:
            slot.addWidget(self._visualizer_widget, 1)
        slot.addWidget(self._right_pane, 1)
        self._phone_live_row.addWidget(self._live_btn)
        self._phone_pane.show()
        self._phone_restyle()
        self._phone_refresh_meta()
        np = get_now_playing()
        self._phone_playing = bool(np.item_id) and not np.is_paused
        self._phone_set_playing(self._phone_playing)
        self._phone_on_position(np.position)
        self._phone_refresh_info()
        self._phone_apply_surface()

    def _phone_exit(self) -> None:
        self._phone_on = False
        self._phone_pane.hide()
        # Hand everything back to where the desktop layout keeps it.
        self._left_cover_row.insertWidget(1, self._cover)
        for overlay in (self._fav_cta, self._download_cta):
            self._cover.installEventFilter(overlay)
        home = self._lyrics_home_idx
        self._left_v.insertWidget(home, self._lyrics_scroll, 100)
        if self._visualizer_widget is not None:
            self._left_v.insertWidget(home, self._visualizer_widget, 100)
        self._outer.insertWidget(1, self._right_pane, 1)
        self._left_live_row.addWidget(self._live_btn)
        self._cover.show()
        self._right_pane.show()
        self._left_pane.show()
        self._compact_tabs.setVisible(getattr(self, "_is_compact", False))
        if getattr(self, "_is_compact", False):
            self._show_compact_pane(0 if self._tab_playing.isChecked() else 1)
        self._update_lyrics_visibility()

    # ── Surfaces ───────────────────────────────────────────────────────────

    def _phone_toggle_surface(self, key: str) -> None:
        self._phone_surface = "cover" if self._phone_surface == key else key
        if self._phone_surface == "lyrics" and self._lyrics_synced:
            self._user_off_live = False
        self._phone_apply_surface()
        if (
            self._phone_surface == "lyrics"
            and self._lyrics_synced
            and 0 <= self._active_line_idx < len(self._lyrics_widgets)
        ):
            QTimer.singleShot(0, lambda: self._scroll_to_active_lyric(self._active_line_idx))

    def _phone_apply_surface(self) -> None:
        if not self._phone_on:
            return
        s = self._phone_surface
        if s == "visualizer" and self._visualizer_widget is None:
            self._build_visualizer_widget()  # lands next to the lyrics, i.e. here
        self._phone_header.setVisible(s != "cover")
        self._phone_cover_block.setVisible(s == "cover")
        self._lyrics_scroll.setVisible(s == "lyrics")
        if self._visualizer_widget is not None:
            self._visualizer_widget.setVisible(s == "visualizer")
        self._right_pane.setVisible(s == "queue")
        self._update_live_btn_visibility()
        no_lyrics = s == "lyrics" and not (self._lyrics_widgets or self._lyrics_starts_ms)
        if no_lyrics:
            self._lyrics_scroll.setVisible(False)
        self._phone_no_lyrics.setVisible(no_lyrics)
        for key in self._SURFACES:
            self._phone_toggles[key].setStyleSheet(self._phone_toggle_qss(key == s))
        self._phone_fit_cover()

    @staticmethod
    def _phone_toggle_qss(active: bool) -> str:
        if not active:
            return button_qss(BTN_ICON)
        return (
            f"QPushButton {{ background: {_u.ink_alpha(0.14)}; border: none; "
            f"border-radius: 22px; }}"
        )

    def _phone_fit_cover(self) -> None:
        """The cover fills the width, or the height left over by the title
        block and the controls, whichever is smaller."""
        if not self._phone_on:
            return
        margins = self._root_layout.contentsMargins()
        avail_w = self.width() - margins.left() - margins.right() - 8
        meta_h = self._phone_meta_height()
        if self._phone_meta.height() != meta_h:
            self._phone_meta.setFixedHeight(meta_h)
        self._phone_elide_artist()
        # Constant, not the block's sizeHint: that grew with a wrapped title.
        cb_extra = 8 + 18 + meta_h
        avail_h = (
            self.height()
            - margins.top()
            - margins.bottom()
            - self._phone_controls.sizeHint().height()
            - cb_extra
            - 12
        )
        side = max(type(self).COVER_SIZE, min(avail_w, avail_h))
        if side != self._cover.width():
            self.COVER_SIZE = side
            self._cover.setFixedSize(side, side)
            orig = getattr(self, "_cover_orig", None)
            if orig is not None and not orig.isNull():
                self._on_cover_loaded(orig)
        self._phone_refresh_thumb()

    def _phone_meta_height(self) -> int:
        title_line = self._phone_title.fontMetrics().lineSpacing()
        self._phone_title.setMaximumHeight(2 * title_line)  # wrap to 2 lines max
        return 2 * title_line + 4 + self._phone_artist.fontMetrics().lineSpacing()

    def _phone_elide_artist(self) -> None:
        w = self._phone_artist.width()
        text = self._phone_artist_full
        if w > 0 and text:
            text = self._phone_artist.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, w)
        self._phone_artist.setText(text)

    # ── Track info ─────────────────────────────────────────────────────────

    def _phone_refresh_meta(self) -> None:
        if not self._phone_built:
            return
        np = get_now_playing()
        title = np.title or QCoreApplication.translate("NowPlayingPage", "Nothing Playing")
        artist = np.subtitle or ""
        self._phone_title.setText(title)
        self._phone_artist_full = artist  # stays visible (empty) so nothing shifts
        self._phone_elide_artist()
        self._phone_h_title.setText(title)
        self._phone_h_artist.setText(artist)
        self._phone_refresh_heart()
        self._phone_refresh_thumb()

    def _phone_refresh_thumb(self) -> None:
        orig = getattr(self, "_cover_orig", None)
        if orig is None or orig.isNull():
            self._phone_thumb.clear()
            return
        side = self._PHONE_THUMB
        dpr = _u.screen_dpr(self)
        pix = _u.scale_pixmap_for_dpr(orig, side, dpr)
        self._phone_thumb.setPixmap(pix)
        self._phone_thumb.setStyleSheet(f"border-radius: {rad(6)}px;")

    def _phone_refresh_heart(self) -> None:
        fav = get_now_playing().is_favorite
        ic = accent_icon("favorite_filled") if fav else icon("favorite_outline")
        self._phone_heart.setIcon(ic)
        self._phone_h_heart.setIcon(ic)

    def _phone_refresh_info(self) -> None:
        bar = getattr(self, "_transport_bar", None)
        text = bar.streaming_info.text() if bar is not None else ""
        self._phone_info.setText(text)

    def _phone_refresh_toggles(self) -> None:
        bar = getattr(self, "_transport_bar", None)
        if bar is None or not self._phone_built:
            return
        on = bar.shuffle_btn.isChecked()
        self._phone_shuffle.setIcon(accent_icon("shuffle") if on else icon("shuffle"))
        mode = getattr(bar, "_repeat_state", "off")
        self._phone_repeat.setIcon(
            icon("repeat")
            if mode == "off"
            else accent_icon("repeat_one" if mode == "one" else "repeat")
        )
        armed = getattr(bar, "_sleep_deadline", None) is not None
        self._phone_toggles["sleep"].setIcon(accent_icon("moon") if armed else icon("moon"))
        casting = getattr(bar, "_casting", False)
        self._phone_toggles["cast"].setIcon(accent_icon("cast") if casting else icon("cast"))

    # ── Transport ──────────────────────────────────────────────────────────

    def _phone_bar_click(self, key: str) -> None:
        bar = getattr(self, "_transport_bar", None)
        if bar is None:
            return
        if key == "shuffle":
            bar.shuffle_btn.click()
        elif key == "repeat":
            bar.repeat_btn.click()
        elif key == "cast":
            bar.cast_requested.emit()
        elif key == "sleep":
            bar._open_sleep_menu(anchor=self._phone_toggles["sleep"])
        self._phone_refresh_toggles()

    def _phone_toggle_favorite(self) -> None:
        bar = getattr(self, "_transport_bar", None)
        if bar is not None:
            bar._toggle_favorite()  # the track, as the bar's own heart does
        self._phone_refresh_heart()

    def _phone_more_menu(self, anchor) -> None:
        from jellytoast.ui_helpers import opaque_menu

        np = get_now_playing()
        if not np.item_id:
            return
        menu = opaque_menu(self)
        from jellytoast import playlist_actions

        playlist_actions.add_playlist_submenu(menu, lambda sid=np.item_id: [sid], host=self)
        download = menu.addAction(QCoreApplication.translate("NowPlayingPage", "Download album"))
        download.setVisible(bool(getattr(self._download_cta, "_enabled", True)))
        fav_album = menu.addAction(
            QCoreApplication.translate("NowPlayingPage", "Remove album from favorites")
            if self._live_source_fav
            else QCoreApplication.translate("NowPlayingPage", "Add album to favorites")
        )
        fav_album.setVisible(bool(self.queue_mgr.context.source_id))
        menu.ensurePolished()
        chosen = menu.exec(
            anchor.mapToGlobal(QPoint(anchor.width() - menu.sizeHint().width(), anchor.height()))
        )
        if chosen is download:
            self._on_download_cta()
        elif chosen is fav_album:
            self._on_favorite_cta()

    def _phone_set_playing(self, playing: bool) -> None:
        self._phone_playing = playing
        if self._phone_built:
            self._phone_play.setIcon(icon("pause") if playing else icon("play"))

    def _phone_on_started(self, _np) -> None:
        self._phone_set_playing(True)
        self._phone_refresh_meta()
        self._phone_on_position(0)
        QTimer.singleShot(0, self._phone_refresh_info)

    def _phone_on_restored(self, np) -> None:
        self._phone_set_playing(False)
        self._phone_refresh_meta()
        self._phone_on_position(np.position)

    def _phone_on_stopped(self) -> None:
        self._phone_set_playing(False)
        self._phone_on_position(0)
        if self._preview_id or not self._phone_built:
            return
        # The queue ran out: match the page's idle state.
        idle = QCoreApplication.translate("NowPlayingPage", "Nothing Playing")
        for lbl in (self._phone_title, self._phone_h_title):
            lbl.setText(idle)
        self._phone_artist_full = ""
        for lbl in (self._phone_artist, self._phone_h_artist):
            lbl.setText("")
        self._phone_thumb.clear()
        self._phone_info.setText("")

    def _phone_on_position(self, ms: int) -> None:
        if not self._phone_built or self._phone_seeking:
            return
        dur = get_now_playing().duration
        live = dur <= 0
        self._phone_seek.setEnabled(not live)
        if not live:
            self._phone_seek.blockSignals(True)
            self._phone_seek.setValue(int(ms / dur * 1000))
            self._phone_seek.blockSignals(False)
        self._phone_set_times(ms, dur)

    def _phone_set_times(self, ms: int, dur: int) -> None:
        self._phone_cur.setText(_u.fmt_time(ms))
        self._phone_rem.setText("-" + _u.fmt_time(max(0, dur - ms)) if dur > 0 else "")

    def _phone_seek_pressed(self) -> None:
        self._phone_seeking = True

    def _phone_seek_moved(self, value: int) -> None:
        if self._phone_seeking:
            dur = get_now_playing().duration
            self._phone_set_times(int(value / 1000 * dur), dur)

    def _phone_seek_released(self) -> None:
        self._phone_seeking = False
        dur = get_now_playing().duration
        if dur > 0:
            self.bus.seek_requested.emit(int(self._phone_seek.value() / 1000 * dur))

    # ── Events ─────────────────────────────────────────────────────────────

    def _phone_event(self, obj, event) -> bool:
        """Tap on the header thumb → back to the cover. Returns True when
        handled (called from _LeftPaneMixin.eventFilter, the page's one)."""
        if obj is getattr(self, "_phone_thumb", None) and event.type() == QEvent.Type.MouseButtonRelease:
            self._phone_surface = "cover"
            self._phone_apply_surface()
            return True
        return False

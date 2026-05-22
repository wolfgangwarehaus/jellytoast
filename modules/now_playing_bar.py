"""
Bottom Now Playing bar + Cast device picker dialog.
"""

from typing import List
from PySide6.QtCore import Qt, QTimer, Signal, Slot, QSize, QPoint, QEvent, QRectF, QPointF
from PySide6.QtGui import (
    QColor,
    QPixmap,
    QPainter,
    QPainterPath,
    QIcon,
    QCursor,
    QPen,
)
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QDialog,
    QListWidget,
    QListWidgetItem,
    QFrame,
    QApplication,
    QScrollArea,
    QSizePolicy,
    QToolTip,
)

from modules.icons import icon, accent_icon


def _round_corners(pix: QPixmap, tl: int, tr: int, br: int, bl: int) -> QPixmap:
    """Round individual corners of a pixmap. Each parameter is the radius
    for one corner; pass 0 for square. Used by the now-playing bar so the
    cover's bottom-left corner matches the window's body radius while the
    inside edges read as a card edge."""
    if pix.isNull():
        return pix
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = pix.width(), pix.height()
    path = QPainterPath()
    path.moveTo(tl, 0)
    path.lineTo(w - tr, 0)
    if tr > 0:
        path.quadTo(w, 0, w, tr)
    else:
        path.lineTo(w, 0)
    path.lineTo(w, h - br)
    if br > 0:
        path.quadTo(w, h, w - br, h)
    else:
        path.lineTo(w, h)
    path.lineTo(bl, h)
    if bl > 0:
        path.quadTo(0, h, 0, h - bl)
    else:
        path.lineTo(0, h)
    path.lineTo(0, tl)
    if tl > 0:
        path.quadTo(0, 0, tl, 0)
    else:
        path.lineTo(0, 0)
    path.closeSubpath()
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pix)
    painter.end()
    return out


from modules.player_state import PlayerBus, NowPlaying, get_now_playing
from modules.cast_manager import CastManager, CastDevice
from modules.providers import get_provider
from modules.async_io import run_async
from modules.ui_helpers import (
    ink_alpha,
    load_image_async,
    fmt_time,
    ACCENT,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    IDLE_TEXT,
    WASH_HOVER,
    WASH_PRESSED,
    ScrubbableSlider,
    MarqueeLabel,
    CoverOverlayButton,
    screen_dpr,
    opaque_menu,
)
from modules.theme import ink_rgb
from modules.design_tokens import (
    RADIUS_WINDOW,
    TYPE_SUBHEAD,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_TINY,
    TYPE_MICRO,
    font,
    type_qss,
)


class _VolumeSliderPopup(QFrame):
    """Floating vertical volume slider that sits above the volume button.

    Built as a child of the host main window (not a top-level Qt.Popup
    or Qt.Tool surface) — keeps positioning portable across X11 and
    Wayland (xdg-shell forbids client-set absolute positions for
    top-levels) and avoids the dismiss-on-click semantics that Qt.Popup
    forces. Hover lifecycle is owned by ``VolumeButton``: the popup
    just emits ``entered`` / ``left`` so the button can run a single
    grace timer covering both surfaces.
    """

    value_changed = Signal(int)
    entered = Signal()
    left = Signal()

    # Width matches the VolumeButton (36px) so the popup sits flush
    # over the button's square outline. Height is taller because the
    # slider needs vertical room.
    POPUP_W = 36
    POPUP_H = 135
    # Corner radius for the right-edge panel mode — matches the mini
    # player's BODY_RADIUS (the host-OS RADIUS_WINDOW) so the popup
    # reads as a built-in slot on the player's right side.
    _RIGHT_EDGE_CORNER_RADIUS = RADIUS_WINDOW

    def __init__(
        self,
        parent: QWidget,
        height: int | None = None,
        right_edge_mode: bool = False,
    ):
        super().__init__(parent)
        self.setObjectName("jtVolumePopup")
        self._right_edge_mode = right_edge_mode
        self.setFixedSize(self.POPUP_W, height if height is not None else self.POPUP_H)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # Vertical volume slider — needed in BOTH modes. Until this was
        # fixed the layout + slider were built inside
        # ``_apply_right_edge_qss``, which only runs in right-edge mode;
        # the center-mode popup (now-playing bar) ended up with no
        # ``self.slider`` at all, so ``set_value()`` raised
        # AttributeError and silently aborted the popup's show path —
        # the slider never appeared on the main window.
        #
        # Symmetric small margin top + bottom — just enough to clear
        # the popup's rounded-corner radius. The slider fills almost the
        # whole popup vertically so the handle can travel to both edges.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)
        self.slider = ScrubbableSlider(Qt.Orientation.Vertical)
        self.slider.setRange(0, 100)
        self.slider.setStyleSheet(self._slider_qss())
        self.slider.valueChanged.connect(self.value_changed.emit)
        layout.addWidget(self.slider, 0, Qt.AlignmentFlag.AlignHCenter)
        self.hide()
        # Live-accent: rebuild the slider QSS when the user picks a
        # new accent so the gauge colour follows immediately.
        PlayerBus.get().theme_changed.connect(self._reapply_accent)

        # Background matches WASH_HOVER — same fill as a hovered icon
        # button. In default mode the popup is fully rounded (8 px) so
        # it reads as one continuous shape with a hovered button. In
        # ``right_edge_mode`` the popup fills the right slice of its
        # host (mini player) and only the right corners round, matching
        # the host's BODY_RADIUS; left corners stay flat because they
        # abut the player's body content rather than free space.
        if right_edge_mode:
            self._apply_right_edge_qss(top_right_radius=self._RIGHT_EDGE_CORNER_RADIUS)
        else:
            self.setStyleSheet(f"""
                QFrame#jtVolumePopup {{
                    background: {WASH_HOVER};
                    border: none;
                    border-radius: 8px;
                }}
            """)

    def _apply_right_edge_qss(self, top_right_radius: int) -> None:
        """Refresh the right-edge popup's QSS. The bottom-right corner
        always matches the body radius (the popup sits flush with the
        player's bottom-right corner). The top-right radius is dynamic:
        when the popup occupies the player's full height it rounds to
        match the body's top-right; when the popup only occupies the
        bottom bar (expanded mini player), the top edge abuts the
        album art and the corner stays square.

        Pure stylesheet refresh — ``_position_popup`` calls this on
        every reposition, so it must not rebuild the layout or slider
        (those live in ``__init__`` and are mode-independent)."""
        br = self._RIGHT_EDGE_CORNER_RADIUS
        self.setStyleSheet(f"""
            QFrame#jtVolumePopup {{
                background: {WASH_HOVER};
                border: none;
                border-top-left-radius: 0px;
                border-bottom-left-radius: 0px;
                border-top-right-radius: {top_right_radius}px;
                border-bottom-right-radius: {br}px;
            }}
        """)

    @staticmethod
    def _slider_qss() -> str:
        """Vertical volume gauge QSS — BOTTOM (add-page, filled level)
        in the current accent, TOP (sub-page, headroom) dim grey.
        Built on each call so live-accent rebuilds pick up the fresh
        ACCENT module global without stale-baking it at construction."""
        from modules.ui_helpers import ACCENT as _ACCENT

        return f"""
            QSlider:vertical {{
                background: transparent;
            }}
            QSlider::groove:vertical {{
                /* Groove fills the slider widget (margin: 0). The
                   handle is auto-inset by Qt within the widget bounds,
                   so handle TOP at max = widget top, handle BOTTOM at
                   min = widget bottom. Combined with popup
                   contentsMargins of 6px, the dot lands ~6px inside
                   the popup's rounded edges at both extremes.
                   Groove is transparent — the visible track is
                   sub-page (above handle, light grey) + add-page
                   (below handle, purple). Some Qt styles skip
                   sub/add-page rendering when the groove has a
                   non-transparent background. */
                width: 4px;
                margin: 0;
                background: transparent;
            }}
            QSlider::sub-page:vertical {{
                background: {ink_alpha(0.25)};
                border-radius: 2px;
            }}
            QSlider::add-page:vertical {{
                background: {_ACCENT};
                border-radius: 2px;
            }}
            QSlider::handle:vertical {{
                width: 12px; height: 12px; margin: 0 -4px;
                background: {TEXT}; border-radius: 6px;
            }}
        """

    def _reapply_accent(self):
        self.slider.setStyleSheet(self._slider_qss())

    def set_value(self, v: int):
        was_blocked = self.slider.blockSignals(True)
        try:
            self.slider.setValue(v)
        finally:
            self.slider.blockSignals(was_blocked)

    def enterEvent(self, e):
        super().enterEvent(e)
        self.entered.emit()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.left.emit()


def _vert_speaker_slider_qss() -> str:
    """Per-speaker vertical slider QSS — accent on the BOTTOM (filled
    portion below the handle), dim grey on top. Speaker variant uses
    a slightly dimmed accent and a skinnier groove + handle so the
    master reads dominant by contrast. Mirrors the single-device /
    master slider style (transparent groove, sub-page + add-page
    paint the visible track) for consistency across all volume
    surfaces. Function so the accent is re-read each construction;
    live-accent rebuilds work as long as the popup is recreated on
    theme change."""
    from modules.theme import _hex_to_rgb

    ar, ag, ab = _hex_to_rgb(ACCENT)
    return f"""
        QSlider:vertical {{
            background: transparent;
        }}
        QSlider::groove:vertical {{
            width: 3px;
            margin: 0;
            background: transparent;
        }}
        QSlider::sub-page:vertical {{
            background: {ink_alpha(0.20)};
            border-radius: 2px;
        }}
        QSlider::add-page:vertical {{
            background: rgba({ar},{ag},{ab},0.75);
            border-radius: 2px;
        }}
        QSlider::handle:vertical {{
            width: 10px; height: 10px; margin: 0 -4px;
            background: {ink_alpha(0.80)}; border-radius: 5px;
        }}
        QSlider::handle:vertical:disabled {{
            background: {ink_alpha(0.30)};
        }}
    """


class _SpeakerColumn(QWidget):
    """One vertical volume bar for a single group-member speaker.
    Emits its name on hover (the popup shows it in a shared label, so
    it works regardless of the global tooltip setting) and its volume
    on change."""

    volume_changed = Signal(str, int)  # uuid, volume 0-100
    hovered = Signal(str)  # speaker name
    unhovered = Signal()

    COL_W = 34
    BAR_H = 104

    def __init__(self, uuid: str, name: str, volume: int, available: bool, parent=None):
        super().__init__(parent)
        self._uuid = uuid
        self._name = name
        self.setFixedWidth(self.COL_W)
        self.setStyleSheet("background: transparent;")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._slider = ScrubbableSlider(Qt.Orientation.Vertical)
        self._slider.setRange(0, 100)
        self._slider.setFixedHeight(self.BAR_H)
        self._slider.setStyleSheet(_vert_speaker_slider_qss())
        self._slider.setValue(max(0, min(100, int(volume))))
        if available:
            self._slider.valueChanged.connect(lambda val: self.volume_changed.emit(self._uuid, val))
        else:
            self._slider.setEnabled(False)
        v.addWidget(self._slider, 0, Qt.AlignmentFlag.AlignHCenter)

    @property
    def name(self) -> str:
        return self._name

    def reapply_accent(self):
        """Re-stamp the slider QSS with the current accent — called by
        the parent _GroupVolumePopup on theme_changed."""
        self._slider.setStyleSheet(_vert_speaker_slider_qss())

    def enterEvent(self, e):
        super().enterEvent(e)
        self.hovered.emit(self._name)

    def leaveEvent(self, e):
        super().leaveEvent(e)
        # The slider child triggers the column's leaveEvent — guard on
        # the cursor so the hover-name doesn't flap while still here.
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self.unhovered.emit()


class _Spinner(QWidget):
    """Small circular loading indicator — rotating 3/4 arc. Animates
    only while visible (timer runs in show/hide event hooks) so a
    hidden spinner doesn't cost CPU."""

    def __init__(self, size: int = 12, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setStyleSheet("background: transparent;")
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def _tick(self):
        self._angle = (self._angle + 24) % 360
        self.update()

    # Vertical paint offset (pixels) — Qt's QPushButton text is
    # centred on the font baseline, but arrow glyphs (◂ ▾) sit near
    # the cap height, so a geometrically-centred arc lands a couple
    # of pixels ABOVE the visual centre of the rendered arrow. This
    # nudges the arc down to ring the glyph cleanly.
    _Y_NUDGE = 2.0

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 2
        margin = (self.width() - side) / 2
        rect = QRectF(margin, margin + self._Y_NUDGE, side, side)
        # Subtle theme-ink at moderate alpha — rings the arrow without
        # competing with it (white on dark, near-black on light).
        pen = QPen(QColor(*ink_rgb(), 140))
        pen.setWidthF(1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        center = QPointF(self.width() / 2, self.height() / 2 + self._Y_NUDGE)
        p.translate(center)
        p.rotate(self._angle)
        p.translate(-center)
        # Qt's drawArc takes 16ths of a degree; 270*16 = 3/4 circle.
        p.drawArc(rect, 0, 270 * 16)


class _GroupVolumePopup(QFrame):
    """Volume popup variant for a Chromecast group. Vertical sliders
    laid out left → right: per-speaker columns (hidden by default) plus
    a thicker master column that always sits on the right edge so it
    stays anchored over the volume button as the popup grows / shrinks.

    Collapsed by default — only the master is visible until the user
    clicks the "▾ group" toggle, which kicks the (slow) member read and
    expands the popup leftward to reveal one bar per speaker.

    While expanded, hover-leave doesn't auto-dismiss (a stray cursor
    slip mid-mix would be terrible UX). Dismissal in that state is
    explicit: collapse via the arrow, or click anywhere outside the
    popup."""

    master_changed = Signal(int)
    member_changed = Signal(str, int)  # member uuid, volume 0-100
    expand_toggled = Signal(bool)  # user toggled the speakers section
    entered = Signal()
    left = Signal()
    relaid_out = Signal()

    # Master matches the single-device popup's slider 1:1 so the two
    # surfaces read as the same control — the dominance signal comes
    # from the speakers being skinnier/dimmer by contrast, not from
    # the master being beefed up. Width matches VolumeButton (36) so
    # the popup's right edge sits flush with the button's right edge
    # when anchored.
    MASTER_COL_W = 36
    SLIDER_H = 112
    # Width of the arrow toggle column on the popup's left edge.
    # Square'd so the loading spinner that ringa the arrow stays
    # visually centered on the glyph regardless of column height.
    ARROW_COL_W = 22

    @staticmethod
    def _master_slider_qss() -> str:
        """QSS for the group master slider — accent on the bottom
        (filled portion), dim grey on top. Mirrors the single-device
        slider style (transparent groove, sub-page + add-page paint
        the visible track) so the two surfaces look identical when
        switching between single-device and group cast modes."""
        return f"""
            QSlider:vertical {{
                background: transparent;
            }}
            QSlider::groove:vertical {{
                width: 4px;
                margin: 0;
                background: transparent;
            }}
            QSlider::sub-page:vertical {{
                background: {ink_alpha(0.25)};
                border-radius: 2px;
            }}
            QSlider::add-page:vertical {{
                background: {ACCENT};
                border-radius: 2px;
            }}
            QSlider::handle:vertical {{
                width: 12px; height: 12px; margin: 0 -4px;
                background: {TEXT}; border-radius: 6px;
            }}
        """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("jtGroupVolumePopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Background matches WASH_HOVER — same as single-device
        # VolumePopup, the VolumeButton hover state, AND every other
        # icon-button hover in the bar. All highlightable surfaces
        # share one fill so the popup + hovered button form one
        # continuous shape.
        self.setStyleSheet(f"""
            QFrame#jtGroupVolumePopup {{
                background: {WASH_HOVER};
                border: none;
                border-radius: 8px;
            }}
            QFrame#jtGroupVolumePopup QLabel {{ background: transparent; }}
        """)
        self._expanded = False
        self._member_cols: list = []
        # Tracks whether our app-level mouse filter is installed — only
        # active while the popup is expanded so we don't pay for it on
        # every event in the common collapsed-popup case.
        self._outside_filter_on = False
        # Group identity + restore-once flag. Set via set_group_uuid()
        # from the host VolumeButton when a group cast becomes active;
        # set_members() reads saved per-speaker volumes from settings
        # the first time this popup sees them and pushes them back to
        # the speakers via member_changed so the user's dialed-in
        # balance survives across cast sessions.
        self._group_uuid: str | None = None
        self._restored = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 8)
        outer.setSpacing(4)

        # Slider row — [arrow toggle][speakers (hidden when collapsed)][master].
        # Arrow on the LEFT edge so the chevron direction telegraphs the
        # expand direction: ▾ when collapsed (familiar dropdown), ◂ when
        # expanded (collapse-back affordance). Speakers fan into the
        # space between the arrow and master.
        sliders_row = QHBoxLayout()
        sliders_row.setContentsMargins(0, 0, 0, 0)
        sliders_row.setSpacing(6)

        # Arrow column: chevron toggle on top, loading spinner directly
        # below it (visible only while a member fetch is in flight after
        # an expand). Vertical stretches above and below keep the pair
        # visually centered with the slider columns next to it.
        arrow_col = QWidget()
        arrow_col.setFixedWidth(self.ARROW_COL_W)
        arrow_col.setStyleSheet("background: transparent;")
        acl = QVBoxLayout(arrow_col)
        acl.setContentsMargins(0, 0, 0, 0)
        acl.setSpacing(4)
        acl.addStretch(1)

        self._toggle_btn = QPushButton("▾")
        self._toggle_btn.setFixedSize(self.ARROW_COL_W, self.ARROW_COL_W)
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_FAINT};
                border: none; padding: 0;
                font-size: {TYPE_BODY.size_px}px;
            }}
            QPushButton:hover {{ color: {TEXT}; }}
        """)
        self._toggle_btn.clicked.connect(self._on_toggle)
        acl.addWidget(self._toggle_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        acl.addStretch(1)
        sliders_row.addWidget(arrow_col)

        # Spinner overlay — child of the toggle button so the rotating
        # arc rings the chevron itself instead of sitting beside it.
        # Same size as the button; the arc paints inset by 1px so it
        # doesn't quite touch the button's edges.
        self._spinner = _Spinner(size=self.ARROW_COL_W, parent=self._toggle_btn)
        self._spinner.move(0, 0)
        self._spinner.hide()

        self._speaker_area = QWidget()
        self._speaker_area.setStyleSheet("background: transparent;")
        self._speaker_layout = QHBoxLayout(self._speaker_area)
        self._speaker_layout.setContentsMargins(0, 0, 0, 0)
        self._speaker_layout.setSpacing(4)
        sliders_row.addWidget(self._speaker_area)
        self._speaker_area.hide()

        self._master_col = QWidget()
        self._master_col.setStyleSheet("background: transparent;")
        self._master_col.setFixedWidth(self.MASTER_COL_W)
        mcl = QVBoxLayout(self._master_col)
        mcl.setContentsMargins(0, 0, 0, 0)
        mcl.setSpacing(0)
        self._master_slider = ScrubbableSlider(Qt.Orientation.Vertical)
        self._master_slider.setRange(0, 100)
        self._master_slider.setFixedHeight(self.SLIDER_H)
        self._master_slider.setStyleSheet(self._master_slider_qss())
        self._master_slider.valueChanged.connect(self.master_changed.emit)
        mcl.addWidget(self._master_slider, 0, Qt.AlignmentFlag.AlignHCenter)
        sliders_row.addWidget(self._master_col)
        outer.addLayout(sliders_row)

        # (No separate "Finding speakers…" row — the spinner directly
        # under the arrow handles the loading affordance, so the
        # slider area doesn't shift when an expand kicks off a fetch.)

        # Footer — hover-name on the left (only meaningful when speakers
        # are visible) and a small "group" label fixed under the master
        # column on the right. Layout mirrors the slider row so the
        # label stays anchored under master as the popup grows.
        footer = QWidget()
        footer.setStyleSheet("background: transparent;")
        fh = QHBoxLayout(footer)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.setSpacing(6)
        # Spacer matches the arrow column width so the hover-name lines
        # up with the speaker area, not under the arrow toggle.
        fh.addSpacing(self.ARROW_COL_W)
        self._hover_name = QLabel("")
        self._hover_name.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        fh.addWidget(self._hover_name, 1)
        self._group_label = QLabel("group")
        self._group_label.setFixedWidth(self.MASTER_COL_W)
        self._group_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._group_label.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        fh.addWidget(self._group_label)
        outer.addWidget(footer)

        self.hide()
        # Live-accent: rebuild master + per-speaker slider QSS when the
        # accent changes so the gauges follow the new colour live.
        PlayerBus.get().theme_changed.connect(self._reapply_accent)

    def _reapply_accent(self):
        self._master_slider.setStyleSheet(self._master_slider_qss())
        for col in self._member_cols:
            col.reapply_accent()

    # ── master ──────────────────────────────────────────────────────
    def set_master_value(self, v: int):
        was = self._master_slider.blockSignals(True)
        try:
            self._master_slider.setValue(max(0, min(100, int(v))))
        finally:
            self._master_slider.blockSignals(was)

    def master_center_x(self) -> int:
        """X of the master column's center in popup-local coords. The
        host VolumeButton anchors this point over the button so the
        master visually stays put as the popup expands leftward.

        Computed from popup geometry instead of ``_master_col.geometry()``
        on purpose — after a collapse, Qt defers child-widget layout
        updates while the popup is hidden, so reading the live geometry
        returns the *expanded* master x and the next hover-open lands
        far to the left. Master is always the rightmost item in the
        slider row (no trailing stretch), so the popup's right body
        edge minus half the master column width is reliable in any
        state."""
        margins = self.contentsMargins()
        return self.width() - margins.right() - self.MASTER_COL_W // 2

    # ── expand / collapse ───────────────────────────────────────────
    def is_expanded(self) -> bool:
        return self._expanded

    def _on_toggle(self):
        self._expanded = not self._expanded
        self._apply_expanded()
        self.expand_toggled.emit(self._expanded)

    def collapse(self):
        """Programmatically collapse — outside-click dismissal uses this
        before hiding so the next hover-open returns to the calm
        single-slider state."""
        if not self._expanded:
            return
        self._expanded = False
        self._apply_expanded()
        self.expand_toggled.emit(False)

    def _apply_expanded(self):
        self._toggle_btn.setText("◂" if self._expanded else "▾")
        if self._expanded:
            has_cols = bool(self._member_cols)
            self._speaker_area.setVisible(has_cols)
            self._spinner.setVisible(not has_cols)
            self._enable_outside_click_filter(True)
            self.layout().invalidate()
            self.adjustSize()
        else:
            self._speaker_area.hide()
            self._spinner.hide()
            self._hover_name.setText("")
            self._enable_outside_click_filter(False)
            # Drop the speaker columns entirely so the slider row's
            # sizeHint definitely shrinks back. They rebuild on the
            # next expand via group_members_async (brief "Finding
            # speakers…" flash, but reliable layout).
            self._clear_speaker_cols()
            self.layout().invalidate()
            # Force the popup back to the collapsed footprint by
            # resizing from constants. adjustSize alone hasn't been
            # reliable here — Qt's layout cache holds the expanded
            # sizeHint past a visibility-only change to a nested
            # child, so the popup keeps painting wide-but-empty.
            self._snap_to_collapsed_size()
        self.relaid_out.emit()

    def _snap_to_collapsed_size(self):
        """Resize the popup directly to its collapsed footprint without
        going through the layout cache. Width = left margin + arrow
        column + spacing + master column + right margin. Margins come
        from the LAYOUT's contentsMargins, not the widget's (the
        widget's defaults to zero — we set the outer QVBoxLayout's
        margins in __init__, not the QFrame's). Height comes from
        sizeHint() since the slider's fixed height drives it and
        doesn't vary across expand state."""
        margins = self.layout().contentsMargins()
        w = margins.left() + self.ARROW_COL_W + 6 + self.MASTER_COL_W + margins.right()
        # Pre-empt minimumSize from holding a wider value than our
        # target after a previous expanded layout pass.
        self.setMinimumWidth(0)
        self.resize(w, self.sizeHint().height())

    def _clear_speaker_cols(self):
        while self._speaker_layout.count():
            item = self._speaker_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._member_cols = []

    def set_group_uuid(self, uuid: str):
        """Identify the active group so the popup can persist + restore
        a per-speaker volume balance keyed by this group."""
        self._group_uuid = uuid or None

    def set_members(self, members: list):
        """Populate the speaker section with one vertical bar per
        member. ``members``: [{uuid, name, volume, available}]. A member
        not in the discovery cache gets a disabled bar — its volume
        can't be read or set without a live connection to it.

        Default order is alphabetical by name. If the user has a saved
        balance for this group from a prior session, the volumes get
        overridden with the saved values and member_changed fires for
        each so the speakers themselves receive the restored level."""
        self._clear_speaker_cols()
        if members:
            members = sorted(members, key=lambda m: (m.get("name") or "").lower())
            if not self._restored and self._group_uuid:
                from modules.settings import get_settings

                saved = get_settings().cast_member_volumes.get(self._group_uuid, {})
                if saved:
                    for m in members:
                        u = m.get("uuid", "")
                        if u in saved and m.get("volume") != saved[u]:
                            m["volume"] = saved[u]
                            self.member_changed.emit(u, saved[u])
                    self._restored = True
            for m in members:
                col = _SpeakerColumn(
                    m.get("uuid", ""),
                    m.get("name") or "Speaker",
                    int(m.get("volume", 50)),
                    bool(m.get("available")),
                )
                col.volume_changed.connect(self.member_changed.emit)
                col.hovered.connect(self._hover_name.setText)
                col.unhovered.connect(lambda: self._hover_name.setText(""))
                self._speaker_layout.addWidget(col)
                self._member_cols.append(col)
        else:
            empty = QLabel("No speakers found")
            empty.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
            self._speaker_layout.addWidget(empty)
        if self._expanded:
            self._spinner.hide()
            self._speaker_area.show()
        self.adjustSize()
        self.relaid_out.emit()

    # ── hover lifecycle ─────────────────────────────────────────────
    def enterEvent(self, e):
        super().enterEvent(e)
        self.entered.emit()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self.left.emit()

    # ── outside-click dismissal (only while expanded) ───────────────
    def _enable_outside_click_filter(self, on: bool):
        app = QApplication.instance()
        if app is None:
            return
        if on and not self._outside_filter_on:
            app.installEventFilter(self)
            self._outside_filter_on = True
        elif not on and self._outside_filter_on:
            app.removeEventFilter(self)
            self._outside_filter_on = False

    def hideEvent(self, e):
        super().hideEvent(e)
        self._enable_outside_click_filter(False)

    def eventFilter(self, obj, event):
        if self._expanded and event.type() == QEvent.Type.MouseButtonPress:
            try:
                gp = event.globalPosition().toPoint()
            except AttributeError:
                gp = event.globalPos()
            if not self.rect().contains(self.mapFromGlobal(gp)):
                # Collapse first so the next hover-open returns to the
                # calm single-slider state, THEN hide. Don't consume —
                # let the click reach its target (the user clicked
                # *something* and probably meant to interact with it).
                self.collapse()
                self.hide()
        return False


class VolumeButton(QPushButton):
    """Volume icon button with hover popup, click-to-mute, and wheel
    scroll. Replaces the old inline ``vol_btn + vol_slider`` pair.

    Tracks volume via PlayerBus (volume_state / mute_state) so the
    popup slider always reflects the current mpv-side volume even
    when changes originate elsewhere (system mixer, MPRIS, etc.).
    """

    WHEEL_STEP = 2

    def __init__(
        self,
        bus,
        parent=None,
        size: int = 36,
        popup_height: int | None = None,
        popup_align: str = "center",
    ):
        super().__init__(parent)
        self.bus = bus
        self._volume = 80
        self._popup_height = popup_height
        # "center" — popup centered over the button (now-playing bar).
        # "right" — popup's right edge flush with the button's right
        # edge (mini player, so it sits flush with the bottom-right
        # control cluster instead of poking out past it).
        self._popup_align = popup_align
        self._popup: "_VolumeSliderPopup | _GroupVolumePopup | None" = None
        # Optional — set via set_cast_manager(). When the active cast is
        # a Chromecast group, the popup switches to the per-speaker
        # variant. None on surfaces that never wired it.
        self._cast_manager = None
        # Guards the (slow) group-member read so a flurry of expand
        # clicks doesn't stack up duplicate fetches.
        self._group_fetch_inflight = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(180)
        self._hide_timer.timeout.connect(self._maybe_hide_popup)

        # Icon scales with the button — 18 of 36 in the bar (50%), so
        # keep that ratio when callers shrink it for the mini player.
        icon_px = max(10, int(round(size * 0.5)))
        self._radius_px = max(3, int(round(size * 0.22)))
        radius_px = self._radius_px
        # Mute state — tracked so _reapply_theme can re-issue the right
        # glyph (volume vs volume_muted) in the new tint.
        self._muted = False
        self.setIcon(icon("volume"))
        self.setIconSize(QSize(icon_px, icon_px))
        self.setFixedSize(size, size)
        self.setToolTip("Mute / unmute · scroll to adjust · hover for slider")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Hover + pressed pull from the app-wide WASH tokens so every
        # highlightable surface in the bar shares one fill — button +
        # popup read as one continuous shape when the popup is open
        # over a hovered button.
        self.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; border-radius: {radius_px}px; }}
            QPushButton:hover {{ background: {WASH_HOVER}; }}
            QPushButton:pressed {{ background: {WASH_PRESSED}; }}
        """)
        self.clicked.connect(lambda: self.bus.mute_toggled.emit())

        # Track upstream state. Bar-construction code may push an
        # initial volume right after instantiation; bus events sync the
        # slider afterwards.
        self.bus.volume_state.connect(self._on_volume_state)
        self.bus.mute_state.connect(self._on_mute_state)
        self.bus.theme_changed.connect(self._reapply_theme)

    def set_cast_manager(self, cm):
        """Wire the CastManager so the popup can switch to the per-
        speaker group variant when the active cast is a Chromecast
        group. Optional — surfaces that don't call this just get the
        normal single slider."""
        self._cast_manager = cm

    def _sync_popup_value(self, v: int):
        """Push the current volume into whichever popup variant is
        live — the single slider or the group's master slider."""
        if self._popup is None:
            return
        if isinstance(self._popup, _GroupVolumePopup):
            self._popup.set_master_value(v)
        else:
            self._popup.set_value(v)

    def set_initial_volume(self, v: int):
        self._volume = max(0, min(100, int(v)))
        self._sync_popup_value(self._volume)

    @Slot(int)
    def _on_volume_state(self, v: int):
        self._volume = v
        self._sync_popup_value(v)

    @Slot(bool)
    def _on_mute_state(self, m: bool):
        self._muted = m
        self.setIcon(icon("volume_muted" if m else "volume"))

    @Slot()
    def _reapply_theme(self):
        """Re-issue the volume glyph in the fresh tint and rebuild the
        background-pill QSS on a live theme switch."""
        self.setIcon(icon("volume_muted" if self._muted else "volume"))
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" border-radius: {self._radius_px}px; }}"
            f"QPushButton:hover {{ background: {WASH_HOVER}; }}"
            f"QPushButton:pressed {{ background: {WASH_PRESSED}; }}"
        )

    # ── Hover lifecycle ────────────────────────────────────────────────
    def enterEvent(self, e):
        super().enterEvent(e)
        self._hide_timer.stop()
        self._show_popup()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._hide_timer.start()

    def _want_group_popup(self) -> bool:
        """True when the active cast is a Chromecast group — the cue to
        show the per-speaker popup instead of the single slider."""
        cm = self._cast_manager
        return (
            cm is not None
            and cm.active_cast is not None
            and getattr(cm.active_cast, "cast_type", "") == "group"
        )

    def _show_popup(self):
        # Resolve the host lazily — at construction time the button
        # isn't parented yet, so self.window() returns the button
        # itself. By first show, the bar is in the window's layout and
        # window() resolves to the top-level main window — which is
        # the only ancestor tall enough to host the popup above the bar.
        #
        # Right-edge mode prefers the painted "miniContainer" child of
        # the window when present (the mini player's body QFrame). The
        # popup then lives as a *sibling* of the stack inside the
        # container, so ``raise_()`` lifts it above the album-cover
        # label cleanly. Parenting to the top-level window worked for
        # z-order in theory but Wayland's translucent-top-level surface
        # ordering left the cover painted on top of the popup.
        window = self.window()
        host = window
        if self._popup_align == "right":
            container = window.findChild(QFrame, "miniContainer")
            if container is not None:
                host = container
        want_group = self._want_group_popup()
        is_group = isinstance(self._popup, _GroupVolumePopup)
        # Rebuild the popup when the host changed or the mode flipped
        # (single device <-> group). Within a mode it's reused.
        if self._popup is None or self._popup.parent() is not host or is_group != want_group:
            if self._popup is not None:
                self._popup.hide()
                self._popup.deleteLater()
                self._popup = None
            if want_group:
                self._popup = _GroupVolumePopup(host)
                self._popup.set_master_value(self._volume)
                # Tell the popup which group it's binding to so the
                # next set_members() can restore a saved per-speaker
                # balance keyed by this uuid.
                active = self._cast_manager.active_cast if self._cast_manager else None
                if active is not None and getattr(active, "uuid", ""):
                    self._popup.set_group_uuid(active.uuid)
                self._popup.master_changed.connect(self.bus.volume_changed.emit)
                self._popup.member_changed.connect(self._on_member_volume)
                self._popup.expand_toggled.connect(self._on_group_expand)
                self._popup.entered.connect(self._hide_timer.stop)
                self._popup.left.connect(self._hide_timer.start)
                self._popup.relaid_out.connect(self._position_popup)
            else:
                self._popup = _VolumeSliderPopup(
                    host,
                    height=self._popup_height,
                    right_edge_mode=(self._popup_align == "right"),
                )
                self._popup.set_value(self._volume)
                self._popup.value_changed.connect(self.bus.volume_changed.emit)
                self._popup.entered.connect(self._hide_timer.stop)
                self._popup.left.connect(self._hide_timer.start)
        self._sync_popup_value(self._volume)
        # adjustSize() first: _GroupVolumePopup has a dynamic,
        # content-driven height, so _position_popup must measure the
        # *real* height. Without this a freshly-built popup is
        # positioned against the pre-layout default and renders clipped
        # past the window's bottom edge — the "Speakers" toggle ends up
        # off-screen and unclickable. (_VolumeSliderPopup is fixed-size,
        # so adjustSize is a harmless no-op there.)
        self._popup.adjustSize()
        self._position_popup()
        self._popup.show()
        self._popup.raise_()
        # The group popup opens *collapsed* — members are fetched only
        # when the user expands it (see _on_group_expand), so the hover
        # popup stays small and stable with no async resize racing it.

    def _on_group_expand(self, expanded: bool):
        """The group popup's "Speakers" toggle was clicked. On expand,
        kick the (slow) member read once; collapse just reflows."""
        if (
            expanded
            and not self._group_fetch_inflight
            and self._cast_manager is not None
            and self._cast_manager.active_cast is not None
        ):
            self._group_fetch_inflight = True
            self._cast_manager.group_members_async(
                self._cast_manager.active_cast, self._on_group_members
            )
        self._position_popup()

    def _position_popup(self):
        """Anchor the popup. Horizontal placement depends on
        ``_popup_align``:

          • ``"center"`` — popup centred over the button just above it
            (now-playing bar default).
          • ``"right"`` — full-right panel mode used by the mini player.
            The popup fills the right slice of the host (mini player)
            from top to bottom, flush with the host's right edge and
            with right-rounded corners matching the player's body. Group
            popups stay anchored to the volume button's right edge in
            this mode (they're too tall to fill the player as a single
            column).
        """
        if self._popup is None:
            return
        host = self.window()
        # Right-edge panel mode — popup hugs the right edge of the host,
        # height = ``_popup_height`` (the host's bar-height; in the mini
        # player that's ``_BAR_HEIGHT = 96``), bottom-anchored.
        #
        # In compact mode the bar IS the whole player, so popup_y = 0
        # and the popup fills the entire right edge — its top-right
        # corner rounds to match the body. In expanded mode the popup
        # only covers the bottom bar (transport row), with a flat top
        # edge abutting the album art above and a rounded bottom-right
        # corner sitting flush with the body's bottom-right.
        if self._popup_align == "right" and isinstance(self._popup, _VolumeSliderPopup):
            popup_h = min(
                self._popup_height or _VolumeSliderPopup.POPUP_H,
                host.height(),
            )
            popup_y = host.height() - popup_h
            self._popup.setFixedSize(self._popup.width(), popup_h)
            self._popup.move(host.width() - self._popup.width(), popup_y)
            top_radius = (
                _VolumeSliderPopup._RIGHT_EDGE_CORNER_RADIUS
                if popup_y == 0
                else 0
            )
            self._popup._apply_right_edge_qss(top_right_radius=top_radius)
            return

        btn_top = self.mapTo(host, QPoint(self.width() // 2, 0))
        if isinstance(self._popup, _GroupVolumePopup):
            master_local = self._popup.master_center_x()
            if self._popup_align == "right":
                btn_right_x = self.mapTo(host, QPoint(self.width(), 0)).x()
                master_right_local = master_local + _GroupVolumePopup.MASTER_COL_W // 2
                popup_x = btn_right_x - master_right_local
            else:
                popup_x = btn_top.x() - master_local
        else:
            popup_x = btn_top.x() - self._popup.width() // 2
        popup_y = btn_top.y() - self._popup.height() - 6
        # Clamp inside the host so the popup is never partially
        # off-screen when the bar is up against a window edge.
        popup_x = max(4, min(popup_x, host.width() - self._popup.width() - 4))
        popup_y = max(4, popup_y)
        self._popup.move(popup_x, popup_y)

    def _on_group_members(self, members: list):
        self._group_fetch_inflight = False
        if isinstance(self._popup, _GroupVolumePopup):
            self._popup.set_members(members)

    def _on_member_volume(self, uuid: str, vol: int):
        if self._cast_manager is None:
            return
        self._cast_manager.set_member_volume_async(uuid, vol)
        # Persist the new level under the active group's uuid so the
        # next session restores this balance via _GroupVolumePopup.set_members.
        active = self._cast_manager.active_cast
        if active is None or not getattr(active, "uuid", ""):
            return
        from modules.settings import get_settings

        s = get_settings()
        saved = dict(s.cast_member_volumes)
        group_data = dict(saved.get(active.uuid, {}))
        group_data[uuid] = vol
        saved[active.uuid] = group_data
        s.cast_member_volumes = saved

    def _maybe_hide_popup(self):
        if self._popup is None:
            return
        # An expanded group popup is 'pinned' — it stays put until the
        # user collapses it, so a stray cursor-leave can't dismiss a
        # surface they're actively mixing on.
        if isinstance(self._popup, _GroupVolumePopup) and self._popup.is_expanded():
            return
        # Geometric hit-test, not underMouse(): underMouse() goes False
        # the instant the cursor is over a *child* of the popup (a
        # slider) — which would wrongly hide the popup the moment the
        # user reaches for a slider. rect().contains(mapFromGlobal(...))
        # is true anywhere within the popup's bounds, children included.
        gpos = QCursor.pos()
        over_popup = self._popup.isVisible() and self._popup.rect().contains(
            self._popup.mapFromGlobal(gpos)
        )
        over_button = self.rect().contains(self.mapFromGlobal(gpos))
        if over_popup or over_button:
            return
        self._popup.hide()

    def wheelEvent(self, e):
        # Conventional: scroll up → louder, scroll down → quieter.
        delta = e.angleDelta().y()
        if delta == 0:
            return
        if delta > 0:
            new_vol = min(100, self._volume + self.WHEEL_STEP)
        else:
            new_vol = max(0, self._volume - self.WHEEL_STEP)
        if new_vol != self._volume:
            self.bus.volume_changed.emit(new_vol)
        e.accept()


class _ScrobbleBadge(QLabel):
    """Compact "Scrobble" pill that surfaces server-side scrobbling.

    Reads ``settings.server_scrobbles_lastfm`` /
    ``settings.server_scrobbles_listenbrainz`` (populated on every login
    by ``modules.scrobble.navidrome_detect``). When both flags are
    false the badge hides itself entirely; when either is true it
    surfaces with a tooltip that names the destination(s) so the user
    knows their listening is being relayed without leaving jellytoast.

    Stays in sync with two events:
      - ``PlayerBus.scrobble_status_changed`` — fired by the scrobble
        layer when the detect run lands fresh flags after a login.
      - ``PlayerBus.theme_changed`` — re-stamps the accent so the pill
        tracks the active theme live.
    """

    _TOOLTIP_BOTH = "Your server scrobbles to Last.fm and ListenBrainz"
    _TOOLTIP_LASTFM = "Your server scrobbles to Last.fm"
    _TOOLTIP_LISTENBRAINZ = "Your server scrobbles to ListenBrainz"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("Scrobble")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # No focus / no mouse interaction beyond the tooltip; keeps the
        # badge from stealing tab focus or eating clicks on the cover
        # row underneath when the metadata block routes mouse events.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._apply_style()
        # Signal connects live in __init__ (see
        # feedback_signal_connects_in_init): only _apply_style is
        # re-callable from _reapply_accent.
        bus = PlayerBus.get()
        bus.scrobble_status_changed.connect(self._refresh)
        bus.theme_changed.connect(self._reapply_accent)
        self._refresh()

    def _apply_style(self):
        # Pull the active accent at call time so a theme swap re-stamps
        # the pill without needing fresh widget instances.
        self.setStyleSheet(
            "QLabel { "
            f"color: {ACCENT}; background: {ink_alpha(0.06)}; "
            f"border: 1px solid {ACCENT}; "
            "border-radius: 6px; padding: 2px 6px; "
            f"{type_qss(TYPE_MICRO)}"
            " }"
        )

    def _reapply_accent(self):
        self._apply_style()

    def _refresh(self):
        """Re-read settings and update visibility + tooltip.

        Defensive against settings import failure / missing accessors —
        on any error the badge stays hidden (the worst case for the
        user is "no badge" rather than a stack trace at construction).
        """
        try:
            from modules.settings import get_settings

            s = get_settings()
            lastfm = bool(s.server_scrobbles_lastfm)
            listenbrainz = bool(s.server_scrobbles_listenbrainz)
        except Exception:
            lastfm = False
            listenbrainz = False

        if lastfm and listenbrainz:
            self.setToolTip(self._TOOLTIP_BOTH)
            self.setVisible(True)
        elif lastfm:
            self.setToolTip(self._TOOLTIP_LASTFM)
            self.setVisible(True)
        elif listenbrainz:
            self.setToolTip(self._TOOLTIP_LISTENBRAINZ)
            self.setVisible(True)
        else:
            self.setToolTip("")
            self.setVisible(False)


class NowPlayingBar(QWidget):
    """Persistent transport at the bottom of the main window."""

    show_now_playing_requested = Signal()
    show_queue_requested = Signal()
    cast_requested = Signal()
    cast_context_requested = Signal(QPoint)  # right-click on the cast button

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.api = get_provider()
        self._is_seeking = False
        # Cast session state — when set, the streaming-info line shows
        # "Casting to <device>" instead of the local codec/bitrate.
        self._casting = False
        self._casting_device = ""
        # ``set_left_cluster_visible(False)`` (called when the
        # now-playing page is showing) hides the title/sub so the
        # full-page cover isn't duplicated by the bar. Our responsive
        # code also toggles title/sub visibility on resize; track the
        # page-suppression state so the two don't fight.
        self._left_suppressed = False
        # Track metadata is held as instance vars so the responsive
        # layout can re-render the same playing track in either
        # "combined" (2-row) or "split" (3-row) mode without needing
        # a fresh playback_started event.
        self._track_title = ""
        self._track_subtitle = ""
        self._track_album = ""
        self._track_year = ""
        self._text_mode: str | None = None  # combined / split / hide

        self.setFixedHeight(108)
        self.setObjectName("npbar")
        # Transparent — the host window paints its translucent body
        # underneath, so the bar inherits that frosted look. The descendant
        # rule clears child container backgrounds (QLabels, plain QWidget
        # holders) that would otherwise paint opaque from GLOBAL_STYLE.
        # QPushButtons/QSliders have their own per-widget stylesheets that
        # take precedence and remain styled.
        self.setStyleSheet("""
            QWidget#npbar { background: transparent; }
            QWidget#npbar QWidget { background: transparent; }
            QWidget#npbar QLabel { background: transparent; }
        """)

        slider_style = self._slider_qss()

        icon_btn_style = self._icon_btn_qss()

        def _icon_btn(name, tooltip, size=36, icon_size=18):
            b = QPushButton()
            b.setIcon(icon(name))
            # Stash the glyph name so _reapply_theme can re-issue it in
            # the new tint on a live theme switch.
            b.setProperty("_jt_icon", name)
            b.setIconSize(QSize(icon_size, icon_size))
            b.setFixedSize(size, size)
            b.setToolTip(tooltip)
            b.setStyleSheet(icon_btn_style)
            return b

        layout = QHBoxLayout(self)
        # Left margin = 0 so the cover sits flush in the bottom-left
        # corner of the window. Right margin = 0 too so the heart and
        # mini-player flanks land symmetric around the bar's true
        # geometric center; the volume-slider's edge breathing room is
        # provided by an internal margin on the right cluster instead.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # ── Left cluster: thumbnail + title/artist + utility icons ──────────
        # Click target for "expand the now-playing detail page". The
        # cover hosts a hover-revealed heart overlay (CoverOverlayButton)
        # so the favorite control no longer eats horizontal space in the
        # bar layout. Mini-player / cast / volume icons live to the
        # right of the title text — moved over from the old right
        # cluster so a snapped window doesn't clip them off-screen. A
        # right-side spacer (built later) mirrors this cluster's width
        # so the seek bar's centerline stays aligned with the play
        # button above it.
        left = QWidget()
        left.setFixedWidth(380)
        left_layout = QHBoxLayout(left)
        # Small left padding on the *info* side via spacing so the title
        # text doesn't visually butt up against the cover's right edge —
        # at narrow widths the marquee head was reading like it was
        # painted onto the album art.
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(18)

        # Thumb is a QLabel parented inside its own QFrame so the heart
        # overlay can attach as a positioned child. The QLabel paints
        # the artwork; CoverOverlayButton sits on top, anchored to
        # bottom-right, only visible while the cover is hovered.
        self.thumb = QLabel()
        self.thumb.setFixedSize(108, 108)
        self.thumb.setStyleSheet("background: transparent;")
        self._cover_orig: QPixmap | None = None
        self.fav_btn = CoverOverlayButton(self.thumb, size=26, margin=6, bordered=False)
        self.fav_btn.setIcon(icon("favorite_outline"))
        self.fav_btn.setIconSize(QSize(14, 14))
        self.fav_btn.setToolTip("Favorite")
        self.fav_btn.clicked.connect(self._toggle_favorite)

        # Title above artist, tight (2px gap), vertically centered against
        # the cover art. Wrapping in another QVBoxLayout with stretches
        # above/below would also work; AlignVCenter on the QLabels +
        # AddStretch guarantees the same look without extra widgets.
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)
        info.addStretch(1)
        # MarqueeLabel scrolls when the text exceeds its width — covers
        # the squeeze case where a snapped window narrows the left
        # cluster enough that "Artist · Album (Anniversary edition…)"
        # would otherwise get cut off mid-word. Stays static when the
        # text fits.
        self.title = MarqueeLabel("Nothing Playing")
        # Idle title color matches the inactive icon color
        # (icons.ICON_DIM = #a8a8a8) so "Nothing Playing" reads at
        # the same visual weight as the transport buttons next to
        # it. _apply_text_mode flips back to TEXT on an active track.
        self.title.setStyleSheet(f"color: {IDLE_TEXT}; {type_qss(TYPE_SUBHEAD)} letter-spacing: 0.1px;")
        self.sub = MarqueeLabel("")
        self.sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        # Third row, used only in narrow ("split") mode where each of
        # title / artist / album lives on its own line so none of them
        # have to marquee. Hidden at wide widths where artist+album
        # share the sub line.
        self.album_line = MarqueeLabel("")
        self.album_line.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self.album_line.setVisible(False)
        # Server-side scrobble pill — hidden when no scrobbling
        # destination is configured server-side. Sits as a small
        # row beneath the artist/album line so it reads as metadata
        # about the playback session, not chrome on the title.
        self.scrobble_badge = _ScrobbleBadge()
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.setSpacing(0)
        badge_row.addWidget(
            self.scrobble_badge, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )
        badge_row.addStretch(1)
        info.addWidget(self.title)
        info.addWidget(self.sub)
        info.addWidget(self.album_line)
        info.addLayout(badge_row)
        info.addStretch(1)

        left_layout.addWidget(self.thumb)
        left_layout.addLayout(info, 1)

        # Mini-player / cast / volume buttons are built here so the
        # NowPlayingBar exposes them as instance attributes; they're
        # added to the right cluster further down.
        self.queue_btn = _icon_btn("miniplayer", "Open mini player")
        self.queue_btn.setCheckable(True)
        self.queue_btn.clicked.connect(lambda: self.show_queue_requested.emit())

        self.cast_btn = _icon_btn("cast", "Cast")
        self.cast_btn.clicked.connect(lambda: self.cast_requested.emit())
        # Right-click → quick menu of hearted devices + Disconnect,
        # handled by the main window (it owns the cast logic).
        self.cast_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.cast_btn.customContextMenuRequested.connect(
            lambda pos: self.cast_context_requested.emit(self.cast_btn.mapToGlobal(pos))
        )

        # Sleep timer — opens a duration menu on click; the icon goes
        # accent-tinted while a timer is armed and the tooltip carries
        # the live countdown. Backed by PlayerBackend's session-scoped
        # timer via the sleep_timer_* bus signals.
        self.sleep_btn = _icon_btn("moon", "Sleep timer")
        self.sleep_btn.clicked.connect(self._open_sleep_menu)
        self._sleep_deadline: float | None = None
        self._sleep_total: int = 0
        self._sleep_tick = QTimer(self)
        self._sleep_tick.setInterval(1000)
        self._sleep_tick.timeout.connect(self._refresh_sleep_tooltip)

        # VolumeButton owns its popup and tracks volume_state /
        # mute_state on the bus. The popup's host (main window) is
        # resolved lazily on first show via self.window().
        self.vol_btn = VolumeButton(self.bus)

        # Click-to-open is scoped to the cover thumb only — moving it
        # off the whole-cluster handler means clicks on the title /
        # subtitle / utility icons no longer trip an unwanted
        # show_now_playing_requested. The bottom-left corner exclusion
        # is still needed so a press right on the window's resize hit
        # zone bubbles to the host instead of opening the page.
        _CORNER_RESIZE_BOX = 16

        def _on_thumb_press(e):
            if e.button() != Qt.MouseButton.LeftButton:
                e.ignore()
                return
            x = e.position().x()
            y = e.position().y()
            if x <= _CORNER_RESIZE_BOX and y >= self.thumb.height() - _CORNER_RESIZE_BOX:
                e.ignore()
                return
            self.show_now_playing_requested.emit()

        self.thumb.mousePressEvent = _on_thumb_press
        # Exposed so the host can blank the cover/title while the
        # now-playing page is showing. The cluster's responsive width
        # (set in _apply_responsive_layout) stays reserved regardless
        # of child visibility — keeps the seek bar centered.
        self.left_cluster = left
        layout.addWidget(left)

        # ── Center column: transport above progress, both centered ──────────
        # Stretches above and below the two rows make the cluster sit
        # vertically in the bar (not glued to the top). Spacing between
        # the rows is tight (6px) so they read as one control surface.
        center = QVBoxLayout()
        # Small horizontal padding so the title text has breathing room
        # before the shuffle button on the left, and the seek-bar tail
        # doesn't run flush into the right cluster's mini-player icon.
        center.setContentsMargins(12, 6, 12, 6)
        center.setSpacing(6)
        center.addStretch(1)

        self.shuffle_btn = _icon_btn("shuffle", "Shuffle")
        self.shuffle_btn.setCheckable(True)
        self.shuffle_btn.toggled.connect(self._on_shuffle_toggled)

        self.prev_btn = _icon_btn("prev", "Previous (Ctrl+Left)")
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())

        # Play is the primary control — slightly larger than the others
        # so the eye lands on it first.
        self.play_btn = _icon_btn("play", "Play / Pause (Space)", size=44, icon_size=22)
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())

        self.next_btn = _icon_btn("next", "Next (Ctrl+Right)")
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())

        self.repeat_btn = _icon_btn("repeat", "Repeat")
        self.repeat_btn.setCheckable(True)
        self._repeat_state = "off"
        self.repeat_btn.clicked.connect(self._cycle_repeat)

        # Optional streaming-info line — "Streaming FLAC · 1411 kbps"
        # etc. Hidden by default; toggled by Settings → Playback. Sits
        # ABOVE the transport row so it reads as a subtle quality
        # readout rather than competing with the controls.
        self.streaming_info = QLabel("")
        self.streaming_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.streaming_info.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} letter-spacing: 0.4px;"
        )
        self.streaming_info.setVisible(False)

        trans_row = QHBoxLayout()
        trans_row.setSpacing(8)
        trans_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trans_row.addStretch()
        for btn in (self.shuffle_btn, self.prev_btn, self.play_btn, self.next_btn, self.repeat_btn):
            trans_row.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
        trans_row.addStretch()

        # Time labels — Qt QSS doesn't support font-variant-numeric so
        # we accept slight digit-shift as time advances (tiny at 11px).
        # min-width tuned to fit "h:mm:ss" comfortably without burning
        # extra pixels that the seek bar wants for readability.
        self.cur_time = QLabel("0:00")
        self.cur_time.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} min-width: 32px;")
        self.cur_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # ScrubbableSlider gives click-to-jump in addition to drag-to-
        # scrub; sliderPressed/Released still fire so the existing
        # _is_seeking gate keeps working.
        self.seek_bar = ScrubbableSlider(Qt.Orientation.Horizontal)
        self.seek_bar.setRange(0, 1000)
        self.seek_bar.setStyleSheet(slider_style)
        self.seek_bar.sliderPressed.connect(lambda: setattr(self, "_is_seeking", True))
        self.seek_bar.sliderReleased.connect(self._on_seek_release)

        self.tot_time = QLabel("0:00")
        self.tot_time.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} min-width: 32px;")
        self.tot_time.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # Internet-radio "LIVE" pip — shown in place of the seek bar +
        # tot_time when the active queue is INTERNET_RADIO. The dot
        # uses the same accent the rest of the player uses; the text
        # is uppercase MICRO so it reads as a status badge rather than
        # a track-row caption. The station name appends after a bullet
        # separator so the user always knows what they're listening to,
        # even after ICY has replaced the title with a per-track name.
        self.live_pip = QLabel()
        self.live_pip.setStyleSheet(
            f"color: {ACCENT}; {type_qss(TYPE_TINY)} font-weight: 700;"
            " letter-spacing: 1px;"
        )
        self.live_pip.setText("●  LIVE")
        self.live_pip.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.live_pip.hide()

        prog_row = QHBoxLayout()
        # No horizontal contentsMargins — the seek bar should fill the
        # full width of the center column so the progress indicator
        # reads as a meaningful surface rather than a thin sliver.
        prog_row.setContentsMargins(0, 0, 0, 0)
        prog_row.setSpacing(8)
        prog_row.addWidget(self.cur_time)
        prog_row.addWidget(self.seek_bar, 1)
        prog_row.addWidget(self.live_pip, 1)
        prog_row.addWidget(self.tot_time)

        center.addWidget(self.streaming_info)
        center.addLayout(trans_row)
        center.addLayout(prog_row)
        center.addStretch(1)
        layout.addLayout(center, 1)

        # ── Right cluster: utility icons (mini / cast / volume) ─────────────
        # Right-aligned inside a fixed-width slot that mirrors the
        # left cluster's width — keeps the seek bar's centerline
        # directly under the play button above it. The internal
        # leading stretch + addWidget order pushes the three buttons
        # against the bar's right edge with a small inner margin so
        # the volume popup has breathing room to anchor above the
        # right-most icon.
        right = QWidget()
        right.setFixedWidth(380)
        right_row = QHBoxLayout(right)
        # Right margin keeps the volume icon away from the window's
        # right edge — at narrow widths the icon used to sit nearly
        # flush against the window border.
        # Generous right inset (48 px) so the volume icon stays clear
        # of the window's right edge even on narrow / VNC-clipped
        # displays where the rightmost pixels can sit off-screen. The
        # right cluster's leading stretch absorbs the extra space, so
        # the seek bar's centering is unaffected.
        right_row.setContentsMargins(0, 0, 48, 0)
        right_row.setSpacing(8)
        right_row.addStretch(1)
        right_row.addWidget(self.queue_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.sleep_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.cast_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        right_row.addWidget(self.vol_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.right_cluster = right
        layout.addWidget(right)

        # Initial volume from settings — VolumeButton owns the popup
        # slider, so we push the persisted value through its API and
        # let the bus syncing handle subsequent changes.
        from modules.settings import get_settings

        self.vol_btn.set_initial_volume(get_settings().volume)

        # ── Connect bus ─────────────────────────────────────────────────────
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        # Unified radio rendering — modules.radio_state owns the
        # parse + cover-lookup pipeline and emits ``radio_state_changed``
        # whenever any user-visible field changes. We translate the
        # RadioState into widget updates here. Seed from the current
        # snapshot in case the bar constructs mid-session.
        self.bus.radio_state_changed.connect(self._on_radio_state)
        self._is_radio = False
        self._radio_station_name: str = ""
        from modules import radio_state as _radio_state

        seed = _radio_state.current()
        if seed is not None:
            self._on_radio_state(seed)
        # Settings → "Refresh album art" — re-fetch the current track's
        # cover so a server-side art update lands on the bar without
        # needing a track change. Replaying _on_started against the
        # current NowPlaying re-runs the cover URL build + load.
        self.bus.image_cache_cleared.connect(self._on_image_cache_cleared)
        # Cover-art prefetch: queue_manager fires this with the
        # next-up NowPlaying every time the queue advances (and on
        # shuffle reorders). We warm our own cache slot so the next
        # track-change is a memory-cache hit instead of a fresh
        # network round-trip — same idea as mpv's audio prefetch.
        self.bus.queue_prefetch_request.connect(self._prefetch_cover)
        self.bus.playback_paused.connect(lambda: self.play_btn.setIcon(icon("play")))
        self.bus.playback_resumed.connect(lambda: self.play_btn.setIcon(icon("pause")))
        self.bus.playback_restored.connect(self._on_restored)
        self.bus.position_updated.connect(self._on_position)
        self.bus.duration_set.connect(self._on_duration)
        # vol_btn / mute icon syncing is handled inside VolumeButton.
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)
        # Live-accent: re-stamp the shuffle / repeat / favorite icons
        # from current state whenever the user picks a new accent in
        # Settings → Display. The icons are cached QIcon objects that
        # baked the OLD accent at construction; only re-calling
        # `accent_icon()` produces icons with the new colour.
        self.bus.theme_changed.connect(self._reapply_theme)
        # Streaming-info live toggle. Settings → Playback emits this
        # so the user doesn't have to restart to flip the indicator
        # on/off. _on_streaming_info_visibility handles both flips.
        self.bus.streaming_info_changed.connect(
            self._on_streaming_info_visibility,
        )
        # MpvController emits this when audio-bitrate stabilizes a
        # few decode-ticks into a new track. Source of truth for the
        # actual streaming codec + bitrate (raw item metadata is
        # often missing the Bitrate field, and is wrong when the
        # server is transcoding anyway).
        self.bus.streaming_info_updated.connect(
            self._on_streaming_info_updated,
        )
        # While casting, the info line shows "Casting to <device>"
        # instead of the local codec/bitrate (mpv is idle — there's no
        # local stream to describe). cast_started carries the name.
        self.bus.cast_started.connect(self._on_cast_started)
        self.bus.cast_stopped.connect(self._on_cast_stopped)
        # Seed initial visibility from the persisted setting.
        try:
            self.streaming_info.setVisible(get_settings().show_streaming_info)
        except Exception:
            pass
        # Cross-DPR cover refresh — re-issue the cover load at the new
        # physical target when the user drags the window to a
        # different-scale monitor. `_on_started` is idempotent for the
        # metadata/icon setters (same values), so this is safe to
        # call repeatedly.
        self.bus.dpr_changed.connect(self._on_dpr_changed)
        # Sleep-timer state — the bar reflects what PlayerBackend owns.
        # `started` carries the initial seconds; `cancelled` / `fired`
        # both clear the armed look (a fired timer has done its job).
        self.bus.sleep_timer_started.connect(self._on_sleep_started)
        self.bus.sleep_timer_cancelled.connect(self._on_sleep_cleared)
        self.bus.sleep_timer_fired.connect(self._on_sleep_cleared)

    # ── Sleep timer ─────────────────────────────────────────────────────────

    # Preset durations offered in the menu, in minutes. "End of track"
    # is handled separately because it's a mode, not a duration.
    _SLEEP_PRESETS = (15, 30, 45, 60, 90)

    def _open_sleep_menu(self):
        """Pop the sleep-timer duration menu under the moon button.
        Built fresh each open so the active-timer state (the Cancel
        row + its live countdown) is always current."""
        menu = opaque_menu(self)
        active = self._sleep_deadline is not None

        for minutes in self._SLEEP_PRESETS:
            label = f"{minutes} minutes" if minutes < 60 else (
                "1 hour" if minutes == 60 else f"{minutes // 60} h {minutes % 60} min"
                if minutes % 60 else f"{minutes // 60} hours"
            )
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(active and self._sleep_total == minutes * 60)
            act.triggered.connect(
                lambda _=False, m=minutes: self.bus.sleep_timer_requested.emit(
                    m * 60, "fade_stop"
                )
            )

        menu.addSeparator()
        eot = menu.addAction("Stop after current track")
        eot.triggered.connect(
            lambda: self.bus.sleep_timer_requested.emit(0, "end_of_track")
        )

        if active:
            menu.addSeparator()
            remaining = self._sleep_remaining()
            cancel = menu.addAction(f"Cancel timer  ({fmt_time(remaining * 1000)} left)")
            cancel.triggered.connect(
                lambda: self.bus.sleep_timer_cancel_requested.emit()
            )

        menu.exec(self.sleep_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height())))

    def _sleep_remaining(self) -> int:
        """Whole seconds left on the armed timer, or 0 if none."""
        if self._sleep_deadline is None:
            return 0
        import time

        return max(0, int(round(self._sleep_deadline - time.monotonic())))

    @Slot(int)
    def _on_sleep_started(self, seconds: int):
        import time

        self._sleep_total = int(seconds)
        self.sleep_btn.setIcon(accent_icon("moon"))
        # `_sleep_deadline` is non-None whenever a timer is armed — the
        # menu reads it to decide whether to show the Cancel row. A
        # 0-second timer is the "stop after current track" mode: armed,
        # but with no countdown to tick.
        self._sleep_deadline = time.monotonic() + max(0, seconds)
        if seconds > 0:
            self._sleep_tick.start()
            self._refresh_sleep_tooltip()
        else:
            self._sleep_tick.stop()
            self.sleep_btn.setToolTip("Sleep timer — stops after this track")

    @Slot()
    def _on_sleep_cleared(self):
        self._sleep_deadline = None
        self._sleep_total = 0
        self._sleep_tick.stop()
        self.sleep_btn.setIcon(icon("moon"))
        self.sleep_btn.setToolTip("Sleep timer")

    @Slot()
    def _refresh_sleep_tooltip(self):
        remaining = self._sleep_remaining()
        if remaining <= 0:
            self._sleep_tick.stop()
            return
        text = f"Sleep timer — {fmt_time(remaining * 1000)} left"
        self.sleep_btn.setToolTip(text)
        # A QToolTip that's already on-screen doesn't re-read the text
        # set via setToolTip() — it stays frozen until the next hover.
        # While the button is hovered, re-show it each tick so the
        # countdown updates live under the cursor.
        if self.sleep_btn.underMouse():
            QToolTip.showText(QCursor.pos(), text, self.sleep_btn)

    def _on_dpr_changed(self):
        np = get_now_playing()
        if np.item_id:
            self._on_started(np)

    def _slider_qss(self) -> str:
        """Seek-bar QSS — ink-on-dim track. Bakes ink_alpha() + TEXT,
        so rebuilt on a live theme switch (see `_reapply_theme`)."""
        return f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: {ink_alpha(0.16)};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {ink_alpha(0.85)};
                border-radius: 2px;
            }}
            QSlider::add-page:horizontal {{
                background: {ink_alpha(0.10)};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 12px; height: 12px; margin: -4px 0;
                background: {TEXT}; border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{
                background: {TEXT};
            }}
        """

    def _icon_btn_qss(self) -> str:
        """Transport icon-button QSS — the background pill. Bakes the
        WASH_* tokens, so rebuilt on a live theme switch."""
        return f"""
            QPushButton {{
                background: transparent; border: none; border-radius: 8px;
            }}
            QPushButton:hover {{ background: {WASH_HOVER}; }}
            QPushButton:pressed {{ background: {WASH_PRESSED}; }}
        """

    def _reapply_theme(self):
        """Full theme re-stamp on PlayerBus.theme_changed — every icon
        tint, text colour, button + slider QSS, so a live light↔dark
        switch lands uniformly on the bar (accent-only picks route
        here too; the extra work is cheap)."""
        np = get_now_playing()

        # 1. Accent-state icons — favorite / shuffle / repeat / sleep.
        self.fav_btn.setIcon(
            accent_icon("favorite_filled") if np.is_favorite else icon("favorite_outline")
        )
        on = self.shuffle_btn.isChecked()
        self.shuffle_btn.setIcon(accent_icon("shuffle") if on else icon("shuffle"))
        if self._repeat_state == "off":
            self.repeat_btn.setIcon(icon("repeat"))
        elif self._repeat_state == "all":
            self.repeat_btn.setIcon(accent_icon("repeat"))
        else:
            self.repeat_btn.setIcon(accent_icon("repeat_one"))
        self.sleep_btn.setIcon(
            accent_icon("moon") if self._sleep_deadline is not None else icon("moon")
        )

        # 2. Stable-glyph buttons — re-issue in the fresh tint. Every
        #    _icon_btn() carries a `_jt_icon` tag; shuffle / repeat /
        #    sleep are accent-state (handled above) so skip their tags.
        _accent_state = {self.shuffle_btn, self.repeat_btn, self.sleep_btn}
        for b in self.findChildren(QPushButton):
            name = b.property("_jt_icon")
            if not name or b in _accent_state:
                continue
            b.setIcon(icon(name))
        # Play / pause glyph reflects playback state.
        self.play_btn.setIcon(
            icon("pause") if (np.item_id and not np.is_paused) else icon("play")
        )

        # 3. Button + seek-bar QSS rebuilt from the fresh tokens.
        btn_qss = self._icon_btn_qss()
        for b in (self.queue_btn, self.cast_btn, self.sleep_btn, self.shuffle_btn,
                  self.prev_btn, self.play_btn, self.next_btn, self.repeat_btn):
            b.setStyleSheet(btn_qss)
        self.seek_bar.setStyleSheet(self._slider_qss())

        # 4. Text colours — title / sub / album via _apply_text_layout
        #    (force=True so sub + album re-stamp even with no mode
        #    change), plus the standalone time / streaming labels.
        self._apply_text_layout(self.width(), force=True)
        self.streaming_info.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} letter-spacing: 0.4px;"
        )
        for lbl in (self.cur_time, self.tot_time):
            lbl.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} min-width: 32px;"
            )
        self.live_pip.setStyleSheet(
            f"color: {ACCENT}; {type_qss(TYPE_TINY)} font-weight: 700;"
        )

    @Slot(object)
    def _on_started(self, np: NowPlaying):
        # Hold raw metadata so the responsive text layout can rebuild
        # the title / artist / album rows on resize without needing
        # another playback_started event. _apply_text_layout picks the
        # row count + font sizes for the current bar width.
        self._track_title = np.title
        self._track_subtitle = np.subtitle
        self._track_album = np.album
        self._track_year = np.year
        self._apply_text_layout(self.width())
        self.play_btn.setIcon(icon("pause"))
        self._set_favorite(np.is_favorite)
        # Clear the streaming-info label until mpv reports the actual
        # codec + bitrate for THIS track. Without this, a track
        # change would briefly carry over the previous track's info
        # (and on app restart the restored np would surface a codec
        # without a bitrate, which read as broken). While casting,
        # keep the "Casting to …" line — mpv never reports a codec for
        # a track playing on the cast device, so there's nothing to
        # wait for and clearing it would just blank the indicator.
        if self._casting:
            self.streaming_info.setText(f"Casting to {self._casting_device}")
        else:
            self.streaming_info.setText("")

        image_id = np.image_id or np.item_id
        if image_id and not self._is_radio:
            # Build our OWN URL at the bar's own target size rather
            # than reusing np.thumb_url (which is sized at 600 for cast
            # / MPRIS / TV consumers). Navidrome resizes on every
            # request and caches the original full-resolution file —
            # NOT the variant — so asking for size=600 when the bar
            # is 108px makes Navidrome do ~5× the WebP/JPEG encode work
            # for an image we'd downscale away anyway. See
            # feedback_now_playing_cover_pipeline. The 256 floor stays
            # sharp at 1× and 2×; at 3+× the DPR multiplier on the
            # thumb's logical size takes over so 4K Retina users get a
            # crisp source instead of an upscale.
            target_px = max(256, int(round(108 * screen_dpr(self))))
            url = self.api.get_image_url(image_id, "Primary", target_px)
            load_image_async(
                f"{image_id}|npbar",
                url,
                target_px,
                target_px,
                self.set_cover_pixmap,
                rounded_radius=0,
                on_error=lambda: None,
                priority="high",
            )

    @Slot(object)
    def _prefetch_cover(self, np):
        """Warm our cover cache slot for the next-up track. Called
        when queue_manager fires queue_prefetch_request — typically
        triggered on every track advance and on queue mutations."""
        if np is None:
            return
        image_id = getattr(np, "image_id", "") or getattr(np, "item_id", "")
        if not image_id:
            return
        # Same DPR-aware target as _on_started so the prefetch warms
        # the exact cache slot the live cover load will hit.
        target_px = max(256, int(round(108 * screen_dpr(self))))
        url = self.api.get_image_url(image_id, "Primary", target_px)
        if not url:
            return
        load_image_async(
            f"{image_id}|npbar",
            url,
            target_px,
            target_px,
            lambda _pix: None,
            rounded_radius=0,
            on_error=lambda: None,
        )

    def set_cover_pixmap(self, pix: QPixmap):
        self._cover_orig = pix
        self.refresh_cover()

    def refresh_cover(self):
        if self._cover_orig is None or self._cover_orig.isNull():
            return
        s = self.thumb.size()
        if s.width() <= 0 or s.height() <= 0:
            return
        # HiDPI: render the cover at physical pixels (logical × dpr) so
        # the QLabel paints at logical size using a full-resolution
        # texture instead of an upscaled logical-sized pixmap. Without
        # this, on a 2× display the painter would downscale a 108-pixel
        # pixmap to 216 physical pixels at paint time — visibly soft.
        dpr = screen_dpr(self)
        phys_w = max(s.width(), int(round(s.width() * dpr)))
        phys_h = max(s.height(), int(round(s.height() * dpr)))
        scaled = self._cover_orig.scaled(
            phys_w,
            phys_h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        # KeepAspectRatioByExpanding may return a pixmap larger than the
        # target on one axis for non-square source art. Center-crop to
        # the exact physical target BEFORE rounding so the corner curves
        # bake at the right edges; otherwise the QLabel's logical clip
        # hides them and the user sees square corners instead.
        if scaled.width() != phys_w or scaled.height() != phys_h:
            cx = max(0, (scaled.width() - phys_w) // 2)
            cy = max(0, (scaled.height() - phys_h) // 2)
            scaled = scaled.copy(cx, cy, phys_w, phys_h)
        # bl seats into the window body's rounded bottom-left corner, so
        # it tracks the host-OS window radius (RADIUS_WINDOW); the other
        # three corners use the standard card radius (10 logical).
        # Multiply radii by dpr so they read at the same logical
        # curvature after setDevicePixelRatio retags the pixmap.
        r10 = int(round(10 * dpr))
        r_body = int(round(RADIUS_WINDOW * dpr))
        scaled = _round_corners(scaled, tl=r10, tr=r10, br=r10, bl=r_body)
        scaled.setDevicePixelRatio(dpr)
        self.thumb.setPixmap(scaled)

    @Slot(object)
    def _on_radio_state(self, state):
        """Unified radio renderer — invoked whenever ``modules.radio_state``
        emits a fresh snapshot. ``state is None`` means we left radio
        mode; otherwise the dataclass carries everything the bar needs
        to repaint in one call.

        Render contract (shared with the mini player + NP page):
          • title slot ← ``state.display_title`` (song, or station as
            fallback before ICY arrives)
          • subtitle slot ← ``state.display_subtitle`` (artist, empty
            when ICY hasn't split)
          • LIVE pip ← ``● LIVE · {station}`` so the user always knows
            which station is streaming, even after a track title
            replaces the placeholder
          • cover ← ``state.display_cover_url`` (per-track MB art when
            available, station logo otherwise)
        """
        if state is None:
            # Leaving radio mode — restore the scrubber chrome. The
            # next playback_started for a normal album will repopulate
            # title / subtitle / cover via the regular _on_started
            # path; nothing for us to clear here that won't be
            # overwritten naturally.
            if self._is_radio:
                self._is_radio = False
                self._radio_station_name = ""
                self.live_pip.hide()
                self.seek_bar.show()
                self.tot_time.show()
            return

        # Entering or updating radio mode.
        first_entry = not self._is_radio
        self._is_radio = True
        self._radio_station_name = state.station_name
        if first_entry:
            self.seek_bar.hide()
            self.tot_time.hide()
            self.live_pip.show()

        # LIVE pip — gated on actual playback. "● LIVE" only paints
        # while audio is streaming; pause downgrades to a dim
        # "PAUSED · station" so the radio context stays visible but
        # the badge doesn't lie about live state; stopped (cold
        # restore / inactive queue) just carries the station name.
        station = (state.station_name or "").strip()
        if state.is_live:
            text = f"●  LIVE  ·  {station}" if station else "●  LIVE"
            self.live_pip.setStyleSheet(
                f"color: {ACCENT}; {type_qss(TYPE_TINY)} font-weight: 700;"
                " letter-spacing: 1px;"
            )
        elif state.playback_state == "paused":
            text = f"PAUSED  ·  {station}" if station else "PAUSED"
            self.live_pip.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} font-weight: 700;"
                " letter-spacing: 1px;"
            )
        else:
            text = station
            self.live_pip.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} font-weight: 700;"
                " letter-spacing: 1px;"
            )
        self.live_pip.setText(text)

        # Title + subtitle rows. The bar's responsive text layout reads
        # _track_* fields; we set them and re-apply.
        self._track_title = state.display_title
        self._track_subtitle = state.display_subtitle
        self._track_album = ""
        self._track_year = ""
        self._apply_text_layout(self.width())

        # Cover — single source of truth, no need to coordinate logo
        # vs. art_url priority here (display_cover_url handles it).
        cover_url = state.display_cover_url
        if cover_url:
            self._load_radio_cover(cover_url)

    def _load_radio_cover(self, url: str) -> None:
        """Fetch ``url`` and stamp it as the bar's cover. Uses the
        same DPR-aware pipeline as the normal _on_started cover load
        so MusicBrainz art / station logos read at the same fidelity
        as album art."""
        if not url:
            return
        target_px = max(256, int(round(108 * screen_dpr(self))))
        load_image_async(
            f"radio:{url}",
            url,
            target_px,
            target_px,
            self.set_cover_pixmap,
            rounded_radius=0,
            on_error=lambda: None,
            priority="high",
        )

    @Slot()
    def _on_stopped(self):
        self._track_title = ""
        self._track_subtitle = ""
        self._track_album = ""
        self._track_year = ""
        self._apply_text_layout(self.width())
        self._cover_orig = None
        self.thumb.setPixmap(QPixmap())
        self.play_btn.setIcon(icon("play"))
        self._set_favorite(False)
        self.seek_bar.setValue(0)
        self.cur_time.setText("0:00")
        self.tot_time.setText("0:00")
        # Keep the "Casting to …" line if a cast is still live (a stop
        # mid-cast shouldn't blank the only sign the audio's elsewhere).
        if not self._casting:
            self.streaming_info.setText("")

    def _on_cast_started(self, device_name: str):
        """A cast session began — the info line becomes the cast
        indicator, shown regardless of the streaming-info setting
        (where the audio is going matters more than a bitrate)."""
        self._casting = True
        self._casting_device = device_name or "device"
        self.streaming_info.setText(f"Casting to {self._casting_device}")
        self.streaming_info.setVisible(True)

    def _on_cast_stopped(self):
        """Cast ended — drop the indicator and hand the info line back
        to the streaming-info setting / the next mpv codec report."""
        from modules.settings import get_settings

        self._casting = False
        self._casting_device = ""
        self.streaming_info.setText("")
        self.streaming_info.setVisible(get_settings().show_streaming_info)

    def _on_streaming_info_visibility(self, visible: bool):
        """Toggle the streaming-info label on user setting change.
        Wired to PlayerBus.streaming_info_changed. While casting the
        line is the cast indicator and stays visible regardless."""
        if self._casting:
            return
        self.streaming_info.setVisible(bool(visible))

    def _on_streaming_info_updated(self, codec: str, kbps: int):
        """Fired by MpvController via the bus as soon as the actual
        playback bitrate stabilizes. Reflects what's being decoded
        right now — so a Jellyfin-transcoded MP3 stream from a FLAC
        source reads "MP3 · 192 kbps", which is what the user is
        actually hearing.

        When the current track is a downloaded local blob the line
        leads with "Local playback" instead of "Streaming" — same
        codec + bitrate, but it's clear nothing is hitting the server.

        Ignored entirely while casting: the line is the "Casting to …"
        indicator then, and mpv is idle so any stray report is stale.
        """
        if self._casting:
            return
        parts = []
        if codec:
            parts.append(codec.upper())
        if kbps and kbps > 0:
            parts.append(f"{kbps} kbps")
        if not parts:
            self.streaming_info.setText("")
            return
        prefix = "Local playback" if get_now_playing().is_local else "Streaming"
        self.streaming_info.setText(prefix + "  ·  " + "  ·  ".join(parts))

    @Slot()
    def _on_image_cache_cleared(self):
        """Re-trigger the cover load for the currently-playing track
        after the user clicked Settings → Refresh album art. No-op
        when nothing is playing."""
        np = get_now_playing()
        if np is None or not (np.image_id or np.item_id):
            return
        self._on_started(np)

    @Slot(object)
    def _on_restored(self, np: NowPlaying):
        """Render the launch-time resume state: track + saved position
        + duration, paused. Same UI as _on_started but the play icon
        stays as 'play' (not 'pause') because mpv hasn't loaded yet."""
        self._on_started(np)
        # _on_started flipped the icon to pause — override back to play.
        self.play_btn.setIcon(icon("play"))
        self._on_duration(np.duration)
        self._on_position(np.position)

    @Slot(int)
    def _on_position(self, ms: int):
        np = get_now_playing()
        if not self._is_seeking and np.duration > 0:
            self.seek_bar.setValue(int(ms / np.duration * 1000))
        self.cur_time.setText(fmt_time(ms))

    @Slot(int)
    def _on_duration(self, ms: int):
        self.tot_time.setText(fmt_time(ms))

    def _on_seek_release(self):
        self._is_seeking = False
        np = get_now_playing()
        if np.duration > 0:
            ms = int(self.seek_bar.value() / 1000 * np.duration)
            self.bus.seek_requested.emit(ms)

    def _cycle_repeat(self):
        order = ["off", "all", "one"]
        idx = order.index(self._repeat_state)
        self._repeat_state = order[(idx + 1) % 3]
        # off=outline, all=accent-tinted repeat, one=accent-tinted repeat-one
        if self._repeat_state == "off":
            self.repeat_btn.setIcon(icon("repeat"))
        elif self._repeat_state == "all":
            self.repeat_btn.setIcon(accent_icon("repeat"))
        else:
            self.repeat_btn.setIcon(accent_icon("repeat_one"))
        self.repeat_btn.setChecked(self._repeat_state != "off")
        self.bus.repeat_changed.emit(self._repeat_state)

    def _on_shuffle_toggled(self, on: bool):
        self.shuffle_btn.setIcon(accent_icon("shuffle") if on else icon("shuffle"))
        self.bus.shuffle_changed.emit(on)

    def set_cast_manager(self, cm):
        """Forward the CastManager to the volume button so its popup can
        switch to the per-speaker variant when casting to a group."""
        self.vol_btn.set_cast_manager(cm)

    def set_left_cluster_visible(self, visible: bool):
        """Hide the cover/title/artist while leaving the cluster widget
        in the layout (so the responsive width still reserves space and
        the seek bar stays centered). The mini-player / cast / volume
        utility icons stay visible and clickable — the user still wants
        mute/cast/mini-player one click away even when the now-playing
        page is showing its own copy of the cover and title.

        The cover click-handler is scoped to the thumb itself, so
        hiding the thumb is enough to suppress the show_now_playing
        emit — no setEnabled gymnastics required.

        Sets ``_left_suppressed`` so the responsive resize logic
        doesn't try to re-show the title/sub on its next pass."""
        self._left_suppressed = not visible
        self.thumb.setVisible(visible)
        self.title.setVisible(visible)
        self.sub.setVisible(visible)
        self.album_line.setVisible(False)  # _apply_text_layout will re-enable in split mode
        # Re-run the responsive pass when un-suppressing so the
        # text-hide / split breakpoints (if applicable at the current
        # width) are honoured instead of leaving title/sub un-hidden.
        if visible:
            self._apply_responsive_layout(self.width())

    def _toggle_favorite(self):
        np = get_now_playing()
        if not np.item_id:
            return
        new_state = not np.is_favorite
        run_async(self.api.toggle_favorite, np.item_id, new_state)
        np.is_favorite = new_state
        self.bus.favorite_toggled.emit(np.item_id, new_state)

    def _set_favorite(self, fav: bool):
        # Filled accent-colored heart when favorited; outline otherwise.
        self.fav_btn.setIcon(accent_icon("favorite_filled") if fav else icon("favorite_outline"))

    @Slot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        np = get_now_playing()
        if np.item_id == item_id:
            self._set_favorite(fav)

    # ── Responsive layout ───────────────────────────────────────────────────
    # Left cluster carries cover + title; right cluster carries
    # mini-player / cast / volume. Cluster widths track in lockstep so
    # the seek bar's centerline stays under the play button — the
    # load-bearing alignment cue for the bar.
    #
    # Cluster width grows / shrinks with the bar to keep the title
    # text legible; main HBox spacing tightens at narrow widths to
    # buy back pixels for the seek bar. The right-cluster inset grows
    # at narrow widths so the volume/cast/mini-player trio doesn't sit
    # flush against the window border on phone-sized surfaces.
    #
    # Text presentation has three modes driven by bar width:
    #   - combined (bar >= _TEXT_SPLIT_WIDTH): 2 rows — title above
    #     "Artist · Album". The classic wide-window look.
    #   - split    (_TEXT_HIDE_WIDTH ≤ bar < _TEXT_SPLIT_WIDTH): 3 rows
    #     — title, artist, album each on their own line. Fonts step
    #     down a tier so 3 lines feel calm rather than crammed, and
    #     each individual line is short enough to avoid marquee scroll.
    #   - hide     (bar < _TEXT_HIDE_WIDTH): cover only, all text rows
    #     hidden. The cover still opens the now-playing page on click,
    #     so the full title is one tap away.
    _BREAKPOINTS = (
        # (min bar width, cluster width, main spacing, right inset)
        # Cluster widths are biased toward the *title* side at wider
        # ranges: we'd rather shrink the seek bar than crush "Artist ·
        # Album" into illegibility. Below the text-hide threshold the
        # left cluster shrinks aggressively so the seek bar / transport
        # row get the horizontal room they need.
        (1200, 380, 16, 48),
        (1080, 360, 14, 48),
        (940, 340, 12, 44),
        (840, 310, 10, 40),
        (760, 280, 8, 36),
        (680, 240, 8, 32),
        (560, 170, 6, 24),
        (0, 140, 4, 20),
    )
    _TEXT_SPLIT_WIDTH = 1080  # below this, switch from 2-row to 3-row text
    _TEXT_HIDE_WIDTH = 680  # below this, hide all text rows

    def _apply_responsive_layout(self, bar_w: int):
        cluster_w, spacing, right_inset = 380, 16, 48
        for min_w, cw, sp, ri in self._BREAKPOINTS:
            if bar_w >= min_w:
                cluster_w, spacing, right_inset = cw, sp, ri
                break
        if self.left_cluster.width() != cluster_w:
            self.left_cluster.setFixedWidth(cluster_w)
        if self.right_cluster.width() != cluster_w:
            self.right_cluster.setFixedWidth(cluster_w)
        if self.layout().spacing() != spacing:
            self.layout().setSpacing(spacing)
        right_layout = self.right_cluster.layout()
        cur_margins = right_layout.contentsMargins()
        if cur_margins.right() != right_inset:
            right_layout.setContentsMargins(0, 0, right_inset, 0)
        self._apply_text_layout(bar_w)

    def _apply_text_layout(self, bar_w: int, force: bool = False):
        """Pick the row count + font sizes for the current bar width
        and re-render title / artist / album from the stored track
        metadata. Idempotent — safe to call on every resize tick.

        ``force`` re-stamps the sub / album label styles even when the
        layout mode hasn't changed — used by `_reapply_theme` so a
        live theme switch refreshes their colours (the per-mode style
        block is otherwise skipped on a same-width call)."""
        # Host owns visibility while the now-playing page is showing;
        # set_left_cluster_visible will re-trigger this when un-suppressing.
        if self._left_suppressed:
            return

        if bar_w < self._TEXT_HIDE_WIDTH:
            mode = "hide"
        elif bar_w < self._TEXT_SPLIT_WIDTH:
            mode = "split"
        else:
            mode = "combined"

        # Visibility — always update because the host may have flipped
        # things off in suppression and we're un-suppressing now.
        self.title.setVisible(mode != "hide")
        self.sub.setVisible(mode != "hide")
        self.album_line.setVisible(mode == "split")

        if mode == "hide":
            self._text_mode = mode
            return

        # Text content per mode. Title always carries the song name (or
        # the placeholder) so the row is never blank when visible.
        is_idle = not bool(self._track_title)
        self.title.setText(self._track_title or "Nothing Playing")
        # Idle title matches the inactive icon color (#a8a8a8 — see
        # icons.ICON_DIM) so the placeholder visually pairs with the
        # transport buttons next to it instead of competing with real
        # track names for the eye.
        self.title.setStyleSheet(
            f"color: {TEXT if not is_idle else IDLE_TEXT}; "
            f"{type_qss(TYPE_SUBHEAD)} letter-spacing: 0.1px;"
        )
        if mode == "combined":
            bits = [b for b in (self._track_subtitle, self._track_album) if b]
            self.sub.setText("  ·  ".join(bits) or self._track_year or "")
        else:  # split
            self.sub.setText(self._track_subtitle or self._track_year or "")
            self.album_line.setText(self._track_album or "")

        # Font sizes — restyle only on mode change. Sub/album use raw
        # font-size in split mode (11px) instead of TYPE_CAPTION (12px)
        # to give the 3-row stack a calmer, more compact rhythm.
        if force or mode != self._text_mode:
            self._text_mode = mode
            if mode == "combined":
                self.title.setStyleSheet(
                    f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)} letter-spacing: 0.1px;"
                )
                self.sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
            else:  # split — step down one size tier on both rows.
                # Title overrides TYPE_BODY's 400 weight to 600 so the
                # split-mode title still reads as the heading of the stack.
                self.title.setStyleSheet(
                    f"color: {TEXT}; {type_qss(TYPE_BODY)} font-weight: 600; letter-spacing: 0.1px;"
                )
                self.sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_TINY)}")
                self.album_line.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_TINY)}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_responsive_layout(self.width())


# ── Cast dialog ──────────────────────────────────────────────────────────────


class _CastDeviceRow(QWidget):
    """One row in the cast device list: glyph + name/kind + a heart
    toggle. Hearted devices are pinned to the top of the list (the
    dialog re-renders on toggle).

    The row owns its own click + hover handling rather than leaning on
    QListWidget: a click anywhere outside the heart emits ``clicked``
    (the dialog selects the matching item), the heart button consumes
    its own clicks, and the empty outline heart only appears while the
    row is hovered so an un-pinned list stays visually calm. The filled
    heart on a pinned device shows always."""

    favorite_toggled = Signal(object, bool)  # CastDevice, is_favorite
    clicked = Signal()

    def __init__(self, dev: CastDevice, is_favorite: bool, parent=None):
        super().__init__(parent)
        self._dev = dev
        self._is_favorite = is_favorite
        self._hovered = False

        h = QHBoxLayout(self)
        h.setContentsMargins(14, 0, 8, 0)
        h.setSpacing(10)

        is_chromecast = dev.device_type == "chromecast"
        kind = "Chromecast" if is_chromecast else "AirPlay"
        glyph = QLabel()
        glyph.setPixmap(icon("cast" if is_chromecast else "airplay").pixmap(QSize(18, 18)))
        glyph.setStyleSheet("background: transparent;")
        h.addWidget(glyph)

        name = QLabel(f"{dev.name}   ·   {kind}")
        name.setStyleSheet(f"color: {TEXT}; background: transparent;")
        h.addWidget(name, 1)

        self._heart = QPushButton()
        self._heart.setFixedSize(28, 28)
        self._heart.setIconSize(QSize(16, 16))
        self._heart.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._heart.setCursor(Qt.CursorShape.PointingHandCursor)
        self._heart.setStyleSheet("""
            QPushButton { background: transparent; border: none;
                          border-radius: 6px; }
            QPushButton:hover { background: rgba(58, 60, 68, 0.92); }
        """)
        self._heart.clicked.connect(self._toggle)
        h.addWidget(self._heart)
        self._update_heart_icon()

    def _update_heart_icon(self):
        # Filled accent heart when pinned (always visible); plain outline
        # only while hovered; otherwise no glyph at all. The button keeps
        # its fixed 28px slot regardless, so showing / hiding the icon
        # never shifts the rest of the row.
        if self._is_favorite:
            self._heart.setIcon(accent_icon("favorite_filled"))
        elif self._hovered:
            self._heart.setIcon(icon("favorite_outline"))
        else:
            self._heart.setIcon(QIcon())
        self._heart.setToolTip("Unpin from top" if self._is_favorite else "Pin to top")

    def _toggle(self):
        self._is_favorite = not self._is_favorite
        self._update_heart_icon()
        self.favorite_toggled.emit(self._dev, self._is_favorite)

    def _set_hovered(self, on: bool):
        if on == self._hovered:
            return
        self._hovered = on
        self._update_heart_icon()
        self.update()

    def enterEvent(self, e):
        self._set_hovered(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        # Moving the cursor onto the heart child fires the row's
        # leaveEvent even though the cursor is still within the row — so
        # confirm against the actual cursor position before clearing.
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self._set_hovered(inside)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        # Accept so the row becomes the grab target and receives the
        # release. The heart button consumes its own presses, so this
        # only fires for clicks on the glyph / name / empty space.
        if e.button() == Qt.MouseButton.LeftButton:
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        # The row sits on top of the QListWidget item, so the item's
        # :selected background still shows through the transparent body.
        # Hover, though, needs the viewport's mouse tracking we no longer
        # get — so the row paints its own hover wash.
        if self._hovered:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*ink_rgb(), 13))
            p.drawRoundedRect(self.rect(), 6, 6)


class _CastSection(QWidget):
    """One section of the cast picker — a clickable header with a chevron
    glyph and a QListWidget body that hides when the section is collapsed.

    Sections are keyed by ``section_type`` (matches ``CastDevice
    .device_type`` for live types, plus the placeholder keys for the
    yet-unmerged DLNA/Sonos/Snapcast backends). The header click toggles
    collapsed state and emits ``toggled``; the body's ``QListWidget``
    selection forwards as ``selection_changed`` and ``item_activated``
    so the parent dialog can drive a single ``selected_device`` across
    all sections.
    """

    toggled = Signal(str, bool)  # section_type, collapsed
    selection_changed = Signal(object)  # CastDevice | None (None on clear)
    item_activated = Signal(object)  # CastDevice
    favorite_toggled = Signal(object, bool)  # CastDevice, is_fav

    HEADER_HEIGHT = 30

    def __init__(self, section_type: str, label: str, parent=None):
        super().__init__(parent)
        self.section_type = section_type
        self._label = label
        self._collapsed = True  # parent will resolve initial state
        self._devices: list = []
        self._favs: set = set()

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        self._header = self._build_header()
        col.addWidget(self._header)

        self._list = QListWidget()
        self._list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._list.setSpacing(0)
        self._list.setIconSize(QSize(18, 18))
        # No frame on the list — the section "owns" its visual region
        # and the QListWidget should disappear into that container.
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                outline-style: none;
                padding: 2px 0;
            }}
            QListWidget::item {{
                color: {TEXT};
                padding: 0;
                border-radius: 6px;
                margin: 1px 0;
            }}
            QListWidget::item:hover {{
                background: {ink_alpha(0.05)};
            }}
            QListWidget::item:selected {{
                background: {ink_alpha(0.10)};
                color: {TEXT};
            }}
        """)
        # Forward signals up to the dialog so it can run cross-section
        # mutual-exclusion + drive the Cast button enable state.
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.itemActivated.connect(self._on_item_activated)
        # Variable height: enough for the visible rows, no scrollbar
        # inside the list itself (the parent QScrollArea handles the
        # case where the dialog overflows).
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        col.addWidget(self._list)
        self._list.hide()

    # ── Header ─────────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        w = QFrame()
        w.setObjectName("jtCastSectionHeader")
        w.setFixedHeight(self.HEADER_HEIGHT)
        w.setCursor(Qt.CursorShape.PointingHandCursor)
        w.setStyleSheet(f"""
            QFrame#jtCastSectionHeader {{
                background: transparent;
                border-radius: 6px;
            }}
            QFrame#jtCastSectionHeader:hover {{
                background: {ink_alpha(0.05)};
            }}
        """)

        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 6, 0)
        h.setSpacing(6)

        self._chevron = QLabel()
        self._chevron.setFixedWidth(14)
        self._chevron.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self._chevron.setStyleSheet(f"color: {TEXT_DIM}; background: transparent;")
        h.addWidget(self._chevron)

        self._name_label = QLabel(self._label)
        self._name_label.setFont(font(TYPE_MICRO))
        self._name_label.setStyleSheet(f"color: {TEXT}; background: transparent;")
        h.addWidget(self._name_label)

        h.addStretch(1)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_TINY)} background: transparent;"
        )
        h.addWidget(self._count_label)

        # Whole-row click toggle — matches the spec ("click anywhere on
        # the row toggles"). Bound on the frame, not the chevron, so
        # the user can grab the entire 30px strip.
        w.mousePressEvent = self._on_header_press
        return w

    def _on_header_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.set_collapsed(not self._collapsed, emit=True)
            e.accept()

    def _refresh_header(self):
        # ▾ when expanded (content visible underneath), ▸ when collapsed
        # (content stowed away). Unicode arrows kept here — using the
        # icon() asset would force a recolour pass on theme change.
        self._chevron.setText("▸" if self._collapsed else "▾")
        n = len(self._devices)
        if n == 0:
            self._count_label.setText("none discovered")
        elif n == 1:
            self._count_label.setText("1 device")
        else:
            self._count_label.setText(f"{n} devices")

    # ── Public API ─────────────────────────────────────────────────────
    def set_devices(self, devices: list, favs: set):
        """Replace the section's device list. Preserves selection by
        UUID across re-renders so a freshly-arriving device doesn't
        deselect the user's current pick."""
        self._devices = list(devices)
        self._favs = favs

        prev_uuid = None
        items = self._list.selectedItems()
        if items:
            dev = items[0].data(Qt.ItemDataRole.UserRole)
            if dev is not None:
                prev_uuid = getattr(dev, "uuid", None)

        self._list.clear()
        # Hearted devices pinned to the top within this section.
        ordered = sorted(self._devices, key=lambda d: d.uuid not in favs)
        for dev in ordered:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, dev)
            item.setSizeHint(QSize(0, 38))
            self._list.addItem(item)
            row = _CastDeviceRow(dev, dev.uuid in favs)
            row.favorite_toggled.connect(self.favorite_toggled.emit)
            row.clicked.connect(lambda it=item: self._list.setCurrentItem(it))
            self._list.setItemWidget(item, row)
            if prev_uuid and dev.uuid == prev_uuid:
                self._list.setCurrentItem(item)

        # Size the list to exactly fit its rows so the parent
        # QScrollArea governs overflow rather than a nested scroll
        # bar inside each section.
        self._list.setFixedHeight(max(0, len(ordered)) * 40 + 4)
        self._refresh_header()
        self._apply_visibility()

    def set_collapsed(self, collapsed: bool, *, emit: bool):
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._apply_visibility()
        self._refresh_header()
        if emit:
            self.toggled.emit(self.section_type, collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def has_devices(self) -> bool:
        return bool(self._devices)

    def list_widget(self) -> QListWidget:
        return self._list

    def clear_selection(self):
        self._list.clearSelection()

    def select_by_uuid(self, uuid: str) -> bool:
        for i in range(self._list.count()):
            item = self._list.item(i)
            dev = item.data(Qt.ItemDataRole.UserRole)
            if dev is not None and getattr(dev, "uuid", None) == uuid:
                self._list.setCurrentItem(item)
                return True
        return False

    def _apply_visibility(self):
        self._list.setVisible(not self._collapsed and bool(self._devices))

    # ── Signal forwarding ──────────────────────────────────────────────
    def _on_selection_changed(self):
        items = self._list.selectedItems()
        if not items:
            self.selection_changed.emit(None)
            return
        dev = items[0].data(Qt.ItemDataRole.UserRole)
        self.selection_changed.emit(dev)

    def _on_item_activated(self, item):
        dev = item.data(Qt.ItemDataRole.UserRole)
        if dev is not None:
            self.item_activated.emit(dev)


class CastDialog(QDialog):
    """Frameless frosted dialog matching the settings + main window. Auto-
    scans on open; devices appear live as discovery callbacks fire. The
    Rescan button is kept as a manual escape hatch but the user shouldn't
    need it for the common path."""

    BODY_RADIUS = 14
    # After this long with no devices, the "Scanning…" placeholder
    # flips to "No devices found" so the dialog doesn't sit in a
    # forever-loading state on networks with nothing castable.
    SCAN_GIVEUP_MS = 6000

    # Cross-thread bridge: pychromecast's get_chromecasts() and zeroconf's
    # ServiceBrowser fire their callbacks on plain Python threads with no
    # Qt event loop. Re-emitting through a signal hands off to the GUI
    # thread automatically (Qt::AutoConnection picks queued mode for
    # cross-thread connections), which a bare QTimer.singleShot can't do
    # because the timer would land in the worker thread that has no
    # event loop running.
    _devices_changed = Signal(list)

    def __init__(self, cast_manager: CastManager, parent=None):
        super().__init__(parent)
        self.cast_manager = cast_manager
        self.selected_device: CastDevice | None = None
        self.setWindowTitle("Cast")
        self.setFixedSize(440, 480)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtCastDialog")
        # Non-modal — a modal exec() disables the parent window, which
        # Qt then paints in its dimmed/desaturated disabled palette.
        # The cast picker behaves like the (non-modal) Settings dialog:
        # the main window stays live and full-colour behind it.
        self.setModal(False)

        from modules.ui_helpers import GLOBAL_STYLE, DIALOG_BODY_COLOR

        self._dialog_body_color = DIALOG_BODY_COLOR
        # GLOBAL_STYLE provides QListWidget/QPushButton baselines; we
        # override per-list and per-button below to keep the cast card
        # aesthetic consistent with the settings dialog.
        self.setStyleSheet(GLOBAL_STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_titlebar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        v = QVBoxLayout(body)
        v.setContentsMargins(20, 6, 20, 16)
        v.setSpacing(10)

        # Active-cast banner — visible only when a cast session is live.
        # Shows "Casting to {name}" + a Disconnect button that kills the
        # session. Hidden otherwise so the dialog reads as a picker.
        self._active_banner = self._build_active_banner()
        self._apply_banner_qss()
        v.addWidget(self._active_banner)

        v.addWidget(self._section_header("Available devices"))

        sub = QLabel("Pick a Chromecast, AirPlay, DLNA, Sonos, or Snapcast receiver.")
        sub.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        sub.setWordWrap(True)
        v.addWidget(sub)

        # Scanning state — visible while we wait for the first device to
        # come back. Replaced by the section column as soon as one shows up.
        self._scanning_label = QLabel("Scanning your network…")
        self._scanning_label.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            f"background: {ink_alpha(0.04)};"
            "border-radius: 8px; padding: 14px 16px;"
        )
        self._scanning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._scanning_label)

        # ── Section column ────────────────────────────────────────────
        # One section per cast type. Section state (collapsed/expanded)
        # persists in QSettings per type — see modules.cast_dialog_sections.
        # A QScrollArea wraps the column so an unusually full network
        # (many Chromecasts + many AirPlays) can overflow gracefully
        # rather than blowing past the fixed dialog height.
        from modules.cast_dialog_sections import SECTION_TYPES, SECTION_LABELS

        self._sections: dict[str, _CastSection] = {}

        self._sections_scroll = QScrollArea()
        self._sections_scroll.setWidgetResizable(True)
        self._sections_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._sections_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._sections_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        sections_host = QWidget()
        sections_host.setStyleSheet("background: transparent;")
        sections_col = QVBoxLayout(sections_host)
        sections_col.setContentsMargins(0, 0, 0, 0)
        sections_col.setSpacing(4)

        for t in SECTION_TYPES:
            section = _CastSection(t, SECTION_LABELS[t], parent=sections_host)
            section.selection_changed.connect(
                lambda dev, src=t: self._on_section_selection_changed(src, dev)
            )
            section.item_activated.connect(self._on_section_item_activated)
            section.favorite_toggled.connect(self._on_favorite_toggled)
            section.toggled.connect(self._on_section_toggled)
            self._sections[t] = section
            sections_col.addWidget(section)

        sections_col.addStretch(1)
        self._sections_scroll.setWidget(sections_host)
        self._sections_scroll.hide()  # hidden until first device lands
        v.addWidget(self._sections_scroll, 1)

        # Bottom action row: Rescan on the left, Cancel + Cast on the
        # right. All three share a consistent transparent-default /
        # grey-box-on-hover language so the dialog reads as a calm
        # bottom strip rather than three differently-weighted controls.
        # Cast is distinguished by accent-colored text (and dims when
        # disabled), not by a different hover treatment.
        action_btn_css = f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                padding: 7px 16px;
                color: {TEXT};
                font-weight: 500;
            }}
            QPushButton:hover {{ background: rgba(58, 60, 68, 0.92); }}
            QPushButton:pressed {{ background: rgba(72, 74, 82, 0.92); }}
            QPushButton:disabled {{ color: {ink_alpha(0.30)}; }}
        """
        # Cast-button QSS is built from current accent — extracted into
        # _cast_btn_qss() so _reapply_accent can re-stamp it when the
        # user picks a new accent in Settings.
        cast_btn_css = self._cast_btn_qss()

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.scan_btn = QPushButton("Rescan")
        self.scan_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scan_btn.setStyleSheet(action_btn_css)
        self.scan_btn.clicked.connect(self.scan)
        btns.addWidget(self.scan_btn)
        # Forget paired device — only enabled when the selected list
        # item is an AirPlay 2 receiver with stored credentials. Clears
        # the credentials so the next cast attempt re-launches the
        # pairing dialog. Lives next to Rescan because both are "fix
        # the list" actions; Cancel / Cast are the dialog's primary
        # decision pair.
        self.forget_btn = QPushButton("Forget")
        self.forget_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.forget_btn.setStyleSheet(action_btn_css)
        self.forget_btn.setEnabled(False)
        self.forget_btn.setToolTip(
            "Clear stored pairing credentials for the selected "
            "AirPlay 2 device so it can be re-paired."
        )
        self.forget_btn.clicked.connect(self._on_forget_clicked)
        btns.addWidget(self.forget_btn)
        btns.addStretch()

        cancel = QPushButton("Cancel")
        cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cancel.setStyleSheet(action_btn_css)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)

        self.cast_btn = QPushButton("Cast")
        self.cast_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cast_btn.setStyleSheet(cast_btn_css)
        self.cast_btn.setEnabled(False)
        self.cast_btn.clicked.connect(self.accept)
        btns.addWidget(self.cast_btn)
        v.addLayout(btns)

        outer.addWidget(body, 1)

        # Section signals were wired up when each _CastSection was built.
        # Live updates as devices are discovered — saves the user from
        # having to click rescan + wait. The callback fires on the
        # discovery thread; emitting our signal there hands off to the
        # GUI thread (queued connection) before _render_devices runs.
        self._devices_changed.connect(self._render_devices)
        self.cast_manager.set_devices_callback(self._devices_changed.emit)
        # Pull whatever's already in the cache, then start a fresh
        # discovery so the list stays current. Banner reflects current
        # active_cast immediately so the user can disconnect without
        # waiting for the discovery callback.
        # Scan-give-up timer — flips the scanning label to the
        # "No devices found" empty state if nothing has landed by
        # SCAN_GIVEUP_MS. Reset on every scan() / kept off when
        # devices arrive.
        self._scan_giveup_timer = QTimer(self)
        self._scan_giveup_timer.setSingleShot(True)
        self._scan_giveup_timer.setInterval(self.SCAN_GIVEUP_MS)
        self._scan_giveup_timer.timeout.connect(self._on_scan_giveup)

        self._render_devices(self.cast_manager.get_all_devices())
        self._refresh_active_banner()
        self.scan()

        # Live-accent: rebuild the banner stylesheet + restamp the
        # Cast button color when the user picks a new accent. Both
        # bake the accent at construction; without this they'd freeze
        # at whatever was active when the dialog opened.
        PlayerBus.get().theme_changed.connect(self._reapply_accent)

    # ── Title bar ──────────────────────────────────────────────────────
    def _build_titlebar(self) -> QWidget:
        tb = QWidget()
        tb.setFixedHeight(46)
        tb.setObjectName("jtCastTitle")
        tb.setStyleSheet("""
            QWidget#jtCastTitle { background: transparent; }
            QWidget#jtCastTitle QLabel { background: transparent; }
        """)
        h = QHBoxLayout(tb)
        h.setContentsMargins(20, 0, 8, 0)
        h.setSpacing(10)

        cast_glyph = QLabel()
        cast_glyph.setPixmap(icon("cast").pixmap(QSize(18, 18)))
        h.addWidget(cast_glyph)

        title = QLabel("Cast to device")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(title)
        h.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ background: rgba(239,68,68,0.85); color: white; }}
        """)
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)

        tb.mousePressEvent = self._titlebar_press
        return tb

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def keyPressEvent(self, e):
        # Esc dismisses the picker. QDialog binds this by default, but
        # the frameless + WA_TranslucentBackground combo on KDE Wayland
        # doesn't reliably route the key event to QDialog's handler.
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        # Park focus on the first section that has devices so Down/Enter
        # just work. If everything is empty (still scanning), the focus
        # steer happens once _render_devices populates a section.
        self._focus_first_populated_section()

    def _focus_first_populated_section(self):
        for section in self._sections.values():
            if section.has_devices() and not section.is_collapsed():
                section.list_widget().setFocus(Qt.FocusReason.OtherFocusReason)
                return

    def _on_section_item_activated(self, _dev):
        # Section's QListWidget.itemActivated forwarded — Return/Enter
        # or double-click. _on_section_selection_changed has already set
        # selected_device + enabled cast_btn, so this is "press Cast".
        if self.selected_device is not None:
            self.accept()

    def _section_header(self, text: str) -> QLabel:
        # font(TYPE_MICRO) handles uppercase + letter-spacing via QFont,
        # so we pass mixed-case text here — Qt's QSS doesn't actually
        # honor text-transform/letter-spacing, only QFont does.
        label = QLabel(text)
        label.setFont(font(TYPE_MICRO))
        label.setStyleSheet(f"color: {TEXT_FAINT};")
        return label

    # ── Device discovery ───────────────────────────────────────────────
    def _any_devices_loaded(self) -> bool:
        return any(s.has_devices() for s in self._sections.values())

    def scan(self):
        # Show the scanning placeholder if nothing is rendered yet — if
        # we already have devices from a previous scan, leave them
        # visible while a fresh discovery runs in the background.
        if not self._any_devices_loaded():
            self._scanning_label.setText("Scanning your network…")
            self._scanning_label.show()
            self._sections_scroll.hide()
            self._scan_giveup_timer.start()
        self.cast_manager.discover_all()

    def _render_devices(self, devices: List[CastDevice]):
        from modules.cast_dialog_sections import (
            group_devices_by_type,
            resolve_state,
            SECTION_TYPES,
        )
        from modules.settings import get_settings

        was_visible = self._sections_scroll.isVisible()
        had_devices_before = self._any_devices_loaded()

        favs = set(get_settings().favorite_cast_device_ids)
        buckets = group_devices_by_type(devices)

        s_settings = get_settings()._s  # underlying QSettings
        for t in SECTION_TYPES:
            section = self._sections[t]
            bucket = buckets.get(t, [])
            # Push devices first so resolve_state's has_devices is fresh.
            section.set_devices(bucket, favs)
            state = resolve_state(s_settings, t, has_devices=bool(bucket))
            # Apply without re-emitting to avoid persisting the default
            # back as an "explicit" choice on every render — the user
            # hasn't toggled anything yet.
            section.set_collapsed(state.collapsed, emit=False)

        if not self._any_devices_loaded():
            # Leave the label alone — it's either "Scanning…" (in
            # progress) or "No devices found" (give-up timer fired).
            # Clearing the sections still matters because devices may
            # have been REMOVED from the cache.
            self._sections_scroll.hide()
            return

        # Devices arrived — stop the give-up timer so the empty
        # state doesn't flip in over a now-populated list.
        self._scan_giveup_timer.stop()
        self._scanning_label.hide()
        self._sections_scroll.show()
        # First device just landed while the dialog is open — steer
        # keyboard focus into the first populated section so Down/Enter
        # immediately drives it.
        if (not was_visible or not had_devices_before) and self.isVisible():
            self._focus_first_populated_section()
        # Banner state can change as devices come and go (active_cast
        # may have just been discovered with full metadata).
        self._refresh_active_banner()

    def _on_favorite_toggled(self, dev: CastDevice, is_fav: bool):
        """Heart toggled on a device row — persist the change (name +
        type alongside the uuid, so the cast button's right-click menu
        can label it later) and re-render so the device jumps to or
        leaves the pinned group."""
        from modules.settings import get_settings

        s = get_settings()
        favs = [f for f in s.favorite_cast_devices if f["uuid"] != dev.uuid]
        if is_fav:
            favs.append(
                {
                    "uuid": dev.uuid,
                    "name": dev.name,
                    "type": dev.device_type,
                }
            )
        s.favorite_cast_devices = favs
        self._render_devices(self.cast_manager.get_all_devices())

    def _on_section_toggled(self, section_type: str, collapsed: bool):
        """User clicked a section header. Persist the new state so the
        next dialog open honours their choice."""
        from modules.cast_dialog_sections import write_collapsed
        from modules.settings import get_settings

        write_collapsed(get_settings()._s, section_type, collapsed)

    @Slot()
    def _on_scan_giveup(self):
        """SCAN_GIVEUP_MS elapsed without any device showing up — flip
        the scanning placeholder to a 'No devices found' empty state
        so the dialog reads as 'done scanning, network empty' instead
        of 'forever loading'."""
        if self._any_devices_loaded():
            return
        self._scanning_label.setText(
            "No devices found on your network.\nTry Rescan, or check that your devices are awake."
        )
        self._scanning_label.show()
        self._sections_scroll.hide()

    # ── Active-cast banner ─────────────────────────────────────────────
    def _build_active_banner(self) -> QWidget:
        w = QFrame()
        w.setObjectName("castActiveBanner")
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 10, 8, 10)
        h.setSpacing(10)

        text_wrap = QVBoxLayout()
        text_wrap.setContentsMargins(0, 0, 0, 0)
        text_wrap.setSpacing(1)
        # Mixed-case text + font(TYPE_MICRO) — QFont applies the uppercase
        # transform and letter-spacing that QSS would silently ignore.
        kicker = QLabel("Casting to")
        kicker.setFont(font(TYPE_MICRO))
        kicker.setStyleSheet(f"color: {TEXT_FAINT};")
        text_wrap.addWidget(kicker)
        self._active_label = QLabel("")
        self._active_label.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        text_wrap.addWidget(self._active_label)
        h.addLayout(text_wrap, 1)

        # Explicit outline — the bare "ghost" object-name styling left
        # the button floating with no edge against the accent-tinted
        # banner. A 1px border gives it a clear hit target.
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._disconnect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._disconnect_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {ink_alpha(0.28)};
                border-radius: 7px;
                padding: 5px 14px;
                color: {TEXT};
                font-weight: 500;
            }}
            QPushButton:hover {{
                background: rgba(58, 60, 68, 0.92);
                border-color: {ink_alpha(0.45)};
            }}
            QPushButton:pressed {{ background: rgba(72, 74, 82, 0.92); }}
        """)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        h.addWidget(self._disconnect_btn)

        w.hide()
        return w

    def _refresh_active_banner(self):
        active = self.cast_manager.active_cast
        if active is None:
            self._active_banner.hide()
            return
        kind = "Chromecast" if active.device_type == "chromecast" else "AirPlay"
        self._active_label.setText(f"{active.name}   ·   {kind}")
        self._active_banner.show()

    def _apply_banner_qss(self):
        """Apply the active-cast banner stylesheet from the CURRENT
        accent — split out so _reapply_accent can re-stamp it on
        theme_changed without rebuilding the whole banner widget."""
        from modules.theme import get_active_theme as _gt, _hex_to_rgb as _hr

        _ar, _ag, _ab = _hr(_gt().accent)
        self._active_banner.setStyleSheet(f"""
            QFrame#castActiveBanner {{
                background: rgba({_ar},{_ag},{_ab},0.14);
                border: 1px solid rgba({_ar},{_ag},{_ab},0.25);
                border-radius: 8px;
            }}
        """)

    def _cast_btn_qss(self) -> str:
        """QSS for the primary Cast action button — accent-coloured
        text, transparent body. Re-callable so _reapply_accent can
        push a fresh stylesheet when the user picks a new accent."""
        from modules.ui_helpers import ACCENT as _ACCENT

        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                padding: 7px 16px;
                color: {_ACCENT};
                font-weight: 600;
            }}
            QPushButton:hover {{ background: rgba(58, 60, 68, 0.92); }}
            QPushButton:pressed {{ background: rgba(72, 74, 82, 0.92); }}
            QPushButton:disabled {{ color: {ink_alpha(0.30)}; }}
        """

    def _reapply_accent(self):
        """Re-stamp every surface whose stylesheet baked the accent at
        construction. Wired to PlayerBus.theme_changed in __init__."""
        self._apply_banner_qss()
        if hasattr(self, "cast_btn"):
            self.cast_btn.setStyleSheet(self._cast_btn_qss())
        # Refresh the cached body fill + repaint — DIALOG_BODY_COLOR
        # opacity differs across theme modes, and paintEvent reads the
        # cached copy rather than the live token.
        from modules.ui_helpers import DIALOG_BODY_COLOR as _DBC

        self._dialog_body_color = _DBC
        self.update()

    def _on_disconnect(self):
        # stop_cast() handles both branches (chromecast.quit_app() +
        # mc.stop(), or AirPlay POST /stop) and clears active_cast.
        self.cast_manager.stop_cast()
        # Tell the rest of the app the cast session ended so the
        # NowPlayingBar / mini player can drop any cast indicators.
        try:
            from modules.player_state import PlayerBus

            PlayerBus.get().cast_stopped.emit()
        except Exception:
            pass
        self._refresh_active_banner()
        # Disconnecting is a terminal action — close the picker rather
        # than leaving the user on a now-stale dialog. reject() (not
        # accept()) so _open_cast_dialog doesn't treat it as a cast.
        self.reject()

    def _on_section_selection_changed(self, source_type: str, dev):
        """A section's QListWidget reported a selection change. Drive
        mutual exclusion (only one row highlighted across all sections)
        + the Cast/Forget button enable state from this one path."""
        if dev is None:
            # The forwarding section just cleared its own selection —
            # only disable global state if NO section currently holds
            # a selection, otherwise we'd race the cross-section
            # clear we're about to trigger.
            for t, sec in self._sections.items():
                if t == source_type:
                    continue
                if sec.list_widget().selectedItems():
                    return
            self.selected_device = None
            self.cast_btn.setEnabled(False)
            self.forget_btn.setEnabled(False)
            return

        # New selection — clear every other section so the user sees
        # exactly one highlighted row across the whole dialog.
        for t, sec in self._sections.items():
            if t != source_type:
                # Block signals so our cross-section clear doesn't
                # re-enter _on_section_selection_changed with dev=None.
                sec.list_widget().blockSignals(True)
                sec.clear_selection()
                sec.list_widget().blockSignals(False)

        self.selected_device = dev
        self.cast_btn.setEnabled(True)
        # Enable Forget only for AirPlay 2 receivers that have stored
        # credentials. Chromecasts and AirPlay 1 devices don't pair, so
        # Forget would be a no-op for them.
        forget_eligible = False
        try:
            from modules import airplay2 as _ap2

            if isinstance(dev.cast_object, _ap2.AirPlay2Device):
                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                if _ap2.get_stored_credentials(ap2_dev.identifier):
                    forget_eligible = True
        except Exception:
            pass
        self.forget_btn.setEnabled(forget_eligible)

    def _on_forget_clicked(self):
        dev = self.selected_device
        if dev is None:
            return
        try:
            from modules import airplay2 as _ap2

            if isinstance(dev.cast_object, _ap2.AirPlay2Device):
                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                _ap2.forget_credentials(ap2_dev.identifier)
                # Reflect immediately — the button should grey out
                # since the credentials we were storing are gone.
                self.forget_btn.setEnabled(False)
        except Exception as e:
            print(f"[CastDialog] forget_credentials failed: {e}")

    def paintEvent(self, e):
        # Rounded card body, matching the settings dialog. The custom
        # titlebar is part of the same surface, so the rounded rect
        # spans the full window.
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            path = QPainterPath()
            path.addRoundedRect(
                0.0,
                0.0,
                float(self.width()),
                float(self.height()),
                self.BODY_RADIUS,
                self.BODY_RADIUS,
            )
            p.setBrush(QColor(*self._dialog_body_color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()

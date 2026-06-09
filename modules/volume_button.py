"""
Volume control button + its slider / group-volume popups.

Extracted from ``now_playing_bar.py``. ``VolumeButton`` is the transport's
volume control; it owns either a ``_VolumeSliderPopup`` (single-device or
cast-group master) or a ``_GroupVolumePopup`` (per-member cast-group mixer
built from ``_SpeakerColumn`` rows). Lives in its own module so the floating
mini-player can reuse ``VolumeButton`` without importing the whole
now-playing bar.
"""

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QCursor,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modules.design_tokens import (
    RADIUS_WINDOW,
    TYPE_BODY,
    TYPE_CAPTION,
    type_qss,
)
from modules.icon_button import IconButton
from modules.icons import _svg_pix as _icon_svg_pix
from modules.icons import icon
from modules.player_state import PlayerBus
from modules.theme import ink_rgb
from modules.ui_helpers import (
    ACCENT,
    IDLE_TEXT,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    WASH_HOVER,
    WASH_PRESSED,
    ScrubbableSlider,
    ink_alpha,
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
    # slider needs vertical room. The mini player passes its own
    # ``height`` (the bar height) so this default only governs the
    # main now-playing bar's popup.
    POPUP_W = 36
    POPUP_H = 101
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
        # Center (now-playing-bar) popup is drawn as REAL frosted glass like the
        # tooltips + menus: a top-level ToolTip-class window so KWin blurs the
        # wallpaper behind it (a child widget has no surface for the compositor
        # to blur, which is why the grey/software-blur/transparent tries never
        # matched). ToolTip windows position on Wayland AND don't grab the mouse,
        # so the hover-open/close lifecycle still works (Qt.Popup would grab +
        # dismiss on click). Right-edge (mini player) mode stays a child panel.
        self._toplevel = not right_edge_mode
        if self._toplevel:
            self.setWindowFlags(
                Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Software backdrop-blur ("in-app acrylic"): when verified compositor
        # blur is active, paintEvent draws a blurred snapshot of the host pixels
        # under the popup (frosted glass) instead of the flat opaque pill — see
        # _refresh_backdrop / paintEvent. None ⇒ fall back to the opaque QSS fill.
        self._backdrop = None
        self._right_edge_top_radius = self._RIGHT_EDGE_CORNER_RADIUS
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
        self.slider.valueChanged.connect(self.value_changed.emit)
        layout.addWidget(self.slider, 0, Qt.AlignmentFlag.AlignHCenter)
        # Bit-perfect mode pins volume at 100 — software attenuation is
        # a float multiply and not bit-identical. The slider gates here;
        # the actual enforcement boundary is the set_volume guard in
        # player_backend (covers MPRIS / hotkeys / system media keys too).

        # Lock keys off the *runtime* bit-perfect state (the contract
        # is actually in force), not the raw setting — playing an MP3
        # with the toggle checked isn't bit-perfect and shouldn't pin
        # the slider. PlayerBus mirrors the latest runtime value so
        # popups constructed mid-track read the right state without
        # waiting for the next track-codec arrival.
        # NOTE: ``_locked`` is mutated by ``set_locked``; the lock
        # overlay is lazily built so popups that open in the unlocked
        # state never pay for the QLabel + pixmap.
        self._locked = False
        self.slider.setStyleSheet(self._slider_qss(locked=False))
        self._lock_overlay: QLabel | None = None
        # set_locked builds the overlay / disables the slider / sets
        # tooltip as needed, idempotent in either direction.
        self.set_locked(PlayerBus.get().bit_perfect_active)
        self.hide()
        # Live-accent: rebuild the slider QSS when the user picks a
        # new accent so the gauge colour follows immediately.
        # UniqueConnection: VolumePopup is rebuilt on speaker-list
        # changes, and we don't want to stack duplicate slot callers.
        PlayerBus.get().theme_changed.connect(
            self._reapply_accent, Qt.ConnectionType.UniqueConnection
        )

        # Background uses volume_popup_fill() — an OPAQUE fill baked to
        # read as the SAME tone as the volume BUTTON's hover highlight
        # (WASH_HOVER over the body). The popup is a CHILD of the host
        # window, not a top-level surface, so KWin's blur protocol can't
        # apply to just this region; a translucent fill would let the
        # host's own UI (songs-list rows, cover art, transport buttons)
        # show through SHARPLY — those paint into the same surface, so
        # they aren't behind the wallpaper-blur layer (verified bleed
        # 2026-05-27 over the Songs view).
        #
        # Previously borrowed the global POPUP_OPAQUE_FILL (tooltips /
        # menus), but on frosted themes that token carries a deliberate
        # cool/blue tint — so the popup read "cooler" than the neutral
        # white button highlight it sits over. volume_popup_fill() bakes
        # the highlight's own neutral wash over the body instead, so
        # popup + hovered button read as one continuous elevated surface.
        if right_edge_mode:
            self._apply_right_edge_qss(top_right_radius=self._RIGHT_EDGE_CORNER_RADIUS)
        else:
            self.setStyleSheet(self._center_body_qss())

    @staticmethod
    def _center_body_qss() -> str:
        """Center-mode popup body fill — shared by ``__init__`` and the
        theme-change re-stamp so the popup can't drift back to a translucent
        wash. A neutral opaque ``volume_popup_fill()`` read live so a
        dark↔light flip recolors the pill."""
        # Frosted-glass fill the menus + tooltips use: popup_body_fill() is a
        # capped translucent tone when verified KWin blur is behind it, opaque
        # otherwise. As a top-level ToolTip window the center popup rides the
        # REAL compositor blur, so this reads as the same material as the volume
        # button's hover highlight (wash_hover over the blurred body) — it
        # matches the bar instead of standing out as a cool/grey slab.
        from modules.ui_helpers import popup_body_fill as _FILL

        return f"""
            QFrame#jtVolumePopup {{
                background: {_FILL()};
                border: none;
                border-radius: 8px;
            }}
        """

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
        from modules.ui_helpers import volume_popup_fill as _WASH

        # Remember the live top-right radius so the blurred-backdrop clip path
        # (paintEvent) matches the QSS rounding in right-edge mode.
        self._right_edge_top_radius = top_right_radius
        br = self._RIGHT_EDGE_CORNER_RADIUS
        self.setStyleSheet(f"""
            QFrame#jtVolumePopup {{
                background: {_WASH()};
                border: none;
                border-top-left-radius: 0px;
                border-bottom-left-radius: 0px;
                border-top-right-radius: {top_right_radius}px;
                border-bottom-right-radius: {br}px;
            }}
        """)

    @staticmethod
    def _slider_qss(locked: bool = False) -> str:
        """Vertical volume gauge QSS — BOTTOM (add-page, filled level)
        in the current accent, TOP (sub-page, headroom) dim grey.
        Built on each call so live-accent rebuilds pick up the fresh
        ACCENT module global without stale-baking it at construction.

        When ``locked`` (bit-perfect mode pins volume at 100), the gauge
        drops the accent fill and the bright handle in favour of a flat
        dim-grey track — visually communicates "not adjustable" alongside
        the floated padlock overlay."""
        from modules.ui_helpers import ACCENT as _ACCENT

        if locked:
            add_page_bg = ink_alpha(0.20)
            handle_bg = ink_alpha(0.35)
        else:
            add_page_bg = _ACCENT
            handle_bg = TEXT
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
                background: {add_page_bg};
                border-radius: 2px;
            }}
            QSlider::handle:vertical {{
                width: 12px; height: 12px; margin: 0 -4px;
                background: {handle_bg}; border-radius: 6px;
            }}
        """

    def _reapply_accent(self):
        self.slider.setStyleSheet(self._slider_qss(locked=self._locked))
        if self._lock_overlay is not None:
            self._lock_overlay.setPixmap(_icon_svg_pix("lock", IDLE_TEXT, 12))
        # Re-stamp the popup body fill on theme_changed so a dark↔light
        # mode flip recolors the pill, not just the gauge inside it.
        if self._right_edge_mode:
            self._apply_right_edge_qss(top_right_radius=self._right_edge_top_radius)
        else:
            self.setStyleSheet(self._center_body_qss())
        # Re-grab the (now recoloured) backdrop so the frost follows the flip
        # instead of keeping stale old-mode pixels.
        self._refresh_backdrop()

    # ── Software backdrop blur (in-app frosted glass) ──────────────────
    #
    # The popup is a CHILD surface and can't ride KWin's wallpaper blur. But
    # we can fake frosted glass: snapshot the host pixels the popup covers,
    # blur them in software, and paint that as the body — see
    # ui_helpers.capture_blurred_backdrop. Gated on popup_blur_active() so on
    # any no-blur box it's byte-identical to the opaque pill.

    def _body_path(self):
        """Rounded-rect clip path matching the popup's QSS corners for the
        current mode, so the painted backdrop+veil land inside the same
        rounded body (centre = 8px all corners; right-edge = square left,
        dynamic top-right, body-radius bottom-right)."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainterPath

        rect = QRectF(self.rect())
        path = QPainterPath()
        if not self._right_edge_mode:
            path.addRoundedRect(rect, 8, 8)
            return path
        w, h = rect.width(), rect.height()
        tr = float(self._right_edge_top_radius)
        br = float(self._RIGHT_EDGE_CORNER_RADIUS)
        path.moveTo(0, 0)
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
        path.lineTo(0, h)
        path.closeSubpath()
        return path

    def _refresh_backdrop(self):
        """(Re)capture the blurred backdrop for the popup's current geometry.
        No-op (clears to the opaque fallback) unless verified blur is active.
        Grabs the HOST (always visible) with the popup momentarily hidden so it
        isn't baked into its own backdrop; the host grab itself is reliable on
        Wayland (it re-renders the widget tree, not the compositor)."""
        from modules import ui_helpers as _u

        # Center (now-playing-bar) popup is transparent — it shows the window
        # content behind it directly, so it carries no software backdrop.
        if not self._right_edge_mode:
            if self._backdrop is not None:
                self._backdrop = None
                self.update()
            return
        if not _u.popup_blur_active():
            if self._backdrop is not None:
                self._backdrop = None
                self.update()
            return
        host = self.parentWidget()
        if host is None:
            return
        was_visible = self.isVisible()
        if was_visible:
            self.setVisible(False)
        try:
            self._backdrop = _u.capture_blurred_backdrop(host, self.geometry())
        finally:
            if was_visible:
                self.setVisible(True)
        self.update()

    def paintEvent(self, e):
        # Opaque QSS pill first (the no-blur fallback); when a blurred backdrop
        # is available, paint it + a thin theme veil over the top, clipped to
        # the same rounded body. The slider child paints on top afterwards.
        super().paintEvent(e)
        if self._backdrop is None:
            return
        from PySide6.QtGui import QPainter

        from modules import ui_helpers as _u

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setClipPath(self._body_path())
        r = _u.VOLUME_BACKDROP_RADIUS
        p.drawPixmap(-r, -r, self._backdrop)
        p.fillRect(self.rect(), _u.volume_popup_veil_qcolor())
        p.end()

    def showEvent(self, e):
        super().showEvent(e)
        if not self._toplevel:
            return
        # Real KWin blur behind the top-level popup — same call the menus +
        # tooltips use. Deferred a tick so the rounded blur region is shaped
        # against the final laid-out geometry (matches the 8px QSS corners).
        from PySide6.QtCore import QTimer

        from modules import ui_helpers as _u

        if _u.popup_blur_active():
            QTimer.singleShot(0, lambda: _u.apply_elevated_blur(self, corner_radius=8))

    def set_value(self, v: int):
        was_blocked = self.slider.blockSignals(True)
        try:
            self.slider.setValue(v)
        finally:
            self.slider.blockSignals(was_blocked)

    def set_locked(self, locked: bool) -> None:
        """Toggle the bit-perfect lock visual. Idempotent — safe to call
        repeatedly with the same value. Lazily builds the lock overlay
        the first time the popup needs one; tears it down when
        unlocking so a track change FLAC → MP3 doesn't leave a stale
        padlock floating over an interactive slider."""
        locked = bool(locked)
        if locked == self._locked and (
            (locked and self._lock_overlay is not None)
            or (not locked and self._lock_overlay is None)
        ):
            return
        self._locked = locked
        self.slider.setEnabled(not locked)
        self.slider.setStyleSheet(self._slider_qss(locked=locked))
        if locked:
            self.slider.setToolTip(
                "Bit-perfect mode locks volume at 100%. Disable in Settings → Playback to adjust."
            )
            if self._lock_overlay is None:
                self._lock_overlay = QLabel(self)
                self._lock_overlay.setPixmap(_icon_svg_pix("lock", IDLE_TEXT, 12))
                self._lock_overlay.setStyleSheet("background: transparent;")
                self._lock_overlay.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents
                )
                self._lock_overlay.setFixedSize(12, 12)
            self._position_lock_overlay()
            self._lock_overlay.show()
            self._lock_overlay.raise_()
        else:
            self.slider.setToolTip("")
            if self._lock_overlay is not None:
                self._lock_overlay.hide()
                self._lock_overlay.deleteLater()
                self._lock_overlay = None

    def _position_lock_overlay(self) -> None:
        """Center the padlock on the slider's groove. Kept out of the inline
        ``set_locked`` path because the right-edge popup is resized AFTER
        construction — ``set_locked`` runs in ``__init__`` with a stale popup
        size, so the lock landed off-centre until something moved it. Centering
        on the slider's own geometry (re-run from ``resizeEvent``) keeps it
        pinned to the visible track centre across the popup's dynamic height."""
        if self._lock_overlay is None:
            return
        cx = self.slider.x() + self.slider.width() // 2
        cy = self.slider.y() + self.slider.height() // 2
        self._lock_overlay.move(
            cx - self._lock_overlay.width() // 2,
            cy - self._lock_overlay.height() // 2,
        )

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._position_lock_overlay()
        # Right-edge reflow (compact↔expanded) changes the overlapped region —
        # re-grab so the frost matches. Guarded on an existing backdrop so the
        # construction-time resize (before first show) doesn't grab early.
        if self._backdrop is not None:
            self._refresh_backdrop()

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
    # Square'd so the loading spinner that rings the arrow stays
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

    @staticmethod
    def _body_qss() -> str:
        """Opaque popup body fill — shared by __init__ and the
        theme-change re-stamp so the group popup can't drift back to a
        translucent wash. Mirrors _VolumeSliderPopup's neutral
        volume_popup_fill() body (baked to match the volume button's
        hover highlight); reads it live so a dark↔light flip recolors it."""
        from modules.ui_helpers import volume_popup_fill as _WASH

        return f"""
            QFrame#jtGroupVolumePopup {{
                background: {_WASH()};
                border: none;
                border-radius: 8px;
            }}
            QFrame#jtGroupVolumePopup QLabel {{ background: transparent; }}
        """

    @staticmethod
    def _toggle_btn_qss() -> str:
        """Chevron toggle QSS — faint ink, accent-less. Reads live
        ``ui_helpers`` tokens so a dark↔light flip re-stamps the glyph
        colour (shared by construction + ``_reapply_accent``)."""
        from modules import ui_helpers as _u

        return f"""
            QPushButton {{
                background: transparent; color: {_u.TEXT_FAINT};
                border: none; padding: 0;
                font-size: {TYPE_BODY.size_px}px;
            }}
            QPushButton:hover {{ color: {_u.TEXT}; }}
        """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("jtGroupVolumePopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Opaque body fill (volume_popup_fill) — a neutral fill baked to
        # match the volume button's hover highlight, same as the
        # single-device _VolumeSliderPopup. A raw WASH_HOVER here read far
        # too transparent — the blurred UI behind bled through the group
        # popup. Stamped via the shared _body_qss() so it can't drift from
        # the single-device popup again, and re-stamped on theme_changed
        # in _reapply_accent.
        self.setStyleSheet(self._body_qss())
        self._expanded = False
        self._member_cols: list = []
        # The "No speakers found" placeholder, when shown — kept so the
        # theme re-stamp can recolor it (set in set_members).
        self._empty_label: QLabel | None = None
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
        self._toggle_btn.setStyleSheet(self._toggle_btn_qss())
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
        # UniqueConnection: this popup is rebuilt on every group change,
        # so stacking duplicate callers would multiply per-flip cost.
        PlayerBus.get().theme_changed.connect(
            self._reapply_accent, Qt.ConnectionType.UniqueConnection
        )

    def _reapply_accent(self):
        # Re-stamp the body fill so a dark↔light flip recolors the
        # opaque pill (POPUP_OPAQUE_FILL changes with theme mode), matching
        # _VolumeSliderPopup. Without this the popup would keep the old
        # mode's fill after a theme switch.
        self.setStyleSheet(self._body_qss())
        self._master_slider.setStyleSheet(self._master_slider_qss())
        for col in self._member_cols:
            col.reapply_accent()
        # Text chrome (chevron toggle + footer labels) bakes faint/dim
        # ink at construction, so re-stamp it from the live tokens too —
        # otherwise it stays wrong-contrast on a dark↔light flip.
        from modules import ui_helpers as _u

        self._toggle_btn.setStyleSheet(self._toggle_btn_qss())
        self._hover_name.setStyleSheet(f"color: {_u.TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self._group_label.setStyleSheet(
            f"color: {_u.TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
        )
        if self._empty_label is not None:
            self._empty_label.setStyleSheet(
                f"color: {_u.TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
            )

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
        # The placeholder (if any) was just deleted above — drop the ref
        # so the theme re-stamp doesn't touch a dangling widget.
        self._empty_label = None

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
            self._empty_label = empty
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


class VolumeButton(IconButton):
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
        # Bit-perfect toggle: refresh tooltip + the slider in any open
        # popup so the lock takes effect without re-hovering the button.
        # Runtime state — see ``_on_bit_perfect_active_changed`` for why
        # the popup lock and button tooltip key off the runtime signal
        # rather than ``bit_perfect_changed`` (the raw setting flag).
        self.bus.bit_perfect_active_changed.connect(self._on_bit_perfect_active_changed)
        self._refresh_tooltip_for_bit_perfect()

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

    def _refresh_tooltip_for_bit_perfect(self):
        """Tooltip reflects the RUNTIME contract, not the raw setting:
        bit-perfect checked + MP3 playing isn't actually bit-perfect, so
        the lock-tooltip would lie. Falls back to the regular tooltip
        in that case — same wording as when the setting is off."""
        if self.bus.bit_perfect_active:
            self.setToolTip(
                "Mute / unmute · volume locked at 100% (Bit-perfect mode)"
            )
        else:
            self.setToolTip(
                "Mute / unmute · scroll to adjust · hover for slider"
            )

    @Slot(bool)
    def _on_bit_perfect_active_changed(self, active: bool):
        """Refresh the slider's lock state in any live popup and the
        button's tooltip so the lock surfaces without re-hovering.

        Listens to ``bit_perfect_active_changed`` (the runtime state)
        rather than ``bit_perfect_changed`` (the setting flag) so a
        track-change from FLAC → MP3 unlocks the slider mid-session
        when the user has bit-perfect checked but the new source is
        lossy in the first place."""
        self._refresh_tooltip_for_bit_perfect()
        if self._popup is None or isinstance(self._popup, _GroupVolumePopup):
            return
        self._popup.set_locked(active)

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
        # Capture the frosted backdrop NOW — geometry is final and the popup
        # isn't painted yet, so the grab excludes it (flicker-free) and dodges
        # the Wayland hidden-widget-grab issue. No-op unless blur is verified.
        if hasattr(self._popup, "_refresh_backdrop"):
            self._popup._refresh_backdrop()
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

        # A top-level ToolTip popup positions in GLOBAL (screen) coords; a
        # child popup positions in host coords.
        toplevel = getattr(self._popup, "_toplevel", False)
        if toplevel:
            btn_top = self.mapToGlobal(QPoint(self.width() // 2, 0))
        else:
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
        if not toplevel:
            # Clamp inside the host so a child popup is never partially
            # off-screen at a window edge. A top-level positions in screen
            # space and KWin keeps it on-screen.
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
        # Restoring a saved balance re-emits member_changed with the
        # already-stored value, which would round-trip an identical write
        # back to disk. The device push above still happens; only the
        # redundant persist is elided.
        if group_data.get(uuid) == vol:
            return
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

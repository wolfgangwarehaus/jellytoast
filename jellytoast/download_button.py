"""
Cover-overlay download control for the now-playing page.

Extracted from ``now_playing_page.py``. ``_DownloadButton`` is the 32-px
download control pinned to the now-playing cover's bottom-left corner
(mirrors the favourite ``CoverOverlayButton`` in shape); it drives the
offline download / remove flow for the current track and reflects
download progress. Self-contained — lives in its own module so other
surfaces can reuse it without importing the whole now-playing page.

``NowPlayingPage`` re-imports it, so
``from jellytoast.now_playing_page import _DownloadButton`` still resolves.
"""


from PySide6.QtCore import (
    QEvent,
    QPointF,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QPushButton,
)

from jellytoast.icons import ICON_BRIGHT, ICON_DIM
from jellytoast.theme import contrast_ink, ink_rgb
from jellytoast.ui_helpers import (
    ACCENT,
    WARN_FG,
    overlay_disc_colors,
)


class _DownloadButton(QPushButton):
    """A 32-px download control pinned to the cover's bottom-left
    corner. Matches the BR favorite ``CoverOverlayButton`` in shape,
    hover behaviour, and circular dark backdrop. Four visual states,
    driven by ``set_state`` from the page's ``download_progress`` hook:

      idle        — a download glyph (the album isn't downloaded)
      pending /   — a faint track ring; ``downloading`` overlays a
      downloading   bright accent arc filling clockwise from 12 o'clock
      complete    — a filled accent disc with a check
      failed      — the download glyph tinted red

    Visibility: hover-only on idle / complete / failed; always visible
    while pending / downloading so the progress ring is legible at a
    glance. Anchoring + visibility mirror CoverOverlayButton — install
    an event filter on the parent cover so the button shows on Enter
    and hides on Leave with a small grace window."""

    _TIPS = {
        "idle": "Download this album",
        "pending": "Queued for download…",
        "downloading": "Downloading… (click to cancel)",
        "complete": "Downloaded — click to remove",
        "failed": "Download failed — click to retry",
    }
    _ANCHOR_MARGIN = 10
    _HIDE_GRACE_MS = 80

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(32, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Circular backdrop — shares CoverOverlayButton's theme-aware
        # disc so it matches the favorite heart exactly. The custom
        # paint inside paintEvent owns the icon + ring; this QSS
        # handles the hit-box, hover wash, and rounding.
        self._apply_disc_style()
        try:
            from jellytoast.player_state import PlayerBus as _Bus

            _Bus.get().theme_changed.connect(self._apply_disc_style)
        except Exception:
            pass
        self._state = "idle"
        self._fraction = 0.0
        # ``_enabled`` is the master gate — set False (live mode on the
        # NP page) hides the button regardless of hover; set True
        # restores the hover-reveal behaviour. Independent of Qt's
        # ``visible`` so the event filter can still drive show/hide
        # without fighting external setVisible() calls.
        self._enabled = True
        self.setToolTip(self._TIPS["idle"])
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self._HIDE_GRACE_MS)
        self._hide_timer.timeout.connect(self._maybe_hide)
        if parent is not None:
            parent.installEventFilter(self)
            self.hide()
            self._reposition()

    def _apply_disc_style(self):
        """(Re)build the circular backdrop QSS — theme-aware via
        overlay_disc_colors(), so the download control's disc matches
        the favorite heart in every theme."""
        normal, hover = overlay_disc_colors()
        self.setStyleSheet(
            f"QPushButton {{ background: {normal}; border: none;"
            f" border-radius: 16px; }}"
            f"QPushButton:hover {{ background: {hover}; }}"
        )
        self.update()

    def state(self) -> str:
        return self._state

    def set_state(self, state: str, fraction: float = 0.0):
        prev_busy = self._state in ("pending", "downloading")
        self._state = state if state in self._TIPS else "idle"
        self._fraction = max(0.0, min(1.0, fraction))
        self.setToolTip(self._TIPS[self._state])
        now_busy = self._state in ("pending", "downloading")
        # Pending / downloading: always visible so the ring is legible
        # without forcing the user to hover. Other states defer to the
        # parent's hover state. ``_enabled`` False suppresses both.
        if not self._enabled:
            self.hide()
        elif now_busy:
            self.show()
            self.raise_()
        elif prev_busy:
            p = self.parentWidget()
            if p is None or not p.underMouse():
                self.hide()
        self.update()

    def set_enabled(self, on: bool) -> None:
        """Master visibility gate. Off → hide unconditionally (live
        mode on the NP page, where downloading "this track's album"
        is ambiguous). On → hover-reveal resumes."""
        if on == self._enabled:
            return
        self._enabled = on
        if not on:
            self.hide()
        else:
            # Re-evaluate based on current state + hover.
            p = self.parentWidget()
            if self._state in ("pending", "downloading"):
                self.show()
                self.raise_()
            elif p is not None and p.underMouse():
                self.show()
                self.raise_()
            else:
                self.hide()

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.Resize:
            self._reposition()
        elif et == QEvent.Type.Enter:
            if not self._enabled:
                return False
            self._hide_timer.stop()
            self.show()
            self.raise_()
        elif et == QEvent.Type.Leave:
            if self._state in ("pending", "downloading"):
                # Stay visible — the user needs the progress.
                return False
            self._hide_timer.start()
        return False

    def _maybe_hide(self):
        if self._state in ("pending", "downloading"):
            return
        p = self.parentWidget()
        if p is None or p.underMouse():
            return
        self.hide()

    def _reposition(self):
        p = self.parentWidget()
        if p is None:
            return
        # Bottom-left of the parent (cover).
        x = self._ANCHOR_MARGIN
        y = p.height() - self.height() - self._ANCHOR_MARGIN
        self.move(max(0, x), max(0, y))
        self.raise_()

    def paintEvent(self, e):
        super().paintEvent(e)  # hover / pressed background
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        r = 9.0
        accent = QColor(ACCENT)
        # Theme ink — the ring track + downloading dot read on the disc
        # in either theme (dark on a light disc, light on a dark one).
        track = QColor(*ink_rgb(), 64)
        glyph = QColor(*ink_rgb(), 210)

        if self._state in ("downloading", "pending"):
            ring = QRectF(cx - r, cy - r, 2 * r, 2 * r)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(track, 2.4))
            p.drawArc(ring, 0, 360 * 16)
            if self._state == "downloading" and self._fraction > 0:
                pen = QPen(accent, 2.4)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                # 12 o'clock start, sweeping clockwise (negative span).
                p.drawArc(ring, 90 * 16, -int(self._fraction * 360) * 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glyph if self._state == "downloading" else track)
            p.drawEllipse(QPointF(cx, cy), 2.0, 2.0)
        elif self._state == "complete":
            # Filled accent disc + a down-arrow — the music-app convention for
            # "downloaded / available offline" (Spotify's downloaded badge,
            # Material's download_for_offline). The arrow ink is contrast-picked
            # against the accent (was a hardcoded white that went sub-AA on the
            # green/teal/orange presets).
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawEllipse(QPointF(cx, cy), r, r)
            pen = QPen(contrast_ink(accent), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawLine(QPointF(cx, cy - 4.0), QPointF(cx, cy + 3.2))
            arrow = QPainterPath()
            arrow.moveTo(cx - 3.0, cy + 0.2)
            arrow.lineTo(cx, cy + 3.6)
            arrow.lineTo(cx + 3.0, cy + 0.2)
            p.drawPath(arrow)
        else:  # idle / failed → download glyph
            # Idle matches the unfilled favorite heart: muted ICON_DIM
            # at rest, brightening to ICON_BRIGHT on hover (the heart's
            # QIcon flips the same way). The dim tone also reads as
            # "not downloaded yet" next to a filled-disc downloaded
            # badge. Failed stays red regardless.
            if self._state == "failed":
                col = QColor(WARN_FG)
            else:
                col = QColor(ICON_BRIGHT if self.underMouse() else ICON_DIM)
            pen = QPen(col, 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawLine(QPointF(cx, cy - 5.5), QPointF(cx, cy + 2.5))
            arrow = QPainterPath()
            arrow.moveTo(cx - 3.4, cy - 1.0)
            arrow.lineTo(cx, cy + 3.0)
            arrow.lineTo(cx + 3.4, cy - 1.0)
            p.drawPath(arrow)
            p.drawLine(QPointF(cx - 4.8, cy + 5.6), QPointF(cx + 4.8, cy + 5.6))
        p.end()

"""EQ T3b — parametric curve editor widget.

Log-frequency canvas with draggable nodes — Symfonium's "Expert PEQ"
surface, scaled to the jellytoast settings dialog. Renders the active
band list as nodes, overlays the cumulative response curve, and emits
``band_edited`` / ``band_dragging`` when the user moves a node.

The widget itself is band-source agnostic — it reads a list of dicts
``{f, w, g, t}`` and writes back via signals. The host (Settings
dialog) decides whether the back-write lands in the graphic-mode
slider state or the AutoEQ profile JSON.

Design notes:

- Frequency axis is log10 from 20 Hz to 22 kHz. Octave gridlines at
  the ISO band centres + 20 Hz / 22 kHz endpoints.
- dB axis is linear from -15 to +15 (clipping into the EQ's ±12
  envelope plus a touch of overhead so nodes never sit flush with
  the top/bottom edge).
- Cumulative response is the per-band sum of Gaussian-approximated
  peaking contributions. Visually intuitive, not DSP-accurate — the
  goal is to show where each band influences the response, not to
  replicate the biquad's exact transfer function.
- ``lock_freq`` mode: in graphic mode the x-coordinate of a node is
  pinned to its ISO band; only y is editable. AutoEQ-profile mode
  unlocks both axes.

See ``docs/research/eq_dsp_v2.md`` §6 T3b for the design rationale.
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget


# ── Plot range ──────────────────────────────────────────────────────────


FREQ_MIN_HZ: float = 20.0
FREQ_MAX_HZ: float = 22000.0
DB_MIN: float = -15.0
DB_MAX: float = 15.0

# Drawn faint behind the response curve. Octave-ish spacing — the same
# ISO centres the graphic EQ uses, plus the endpoints.
_GRID_FREQS = (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)
_GRID_DBS = (-12, -6, 0, 6, 12)


# ── Coordinate transforms (pure functions; testable) ────────────────────


def freq_to_x(
    freq: float, width: int, pad_l: int, pad_r: int
) -> float:
    """Map frequency (Hz) to x pixel using log10 scaling. Out-of-range
    frequencies clamp to the plot edges so a malformed profile can't
    paint outside the canvas."""
    plot_w = max(1, width - pad_l - pad_r)
    if freq <= FREQ_MIN_HZ:
        return float(pad_l)
    if freq >= FREQ_MAX_HZ:
        return float(width - pad_r)
    log_min = math.log10(FREQ_MIN_HZ)
    log_max = math.log10(FREQ_MAX_HZ)
    norm = (math.log10(freq) - log_min) / (log_max - log_min)
    return pad_l + norm * plot_w


def x_to_freq(
    x: float, width: int, pad_l: int, pad_r: int
) -> float:
    """Inverse of ``freq_to_x``. Clamps to the plot range."""
    plot_w = max(1, width - pad_l - pad_r)
    norm = (x - pad_l) / plot_w
    norm = max(0.0, min(1.0, norm))
    log_min = math.log10(FREQ_MIN_HZ)
    log_max = math.log10(FREQ_MAX_HZ)
    return 10 ** (log_min + norm * (log_max - log_min))


def db_to_y(
    db: float, height: int, pad_t: int, pad_b: int
) -> float:
    """Map dB (linear) to y pixel. Positive dB is up — y=0 at the
    top, y=height at the bottom (Qt's coordinate convention)."""
    plot_h = max(1, height - pad_t - pad_b)
    norm = (DB_MAX - db) / (DB_MAX - DB_MIN)
    norm = max(0.0, min(1.0, norm))
    return pad_t + norm * plot_h


def y_to_db(
    y: float, height: int, pad_t: int, pad_b: int
) -> float:
    plot_h = max(1, height - pad_t - pad_b)
    norm = (y - pad_t) / plot_h
    norm = max(0.0, min(1.0, norm))
    return DB_MAX - norm * (DB_MAX - DB_MIN)


def width_to_q(freq: float, width: float) -> float:
    """Inverse of ``modules.eq_presets.q_to_width_hz`` — recover the
    Q factor from a (freq, width-in-Hz) pair. Clamps to a sensible
    range so a malformed band can't return zero or infinity."""
    if freq <= 0 or width <= 0:
        return 1.0
    return max(0.1, min(20.0, float(freq) / float(width)))


# Soft cap on band count — AutoEQ profiles in the wild are typically
# 10; some go to 12. 16 has headroom without making the canvas
# unreadable from node overlap. ``Add band`` is gated at this cap.
MAX_BANDS: int = 16


def band_contribution_db(band: dict, freq: float) -> float:
    """Approximate dB contribution of a peaking band at ``freq``.

    Visually intuitive Gaussian bell — not DSP-accurate. The exponent's
    1.4 factor is tuned so a band's "edge" (one bandwidth from centre)
    sits at ~14 % of peak gain, which reads as the natural skirt of a
    bell on the canvas. The actual filter response is biquad-shaped;
    the editor's job is to show where each band influences things, not
    to predict the exact transfer function.

    Bands with ``g == 0`` contribute nothing (skip the math entirely)
    so a flat profile doesn't waste cycles in the inner loop."""
    g = float(band.get("g", 0.0))
    if g == 0.0:
        return 0.0
    f_c = float(band.get("f", 1000.0))
    if f_c <= 0 or freq <= 0:
        return 0.0
    w = max(1.0, float(band.get("w", f_c)))
    # Convert width-in-Hz to bandwidth-in-octaves via log2(1 + w/f_c).
    # Clamp to a minimum of 0.1 octave so a near-zero w doesn't divide
    # to infinity (and renders as a sharp spike, not a hole).
    bw_oct = max(0.1, math.log2(1.0 + w / f_c))
    d_oct = math.log2(freq / f_c)
    normalized = d_oct / bw_oct
    return g * math.exp(-((normalized * 1.4) ** 2))


def cumulative_response_db(bands: list, freq: float) -> float:
    """Sum band contributions at ``freq``. Pure function for tests."""
    total = 0.0
    for b in bands:
        total += band_contribution_db(b, freq)
    return total


# ── Widget ──────────────────────────────────────────────────────────────


class EqCurveEditor(QWidget):
    """Log-frequency parametric EQ curve editor.

    Set the active bands via ``set_bands(bands, lock_freq=...)``. The
    widget repaints automatically. User drag emits:

    * ``band_dragging(index, freq, gain)`` — live during drag (every
      ``mouseMoveEvent``); the host can mirror this into a slider or
      a status label.
    * ``band_edited(index, freq, gain)`` — on drag release (one final
      value). The host writes this back to settings.

    Q (width) editing isn't exposed on the canvas in v1 — mouse wheel
    over a node will land in T3c. For now Q comes from the band data
    and stays put.
    """

    PAD_L: int = 32  # left axis labels
    PAD_R: int = 8
    PAD_T: int = 10
    PAD_B: int = 18  # x-axis labels
    NODE_RADIUS: int = 6
    NODE_HIT_RADIUS: int = 10

    # (index, freq, gain). Freq is Hz, gain is dB.
    band_edited = Signal(int, float, float)
    band_dragging = Signal(int, float, float)
    # T3c — additional gestures. ``band_q_changed(idx, new_w_hz)``
    # fires from the wheel; ``band_added(freq, gain)`` fires from a
    # double-click on empty canvas; ``band_removed(idx)`` fires from
    # a right-click on a node. All three only fire when ``lock_freq``
    # is False (PEQ mode — graphic mode keeps a fixed 10-band layout).
    band_q_changed = Signal(int, float)
    band_added = Signal(float, float)
    band_removed = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._bands: list[dict] = []
        self._lock_freq: bool = True
        self._dragging_index: int = -1
        self._hover_index: int = -1
        self.setMinimumSize(320, 130)
        self.setMouseTracking(True)
        # Cursor flips between arrow / pointing-hand on node hover so
        # the affordance is obvious without a tooltip per node.
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ── Public API ─────────────────────────────────────────────────────

    def set_bands(self, bands: list, *, lock_freq: bool = True) -> None:
        """Replace the band list. ``lock_freq=True`` pins the x position
        of each node — graphic mode. ``False`` allows horizontal drag —
        AutoEQ-profile mode."""
        self._bands = [dict(b) for b in bands]
        self._lock_freq = bool(lock_freq)
        self._dragging_index = -1
        self.update()

    # ── Painting ──────────────────────────────────────────────────────

    def paintEvent(self, _event: QPaintEvent) -> None:
        from modules.ui_helpers import ACCENT as _ACCENT, TEXT_DIM, TEXT_FAINT

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        # Background: transparent — host frame already provides the
        # rounded surface. The plot area gets a faint ink wash so the
        # curve has visible context.
        plot_l = self.PAD_L
        plot_r = w - self.PAD_R
        plot_t = self.PAD_T
        plot_b = h - self.PAD_B

        # Grid
        grid_pen = QPen(QColor(TEXT_FAINT))
        grid_pen.setWidthF(0.5)
        zero_pen = QPen(QColor(TEXT_DIM))
        zero_pen.setWidthF(1.0)
        for freq in _GRID_FREQS:
            x = freq_to_x(freq, w, self.PAD_L, self.PAD_R)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot_t), QPointF(x, plot_b))
        for db in _GRID_DBS:
            y = db_to_y(db, h, self.PAD_T, self.PAD_B)
            p.setPen(zero_pen if db == 0 else grid_pen)
            p.drawLine(QPointF(plot_l, y), QPointF(plot_r, y))

        # Axis labels
        p.setPen(QColor(TEXT_DIM))
        small = QFont(self.font())
        small.setPixelSize(9)
        p.setFont(small)
        fm = QFontMetrics(small)
        # x labels under each octave gridline that has room
        for freq in (50, 200, 1000, 5000, 20000):
            x = freq_to_x(freq, w, self.PAD_L, self.PAD_R)
            label = (
                f"{int(freq / 1000)}k" if freq >= 1000 else f"{int(freq)}"
            )
            tw = fm.horizontalAdvance(label)
            p.drawText(QPointF(x - tw / 2, h - 4), label)
        # y labels at ±12 / 0
        for db in (-12, 0, 12):
            y = db_to_y(db, h, self.PAD_T, self.PAD_B)
            label = f"{db:+d}" if db != 0 else "0"
            p.drawText(QPointF(2, y + 3), label)

        # Cumulative response curve
        if self._bands:
            curve_pen = QPen(QColor(_ACCENT))
            curve_pen.setWidthF(1.5)
            curve_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            curve_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(curve_pen)
            path = QPainterPath()
            first = True
            # One sample per pixel column across the plot — gives a
            # smooth curve without per-band-evaluation overhead per
            # pixel-pair (10 bands × ~400 pixels = 4 k mults per
            # repaint, comfortably real-time).
            for px in range(int(plot_l), int(plot_r) + 1):
                freq = x_to_freq(px, w, self.PAD_L, self.PAD_R)
                db = cumulative_response_db(self._bands, freq)
                py = db_to_y(db, h, self.PAD_T, self.PAD_B)
                if first:
                    path.moveTo(QPointF(px, py))
                    first = False
                else:
                    path.lineTo(QPointF(px, py))
            p.drawPath(path)

        # Nodes
        for idx, band in enumerate(self._bands):
            x = freq_to_x(float(band.get("f", 1000)), w, self.PAD_L, self.PAD_R)
            y = db_to_y(float(band.get("g", 0)), h, self.PAD_T, self.PAD_B)
            is_active = idx == self._dragging_index or idx == self._hover_index
            radius = self.NODE_RADIUS + (1 if is_active else 0)
            # Filled accent circle with a thin dark outline so it
            # reads against both the curve and the grid.
            p.setPen(QPen(QColor(0, 0, 0, 160), 1.0))
            p.setBrush(QColor(_ACCENT))
            p.drawEllipse(QPointF(x, y), radius, radius)

        # T3c — hover / drag tooltip. Floats near the active node and
        # surfaces (freq, gain, Q) for the user. Skipped when nothing
        # is hovered/dragged.
        active_idx = (
            self._dragging_index
            if self._dragging_index >= 0
            else self._hover_index
        )
        if 0 <= active_idx < len(self._bands):
            band = self._bands[active_idx]
            f = float(band.get("f", 1000))
            g = float(band.get("g", 0))
            w_hz = float(band.get("w", f))
            q = width_to_q(f, w_hz)
            f_label = (
                f"{int(round(f / 1000))}k"
                if f >= 1000 and round(f / 1000) * 1000 == round(f)
                else (f"{f / 1000:.2f}k" if f >= 1000 else f"{int(round(f))}")
            )
            text = f"{f_label} Hz · {g:+.1f} dB · Q {q:.2f}"
            tooltip_font = QFont(self.font())
            tooltip_font.setPixelSize(10)
            p.setFont(tooltip_font)
            fm = QFontMetrics(tooltip_font)
            tw = fm.horizontalAdvance(text) + 12
            th = fm.height() + 4
            # Anchor above the node when there's room, else below.
            nx = freq_to_x(f, w, self.PAD_L, self.PAD_R)
            ny = db_to_y(g, h, self.PAD_T, self.PAD_B)
            tx = max(plot_l, min(plot_r - tw, nx - tw / 2))
            ty = ny - self.NODE_RADIUS - th - 4
            if ty < plot_t:
                ty = ny + self.NODE_RADIUS + 4
            rect = QRectF(tx, ty, tw, th)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 200))
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(QColor(255, 255, 255))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    # ── Mouse interaction ─────────────────────────────────────────────

    def _hit_node(self, pos: QPointF) -> int:
        """Return the index of the node under ``pos``, or -1. Searched
        in reverse order so the visually topmost node (drawn last)
        wins when two nodes overlap."""
        w = self.width()
        h = self.height()
        r = self.NODE_HIT_RADIUS
        for idx in range(len(self._bands) - 1, -1, -1):
            band = self._bands[idx]
            nx = freq_to_x(float(band.get("f", 1000)), w, self.PAD_L, self.PAD_R)
            ny = db_to_y(float(band.get("g", 0)), h, self.PAD_T, self.PAD_B)
            if (pos.x() - nx) ** 2 + (pos.y() - ny) ** 2 <= r * r:
                return idx
        return -1

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        idx = self._hit_node(event.position())
        if idx >= 0:
            self._dragging_index = idx
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._dragging_index >= 0:
            # Drag in progress — update the band's freq + gain.
            band = self._bands[self._dragging_index]
            new_gain = max(-12.0, min(12.0, y_to_db(
                pos.y(), self.height(), self.PAD_T, self.PAD_B
            )))
            band["g"] = new_gain
            if not self._lock_freq:
                new_freq = x_to_freq(
                    pos.x(), self.width(), self.PAD_L, self.PAD_R
                )
                band["f"] = int(round(new_freq))
                # When the freq moves, the bandwidth follows it
                # (w = f keeps the one-octave shape). AutoEQ-loaded
                # bands carry their own w, but graphic-mode bands
                # propagate the rule.
                band["w"] = float(band["f"])
            self.band_dragging.emit(
                self._dragging_index,
                float(band["f"]),
                float(band["g"]),
            )
            self.update()
            event.accept()
            return
        # Hover — repaint to grow the under-cursor node by 1 px.
        idx = self._hit_node(pos)
        if idx != self._hover_index:
            self._hover_index = idx
            if idx >= 0:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._dragging_index >= 0
        ):
            idx = self._dragging_index
            band = self._bands[idx]
            self._dragging_index = -1
            self.setCursor(
                Qt.CursorShape.OpenHandCursor
                if self._hit_node(event.position()) >= 0
                else Qt.CursorShape.ArrowCursor
            )
            self.band_edited.emit(
                idx, float(band["f"]), float(band["g"])
            )
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        if self._dragging_index < 0:
            self._hover_index = -1
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    # ── T3c — Q wheel, add (dbl-click), remove (right-click) ───────

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Scroll wheel over a node adjusts Q (bandwidth). Up/scroll-
        forward narrows (higher Q); down/scroll-back widens. Disabled
        in graphic mode — the 10-band ISO layout's widths are fixed."""
        if self._lock_freq:
            return super().wheelEvent(event)
        idx = self._hit_node(event.position())
        if idx < 0:
            return super().wheelEvent(event)
        band = self._bands[idx]
        f = float(band.get("f", 1000))
        w = float(band.get("w", f))
        q_now = width_to_q(f, w)
        # 120 angle-units = one notch on a typical mouse wheel.
        steps = event.angleDelta().y() / 120.0
        # Scaling: each notch multiplies Q by ~1.2 (up) or divides
        # (down). Geometric step keeps Q-doubling reachable in ~3
        # notches across the useful range (0.3–6).
        new_q = q_now * (1.2 ** steps)
        new_q = max(0.1, min(20.0, new_q))
        new_w = f / new_q
        band["w"] = new_w
        self.band_q_changed.emit(idx, new_w)
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Double-click on empty canvas adds a band at click freq/gain
        (PEQ mode only). On a node, the parent class handles default
        behaviour. Capped at ``MAX_BANDS`` (16); past that, no-op."""
        if (
            event.button() != Qt.MouseButton.LeftButton
            or self._lock_freq
        ):
            return super().mouseDoubleClickEvent(event)
        if self._hit_node(event.position()) >= 0:
            return super().mouseDoubleClickEvent(event)
        if len(self._bands) >= MAX_BANDS:
            return super().mouseDoubleClickEvent(event)
        pos = event.position()
        freq = x_to_freq(pos.x(), self.width(), self.PAD_L, self.PAD_R)
        gain = y_to_db(pos.y(), self.height(), self.PAD_T, self.PAD_B)
        gain = max(-12.0, min(12.0, gain))
        self.band_added.emit(float(freq), float(gain))
        event.accept()

    def contextMenuEvent(self, event) -> None:
        """Right-click on a node removes the band (PEQ mode only).
        Implemented via ``contextMenuEvent`` so we get the platform's
        right-click semantics for free without manually fishing button
        events out of ``mousePressEvent``."""
        if self._lock_freq:
            return super().contextMenuEvent(event)
        idx = self._hit_node(QPointF(float(event.pos().x()), float(event.pos().y())))
        if idx < 0:
            return super().contextMenuEvent(event)
        if len(self._bands) <= 1:
            # Don't let the user delete the last band — leaves the
            # editor with nothing to do and confuses the apply_eq
            # cache (empty AutoEQ profile vs cleared profile).
            return
        self.band_removed.emit(idx)
        event.accept()

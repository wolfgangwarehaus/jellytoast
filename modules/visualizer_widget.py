"""Spectrum-bar visualizer widget — the third NP-page left-pane mode.

Implements the spec at ``docs/research/visualizer_rendering.md`` —
32 grounded log-spaced bars driven by the FFT pipeline in
``modules.visualizer``, asymmetric exponential smoothing, accent
gradient, slow decay to a 2 % baseline, cast-active placeholder.

The widget pulls band data from ``PlayerBus.visualizer_bands_changed``
and paints on every signal — no internal timer. The backend already
throttles to 30 Hz so painting per-signal gives a free 30 Hz repaint
with no risk of overrun.
"""

from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import QWidget

from modules.player_state import PlayerBus


# ── Tunables (spec §3-6) ────────────────────────────────────────────────────


_BAR_COUNT = 32
_BAR_GAP_PX = 2  # logical pixels between bars
# Asymmetric exponential smoothing per spec §4. Attack > release so a
# kick reads as a jab and bars don't strobe on release.
_ATTACK_ALPHA = 0.35
_RELEASE_ALPHA = 0.12
# Idle baseline — bars never fall below this so the surface always
# reads as "alive, listening" rather than crashed.
_IDLE_BASELINE = 0.02
_MIN_BAR_HEIGHT_PX = 2
_PLACEHOLDER_ICON_PX = 48


class VisualizerWidget(QWidget):
    """Spectrum-bar widget for the NP page's left pane.

    All sizing is logical-pixel — Qt's PassThrough rounding handles
    fractional / Retina scales. No pixmap cache (repaint every signal),
    no internal timer.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")
        # Parallel arrays: ``targets`` are the latest band values from
        # the FFT signal; ``displayed`` are the smoothed values we
        # actually draw. Both live at module-level constant length so
        # we never re-allocate during paint.
        self._targets: List[float] = [0.0] * _BAR_COUNT
        self._displayed: List[float] = [0.0] * _BAR_COUNT
        # Cast placeholder state — when active, paint a centred icon +
        # "Casting to <device>" line instead of bars (mpv is idle
        # locally while casting so the FFT would freeze on silence).
        self._cast_active: bool = False
        self._cast_device: str = ""

        bus = PlayerBus.get()
        # Per ``[[feedback-signal-connects-in-init]]`` connects live in
        # __init__, never in _reapply_*. Theme/dpr just trigger a
        # repaint — colours are re-read each paint via the module-level
        # ACCENT / ACCENT_DEEP constants that ``refresh_theme`` keeps
        # current.
        bus.visualizer_bands_changed.connect(self._on_bands)
        bus.theme_changed.connect(self._reapply_accent)
        bus.dpr_changed.connect(self.update)
        bus.cast_started.connect(self._on_cast_started)
        bus.cast_stopped.connect(self._on_cast_stopped)

    # ── Bus slots ────────────────────────────────────────────────────────

    @Slot(list)
    def _on_bands(self, bands: List[float]) -> None:
        """Receive a fresh 32-float band vector and advance the
        smoothing state by one step. Skip the repaint when hidden
        (still keep the smoothing state moving so a re-show doesn't
        snap a stale frame back into view; the next signal smooths
        from current state)."""
        if not bands:
            return
        # Defensive truncate/pad in case the backend ever ships a
        # different band count — we paint a fixed 32.
        if len(bands) >= _BAR_COUNT:
            self._targets = [float(v) for v in bands[:_BAR_COUNT]]
        else:
            padded = [float(v) for v in bands] + [0.0] * (_BAR_COUNT - len(bands))
            self._targets = padded
        self._advance_smoothing()
        if self.isVisible():
            self.update()

    @Slot()
    def _reapply_accent(self) -> None:
        """Theme / accent changed — just request a repaint. Colours
        are read at paint time so no cache to invalidate."""
        self.update()

    @Slot(str)
    def _on_cast_started(self, device: str) -> None:
        self._cast_active = True
        self._cast_device = device or ""
        self.update()

    @Slot()
    def _on_cast_stopped(self) -> None:
        self._cast_active = False
        self._cast_device = ""
        self.update()

    # ── Smoothing ───────────────────────────────────────────────────────

    def _advance_smoothing(self) -> None:
        """One step of asymmetric exponential smoothing on every band.
        Fast attack, slower release per spec §4. The idle baseline
        clamp lives in paint, NOT here — keeps the smoothing math
        monotone so a sequence of zeros decays cleanly to zero in
        state, then paint lifts it to 0.02."""
        for i in range(_BAR_COUNT):
            target = self._targets[i]
            current = self._displayed[i]
            if target > current:
                self._displayed[i] = current + _ATTACK_ALPHA * (target - current)
            else:
                self._displayed[i] = current + _RELEASE_ALPHA * (target - current)

    # ── Paint ───────────────────────────────────────────────────────────

    def showEvent(self, event):  # noqa: N802 — Qt naming
        """Paint immediately on show so the user sees current bands
        without a 33 ms wait for the next signal."""
        super().showEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        from modules.ui_helpers import ACCENT, ACCENT_DEEP, TEXT_DIM

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            rect = self.rect()
            w, h = rect.width(), rect.height()
            if w <= 0 or h <= 0:
                return

            # Cast-active branch — static placeholder, no bars (spec §8).
            if self._cast_active:
                self._paint_cast_placeholder(painter, w, h, TEXT_DIM)
                return

            # Bar geometry. Width math: full widget width minus the
            # (N-1) inter-bar gaps, integer-divided by N. Leftover
            # pixels go into the left margin so bars stay flush with
            # the right edge on resize (spec §3).
            total_gap = (_BAR_COUNT - 1) * _BAR_GAP_PX
            usable = w - total_gap
            bar_w = max(1, usable // _BAR_COUNT)
            leftover = w - (bar_w * _BAR_COUNT + total_gap)
            left_margin = max(0, leftover)

            # Single gradient per paint pass — spans the widget's full
            # vertical range so tall bars show full ACCENT→ACCENT_DEEP
            # and short bars only the deeper bottom slice. Reads as
            # "energy = brightness" (spec §5).
            gradient = QLinearGradient(0.0, float(h), 0.0, 0.0)
            gradient.setColorAt(0.0, QColor(ACCENT_DEEP))
            gradient.setColorAt(1.0, QColor(ACCENT))

            x = left_margin
            for i in range(_BAR_COUNT):
                # Idle baseline clamp at draw time — never feed back
                # into smoothing state (spec §6).
                val = self._displayed[i]
                if val < _IDLE_BASELINE:
                    val = _IDLE_BASELINE
                bar_h = int(round(val * h))
                if bar_h < _MIN_BAR_HEIGHT_PX:
                    bar_h = _MIN_BAR_HEIGHT_PX
                if bar_h > h:
                    bar_h = h
                y = h - bar_h
                painter.fillRect(x, y, bar_w, bar_h, gradient)
                x += bar_w + _BAR_GAP_PX
        finally:
            painter.end()

    def _paint_cast_placeholder(
        self, painter: QPainter, w: int, h: int, color: str
    ) -> None:
        """Centred cast icon + 'Casting to <device>' caption.
        Static — no spinner, no marquee. This state is paused, not
        busy (spec §8)."""
        from modules.icons import _svg_pix

        # Icon — fetched at the configured logical size. _svg_pix
        # honours screen DPR internally so the rendered pixmap is
        # crisp on Retina without us re-doing the math.
        try:
            pix = _svg_pix("cast", color, _PLACEHOLDER_ICON_PX)
        except Exception:
            pix = None

        # Caption font + measurements first so we can vertically
        # centre the icon + caption block as one unit.
        from modules.design_tokens import TYPE_CAPTION

        caption = (
            f"Casting to {self._cast_device}" if self._cast_device else "Casting"
        )
        font = QFont(painter.font())
        font.setPixelSize(TYPE_CAPTION.size_px)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_w = fm.horizontalAdvance(caption)
        text_h = fm.height()

        gap = 8
        icon_h = _PLACEHOLDER_ICON_PX if pix is not None and not pix.isNull() else 0
        block_h = icon_h + (gap if icon_h else 0) + text_h
        top_y = max(0, (h - block_h) // 2)

        if pix is not None and not pix.isNull():
            icon_x = (w - _PLACEHOLDER_ICON_PX) // 2
            painter.drawPixmap(icon_x, top_y, pix)
            text_top = top_y + icon_h + gap
        else:
            text_top = top_y

        painter.setPen(QColor(color))
        text_x = (w - text_w) // 2
        # Qt's drawText with (x, baseline_y, str). Baseline = top + ascent.
        painter.drawText(text_x, text_top + fm.ascent(), caption)

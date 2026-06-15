"""Spectrum-bar visualizer widget — the third NP-page left-pane mode.

Implements the spec at ``docs/research/visualizer_rendering.md`` —
32 grounded log-spaced bars driven by the FFT pipeline in
``jellytoast.visualizer``, asymmetric exponential smoothing, accent
gradient, slow decay to a 2 % baseline, cast-active placeholder.

The widget pulls band data from ``PlayerBus.visualizer_bands_changed``
and paints on every signal — no internal timer. The backend already
throttles to 50 Hz so painting per-signal gives a free 50 Hz repaint
with no risk of overrun.
"""

from __future__ import annotations

from typing import List

from PySide6.QtCore import QPointF, Qt, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
)
from PySide6.QtWidgets import QWidget

from jellytoast.player_state import PlayerBus

# ── Tunables ────────────────────────────────────────────────────────────────


_BAR_COUNT = 32
# Asymmetric exponential smoothing — attack > release so a kick reads
# as a jab and the wave doesn't strobe on release. One step runs per
# emitted band frame, so these are COUPLED to the backend emit rate
# (_EMIT_INTERVAL_S). Tuned for the 20 ms / 50 Hz emit: recomputed from
# the old 33 ms values (0.35 / 0.12) to preserve the wall-clock time-
# constants — α_new = 1 − (1−α_old)^(20/33) — so attack τ ≈ 77 ms and
# release τ ≈ 258 ms stay identical while the motion gains resolution.
# Raising the emit rate WITHOUT lowering these would strobe; change both.
_ATTACK_ALPHA = 0.23
_RELEASE_ALPHA = 0.075
_PLACEHOLDER_ICON_PX = 48

# Number of control points the wave's Bezier passes through. The
# backend emits 32 log-spaced bands, but rendering a Catmull-Rom curve
# through all 32 produces ~8–10 visible wiggles that read as fidgety.
# Downsampling by block-mean (not decimation — keeps energy from each
# block's bands) tames the wiggle while preserving the envelope. 24
# (≈1.33 bands/point) keeps transients articulate — "tighter" — where
# 16 averaged adjacent pairs and blunted single-band peaks.
_WAVE_POINT_COUNT = 24
# X-axis warp exponent. The 32 log-spaced bands the backend emits cover
# 50 Hz – 16 kHz, but typical music's energy concentrates below ~4 kHz,
# which crowds the action into bands 0–18 (≈ the left ~58 % of the
# canvas with linear x). Warping x with a sub-1 power expands the low
# bands across more horizontal real estate and compresses the quiet
# high bands toward the right edge. 0.55 puts band ~12 (around 700 Hz)
# near the visual midpoint instead of band ~16 (1.7 kHz). Tuned by eye.
_X_WARP_POWER = 0.55
# Per-band amplitude weighting. Pop/rock/folk music has bass-heavy
# distribution that puts the loudest hill on the leftmost bands and
# leaves the right side flat; compensating with a linear gain ramp
# (1.0 at the bass end → 3.0 at the treble end, roughly +1.6 dB/octave
# in our log-band layout) flattens the visual response so peaks spread
# evenly across the wave's width. Values clamp back to [0, 1] after
# multiplication so loud bass doesn't go negative-magnitude after the
# pre-smoothing weighting.
_AMPLITUDE_WEIGHTS: List[float] = [
    1.0 + (i / (_BAR_COUNT - 1)) * 2.0 for i in range(_BAR_COUNT)
]
# Maximum fraction of widget height the wave can occupy. The tap decodes
# at a fixed amplitude (independent of the playback volume), so most band
# energy saturates and full-scale peaks are common — at a high cap they
# slammed the canvas top constantly. 0.51 keeps even a saturated peak
# around the canvas midpoint, leaving the upper half as breathing room so
# the wave reads as a curbed ribbon rather than a wall.
_MAX_HEIGHT_FRACTION = 0.51
# Stroke width for the wave outline. The gradient fill under the curve
# carries most of the signal; the stroke adds a crisp top edge so the
# wave reads cleanly against the page background.
_WAVE_STROKE_PX = 1.5


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
        # Pre-signal flag — flips to True on the first non-empty band
        # payload and stays True. Before flip we paint a "waiting"
        # caption instead of the 2 % baseline, because a row of equal
        # 2-px bars reads as a broken meter when the audio tap is a
        # stub (current state) or simply hasn't fired yet on a fresh
        # page load. After flip the spec idle behaviour applies — quiet
        # passages decay to the baseline and read as "alive, listening".
        self._has_ever_received_bands: bool = False

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
            raw = [float(v) for v in bands[:_BAR_COUNT]]
        else:
            raw = [float(v) for v in bands] + [0.0] * (_BAR_COUNT - len(bands))
        # Per-band amplitude weighting so bass doesn't visually dominate
        # the wave (see ``_AMPLITUDE_WEIGHTS``). Clamp into [0, 1] after
        # multiplication so the boosted treble bands can't break the
        # smoothing math's monotonicity contract.
        self._targets = [
            max(0.0, min(1.0, raw[i] * _AMPLITUDE_WEIGHTS[i]))
            for i in range(_BAR_COUNT)
        ]
        self._has_ever_received_bands = True
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
        without a 20 ms wait for the next signal."""
        super().showEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        from jellytoast.ui_helpers import ACCENT, ACCENT_DEEP, TEXT_DIM

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            w, h = rect.width(), rect.height()
            if w <= 0 or h <= 0:
                return

            # Cast-active branch — static placeholder, no wave.
            if self._cast_active:
                self._paint_cast_placeholder(painter, w, h, TEXT_DIM)
                return

            # Pre-signal branch — see __init__ docstring for the
            # _has_ever_received_bands flag rationale.
            if not self._has_ever_received_bands:
                self._paint_pre_signal_caption(painter, w, h, TEXT_DIM)
                return

            self._paint_wave(painter, w, h, ACCENT, ACCENT_DEEP)
        finally:
            painter.end()

    # ── Wave geometry ───────────────────────────────────────────────────

    def _band_points(self, w: int, h: int) -> List[QPointF]:
        """Compute the (x, y) control points the wave passes through.

        X positions are warped with ``_X_WARP_POWER < 1`` so low bands
        spread across more horizontal real estate and high bands
        compress toward the right edge — music's energy lives below
        ~4 kHz, so the warp puts the active region at the visual
        midpoint instead of crowding it on the left third.

        Y values are spatially smoothed across each band's two
        neighbours before paint. At our 2048-sample window the FFT bin
        density is coarse below ~250 Hz (only 1 bin per band), so
        consecutive low bands hit/miss real frequency content and the
        raw wave shows sharp single-band notches — most visibly between
        bands 1 and 3 where the warp clusters the first few points.
        The [0.15, 0.70, 0.15] kernel fills those notches without
        flattening genuine peaks (a real peak surrounded by zeros still
        reads at 70 % of full height, plenty to dominate the wave) while
        keeping transients sharper than the old wider kernel. Smoothing
        happens at paint time only — the underlying ``_displayed`` state
        keeps the asymmetric exponential temporal smoothing clean.

        Idle behaviour: when ``_displayed`` decays to zero (no audio
        flowing), the wave collapses to the baseline — y = h — and the
        closed-path fill has zero area, so silence paints nothing at
        all. The pre-signal caption covers the pre-first-payload case.
        """
        n = _BAR_COUNT
        smoothed: List[float] = [0.0] * n
        for i in range(n):
            left = self._displayed[i - 1] if i > 0 else self._displayed[i]
            center = self._displayed[i]
            right = self._displayed[i + 1] if i < n - 1 else self._displayed[i]
            smoothed[i] = 0.15 * left + 0.70 * center + 0.15 * right

        # Downsample to ``_WAVE_POINT_COUNT`` control points by averaging
        # contiguous blocks of smoothed bands. Block-mean (rather than
        # decimation) keeps both bands' energy in the control value, so
        # a loud single band still pulls the wave up at its block — it
        # just shares that height with its block-neighbour.
        n_ctrl = _WAVE_POINT_COUNT
        ctrl_values: List[float] = []
        for i in range(n_ctrl):
            lo = round(i * n / n_ctrl)
            hi = round((i + 1) * n / n_ctrl)
            if hi <= lo:
                hi = lo + 1
            hi = min(hi, n)
            block = smoothed[lo:hi]
            ctrl_values.append(sum(block) / len(block))

        points: List[QPointF] = []
        denom = float(n_ctrl - 1) if n_ctrl > 1 else 1.0
        max_pixels = float(h) * _MAX_HEIGHT_FRACTION
        for i in range(n_ctrl):
            t = i / denom
            x = w * (t ** _X_WARP_POWER)
            val = ctrl_values[i]
            if val < 0.0:
                val = 0.0
            elif val > 1.0:
                val = 1.0
            # Cap the wave so even a full-scale peak lands around the canvas
            # midpoint instead of slamming the top edge — leaves the upper
            # half as headroom for the rest of the NP page to breathe.
            y = h - val * max_pixels
            points.append(QPointF(x, y))
        return points

    @staticmethod
    def _append_catmull_segments(
        path: QPainterPath, points: List[QPointF]
    ) -> None:
        """Append Catmull-Rom cubic Bezier segments through ``points``.

        ``path`` must already be positioned at ``points[0]`` (via
        ``moveTo`` or ``lineTo``). Each segment's handles are derived
        from the slope between the neighbour-pair before and after,
        scaled by 1/6 (the standard Catmull→Bezier conversion factor),
        so the curve is smooth at every control point without ringing
        past 1.0. Shared by ``_wave_path`` (closed fill) and
        ``_paint_wave`` (open stroke) so both trace the identical curve.
        """
        n = len(points)
        for i in range(n - 1):
            p0 = points[i - 1] if i > 0 else points[i]
            p1 = points[i]
            p2 = points[i + 1]
            p3 = points[i + 2] if i + 2 < n else points[i + 1]
            # Catmull-Rom to cubic Bezier control points.
            cp1 = QPointF(
                p1.x() + (p2.x() - p0.x()) / 6.0,
                p1.y() + (p2.y() - p0.y()) / 6.0,
            )
            cp2 = QPointF(
                p2.x() - (p3.x() - p1.x()) / 6.0,
                p2.y() - (p3.y() - p1.y()) / 6.0,
            )
            path.cubicTo(cp1, cp2, p2)

    @staticmethod
    def _wave_path(points: List[QPointF], w: int, h: int) -> QPainterPath:
        """Build a closed filled path through ``points``.

        Uses a Catmull-Rom-style cubic Bezier so the curve is smooth at
        every control point without ringing past 1.0 — each segment's
        handles are derived from the slope between the neighbour-pair
        before and after, scaled by 1/6 (the standard Catmull→Bezier
        conversion factor).

        The path enters at the bottom-left (x of point 0, y = h), rises
        to point 0, Bezier-glides through all points, drops back to the
        bottom-right (x of last point, y = h), and closes along the
        baseline.
        """
        path = QPainterPath()
        if not points:
            return path

        first = points[0]
        last = points[-1]
        # Start at the baseline directly under point 0 so the closed
        # fill has a clean left edge instead of a diagonal from (0, h)
        # up to (first.x, first.y).
        path.moveTo(first.x(), float(h))
        path.lineTo(first)

        VisualizerWidget._append_catmull_segments(path, points)

        # Close the path along the baseline.
        path.lineTo(last.x(), float(h))
        path.closeSubpath()
        return path

    def _paint_wave(
        self, painter: QPainter, w: int, h: int, accent: str, accent_deep: str
    ) -> None:
        """Render the band data as a smooth gradient-filled wave."""
        points = self._band_points(w, h)
        if not points:
            return
        # Skip paint entirely when the wave has collapsed to the
        # baseline — otherwise the stroke's anti-aliased round cap can
        # bleed a sliver of accent into the bottom row even though the
        # closed-path fill has zero area. "Silence = no purple at all"
        # per the user-facing contract; the pre-signal caption handles
        # the never-received case separately.
        if all(p.y() >= float(h) for p in points):
            return

        path = self._wave_path(points, w, h)

        # Vertical gradient — bright accent at the top of the canvas,
        # deeper accent at the baseline. The wave's peaks paint with
        # the bright top of the gradient; troughs only reach the deeper
        # bottom slice. Reads as "energy = brightness."
        gradient = QLinearGradient(0.0, 0.0, 0.0, float(h))
        # Lift the very top of the gradient a touch above raw ACCENT so peak
        # crests read as a brighter glow without a real glow pass. Cap the
        # value lift at ~18% and keep saturation high (0.95x) so the accent
        # identity never washes toward white. Derived from the live ACCENT
        # each paint, so it stays theme-aware.
        _c = QColor(accent)
        _hue, _sat, _val, _alpha = _c.getHsvF()
        top_color = QColor.fromHsvF(
            _hue, min(1.0, _sat * 0.95), min(1.0, _val * 1.18), _alpha
        )
        bottom_color = QColor(accent_deep)
        gradient.setColorAt(0.0, top_color)
        # Crest highlight tucked just below the top edge so the lift only
        # touches full-scale peaks, leaving the body below identical.
        gradient.setColorAt(0.12, QColor(accent))
        gradient.setColorAt(1.0, bottom_color)
        painter.fillPath(path, gradient)

        # Stroke just the upper outline (not the baseline) so the wave
        # has a crisp top edge against the page background. We rebuild
        # the open polyline path here — drawing the closed path's
        # outline would also stroke the baseline.
        outline = QPainterPath()
        outline.moveTo(points[0])
        self._append_catmull_segments(outline, points)
        pen = painter.pen()
        pen.setColor(top_color)
        pen.setWidthF(_WAVE_STROKE_PX)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.strokePath(outline, pen)

    def _paint_pre_signal_caption(
        self, painter: QPainter, w: int, h: int, color: str
    ) -> None:
        """Centred dim caption shown before any FFT band payload has
        arrived. Matches the cast-placeholder typography so the widget
        has a single visual idiom for 'paused / not-yet-active'."""
        self._paint_caption(
            painter, w, h, color, "Visualizer · waiting for audio signal"
        )

    def _paint_caption(
        self, painter: QPainter, w: int, h: int, color: str, caption: str
    ) -> None:
        """Shared centred-dim-caption painter (pre-signal + ALSA-direct
        states use the same idiom)."""
        from jellytoast.design_tokens import TYPE_CAPTION

        font = QFont(painter.font())
        font.setPixelSize(TYPE_CAPTION.size_px)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_w = fm.horizontalAdvance(caption)
        painter.setPen(QColor(color))
        text_x = max(0, (w - text_w) // 2)
        text_y = (h // 2) + (fm.ascent() // 2)
        painter.drawText(text_x, text_y, caption)

    def _paint_cast_placeholder(
        self, painter: QPainter, w: int, h: int, color: str
    ) -> None:
        """Centred cast icon + 'Casting to <device>' caption.
        Static — no spinner, no marquee. This state is paused, not
        busy (spec §8)."""
        from jellytoast.icons import _svg_pix

        # Icon — fetched at the configured logical size. _svg_pix
        # honours screen DPR internally so the rendered pixmap is
        # crisp on Retina without us re-doing the math.
        try:
            pix = _svg_pix("cast", color, _PLACEHOLDER_ICON_PX)
        except Exception:
            pix = None

        # Caption font + measurements first so we can vertically
        # centre the icon + caption block as one unit.
        from jellytoast.design_tokens import TYPE_CAPTION

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

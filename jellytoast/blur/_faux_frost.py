"""Faux frosted-glass backdrop — the no-real-blur fallback.

When a frosted theme can't get genuine compositor blur, the body would
otherwise be a flat near-opaque panel (``fallback_body_alpha``). The cases:

  * GNOME / Wayland — no "blur-behind" Wayland protocol exists, and Mutter
    doesn't implement KDE's private one.
  * Windows 10 / transparency-disabled — no Mica backdrop.
  * KDE with the Blur desktop effect switched off.

Instead of a flat rectangle we paint a *self-contained dark frosted texture*:
a few soft lighter blooms over the theme's body colour, melted together by a
smooth upscale (cheap blur) and finished with a faint film grain so it reads as
frosted/diffused glass rather than dead paint. Neutral (no hue tint), with zero
compositor support and — unlike a blurred-album-art backdrop — no per-track
churn and a consistent look across tracks. Cached per (size, base colour);
rebuilt only on resize or theme change. Shared by the main window and the mini
player so both fall back identically (see ``blur.status`` / ``body_color_tuple``)."""
from __future__ import annotations

import random
from typing import Optional, Tuple

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
    QRadialGradient,
)


class FauxFrost:
    """Generates and caches a dark frosted-glass texture sized to a surface.
    Cheap to keep one per frosted surface."""

    # Build the blooms at 1/_SCALE then smooth-upscale — a cheap blur that
    # melts them into soft cloud and erases banding. Grain is added AFTER the
    # upscale (full-res) so it stays crisp.
    _SCALE = 6
    # Soft blooms over the base: (cx, cy, radius_frac, lighten%, alpha).
    # Bigger radii + higher alpha read as more pronounced, lighter frost.
    _BLOOMS = (
        (0.20, 0.14, 1.05, 134, 66),
        (0.84, 0.84, 1.18, 120, 56),
        (0.58, 0.46, 0.72, 142, 40),
        (0.92, 0.22, 0.55, 126, 30),
    )
    # Faint monochrome film grain, tiled. Fixed seed → deterministic so the
    # cached texture is stable across rebuilds. Sparse + low-alpha = fine film
    # grain rather than a haze.
    _GRAIN_TILE = 64
    _GRAIN_ALPHA_MAX = 9
    _GRAIN_DENSITY = 0.20  # fraction of pixels that get any speck
    _GRAIN_SEED = 0xF0057
    _grain: Optional[QPixmap] = None

    def __init__(self) -> None:
        self._cache: Optional[QPixmap] = None
        self._key: Optional[Tuple[int, int, int]] = None

    @classmethod
    def _grain_tile(cls) -> QPixmap:
        if cls._grain is not None:
            return cls._grain
        n = cls._GRAIN_TILE
        img = QImage(n, n, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        rnd = random.Random(cls._GRAIN_SEED)
        for y in range(n):
            for x in range(n):
                if rnd.random() > cls._GRAIN_DENSITY:
                    continue
                a = rnd.randint(2, cls._GRAIN_ALPHA_MAX)
                v = rnd.randint(185, 235)
                img.setPixelColor(x, y, QColor(v, v, v, a))
        cls._grain = QPixmap.fromImage(img)
        return cls._grain

    def _ensure(self, size: QSize, base: QColor) -> Optional[QPixmap]:
        if size.width() <= 0 or size.height() <= 0:
            return None
        key = (size.width(), size.height(), base.rgba())
        if self._cache is not None and self._key == key:
            return self._cache
        sw = max(2, size.width() // self._SCALE)
        sh = max(2, size.height() // self._SCALE)
        small = QPixmap(sw, sh)
        small.fill(base)  # near-opaque dark base — keeps the body's own alpha
        p = QPainter(small)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        reach = float(max(sw, sh))
        for cx, cy, rfrac, lighten, alpha in self._BLOOMS:
            grad = QRadialGradient(QPointF(cx * sw, cy * sh), rfrac * reach)
            center = base.lighter(lighten)
            center.setAlpha(alpha)
            edge = QColor(center)
            edge.setAlpha(0)
            grad.setColorAt(0.0, center)
            grad.setColorAt(1.0, edge)
            p.fillRect(small.rect(), grad)
        p.end()
        big = small.scaled(
            size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Film grain on top (full-res, tiled) for the frosted diffusion feel.
        gp = QPainter(big)
        gp.fillRect(big.rect(), QBrush(self._grain_tile()))
        gp.end()
        self._cache = big
        self._key = key
        return big

    def paint(self, painter: QPainter, rect: QRect, base: QColor, radius: int = 0) -> bool:
        """Paint the dark frost texture to fill ``rect`` (clipped to a rounded
        rect of ``radius`` when > 0), built from ``base`` (the body colour).
        Saves/restores painter state. Returns True if it painted."""
        pm = self._ensure(rect.size(), base)
        if pm is None:
            return False
        painter.save()
        try:
            if radius > 0:
                path = QPainterPath()
                path.addRoundedRect(QRectF(rect), radius, radius)
                painter.setClipPath(path)
            painter.drawPixmap(rect.topLeft(), pm)
        finally:
            painter.restore()
        return True

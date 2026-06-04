"""No-op blur backend — non-Linux platforms (Windows, macOS).

Blur-behind is requested through KDE's KWindowSystem, which only
exists on Linux. Elsewhere the window simply renders without blur —
the theme's body opacity is the intended no-blur baseline. macOS has
its own vibrancy API and Windows has acrylic/mica; wiring those is a
separate per-platform job, deliberately not attempted here.
"""

from __future__ import annotations


def is_supported() -> bool:
    return False


def apply(widget, enabled: bool, corner_radius: int = 0) -> bool:
    return False


def probe():
    """No backend can request blur here → the frosted body paints its
    near-opaque fallback (never see-through)."""
    from modules.blur import BlurStatus

    return BlurStatus.UNSUPPORTED

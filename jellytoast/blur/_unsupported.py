"""No-op blur backend — the fallback when no platform backend applies.

This is the final ``else`` in :mod:`jellytoast.blur` dispatch: it is
selected only for platforms with no real blur backend of their own.
Windows (``_dwm``, acrylic) and macOS (``_macos``) each have their own
backend module now, so the boxes that land here are Linux machines
without KWindowSystem, or a compositor with no app-controllable blur
protocol (Hyprland / Wayfire / sway / GNOME). The window simply renders
without blur — the theme's near-opaque body is the intended no-blur
baseline.
"""

from __future__ import annotations


def is_supported() -> bool:
    return False


def apply(
    widget, enabled: bool, corner_radius: int = 0, dark: bool = True
) -> bool:
    return False


def probe():
    """No backend can request blur here → the frosted body paints its
    near-opaque fallback (never see-through)."""
    from jellytoast.blur import BlurStatus

    return BlurStatus.UNSUPPORTED


def reason(status):
    return "this platform has no window-blur support — using a near-opaque body"

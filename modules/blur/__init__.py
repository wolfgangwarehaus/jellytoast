"""Window "blur behind" — the frosted-glass effect for translucent
surfaces. This is what visually separates Frosted mode (blurred glass)
from Transparent mode (clear glass).

Public API:
    is_supported() -> bool          # backend can request blur
    apply(widget, enabled) -> bool  # enable/disable blur behind widget

Backend (Linux): KDE's KWindowSystem — `KWindowEffects::enableBlurBehind`
called via ctypes (no PySide6 binding exists for it). KWindowSystem
speaks `ext-background-effect-v1` (the freedesktop standard) where the
compositor offers it and falls back to the legacy `org_kde_kwin_blur` —
so this covers KWin, and also niri / COSMIC where KWindowSystem is
installed. It re-applies blur itself when the surface is recreated, so
callers only call once per show / theme change.

Everywhere else (Windows, macOS, or a Linux box without KWindowSystem,
or a compositor with no blur protocol — Hyprland/Wayfire/sway/GNOME):
the unsupported backend no-ops. The window still renders fine — the
theme's body opacity is the no-blur baseline. On compositors that blur
via user config rather than a protocol (Hyprland, Wayfire, SwayFX), the
user can target jellytoast in their own window rules: our Wayland
app_id is the stable string "jellytoast" (set via setDesktopFileName).
"""

from __future__ import annotations

import sys

if sys.platform.startswith("linux"):
    from modules.blur import _kwin as _backend
else:
    from modules.blur import _unsupported as _backend


def is_supported() -> bool:
    """True if the backend can actually request blur (KWindowSystem
    present). A True here doesn't guarantee the *compositor* will blur
    — that's still up to KWin / niri / COSMIC vs Hyprland / GNOME."""
    return _backend.is_supported()


def apply(widget, enabled: bool, corner_radius: int = 0) -> bool:
    """Enable (``enabled=True``) or remove (``False``) compositor blur
    behind ``widget``'s window. ``widget`` is a QWidget; its QWindow
    must already exist (call after ``show()``).

    ``corner_radius``: when > 0 the blur region is shaped to a rounded
    rectangle of that radius matching the widget's current size — pass
    this for frameless rounded windows (mini player, settings dialog)
    so the blur doesn't bleed into the transparent corners. Blurring
    the full bounding rectangle there leaves the corner slivers
    smearing as thin lines while the window is dragged. ``0`` blurs
    the whole window rectangle — correct for server-side-decorated
    windows, where KWin clips to the decoration shape itself.

    Returns True if the request was issued, False on any unsupported /
    not-yet-shown case. Never raises."""
    return _backend.apply(widget, enabled, corner_radius)

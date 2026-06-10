"""No-op drag-repaint backend for everything that isn't KDE Wayland.

The stale-blur drag artifact is a KWin/NVIDIA-specific compositor bug,
and the fix — a KWin scripted effect — only has anywhere to live on
KDE. On Windows, macOS, X11, and non-KDE Wayland (GNOME, Sway, …) there
is nothing to install and nothing to fix, so every call no-ops.
"""

from __future__ import annotations

from jellytoast.platform_compat import (
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
    is_kde_desktop,
    will_be_wayland,
)


def is_supported() -> bool:
    return False


def install() -> bool:
    return False


def uninstall() -> bool:
    return False


def diagnose() -> dict:
    return {
        "backend": "unsupported",
        "is_supported": False,
        "is_linux": IS_LINUX,
        "is_windows": IS_WINDOWS,
        "is_macos": IS_MACOS,
        "is_kde": is_kde_desktop(),
        "is_wayland": will_be_wayland(),
    }

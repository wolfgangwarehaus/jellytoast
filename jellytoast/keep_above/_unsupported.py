"""No-op keep-above backend for platforms that don't need (or can't
host) a compositor-side rule. Used on:
  • Windows / macOS / X11 — Qt.WindowStaysOnTopHint works directly,
    so the mini-player setup already handles always-on-top.
  • Non-KDE Wayland (GNOME, Sway, …) — the protocol forbids it and no
    compositor we know of exposes a hook we could use.

The `is_supported() -> False` return is what the settings UI reads to
hide the toggle, so the user never sees a dead control.
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


def install_mini_player_rule() -> bool:
    return False


def remove_mini_player_rule() -> bool:
    return False


def install_noborder_rules() -> bool:
    return False


def remove_noborder_rules() -> bool:
    return False


def install_main_window_noborder() -> bool:
    return False


def remove_main_window_noborder() -> bool:
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

"""Mini-player "always on top" workaround layer. Some platforms honour
Qt.WindowStaysOnTopHint directly (Windows, macOS, X11); others (notably
KDE Wayland) need a compositor-side rule because xdg-shell forbids
client-controlled stacking. This package centralises that gap.

Public API:
    is_supported() -> bool         # backend can install/remove a rule
    install_mini_player_rule()     # idempotent install; True on success
    remove_mini_player_rule()      # idempotent remove; True if removed
    diagnose() -> dict             # backend-specific debug snapshot
    MINI_PLAYER_WINDOW_TITLE       # title that backends scope-match on

Linux + KDE + Wayland: writes a "keep above" rule into kwinrulesrc and
asks KWin to reload (`_kwin` backend).

Anywhere else (X11, Windows, macOS, GNOME-Wayland, Sway, …): the
unsupported backend returns False from is_supported and no-ops every
call. The mini-player's WindowStaysOnTopHint flag does the work on
those platforms; no rule needed.
"""

from __future__ import annotations

from modules.platform_compat import is_kde_wayland


# Title set on the FloatingMiniPlayer so backends can scope-match it.
# Anyone editing one must update mini_player.setWindowTitle to match.
MINI_PLAYER_WINDOW_TITLE = "jellytoast Mini Player"


# KDE Wayland is the only environment where we need a compositor-side
# workaround today. Choose the backend at import time; the gate is
# stable for the life of the process.
if is_kde_wayland():
    from modules.keep_above import _kwin as _backend
else:
    from modules.keep_above import _unsupported as _backend


def is_supported() -> bool:
    return _backend.is_supported()


def install_mini_player_rule() -> bool:
    return _backend.install_mini_player_rule()


def remove_mini_player_rule() -> bool:
    return _backend.remove_mini_player_rule()


def diagnose() -> dict:
    return _backend.diagnose()

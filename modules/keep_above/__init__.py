"""KWin window-rule workarounds for jellytoast's frameless surfaces.

Two distinct gaps this package fills on KDE Wayland, both via rules
written into kwinrulesrc:

1. **Keep-above.** Some platforms honour Qt.WindowStaysOnTopHint
   directly (Windows, macOS, X11); KDE Wayland doesn't — xdg-shell
   forbids client-controlled stacking — so the mini player needs a
   compositor-side "keep above" rule.

2. **No-border.** The mini player and settings dialog are server-side-
   *decorated* on KDE Wayland (NOT Qt.FramelessWindowHint), because
   KWin's blur effect drops blur for *undecorated* windows while they
   move — a decorated window keeps its blur through a drag. A KWin
   `noborder` Force rule strips the visible chrome so they still look
   frameless. (Off KDE Wayland those windows stay plain frameless and
   need no rule.)

Public API:
    is_supported() -> bool         # backend can install/remove a rule
    install_mini_player_rule()     # idempotent keep-above install
    remove_mini_player_rule()      # idempotent keep-above remove
    install_noborder_rules()       # idempotent no-border install
    remove_noborder_rules()        # idempotent no-border remove
    diagnose() -> dict             # backend-specific debug snapshot
    MINI_PLAYER_WINDOW_TITLE       # titles backends scope-match on
    SETTINGS_WINDOW_TITLE

Linux + KDE + Wayland: the `_kwin` backend writes rules into
kwinrulesrc and asks KWin to reload. Anywhere else (X11, Windows,
macOS, GNOME-Wayland, Sway, …): the unsupported backend no-ops every
call — those windows stay frameless and WindowStaysOnTopHint works.
"""

from __future__ import annotations

from modules.platform_compat import is_kde_wayland

# Window titles the KWin-rule backend scope-matches on. Anyone editing
# one must update the matching setWindowTitle call:
# MINI_PLAYER_WINDOW_TITLE → mini_player.py, SETTINGS_WINDOW_TITLE →
# settings_dialog.py, CAST_DIALOG_WINDOW_TITLE → now_playing_bar.py,
# MAIN_WINDOW_TITLE → jellytoast.py. The main window's rule uses an
# *exact* title match (see _kwin.py), so its title must stay exactly
# "jellytoast" — don't append track info to it.
MINI_PLAYER_WINDOW_TITLE = "jellytoast Mini Player"
SETTINGS_WINDOW_TITLE = "jellytoast Settings"
CAST_DIALOG_WINDOW_TITLE = "jellytoast Cast"
SMART_PLAYLIST_EDITOR_WINDOW_TITLE = "jellytoast Smart Playlist Editor"
ABOUT_DIALOG_WINDOW_TITLE = "jellytoast About"
MAIN_WINDOW_TITLE = "jellytoast"


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


def install_noborder_rules() -> bool:
    return _backend.install_noborder_rules()


def remove_noborder_rules() -> bool:
    return _backend.remove_noborder_rules()


def install_main_window_noborder() -> bool:
    return _backend.install_main_window_noborder()


def remove_main_window_noborder() -> bool:
    return _backend.remove_main_window_noborder()


def diagnose() -> dict:
    return _backend.diagnose()

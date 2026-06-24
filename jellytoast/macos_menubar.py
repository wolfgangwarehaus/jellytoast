"""macOS global menu bar + Dock menu + Dock-reopen — native-app conventions.

macOS shows a single global menu bar (App / File / Edit / View / Window / Help).
A Qt app with no ``QMenuBar`` presents as a menu-less window — the #1 "this is a
half-finished port" tell. This module builds that bar (Qt relocates it to the
global menu area and maps About/Settings/Quit into the app menu by QAction
*menu role*), plus the Dock menu transport controls and the Dock-click reopen
behaviour. macOS-only; imported lazily from ``app.main()`` behind ``IS_MACOS``.

Pure PySide6 — no pyobjc. Everything here is best-effort and never raises into
boot.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QObject, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import QApplication, QMenu, QMenuBar, QMessageBox

logger = logging.getLogger(__name__)

_HELP_URL = "https://wolfgangwarehaus.com/jellytoast"


def set_app_name(name: str = "jellytoast") -> None:
    """Force the macOS application-menu name. A from-source ``python -m
    jellytoast`` run shows "Python" in the menu bar (no .app bundle → the
    process name); the frozen ``.app`` already gets "jellytoast" from its
    ``CFBundleName``. Overwrite the running bundle's ``CFBundleName`` via
    pyobjc so the app menu reads "jellytoast" either way. MUST run before
    ``QApplication`` builds the native menu. Best-effort; never raises."""
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        if bundle is None:
            return
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
    except Exception:
        pass


def install(window) -> None:
    """Build the global menu bar + Dock menu + Dock-reopen. Call once, after
    the window + PlayerBus exist. Never raises."""
    try:
        _install_menubar(window)
        _install_dock_menu(window)
        _install_dock_reopen(window)
        logger.info("macOS menu bar + Dock menu installed")
    except Exception as e:  # pragma: no cover — macOS-only
        logger.info("macOS menu bar setup failed: %s", e)


# ── menu bar ────────────────────────────────────────────────────────────


def _install_menubar(window):
    mb = QMenuBar()
    window.setMenuBar(mb)  # QMainWindow → relocated to the global menu bar on Mac
    window._macos_menubar = mb  # keep a strong ref

    SK = QKeySequence.StandardKey
    MR = QAction.MenuRole

    # App-menu items — Qt RELOCATES these into the native (bold, app-named)
    # application menu by their menu ROLE, no matter which menu they're
    # attached to. We hang them on File (they vanish from File on relocation)
    # rather than create a dedicated menu, which would be left empty + show a
    # redundant app-named menu in the bar.
    file_menu = mb.addMenu("File")
    _act(file_menu, window, "About jellytoast", role=MR.AboutRole, slot=lambda: _about(window))
    _act(file_menu, window, "Settings…", role=MR.PreferencesRole, key=SK.Preferences,
         slot=window._open_settings)
    _act(file_menu, window, "Quit jellytoast", role=MR.QuitRole, slot=lambda: _quit(window))
    _act(file_menu, window, "Close Window", key=SK.Close,
         slot=lambda: (QApplication.activeWindow() or window).close())

    # Edit — present so system text shortcuts + the Services menu work; each
    # item dispatches to the focused widget's matching method.
    edit_menu = mb.addMenu("Edit")
    for label, key, meth in (
        ("Undo", SK.Undo, "undo"),
        ("Redo", SK.Redo, "redo"),
        (None, None, None),
        ("Cut", SK.Cut, "cut"),
        ("Copy", SK.Copy, "copy"),
        ("Paste", SK.Paste, "paste"),
        ("Select All", SK.SelectAll, "selectAll"),
    ):
        if label is None:
            edit_menu.addSeparator()
            continue
        _act(edit_menu, window, label, key=key,
             slot=lambda _=False, m=meth: _dispatch_edit(m))

    # View
    view_menu = mb.addMenu("View")
    _act(view_menu, window, "Enter Full Screen", key=SK.FullScreen,
         slot=lambda: _toggle_fullscreen(window))

    # Window
    win_menu = mb.addMenu("Window")
    _act(win_menu, window, "Minimize", key="Ctrl+M",
         slot=lambda: (QApplication.activeWindow() or window).showMinimized())
    _act(win_menu, window, "Zoom", slot=lambda: _zoom(window))
    win_menu.addSeparator()
    _act(win_menu, window, "Mini Player", slot=lambda: _bus().show_mini_player.emit())

    # Help
    help_menu = mb.addMenu("Help")
    _act(help_menu, window, "jellytoast Help",
         slot=lambda: QDesktopServices.openUrl(QUrl(_HELP_URL)))


def _act(menu, parent, text, *, role=None, key=None, slot=None):
    a = QAction(text, parent)
    # Always set a role explicitly: default to NoRole so Qt's macOS text
    # heuristic can't silently relocate a non-app item (e.g. anything that
    # looks like "settings"/"about"/"quit") into the application menu.
    a.setMenuRole(role if role is not None else QAction.MenuRole.NoRole)
    if key is not None:
        a.setShortcut(QKeySequence(key))
    if slot is not None:
        a.triggered.connect(slot)
    menu.addAction(a)
    return a


# ── Dock menu (transport controls; macOS-only Qt API, no pyobjc) ─────────


def _install_dock_menu(window):
    bus = _bus()
    menu = QMenu()
    window._macos_dock_menu = menu  # keep a strong ref
    _act(menu, window, "Play / Pause", slot=lambda: bus.pause_toggled.emit())
    _act(menu, window, "Next", slot=lambda: bus.next_track.emit())
    _act(menu, window, "Previous", slot=lambda: bus.prev_track.emit())
    menu.addSeparator()
    _act(menu, window, "Stop", slot=lambda: bus.stop_requested.emit())
    menu.setAsDockMenu()


# ── Dock-click reopen (boot-safe) ───────────────────────────────────────


class _DockReopenFilter(QObject):
    """Re-show the main window when the app is activated with no window
    visible (Dock click). Boot-safe: only acts AFTER it has observed the
    window visible at least once, so it never fights the boot-auth reveal."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._shown_once = False

    def eventFilter(self, obj, event):
        w = self._window
        try:
            if w.isVisible():
                self._shown_once = True
            elif (
                self._shown_once
                and event.type() == QEvent.Type.ApplicationStateChange
                and QApplication.applicationState() == Qt.ApplicationState.ApplicationActive
            ):
                w.show()
                w.raise_()
                w.activateWindow()
        except Exception:  # pragma: no cover — macOS-only
            pass
        return False


def _install_dock_reopen(window):
    f = _DockReopenFilter(window)
    window._macos_dock_reopen = f
    QApplication.instance().installEventFilter(f)


# ── helpers ─────────────────────────────────────────────────────────────


def _bus():
    from jellytoast.player_state import PlayerBus

    return PlayerBus.get()


def _about(window):
    ver = ""
    try:
        from importlib.metadata import version

        ver = version("jellytoast")
    except Exception:
        pass
    QMessageBox.about(
        window,
        "jellytoast",
        "<b>jellytoast</b>"
        + (f"<br>Version {ver}" if ver else "")
        + "<br>A native music client for Jellyfin &amp; Subsonic."
        + "<br><br>© 2026 William August Mueller",
    )


def _quit(window):
    # The macOS App-menu / Cmd-Q Quit is a HARD quit (not hide-to-tray): set
    # the flag the closeEvent honours, then quit the app.
    window._quitting = True
    QApplication.instance().quit()


def _dispatch_edit(method: str):
    w = QApplication.focusWidget()
    fn = getattr(w, method, None)
    if callable(fn):
        fn()


def _toggle_fullscreen(window):
    w = QApplication.activeWindow() or window
    (w.showNormal if w.isFullScreen() else w.showFullScreen)()


def _zoom(window):
    w = QApplication.activeWindow() or window
    (w.showNormal if w.isMaximized() else w.showMaximized)()

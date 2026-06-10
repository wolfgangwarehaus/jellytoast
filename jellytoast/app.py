"""
jellytoast — fully-native Linux desktop Jellyfin client.

Browse / search / suggestions / login / account: native PySide6 surfaces.
Playback engine: mpv via the existing PlayerBus.

The Jellyfin Web embed (QWebEngine) was retired once every user-visible
surface had a native replacement. The REST client (jellytoast/jellyfin_api.py)
talks to the server directly; native auth (jellytoast/login_view.py) calls
api.authenticate. No Chromium runtime, no JF Web shim, no URL interceptor.
"""

import logging
import os
import signal
import sys
from pathlib import Path

# Route diagnostics through stdlib logging. JT_LOG_LEVEL overrides (DEBUG /
# INFO / WARNING / ERROR); default INFO keeps the visibility we had from
# the pre-sweep print() calls. Format mirrors the old `[jellytoast]` tag
# via the logger name.
logging.basicConfig(
    level=os.environ.get("JT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jellytoast")

# libmpv requires LC_NUMERIC=C; Qt's setlocale() undoes Python-side fixes.
# Setting it before any libmpv / Qt import is enough — libmpv reads it
# lazily on first use. (We used to os.execve here for the same effect,
# but that doesn't exist on Windows; mutating os.environ is portable
# and works for our case because nothing has loaded mpv yet.)
os.environ.pop("LC_ALL", None)
os.environ["LC_NUMERIC"] = "C"
os.environ.setdefault("LANG", "C.UTF-8")

# Silence the cosmetic Qt portal warning ("Could not register app ID … App
# info not found for 'jellytoast'") that fires on installs without a .desktop
# file (pipx, run-from-source). Appended so it doesn't clobber a user's own
# QT_LOGGING_RULES; must be set before QApplication so Qt picks it up.
_qt_rules = os.environ.get("QT_LOGGING_RULES", "")
os.environ["QT_LOGGING_RULES"] = (
    (_qt_rules + ";" if _qt_rules else "") + "qt.qpa.services.warning=false"
)

# Identify the audio stream as "jellytoast" in the system mixer
# (KDE Plasma's Audio Volume → Applications, pavucontrol, etc.) instead
# of "python3.14". PulseAudio + PipeWire-PA compat read these
# ``PULSE_PROP_*`` env vars at client-connection time and stamp them
# into PA_PROP_APPLICATION_NAME / _APPLICATION_ICON_NAME, which is what
# the mixer shows. mpv's ``audio-client-name`` option only renames the
# per-stream media slot — not the application row above it.
# Set BEFORE any audio client (mpv, anything PulseAudio-aware) can connect.
os.environ.setdefault("PULSE_PROP_application.name", "jellytoast")
os.environ.setdefault("PULSE_PROP_application.icon_name", "jellytoast")

from jellytoast.platform_compat import (  # noqa: E402
    IS_LINUX,
    IS_WINDOWS,
    is_kde_wayland,
    will_be_wayland,
)

# Native Wayland by default — Qt picks the platform from WAYLAND_DISPLAY
# / DISPLAY in the usual way. Set QT_QPA_PLATFORM=xcb in the environment
# to fall back to XWayland (escape hatch in case a Wayland regression
# bites). All X11-only code paths (cursor env bootstrap, startup-notify
# ClientMessage, off-screen positioning, taskbar-skip via xprop) are
# gated on the platform — see jellytoast/platform_compat.py.
#
# Known Wayland gap: mini player drag/resize uses absolute QWidget.move
# / setGeometry which the protocol forbids; KWin will pick its initial
# position and drag/resize will no-op until those are switched to
# windowHandle().startSystemMove/Resize.


# Make Qt pick up the KDE cursor theme + size so the
# cursor doesn't visibly shrink when entering the jellytoast window.
# Qt reads XCURSOR_THEME / XCURSOR_SIZE; KDE stores the theme in
# ~/.config/kcminputrc and the size as Xcursor.size in xrdb. The
# requested size often doesn't exist in the theme — capitaine-cursors
# for example ships only [24, 36, 48, 60, 72, 96, 120, 144]. If we
# pass the raw xrdb value (e.g. 30) Xcursor rounds DOWN to 24, which
# looks visibly smaller than what KWin renders elsewhere on the
# desktop. We round UP to the next available size in the theme so
# the cursor matches the rest of the session.
def _theme_sizes(theme: str) -> list[int]:
    import struct

    for base in (
        "/usr/share/icons",
        os.path.expanduser("~/.icons"),
        os.path.expanduser("~/.local/share/icons"),
    ):
        path = os.path.join(base, theme, "cursors", "default")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
            _, _, _, ntoc = struct.unpack("<4sIII", data[:16])
            sizes = set()
            for i in range(ntoc):
                off = 16 + i * 12
                typ, sub, _ = struct.unpack("<III", data[off : off + 12])
                if typ == 0xFFFD0002:  # Xcursor IMAGE chunk
                    sizes.add(sub)
            return sorted(sizes)
        except Exception:
            continue
    return []


def _bootstrap_cursor_env():
    # X11/XWayland-only concept. On Wayland, KWin renders cursors
    # itself; on Windows / macOS the OS owns the cursor entirely.
    if not IS_LINUX or will_be_wayland():
        return
    try:
        theme = os.environ.get("XCURSOR_THEME", "")
        if not theme:
            from configparser import ConfigParser

            cfg = ConfigParser(strict=False)
            cfg.read(os.path.expanduser("~/.config/kcminputrc"))
            theme = cfg.get("Mouse", "cursorTheme", fallback="").strip()
            if theme:
                os.environ["XCURSOR_THEME"] = theme
        if "XCURSOR_SIZE" not in os.environ:
            import subprocess

            out = subprocess.run(
                ["xrdb", "-query"],
                capture_output=True,
                text=True,
                timeout=1,
            ).stdout
            requested = 0
            for line in out.splitlines():
                if line.startswith("Xcursor.size:"):
                    s = line.split(":", 1)[1].strip()
                    if s.isdigit():
                        requested = int(s)
                    break
            if theme and requested:
                available = _theme_sizes(theme)
                if available:
                    # Smallest size >= requested, else the largest.
                    size = next((s for s in available if s >= requested), available[-1])
                    os.environ["XCURSOR_SIZE"] = str(size)
                else:
                    os.environ["XCURSOR_SIZE"] = str(requested)
    except Exception:
        pass


_bootstrap_cursor_env()

# Source-checkout convenience: make the directory HOLDING the package
# importable regardless of launch cwd (`import jellytoast` already
# succeeded to get here, but child tooling that re-execs the interpreter
# inherits sys.path via PYTHONPATH-less spawns). Must be the package's
# PARENT — inserting the package dir itself would let its module names
# (settings, theme, …) shadow top-level imports.
_PKG_PARENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG_PARENT_DIR))

# KDE + a pip/pipx-bundled PySide6: the bundled Qt can't see the system KDE
# Qt plugins, so KWindowSystem fails to load its platform backend and frosted
# blur silently degrades to the near-opaque fallback. Point Qt at the system
# plugin dir so blur works on a plain `pipx install`. No-op on a distro/system
# PySide6, off KDE, inside Flatpak, or with JT_NO_QT_PLUGIN_FIX=1. MUST run
# before the PySide6 import below — Qt reads QT_PLUGIN_PATH at plugin-load
# time. See jellytoast/kde_qt_plugin_fix.py.
from jellytoast.kde_qt_plugin_fix import heal_qt_plugin_path  # noqa: E402

_qt_plugin_added = heal_qt_plugin_path()
if _qt_plugin_added:
    logger.debug(
        "KDE + bundled Qt: added %s to QT_PLUGIN_PATH for compositor blur",
        _qt_plugin_added,
    )

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from jellytoast.cast_dispatcher import _CastDispatcherMixin
from jellytoast.cast_manager import CastManager
from jellytoast.design_tokens import RADIUS_WINDOW
from jellytoast.jellyfin_api import get_api
from jellytoast.library_selection_controller import _LibrarySelectionMixin
from jellytoast.media_controls import MediaControlsService
from jellytoast.mini_player import FloatingMiniPlayer
from jellytoast.nav_controller import _NavMixin
from jellytoast.now_playing_bar import NowPlayingBar
from jellytoast.now_playing_page import NowPlayingPage
from jellytoast.player_backend import MPV_AVAILABLE, MpvController
from jellytoast.player_state import (
    PlayerBus,
)
from jellytoast.providers import get_provider
from jellytoast.queue_manager import QueueManager
from jellytoast.session_controller import _SessionMixin
from jellytoast.settings import get_settings
from jellytoast.settings_dialog import SettingsDialog
from jellytoast.shuffle_primer import _ShufflePrimerMixin
from jellytoast.top_bar import JtTopBar
from jellytoast.tray import TrayController
from jellytoast.ui_helpers import (
    GLOBAL_STYLE,
    make_app_icon,
)
from jellytoast.version import __version__

# Per-intent / per-track-change diagnostics (URL, queue contents,
# cooldown deltas) are gated behind this. Install/skip/error lines stay
# unconditional so a post-mortem from the terminal alone is still possible.
_SHUFFLE_DEBUG = os.environ.get("JT_SHUFFLE_DEBUG") == "1"

# Streaming-friendly opaque body. Setting JT_OPAQUE=1 in the env skips
# WA_TranslucentBackground on the main window and forces an opaque body
# fill. Diagnostic switch for Sunshine/Moonlight scroll flicker: the
# default translucent backing-store path triggers a buffer-attach-before-
# paint race (QTBUG-128029 family) that the local KMS commit hides but
# wlr-screencopy/kmsgrab can grab mid-composite, producing the white
# flash on heavy scroll. Promote to a real Settings → Display toggle if
# this confirms the diagnosis.
_OPAQUE_BODY = os.environ.get("JT_OPAQUE") == "1"


class _SpacePlayFilter(QObject):
    """Application-wide event filter for Space-to-play. A plain
    keyPressEvent override on the main window only catches Space
    when the window itself has focus — child widgets (QListView's
    selection-toggle, QScrollArea's page-down, etc.) consume Space
    before it can bubble up. Filtering at the QApplication level
    lets us intercept the press before any widget claims it, while
    still respecting text-input focus so spaces typed in the search
    or login fields go through unmolested."""

    def __init__(self, bus, parent=None):
        super().__init__(parent)
        self._bus = bus

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Space
            and not event.modifiers()
        ):
            # An open popup (QMenu, combo dropdown) needs Space to
            # activate its current item — don't swallow it.
            if QApplication.activePopupWidget() is not None:
                return False
            focused = QApplication.focusWidget()
            if not isinstance(focused, (QLineEdit, QTextEdit)):
                self._bus.pause_toggled.emit()
                return True
        return False


class _ChromeDownFilter(QObject):
    """When focus is on a chrome widget (top bar button, etc.) and
    the user presses Down, dive into the current content surface's
    first item — same intent as pressing Down on the main window
    itself, but works even when a top-bar button has focus and
    happens to consume the event before it propagates up."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._w = window

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.KeyPress:
            return False
        if event.key() != Qt.Key.Key_Down or event.modifiers():
            return False
        # When a popup (top-bar dropdown, context menu, combo) is open,
        # Down belongs to it — bailing here keeps the menu's arrow-nav
        # from leaking into focus_first_item on the content surface.
        if QApplication.activePopupWidget() is not None:
            return False
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QTextEdit)):
            return False
        cur = self._w.content_stack.currentWidget()
        if cur is None:
            return False
        # If focus is already inside the content surface, let its
        # own Down handling run (rail nav, songs list, etc.).
        if focused is not None and (focused is cur or cur.isAncestorOf(focused)):
            return False
        getter = getattr(cur, "focus_first_item", None)
        if callable(getter):
            getter()
            return True
        return False


class _MouseClearFocusFilter(QObject):
    """Any mouse press anywhere in the app drops the keyboard-mode
    flag on every view that tracks one. That clears the accent
    focus rings on grid/rail tiles — keyboard nav is the only way
    the rings should appear, so a click (even on a chrome button
    like Back or Home) is the signal to put them away."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            # Iterate only the registered keyboard-mode views, not the whole
            # widget tree, on this click hot-path.
            from jellytoast.keyboard_focus import clear_all_keyboard_mode

            clear_all_keyboard_mode()
        return False


class _SectionTabFilter(QObject):
    """Tab rotates focus between the three structural sections of the
    main window — top bar, content, bottom transport bar — instead of
    walking widget-by-widget through every focusable child. Within a
    section the user navigates by arrow keys; Tab is reserved for the
    big jumps. Shift+Tab goes the other direction.

    Text-input focus (QLineEdit / QTextEdit) is exempt so Tab keeps
    its native "indent / next field" semantics in search and login.
    """

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._w = window

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.KeyPress:
            return False
        if event.key() not in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            return False
        # An open popup needs Tab for its own item navigation.
        if QApplication.activePopupWidget() is not None:
            return False
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QTextEdit)):
            return False
        anchors = self._w._tab_anchors()
        if not anchors:
            return False
        cur_idx = self._w._current_section_index(focused, anchors)
        # Shift+Tab on most platforms arrives as Key_Backtab. Plain Tab
        # cycles forward; Shift+Tab cycles backward. Wrap at the ends
        # so the user can keep tapping in one direction indefinitely.
        forward = event.key() == Qt.Key.Key_Tab and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        )
        step = 1 if forward else -1
        target_idx = (cur_idx + step) % len(anchors)
        target = anchors[target_idx]
        if target is not None:
            target.setFocus(Qt.FocusReason.TabFocusReason)
            return True
        return False


class _ResizeEdgeFilter(QObject):
    """Edge + corner resize for the borderless main window.

    The borderless window is still server-side-decorated (KWin owns the
    real window rect — a `noborder` rule just strips the visible
    chrome), so `startSystemResize` works directly. KWin no longer
    draws resize borders, though, so this filter re-supplies the hit
    detection + cursor feedback the missing decoration would have given.

    Installed on the QApplication so it catches mouse events whatever
    child widget they land on — a content-filling window leaves no
    uncovered edge strip for the window's own handlers to see.
    """

    MARGIN = 6  # single-edge band thickness, logical px
    CORNER = 16  # corner zones are fatter — forgiving diagonal grab

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._cursor_on = False

    def eventFilter(self, obj, event):
        et = event.type()
        if et not in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress):
            return False
        win = self._window
        if (
            win.isMaximized()
            or win.isFullScreen()
            or not win.isVisible()
            or not win.isActiveWindow()
        ):
            self._clear_cursor()
            return False
        local = win.mapFromGlobal(event.globalPosition().toPoint())
        if not win.rect().contains(local):
            self._clear_cursor()
            return False
        edges = self._edges_at(local, win.width(), win.height())
        if edges == Qt.Edge(0):
            self._clear_cursor()
            return False
        # A press/hover landing on an interactive control near the edge
        # — the titlebar's window-control buttons — is a click, not a
        # resize. Let it through.
        if isinstance(win.childAt(local), QAbstractButton):
            self._clear_cursor()
            return False
        if et == QEvent.Type.MouseMove:
            win.setCursor(self._cursor_for(edges))
            self._cursor_on = True
            return False
        if event.button() == Qt.MouseButton.LeftButton:
            handle = win.windowHandle()
            if handle is not None:
                handle.startSystemResize(edges)
                return True  # consume — the child under it must not also react
        return False

    def _clear_cursor(self):
        if self._cursor_on:
            self._window.unsetCursor()
            self._cursor_on = False

    def _edges_at(self, pos, w, h):
        m, c = self.MARGIN, self.CORNER
        x, y = pos.x(), pos.y()
        near_l, near_r = x <= c, x >= w - c
        near_t, near_b = y <= c, y >= h - c
        # Corner zones first — generous c-sized boxes for the diagonal.
        if near_l and near_t:
            return Qt.Edge.LeftEdge | Qt.Edge.TopEdge
        if near_r and near_t:
            return Qt.Edge.RightEdge | Qt.Edge.TopEdge
        if near_l and near_b:
            return Qt.Edge.LeftEdge | Qt.Edge.BottomEdge
        if near_r and near_b:
            return Qt.Edge.RightEdge | Qt.Edge.BottomEdge
        # Single edges — tighter m-sized band away from the corners.
        if x <= m:
            return Qt.Edge.LeftEdge
        if x >= w - m:
            return Qt.Edge.RightEdge
        if y <= m:
            return Qt.Edge.TopEdge
        if y >= h - m:
            return Qt.Edge.BottomEdge
        return Qt.Edge(0)

    @staticmethod
    def _cursor_for(edges):
        left, right = Qt.Edge.LeftEdge, Qt.Edge.RightEdge
        top, bottom = Qt.Edge.TopEdge, Qt.Edge.BottomEdge
        if edges in (left | top, right | bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in (right | top, left | bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if edges in (left, right):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor


class JellytoastWindow(_NavMixin, _SessionMixin, _CastDispatcherMixin, _ShufflePrimerMixin, _LibrarySelectionMixin, QMainWindow):
    # Decoration is dual-mode (see `self._borderless`, set in __init__):
    #  • Borderless (default on KDE Wayland) — a KWin `noborder` rule
    #    strips the chrome; the window paints its own rounded body
    #    (paintEvent), the top bar doubles as a draggable titlebar with
    #    min/max/close, and `_ResizeEdgeFilter` supplies edge resize.
    #  • Native border (the "Use native window border" setting, and the
    #    only mode off KDE Wayland) — KWin renders the titlebar, window
    #    controls, corner radius, and resize handles itself.
    # Either way WA_TranslucentBackground stays on so the body card
    # alpha reads correctly.

    def __init__(self, server_url: str):
        super().__init__()
        self.setWindowTitle("jellytoast")
        self.setWindowIcon(QIcon(make_app_icon(64)))
        # Minimum size — width vs height have different constraints:
        # * 720 wide sits inside the now-playing bar's split-text tier
        #   (680≤bar<1080 in now_playing_bar's _BREAKPOINTS), so a free-
        #   floating window at the floor still shows title / artist /
        #   album cleanly. Going below 680 pushes the bar into hide-
        #   text mode and starts crowding the transport row; tighten
        #   the width only after the other surfaces get responsive
        #   treatment too.
        # * 440 tall is a snug "corner-snap-ish" floor: top nav bar
        #   (~56px) + a partial album row (top of the cover + the
        #   year/artist text from the row above) + the full now-
        #   playing bar (108px) + grid padding. A whole album row
        #   (cover + title + year + artist ≈ 250px logical) does NOT
        #   fit at this height — the user gets a peek, not a full row.
        #   That's intentional: the previous 520 floor enforced "one
        #   full row" but felt taller than KDE quadrant-snap, so
        #   floating-min got bumped down to match the snap aesthetic.
        # Height floor is set by the now-playing page's cover + header
        # block (COVER_SIZE 200 + ~160 of typography/CTAs + outer margins
        # 24) plus the top bar (48) and transport bar (108). Locking it
        # here means every view honors the same minimum — albums grid
        # included — so the user never overshoots into a layout that the
        # now-playing page can't render cleanly.
        self.setMinimumSize(720, 560)
        # Default size tuned to fit ~3 columns × 2 rows of the
        # albums grid (3 × 240 px tiles + margins + scrollbar +
        # alphabet sidebar; 2 rows of tile-height with the top bar
        # and now-playing strip subtracted). The previous 1280×820
        # default felt unnecessarily tall and showed extra empty
        # rows below the second row of tiles on first launch.
        self.resize(920, 720)
        # Restore previous window geometry if persisted. Done after
        # the default resize so an empty / corrupt blob falls back to
        # the 920×720 default cleanly. restoreGeometry returns False
        # on failure, in which case the explicit resize above is what
        # the user sees on first run.
        saved_geom = get_settings().window_geometry
        if saved_geom:
            self.restoreGeometry(saved_geom)
        # GLOBAL_STYLE paints `QWidget { background: BG }` which would cover
        # the body we paint in paintEvent. Override by ID for the central
        # widget and the QMainWindow itself. In opaque mode we still want
        # the ID rule's `transparent` so paintEvent's fill is what shows,
        # not a competing QSS-painted layer.
        self.setObjectName("jtMain")
        self.setStyleSheet(
            GLOBAL_STYLE
            + """
            QMainWindow#jtMain { background: transparent; }
            QWidget#jtCentral { background: transparent; }
        """
        )

        # Use KDE's server-side decorations (standard windowed mode):
        # KWin draws the titlebar + window controls + corner radius, and
        # all snap / unsnap / quadrant interactions are handled natively
        # — no more "fight Wayland" geometry heuristics. We keep
        # WA_TranslucentBackground by default so the body's card alpha
        # (from the theme palette) reads correctly inside the client area.
        # JT_OPAQUE=1 skips translucency — see the env-var comment above
        # for the streaming-flicker rationale; paintEvent uses
        # _body_qcolor below, which forces alpha=255 in opaque mode.
        # Real frosted-glass blur — the DEFAULT on Windows (frameless chrome),
        # with JT_NO_WIN_BLUR as the escape hatch. Research-verified
        # qframelesswindow recipe: DWM/accent backdrops NEVER composite behind
        # a per-pixel-alpha LAYERED window — which is exactly what
        # WA_TranslucentBackground makes — so we must NOT set it. Make the
        # window background transparent the qframelesswindow way: a styled
        # (QSS) transparent background, repainted normally each frame (NOT
        # WA_NoSystemBackground + no-paint, which never clears → ghosting). The
        # Acrylic blur itself is applied to the HWND in jellytoast/blur/_dwm.apply
        # (legacy ACCENT_ENABLE_ACRYLICBLURBEHIND). Gated to the frameless
        # chrome — native_window_border / JT_NO_WIN_CHROME opt out of both.
        self._win_blur = (
            IS_WINDOWS
            and not get_settings().native_window_border
            and not os.environ.get("JT_NO_WIN_CHROME")
            and not os.environ.get("JT_NO_WIN_BLUR")
        )
        if self._win_blur:
            from jellytoast.theme import get_active_theme as _gat0

            # Live: True only while a FROSTED theme is active (Acrylic on) —
            # then paintEvent stays transparent so the blur shows. A SOLID
            # theme disables the Acrylic, so it must paint its opaque body
            # instead (else transparent-over-nothing = black). Updated on
            # every theme swap in _refresh_body_color.
            self._win_blur_active = _gat0().blur
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.setStyleSheet(
                self.styleSheet() + "\nJellytoastWindow{background:transparent}"
            )
        else:
            self._win_blur_active = False
            if not _OPAQUE_BODY:
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        if _OPAQUE_BODY:
            logger.info(
                "JT_OPAQUE=1: skipping WA_TranslucentBackground "
                "on the main window (streaming-flicker diagnostic)."
            )
        # Build the body fill QColor. JT_OPAQUE forces alpha 255 (no
        # compositor blend for screencast to grab mid-paint). Otherwise the
        # alpha is a FUNCTION of whether real compositor blur is verified
        # behind the window — full glass (~67%) when it is, near-opaque
        # (~92%) when it isn't — so a frosted theme never renders
        # see-through. Probed once here for the first paint, then re-checked
        # once the surface is mapped (_first_blur_pass). See
        # jellytoast/blur.status() + theme.body_color_for().
        self._body_qcolor = self._resolve_body_qcolor()

        # Windows: there's no KWin to strip the native decoration, so go
        # Qt-frameless and reuse the borderless chrome (top bar = titlebar,
        # edge-resize filter, rounded self-painted body) to match the Linux
        # look. FramelessWindowHint ALSO gives Qt's Windows backend a
        # per-pixel alpha channel — which a *decorated* window never gets —
        # so WA_TranslucentBackground actually yields transparent body pixels
        # for the DWM Mica backdrop to show through (without it the body
        # paints over an opaque backbuffer = solid dark). The one flag
        # delivers both the borderless frame AND the Frosted/Mica look. Opt
        # back into the native title bar via native_window_border, or
        # JT_NO_WIN_CHROME=1 as a safety hatch.
        self._win_frameless = (
            IS_WINDOWS
            and not get_settings().native_window_border
            and not os.environ.get("JT_NO_WIN_CHROME")
        )
        if self._win_frameless:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # Borderless mode: on KDE Wayland a KWin `noborder` rule (installed
        # at boot) strips the decoration; on Windows it's the Qt-frameless
        # flag above. Either way the window draws its own rounded body +
        # blended top-bar titlebar + edge-resize zones. With native borders
        # on (or off both KDE Wayland and Windows) the compositor owns the
        # chrome and paintEvent fills a plain rect.
        self._borderless = (
            is_kde_wayland() and not get_settings().native_window_border
        ) or self._win_frameless

        # Borderless: KWin draws no resize border, so an app-level event
        # filter re-supplies edge/corner resize. Installed on the
        # QApplication (a content-filling window has no uncovered edge
        # strip the window's own handlers could see).
        if self._borderless:
            self._resize_filter = _ResizeEdgeFilter(self)
            QApplication.instance().installEventFilter(self._resize_filter)
            # The rounded blur region is a fixed-size rounded rect, so
            # it must be re-shaped after a resize. Debounced so a
            # drag-resize doesn't spam the compositor with blur calls.
            self._blur_settle = QTimer(self)
            self._blur_settle.setSingleShot(True)
            self._blur_settle.setInterval(120)
            self._blur_settle.timeout.connect(self._apply_blur)

        self.api = get_api()
        # Provider abstraction — wraps the api with a backend-agnostic
        # interface so a future Subsonic / Navidrome provider can plug
        # in without touching the host. For now both the api and the
        # provider point at the same Jellyfin instance; phase-2 work
        # will migrate the heavier call sites (browse / playback) and
        # add the SubsonicProvider alongside.
        self.provider = get_provider()
        self.bus = PlayerBus.get()
        self.cast_manager = CastManager()
        # Pre-warm cast discovery a few seconds after boot so the cast
        # dialog opens with results already loaded. Network probe runs
        # off the GUI thread per CastManager's async_io path; the
        # delay avoids piling onto the heavy first-paint workload.
        # ``discover_all_at_boot`` honors the ``cast/discovery_timing``
        # setting — a user on ``on_demand`` (default) skips this scan
        # entirely and discovery fires when the cast menu opens.
        QTimer.singleShot(4000, self.cast_manager.discover_all_at_boot)
        self.queue_mgr = QueueManager(self)

        central = QWidget()
        central.setObjectName("jtCentral")
        # MouseTracking lets the window receive move events without a button
        # held — needed for the edge-resize cursor feedback.
        central.setMouseTracking(True)
        self.setCentralWidget(central)

        # Single full-bleed layout holding the chrome (titlebar + top
        # bar + view + np bar). There is no boot-time loading overlay:
        # the window stays hidden until _do_boot_auth_check builds the
        # initial surface, so the user never sees a partial window (see
        # the addWidget below and __init__ end).
        central_stack = QVBoxLayout(central)
        central_stack.setContentsMargins(0, 0, 0, 0)
        central_stack.setSpacing(0)

        # Parent every child to its eventual container at construction
        # time. On Wayland, a parentless QWidget gets a top-level
        # surface allocated for an instant before addWidget reparents
        # it — which surfaces as small rectangles flashing in the
        # middle of the screen during boot.
        chrome = QWidget(central)
        chrome.setObjectName("jtChrome")
        chrome.setStyleSheet("QWidget#jtChrome { background: transparent; }")
        chrome.setMouseTracking(True)
        self._chrome_layout = QVBoxLayout(chrome)
        # KWin's decoration owns the window's outer geometry, so the
        # chrome layout no longer needs the resize-hit-zone margin
        # gymnastics that the frameless variant required.
        self._chrome_layout.setSpacing(0)
        self._chrome_layout.setContentsMargins(0, 0, 0, 0)
        layout = self._chrome_layout

        # Borderless: the top bar doubles as the window's titlebar —
        # draggable, with min/max/close. Native-border mode leaves
        # those to KWin's decoration.
        self.top_bar = JtTopBar(chrome, titlebar_mode=self._borderless)
        self.top_bar.nav_requested.connect(self._on_nav_requested)
        self.top_bar.settings_requested.connect(self._open_settings)
        self.top_bar.tab_requested.connect(self._on_tab_requested)
        # Library controls (visible only when the native album grid is
        # the active surface). Shuffle reuses the existing library
        # shuffle path; sort reaches the grid directly; the view-mode
        # toggle is wired but list-view rendering is a follow-up.
        self.top_bar.shuffle_all_requested.connect(self._library_shuffle)
        self.top_bar.sort_changed.connect(self._on_library_sort_changed)
        self.top_bar.view_mode_changed.connect(self._on_library_view_mode_changed)
        # Multi-library selection: the top-bar "Music" dropdown emits when
        # the user changes which libraries are loaded; the bus carries the
        # reload ping to every browse surface (queued so a burst doesn't
        # re-enter a grid mid-load).
        self.top_bar.libraries_selected.connect(self._on_libraries_selected)
        PlayerBus.get().libraries_changed.connect(
            self._on_libraries_changed,
            Qt.ConnectionType.QueuedConnection,
        )
        layout.addWidget(self.top_bar)

        # Content stack — every visible surface is a native PySide6
        # widget now (album / playlist / artist grids, songs, genres,
        # suggestions, search, login, now-playing). The Jellyfin Web
        # embed that used to live here was retired once the native
        # surfaces covered every user-clicked path (browse, search,
        # account, sign-in). Saved ~750 LOC of bridge scaffolding +
        # the entire Chromium runtime cost.
        self.content_stack = QStackedWidget(chrome)
        # Chrome → content_stack → page must stay transparent so the
        # main window's painted body color shows through. GLOBAL_STYLE
        # paints every QWidget with the solid BG color by default; this
        # ID rule wins by specificity.
        self.content_stack.setObjectName("jtContentStack")
        self.content_stack.setStyleSheet(
            "QStackedWidget#jtContentStack { background: transparent; }"
        )
        # Single hook keeps the top-bar in sync with the visible
        # surface: leaving the np_page exits "Now Playing" mode so the
        # library-tab dropdown can repopulate normally.
        self.content_stack.currentChanged.connect(self._on_content_changed)
        layout.addWidget(self.content_stack, 1)

        self.np_bar = NowPlayingBar(chrome)
        self.np_bar.show_now_playing_requested.connect(self._show_now_playing)
        # Bus-level Navigate-to-Now-Playing — call sites that already
        # depend on PlayerBus (smart-playlist play, future "play whole
        # X" surfaces) emit ``show_now_playing`` to land the user on
        # the NP page after queuing.
        self.bus.show_now_playing.connect(self._show_now_playing)
        self.np_bar.show_queue_requested.connect(lambda: self.bus.show_mini_player.emit())
        self.np_bar.cast_requested.connect(self._open_cast_dialog)
        self.np_bar.cast_context_requested.connect(self._show_cast_context_menu)
        # Lets the bar's volume popup switch to the per-speaker variant
        # when the active cast is a Chromecast group.
        self.np_bar.set_cast_manager(self.cast_manager)
        layout.addWidget(self.np_bar)

        # The now-playing page is constructed lazily on first open
        # (see _show_now_playing). Building eagerly here was ~30-80ms
        # of widget tree + bus signal connection that the user doesn't
        # need until they click into the page. The trade-off is no live
        # state until first open — fine because the right pane reads
        # from queue_mgr at render time anyway and the left pane is
        # driven by playback_started which fires per-track.
        self.np_page: "NowPlayingPage | None" = None
        # Native library grids — Phase 4. Each kind gets its own lazy-
        # built instance so toggling between Albums and Playlists tabs
        # doesn't tear down + rebuild. Top-bar tab clicks route through
        # _on_tab_requested.
        self.album_grid = None  # LibraryGrid(kind="album") | None
        self.playlist_grid = None  # LibraryGrid(kind="playlist") | None
        self.artist_grid = None  # LibraryGrid(kind="artist") | None
        # Artist detail page — chronological album grid, opened by
        # clicking an artist tile in the Artists grid.
        self.artist_page = None  # ArtistPage | None
        # Songs (list view) and Genres (tile grid). Songs reuses the
        # standard sort/library controls; Genres has no inline
        # controls (clicking a tile pivots the user into a filtered
        # album grid for that genre).
        self.songs_view = None  # SongsView | None
        self.genres_view = None  # GenresView | None
        self.suggestions_view = None  # SuggestionsView | None
        self.search_view = None  # SearchView | None
        # Browser-style navigation history. Each entry is a thunk that
        # re-shows the surface; back / forward walk the list. Surfaces
        # push themselves at the end of their _show_* method; the
        # _suppress_nav_push flag is set during back/forward replay so
        # the replay doesn't itself add a new history entry.
        self._nav_history: list = []
        self._nav_pos: int = -1
        self._suppress_nav_push: bool = False

        # Wire the chrome into the central stacked layout. The
        # window stays hidden until _do_boot_auth_check finishes
        # building the initial surface (see __init__ end + main());
        # there's no boot-time loading overlay because the user
        # never sees a partially-constructed window.
        central_stack.addWidget(chrome)

        self.bus.open_main_window.connect(self._show_self)
        # Live-apply for global stylesheet bits (QCheckBox indicator
        # background, QComboBox dropdown items, etc.) when the color
        # editor fires theme_changed. The accent picker's own
        # _on_accent_picked already runs an equivalent cascade; this
        # hook makes the Colors page slider drag behave identically.
        self.bus.theme_changed.connect(self._cascade_global_style)
        # A live theme swap (incl. OS-driven "auto") also changes the
        # painted body fill and, on Windows, the Mica variant — re-resolve
        # the body colour and re-issue blur so a light↔dark flip repaints
        # the window without a restart. _refresh_body_color only repaints
        # when the colour actually changed, so this is a no-op on accent
        # tweaks. get_active_theme() is already fresh: the emitter calls
        # refresh_theme() before theme_changed.emit().
        self.bus.theme_changed.connect(self._refresh_body_color)
        self.bus.theme_changed.connect(self._apply_blur)
        # "Auto (follow OS)" theme: track the OS light/dark setting live.
        # QStyleHints.colorSchemeChanged fires on a system theme toggle —
        # but Windows emits it several times per toggle (multiple
        # WM_SETTINGCHANGE), so coalesce into ONE re-theme via a short
        # debounce; the timeout re-resolves + re-stamps through the very
        # same path the Settings theme picker uses.
        self._os_scheme_timer = QTimer(self)
        self._os_scheme_timer.setSingleShot(True)
        self._os_scheme_timer.setInterval(150)
        self._os_scheme_timer.timeout.connect(self._apply_os_color_scheme)
        try:
            QApplication.instance().styleHints().colorSchemeChanged.connect(
                lambda *_: self._os_scheme_timer.start()
            )
        except Exception:
            pass
        # Persistent auth-failure → drop to LoginView. Without this a
        # genuinely-bad stored credential (e.g. server-side password
        # change) leaves the user staring at "No albums yet" with no
        # affordance for recovery; the connectivity tracker tallies
        # N consecutive 401/403 (Jellyfin) or code-40 (Subsonic) and
        # fires this when the threshold trips.
        self.bus.auth_failed.connect(self._on_auth_failed)

        # Failover feedback — the connectivity engine emits host_switched
        # when it fails over to (or climbs back from) an alternate
        # server URL. Surface it as a transient toast so the user knows
        # which address they're on without a modal interruption.
        self.bus.host_switched.connect(self._on_host_switched)

        # All in-app keyboard shortcuts (Ctrl+F, /, Ctrl+Q, Ctrl+Shift+L,
        # opt-in Ctrl+Shift+A) live in jellytoast.hotkeys. The registry
        # there is the single source of truth and the future Settings
        # → Hotkeys page consumes the same list. We hold the returned
        # QShortcut refs on self so Qt doesn't GC them mid-session.
        from jellytoast import hotkeys as _hotkeys

        self._hotkey_shortcuts = _hotkeys.install_shortcuts(self)
        # Settings → Hotkeys emits hotkeys_changed on every rebind /
        # reset; re-install so the change is live without a restart.
        self.bus.hotkeys_changed.connect(self._reinstall_hotkeys)
        # Space-to-play, installed at the application level so it
        # fires regardless of which widget happens to have focus
        # (the QListView popup, lyrics scroll, etc. all consume
        # Space if it reaches them). Filter skips text inputs so
        # spaces typed in search / login / settings still work.
        self._space_filter = _SpacePlayFilter(self.bus, self)
        QApplication.instance().installEventFilter(self._space_filter)
        # Tab rotates between the three structural sections (top
        # bar → content → bottom transport). See _SectionTabFilter.
        self._tab_filter = _SectionTabFilter(self, self)
        QApplication.instance().installEventFilter(self._tab_filter)
        # Down arrow from chrome (top bar / bottom bar) dives into
        # the current content surface's first item.
        self._chrome_down_filter = _ChromeDownFilter(self, self)
        QApplication.instance().installEventFilter(self._chrome_down_filter)
        # Custom frosted tooltips — a top-level translucent widget replaces
        # Qt's reused QTipLabel (which kept an opaque box behind the text after
        # a live theme swap and couldn't be repositioned on Wayland). The
        # filter intercepts QEvent.ToolTip, enforces the show_tooltips setting,
        # and drives our popup. See jellytoast/custom_tooltip.py.
        from jellytoast import custom_tooltip

        self._tooltip_filter = custom_tooltip.install(QApplication.instance())
        # Mouse activity clears any keyboard-focus rings (rings are
        # a keyboard-only affordance — see _MouseClearFocusFilter).
        self._mouse_clear_filter = _MouseClearFocusFilter(self)
        QApplication.instance().installEventFilter(self._mouse_clear_filter)

        # Library ids are resolved lazily on first load — the start
        # destination preference picks which one we navigate to.
        self._library_ids: dict[str, str] = {}
        # Pre-fetched random library queue, primed in the background so
        # the first shuffle click after launch can install it instantly
        # instead of waiting for the REST round-trip. Refreshed after
        # each use so the next click also gets a snappy install.
        self._random_queue_cache: list[dict] = []
        # Re-entry guard for the library shuffle button — prevents a
        # double-click from kicking off two parallel installs. Cleared
        # at the end of each shuffle path (cached, async-loaded, error).
        self._shuffle_in_flight: bool = False

        # Native sign-in surface: shown on boot when there are no
        # credentials in our store, or when the persisted token is
        # rejected by the server (admin revoked the device session).
        # On a successful auth the host swaps to the chosen home
        # destination via _on_native_signed_in.
        from jellytoast.login_view import LoginView

        self.login_view = LoginView(self)
        self.login_view.signed_in.connect(self._on_native_signed_in)
        self.content_stack.addWidget(self.login_view)

        # Connection-keepalive heartbeat. Servers (Navidrome included)
        # close idle keep-alive connections after 30-60s. When the user
        # leaves the app sitting and comes back, the next cover-art
        # request pays a fresh TCP+TLS handshake (50-200ms) on TOP of
        # the actual fetch — we noticed this as "art is slow if you
        # leave the app sitting". A cheap /ping every 25s keeps QNAM's
        # connection pool warm so the user-visible request always lands
        # on an established socket.
        self._keep_alive_timer = QTimer(self)
        self._keep_alive_timer.setInterval(25_000)
        self._keep_alive_timer.timeout.connect(self._heartbeat)
        self._keep_alive_timer.start()
        # Defer the auth decision via QTimer.singleShot(0). Why: the
        # window is hidden until this fires (main() doesn't call show
        # eagerly). Deferring lets __init__ return so main() can
        # finish scheduling post-show init, then the auth check runs
        # against credentials that may need a fast keyring read or
        # the encrypted-file fallback. Once the right surface is
        # current we call self.show() — the user sees a fully-drawn
        # window on first paint instead of a dark overlay that fades
        # to content.
        QTimer.singleShot(0, self._do_boot_auth_check)

    # Brief hold between "we've picked the boot destination" and the
    # actual ``show()``. Lets the LibraryGrid's first-load cover
    # pre-warm (rows 0..N for the visible-at-top range) land its
    # cache-hit callbacks into the model before the window maps, so
    # first paint shows populated tiles instead of a flash of empty
    # cells filling in as the user watches. Subjectively-perceptible
    # ceiling around half a second; below that the user can't see the
    # difference, above it the launch feels sluggish.
    _REVEAL_DELAY_MS = 500

    def _reveal_window(self):
        """Show the window now that the initial surface has been
        chosen. Idempotent so the verify-session failure path can
        safely call this even though the success path already did."""
        if self.isVisible():
            return
        QTimer.singleShot(self._REVEAL_DELAY_MS, self._actually_show_window)

    def _actually_show_window(self):
        if self.isVisible():
            return
        self.show()
        # Apply compositor blur once the window has a mapped surface
        # (deferred a tick past show()), then verify whether it actually
        # landed and settle the body alpha (glass vs near-opaque fallback).
        QTimer.singleShot(0, self._first_blur_pass)
        # Tell KDE the launch is complete so the taskbar entry stops
        # bouncing and transitions from 'launching' to active. The
        # startup id was stashed by main() during construction.
        startup_id = getattr(self, "_startup_id", "")
        if startup_id:
            _send_startup_notification_remove(startup_id)

    def _is_edge_flush(self) -> bool:
        """True when the window sits flush against a screen edge so its
        rounded corners shouldn't be painted — true Qt-maximized OR the
        double-click vertical-maximize (height == the screen's available
        height, which ``setGeometry`` produces without flipping
        ``windowState`` to Maximized). ``paintEvent`` squares the body in
        this state so it sits flush against the screen edges, and
        ``_apply_blur`` squares the shaped blur region to match."""
        if self.isMaximized() or self.isFullScreen():
            return True
        screen = self.screen()
        if screen is None:
            return False
        avail = screen.availableGeometry()
        geo = self.geometry()
        # Height-flush is the vertical-max tell; allow a 1px slop for
        # rounding between logical geometry and the compositor's idea.
        return abs(geo.height() - avail.height()) <= 1 and abs(geo.y() - avail.y()) <= 1

    def _resolve_body_qcolor(self) -> QColor:
        """The main-window body fill colour. JT_OPAQUE forces fully opaque;
        otherwise the alpha tracks the verified blur status so a frosted
        theme rides real blur (glass ~67%) or falls back to a near-opaque
        panel (~92%, never see-through). The status-resolution lives in
        ``ui_helpers.body_color_tuple`` — shared with the mini player and
        dialogs so every frosted surface degrades together."""
        if _OPAQUE_BODY:
            from jellytoast.theme import get_active_theme

            c = get_active_theme().body_color
            return QColor(c[0], c[1], c[2], 255)
        from jellytoast import ui_helpers

        return QColor(*ui_helpers.body_color_tuple("main"))

    def _refresh_body_color(self):
        """Recompute the cached body colour (blur status or theme may have
        changed) and repaint only if it actually changed. Also re-derive
        whether the Windows Acrylic blur is live (frosted theme) — that flag
        gates the transparent paint, so a frosted↔solid swap repaints with
        the right fill instead of leaving a Solid theme transparent (black)."""
        if self._win_blur:
            from jellytoast.theme import get_active_theme

            was = self._win_blur_active
            self._win_blur_active = get_active_theme().blur
            if was != self._win_blur_active:
                self.update()
        new = self._resolve_body_qcolor()
        if new != self._body_qcolor:
            self._body_qcolor = new
            self.update()

    def _first_blur_pass(self):
        """Post-show one-shot: issue blur now the surface is mapped, then
        re-probe the verified blur status (the probe is most reliable once a
        platform window exists) and re-pick the body alpha, logging the
        outcome once so a silent no-op is diagnosable from the terminal."""
        from jellytoast import blur
        from jellytoast.theme import get_active_theme

        self._apply_blur()
        # Only the frosted themes ride blur — for Solid / Transparent there's
        # nothing to verify, so skip the probe and the (misleading) status
        # log entirely; the body alpha is already correct (DISABLED path).
        if not get_active_theme().blur:
            return
        status = blur.status(force=True)
        self._refresh_body_color()
        if status is not blur.BlurStatus.ACTIVE:
            # Blur is broken/unavailable — a real heads-up (also surfaced in
            # Settings → Display), keep it visible at INFO.
            logger.info(
                "Frosted theme: %s (%s). Or pick Solid dark.",
                blur.reason(),
                status.value,
            )
        else:
            # Happy path — diagnostic only, don't clutter a normal boot.
            logger.debug("Compositor blur: %s", blur.reason())

    def _apply_blur(self):
        """Shape the compositor blur to the body's rounded rect when the
        active theme is frosted, so no square blur halo pokes past the 4
        rounded corners. Silent no-op where the compositor / platform has no
        blur support.

        Hybrid (2026-06-05) — replaces the 2026-06-01 whole-window tradeoff.
        A shaped region can desync on Wayland because the committed surface
        size lags the QWidget geometry during maximize / vertical-expand /
        drag-to-unmaximize, which left a transparent strip. So we only shape
        the region AT REST: ``resizeEvent`` / ``changeEvent`` drop to
        whole-window (square, auto-tracking) blur via ``_apply_blur_whole``
        for the duration of an interaction, and this re-shapes once
        ``_blur_settle`` fires on a stable geometry. Radius follows the
        painted body — squared when flush against a screen edge (maximized /
        vertical-max), rounded otherwise. See decisions.md."""
        from jellytoast import blur
        from jellytoast.theme import get_active_theme

        radius = 0 if self._is_edge_flush() else RADIUS_WINDOW
        blur.apply(self, get_active_theme().blur, radius)

    def _apply_blur_whole(self):
        """Whole-window (square, empty-region) blur applied LIVE during an
        active resize / maximize. KWindowSystem auto-tracks an empty region
        across surface recreation, so it can't desync into a transparent
        strip while the Wayland surface size lags the QWidget geometry —
        ``_apply_blur`` restores the rounded region once geometry settles."""
        from jellytoast import blur
        from jellytoast.theme import get_active_theme

        blur.apply(self, get_active_theme().blur, 0)

    def paintEvent(self, e):
        # Fill the body with `_body_qcolor` (computed in __init__ — full
        # alpha in JT_OPAQUE=1 mode so there's no compositor blend for
        # Sunshine's screencopy to race, theme-alpha otherwise).
        #
        # Borderless: KWin draws no decoration, so we round the body
        # ourselves at the host-OS radius — squared while maximized so
        # it sits flush. Native-border / non-KDE: KWin owns the corner
        # radius, so a plain rect fill is correct.
        if self._win_blur_active:
            # Real-blur mode + a FROSTED theme: paint the styled (QSS)
            # transparent background the way Qt's default does for a styled
            # widget — this CLEARS the surface each frame (no ghosting) while
            # staying transparent so the Acrylic blur behind the non-layered
            # window shows through. A Solid theme has _win_blur_active False
            # and falls through to the opaque body fill below. Corners come
            # from DWM's corner preference.
            from PySide6.QtWidgets import QStyle, QStyleOption

            opt = QStyleOption()
            opt.initFrom(self)
            sp = QPainter(self)
            self.style().drawPrimitive(
                QStyle.PrimitiveElement.PE_Widget, opt, sp, self
            )
            sp.end()
            return
        p = QPainter(self)
        try:
            if self._borderless:
                # Square the body whenever the window is flush to a screen
                # edge (maximized OR vertically-maximized) so the painted
                # body matches the squared blur region — otherwise the body
                # rounds its bottom corners while the blur is square and the
                # corners read as a mismatched notch.
                radius = 0 if self._is_edge_flush() else RADIUS_WINDOW
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(self._body_qcolor)
                p.drawRoundedRect(self.rect(), radius, radius)
            else:
                p.fillRect(self.rect(), self._body_qcolor)
        finally:
            p.end()

    def changeEvent(self, e):
        # Catch Qt 6's authoritative cross-DPR event so subscribers
        # (LibraryGrid, NowPlayingBar, MiniPlayer, NowPlayingPage) can
        # re-issue cover loads sized for the new physical target. Fires
        # when the user drags jellytoast between monitors of different
        # KDE scales, or when the global scale slider moves while the
        # window is mapped. The L1 in-memory cover cache is keyed by
        # physical size, so the new requests naturally cache-miss and
        # derive fresh from the L2 raw cache — no manual invalidation
        # needed for the cache itself, only for what's already painted.
        from PySide6.QtCore import QEvent as _QEvent

        if e.type() == _QEvent.Type.DevicePixelRatioChange:
            try:
                from jellytoast.player_state import PlayerBus as _PB

                _PB.get().dpr_changed.emit()
            except Exception as exc:
                logger.warning("dpr_changed emit failed: %s", exc)
        elif getattr(self, "_borderless", False) and (
            e.type() == _QEvent.Type.WindowStateChange
        ):
            # Maximize / restore flips the corner radius (squared when
            # maximized so the body sits flush against the screen edges)
            # — repaint so paintEvent re-evaluates it. `getattr` guards the
            # early WindowTitleChange that setWindowTitle() fires before
            # __init__ has assigned `_borderless`. The surface size lags the
            # state flip during the transition, so blur whole-window (square)
            # now — safe, auto-tracks — and let _blur_settle re-shape to the
            # rounded (or squared-when-maximized) region on a stable geometry.
            self.update()
            self._apply_blur_whole()
            self._blur_settle.start()
        super().changeEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Borderless: the rounded blur region is sized to the window. Go
        # whole-window (square) NOW — an empty region auto-tracks the lagging
        # Wayland surface, so no transparent strip — and re-shape to rounded
        # once the resize settles (debounced).
        if getattr(self, "_borderless", False) and hasattr(self, "_blur_settle"):
            self._apply_blur_whole()
            self._blur_settle.start()

    # Space-to-play is wired through an application-wide event
    # filter (see _SpacePlayFilter) installed in __init__. A plain
    # keyPressEvent override on the window only fires when focus is
    # on the window itself — child widgets like QListView consume
    # Space for their own purposes (selection toggle, page-down)
    # before it ever reaches us. An app-level filter intercepts
    # the key press before any widget can swallow it.

    @Slot(bool)
    def _open_settings(self):
        # Singleton: re-clicking Settings while it's already open just
        # raises the existing dialog. The dialog is non-modal so the
        # main window + mini player stay interactive — without this
        # guard a second click would stack another instance on top.
        existing = getattr(self, "_settings_dlg", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        # parent=self so KWin establishes a transient-for relationship
        # under Wayland — that gives us compositor-side center-on-parent
        # placement (xdg-shell forbids client-side move(), so without
        # the parent KWin's "Smart" auto-placement drops the dialog at
        # an arbitrary spot). The trade-off: the dialog pins above the
        # main window — clicking the main window won't raise it past
        # Settings — but that matches how every other app's Settings
        # behaves and is the right call for a contextual surface.
        dlg = SettingsDialog(self)
        self._settings_dlg = dlg
        # Close the dialog before tearing down credentials so the
        # LoginView underneath becomes visible immediately — otherwise
        # the dialog sits on top of it until the user dismisses it.
        dlg.sign_out_requested.connect(dlg.accept)
        dlg.sign_out_requested.connect(self._on_sign_out_requested)
        dlg.server_change_requested.connect(dlg.accept)
        dlg.server_change_requested.connect(self._on_server_change_requested)
        # Drop the singleton reference when the dialog closes so the
        # next click builds a fresh one instead of raising a hidden
        # corpse.
        dlg.finished.connect(self._on_settings_closed)
        # Open centered on the main window. Dialog uses parent=None (see
        # the comment above) so Qt has no anchor; without an explicit
        # move() it lands wherever the compositor's default placement
        # picks. On KDE Wayland xdg-shell forbids client-side positioning
        # so move() is silently dropped — on X11 / Windows / macOS it
        # places the dialog as intended. Clamp to the dialog's screen so
        # a main window dragged off-screen doesn't launch Settings into
        # the void.
        self._center_dialog_on_main(dlg)
        dlg.show()

    def _on_settings_closed(self, _result=0):
        self._settings_dlg = None

    def _center_dialog_on_main(self, dlg) -> None:
        """Position ``dlg`` centered on the main window. Dialog should
        have a meaningful size already (setFixedSize / setMinimumSize in
        __init__); we use sizeHint() as a fallback. Clamps to the
        dialog's screen rect so a partially-off-screen main window
        doesn't push the dialog out of bounds. KDE Wayland silently
        ignores client-side move(); other platforms honour it."""
        main_rect = self.frameGeometry()
        size = dlg.size()
        if size.width() <= 0 or size.height() <= 0:
            size = dlg.sizeHint()
        center = main_rect.center()
        x = center.x() - size.width() // 2
        y = center.y() - size.height() // 2
        screen = self.screen() or dlg.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left(), min(x, avail.right() - size.width()))
            y = max(avail.top(), min(y, avail.bottom() - size.height()))
        dlg.move(x, y)

    def _position_dialog_above_now_playing(self, dlg) -> None:
        """Anchor ``dlg`` so its right edge tracks the main window's
        right edge and its bottom sits just above the now-playing bar.
        Used by the Cast picker — popping up next to the cast button
        reads as a contextual menu rather than a floating dialog.
        Falls back to centering if the np_bar isn't mounted yet."""
        bar = getattr(self, "np_bar", None)
        if bar is None or not bar.isVisible():
            self._center_dialog_on_main(dlg)
            return
        main_rect = self.frameGeometry()
        size = dlg.size()
        if size.width() <= 0 or size.height() <= 0:
            size = dlg.sizeHint()
        margin = 8  # breathing room from window edge + bar
        bar_top_global = bar.mapToGlobal(QPoint(0, 0)).y()
        x = main_rect.right() - margin - size.width()
        y = bar_top_global - margin - size.height()
        screen = self.screen() or dlg.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left(), min(x, avail.right() - size.width()))
            y = max(avail.top(), min(y, avail.bottom() - size.height()))
        dlg.move(x, y)

    @Slot()
    def _apply_os_color_scheme(self):
        """Debounce-fired after the OS light/dark setting toggled. When the
        theme is "Auto (follow OS)", re-resolve and live-restamp the whole app
        through the same path the Settings theme picker uses (refresh the token
        constants → notify every subscriber), wrapped in the repaint guard so
        it lands as one frame. No-op for any explicit theme choice."""
        from jellytoast.settings import get_settings

        if get_settings().theme_mode != "auto":
            return
        from jellytoast import icons as _icons
        from jellytoast import ui_helpers as _uih

        with _uih.theme_swap_guard():
            _uih.refresh_theme()
            _icons.refresh_theme()
            self.bus.theme_changed.emit()

    def _cascade_global_style(self):
        """Re-stamp the app-wide stylesheet + repolish indicator-style
        widgets on PlayerBus.theme_changed. The Colors page emits
        theme_changed from its slider drags but doesn't have the
        accent picker's full cascade — without this, QCheckBox
        indicator backgrounds + QComboBox dropdown items stay on the
        old accent until the user reopens the dialog or restarts.

        Idempotent + cheap; safe to run on every theme_changed."""
        from jellytoast import icons as _icons
        from jellytoast import ui_helpers as _uih

        # 1. Rebuild GLOBAL_STYLE from the (already-refreshed) token
        # constants and push it onto QApplication. The emitter
        # (_on_accent_picked / _on_theme_changed / color_tokens) is
        # contractually required to have mutated the ui_helpers tokens
        # *before* emitting theme_changed, so _build_global_style here
        # reads fresh values — re-running refresh_theme would be a
        # redundant QSettings round-trip.
        # Set the new sheet ONCE. We used to clear-then-set ("" first) to
        # force Qt past the KDE Fusion QCheckBox-indicator pixmap cache, but
        # that doubled the whole-app re-polish (the visible cost of a live
        # accent/colour change). The pixmap cache is already handled directly
        # by the per-widget QCheckBox/QRadioButton unpolish+polish in step 3
        # below, so the clear is redundant — and on a real colour change the
        # sheet string differs, so this set is never silently no-op'd.
        new_global_style = _uih._build_global_style()
        _uih.GLOBAL_STYLE = new_global_style
        _icons.refresh_theme()
        app = QApplication.instance()
        if app is None:
            return
        app.setStyleSheet(new_global_style)
        # 2. Refresh the full app palette so every Qt-style-painted role
        # (Highlight, ToolTipBase, WindowText, ButtonText, Text,
        # ToolTipText, disabled fg) tracks the new theme. The earlier
        # version only stamped Highlight + HighlightedText, leaving
        # ToolTipBase stale — tooltips that were styled before a theme
        # swap kept the old backdrop (white on dark, transparent on
        # certain owners) until the next process restart.
        try:
            _uih.apply_app_palette()
        except Exception:
            pass
        # 2b. Rebuild the custom tooltip popup so the next hover gets a fresh
        # ARGB surface. The reused top-level's re-polish on a live swap can
        # leave opaque corners that show as a box behind the rounded pill —
        # correct on a fresh launch, wrong on a swap. reset() is the popup's
        # "restart" (see jellytoast/custom_tooltip.ToolTipPopup.reset).
        try:
            from jellytoast.custom_tooltip import ToolTipPopup

            ToolTipPopup.reset()
        except Exception:
            pass
        # 3. Indicator-rule fix-up for QCheckBox / QRadioButton.
        # The ::checked rule bakes ACCENT_DEEP / ACCENT; on KDE Fusion
        # the cached indicator pixmap doesn't reliably invalidate from
        # the app-level QSS alone — stamp the rule directly on each
        # QCheckBox (widget-level QSS wins + forces a fresh render) and
        # repolish so the change lands synchronously rather than on the
        # next hover/focus. Built once, then applied in a single
        # allWidgets() pass (the walk is the expensive part — don't do
        # it twice).
        from PySide6.QtWidgets import QCheckBox, QRadioButton

        from jellytoast.ui_helpers import (
            ACCENT as _ACC,
        )
        from jellytoast.ui_helpers import (
            BORDER as _BORDER,
        )
        from jellytoast.ui_helpers import (
            _hex_to_rgb_safe,
            ink_alpha,
        )
        from jellytoast.ui_helpers import (
            check_url_for_accent as _check_url_fn,
        )

        _ar, _ag, _ab = _hex_to_rgb_safe(_ACC)
        _check = _check_url_fn()
        cb_qss = f"""
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border: 1px solid {_BORDER};
                border-radius: 3px;
                background: {ink_alpha(0.04)};
            }}
            QCheckBox::indicator:hover {{
                border-color: {ink_alpha(0.30)};
            }}
            QCheckBox::indicator:checked {{
                background: rgba({_ar},{_ag},{_ab},0.15);
                border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                image: url({_check});
            }}
            QCheckBox::indicator:checked:hover {{
                background: rgba({_ar},{_ag},{_ab},0.28);
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
        """
        # The walk over `app.allWidgets()` is the hot spot of the
        # theme switch — on a populated app it touches every list
        # tile, every settings row, every cast popup. Defer it via
        # singleShot(0) so the GLOBAL_STYLE re-stamp + palette + body
        # repaint above land on this frame; the indicator repolish
        # catches up in the next event-loop tick. The user perceives
        # the theme as "instant" with checkboxes blinking to the new
        # accent ~16ms later instead of holding the whole cascade
        # synchronous (was perceptibly laggy on large libraries).
        #
        # Additionally: skip widgets that aren't currently visible.
        # Hidden surfaces (the mini player when closed, settings pages
        # the user hasn't visited, the cast dialog when not open) get
        # their style re-evaluated by Qt's showEvent chain when they
        # next become visible, so polishing them now is wasted work
        # that we'd just have to redo per-show anyway.
        def _repolish_indicators():
            for w in app.allWidgets():
                if not w.isVisible():
                    continue
                if isinstance(w, QCheckBox):
                    w.setStyleSheet(cb_qss)
                    w.style().unpolish(w)
                    w.style().polish(w)
                    w.update()
                elif isinstance(w, QRadioButton):
                    w.style().unpolish(w)
                    w.style().polish(w)
                    w.update()

        QTimer.singleShot(0, _repolish_indicators)
        # 5. Repaint the window body. paintEvent fills `_body_qcolor`,
        # which is cached (not read live) — and a theme-mode switch changes
        # the body opacity (frosted glass-or-fallback / solid 100% /
        # transparent ~43%). _resolve_body_qcolor() reads the live active
        # theme + verified blur status; recompute + repaint if it changed.
        self._refresh_body_color()
        # 6. Frosted theme blurs behind the window; Transparent / Solid
        # don't. Re-evaluate on every theme change.
        self._apply_blur()

    def _kick_load_when_ready(self, fn):
        """Run `fn` immediately if the provider's credentials are
        ready. The provider's authenticate() populates token + user_id
        before LoginView emits signed_in, so the synchronous path is
        the common case; this is a guard for the rare case where a
        native surface is built before authentication completes."""
        if self.provider.is_authenticated:
            fn()

    # ── Navigation history ─────────────────────────────────────────────

    _NAV_HISTORY_CAP = 200

    def keyPressEvent(self, event):
        """Window-level Down dives into the active surface's first
        item (suggestions only — exposes focus_first_item)."""
        if (
            event.key() == Qt.Key.Key_Down
            and not event.modifiers()
            and QApplication.activePopupWidget() is None
        ):
            cur = self.content_stack.currentWidget()
            getter = getattr(cur, "focus_first_item", None)
            if callable(getter):
                getter()
                event.accept()
                return
        super().keyPressEvent(event)

    def closeEvent(self, e):
        # _quitting is set by the tray's "Quit jellytoast" handler so
        # that path bypasses the minimize-to-tray divert and actually
        # exits. Without this, app.quit() fires the implicit close
        # cascade, this handler ignores the event, and the app stays
        # alive with the mini player still floating + audio still
        # playing.
        # Save window geometry on every close — both the hard-quit
        # path and the minimize-to-tray hide path — so the next
        # launch restores the user's last visible position. We save
        # before the quit/hide so a crash mid-shutdown still keeps
        # the prior known-good geometry.
        try:
            get_settings().window_geometry = bytes(self.saveGeometry())
        except Exception:
            pass
        # Dismiss any tracked top-level dialogs (Settings, Cast) so they
        # don't sit on the desktop after the main window is hidden or
        # gone. The dialogs are transient children of the main window
        # on Wayland but the compositor doesn't always hide a transient
        # when its parent hides — explicit close() makes the behaviour
        # uniform across X11 / Wayland / Windows / macOS, and fires
        # finished() so the singleton refs get cleared.
        for attr in ("_settings_dlg", "_cast_dlg"):
            dlg = getattr(self, attr, None)
            if dlg is not None:
                try:
                    dlg.close()
                except Exception:
                    pass
        if getattr(self, "_quitting", False) or not get_settings().minimize_to_tray:
            QApplication.instance().quit()
        else:
            self.hide()
            e.ignore()


def _send_startup_notification_remove(startup_id: str):
    """Tell KDE the startup is complete by sending the freedesktop
    startup-notification 'remove' message via X11 ClientMessage.
    KDE listens for these on the root window and stops the bouncing
    cursor / 'launching' taskbar entry on receipt. Normally Qt sends
    this automatically when the first window maps — we suppress that
    by popping DESKTOP_STARTUP_ID from os.environ before QApplication
    init, then call this when we're actually ready to be seen."""
    if not startup_id or not IS_LINUX:
        return
    try:
        from Xlib import X, display
        from Xlib.protocol import event as xevent

        d = display.Display()
        root = d.screen().root
        # Throwaway sender window — required by the spec; root sees
        # the ClientMessage and rebroadcasts logically via the event.
        sender = root.create_window(-100, -100, 1, 1, 0, X.CopyFromParent)
        msg = f'remove: ID="{startup_id}"\x00'.encode("utf-8")
        type_begin = d.intern_atom("_NET_STARTUP_INFO_BEGIN")
        type_cont = d.intern_atom("_NET_STARTUP_INFO")
        first = True
        for i in range(0, len(msg), 20):
            chunk = msg[i : i + 20].ljust(20, b"\x00")
            ev = xevent.ClientMessage(
                window=sender,
                client_type=type_begin if first else type_cont,
                data=(8, chunk),
            )
            root.send_event(ev, event_mask=X.PropertyChangeMask)
            first = False
        sender.destroy()
        d.flush()
        d.close()
    except Exception as e:
        # Non-fatal: worst case the bounce keeps going until KDE's
        # ~30s timeout. Don't let a missing python-xlib or a non-X11
        # session crash startup.
        logger.warning("startup-notify remove failed: %s", e)


def _setup_hidpi() -> None:
    """High-DPI scaffolding for Qt 6 / PySide6.

    Qt 6 turns HiDPI ON by default; the Qt 5 ceremony
    (AA_EnableHighDpiScaling / AA_UseHighDpiPixmaps /
    AA_DisableWindowContextHelpButton) is now a no-op and intentionally
    omitted. What still matters:

    - **Rounding policy.** Qt 6's default is ``PassThrough``, which is
      what we want: at KDE fractional scales (125 % / 150 % / 175 %)
      Qt renders at the true buffer size via wp_fractional_scale_v1,
      so text and chrome stay sharp. We set it explicitly anyway so a
      future Qt default change (or a user with ``QT_SCALE_FACTOR_*``
      already exported) lands on a known-good policy. ``Round`` would
      lose density on 125 % laptops; ``RoundPreferFloor`` is the only
      sane alternative if a future widget glitches at 1.5 ×.
    - **Per-Monitor V2 (Windows).** Qt 6 already requests it at
      startup, no manifest required. PyInstaller builds must keep the
      bundled manifest's ``dpiAwareness`` at ``PerMonitorV2`` — never
      downgrade to ``System``.
    - **NSHighResolutionCapable (macOS).** Must be ``true`` in the
      bundled ``Info.plist``; without it AppKit pixel-doubles the app
      and Retina rendering is lost. The bundler concern, not a
      runtime call.
    - **Wayland fractional scaling.** Qt 6.7+ talks
      ``wp_fractional_scale_v1`` to KWin natively. Do NOT force
      ``QT_QPA_PLATFORM=xcb`` to "fix" blur reports from older Qt
      versions; xcb + XWayland produces worse fractional-scale output
      than native Wayland on Plasma 6.

    Must run before ``QApplication`` is constructed — the rounding
    policy is consulted during Qt platform-plugin init.
    """
    # Power-user override path: respect an existing env var rather
    # than clobbering it, so ``QT_SCALE_FACTOR_ROUNDING_POLICY=Round
    # ./jellytoast/app.py`` works for users with picky displays.
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


def _shutdown_log(msg: str) -> None:
    """Record a shutdown step to both stderr and a log file.

    The file (``/tmp/jellytoast-shutdown.log``) survives the launch
    terminal closing — so when the app is killed by closing its
    terminal, there's still a record of whether the signal handler
    fired and how far cast cleanup got. Diagnostic aid for the
    "Chromecast keeps playing after the app exits" class of bug."""
    import time as _t

    line = f"{_t.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open("/tmp/jellytoast-shutdown.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    logger.info("%s", msg)


def _enable_faulthandler() -> None:
    """Convert a hard native crash (embedded libmpv, or a cross-thread
    ~QObject — jellytoast's documented SIGSEGV class) into an
    attributable Python+C stack on stderr instead of silent process
    death. Cheap, no behavioural change — EXCEPT under a GUI-subsystem
    interpreter (the pipx `jellytoast.exe` gui-script on Windows /
    pythonw), where ``sys.stderr`` is ``None`` and ``enable()`` raises
    ``RuntimeError``, killing the app before ``app.exec()`` (the silent
    no-window launch failure, 2026-06-10 Windows round). No stderr →
    nowhere to write a crash stack anyway, so skip it there."""
    if sys.stderr is None:
        return
    import faulthandler

    try:
        faulthandler.enable()
    except Exception:
        # e.g. a stderr replaced by a fileno-less stream (test capture,
        # embedded hosts) — losing the crash hook must never be fatal.
        pass


def main():
    _enable_faulthandler()
    # HiDPI setup runs before any other Qt action — the rounding
    # policy is consulted during platform-plugin init, so a later
    # call has no effect.
    _setup_hidpi()
    # Kick the OS secret service awake on a background thread *before*
    # QApplication is constructed, so kwalletd6 / gnome-keyring start
    # registering on the bus while the rest of the app boots. By the
    # time the deferred auth check fires (a couple seconds later) the
    # backend has had a head start on warming up. Empirical reads on
    # KDE Wayland have shown 9+ seconds from cold to first-good
    # response, longer than any sane synchronous wait — this overlaps
    # the worst of it with widget construction.
    from jellytoast.settings import warm_keyring_async

    warm_keyring_async()
    # Capture and suppress DESKTOP_STARTUP_ID before QApplication init.
    # X11 only: Qt's xcb plugin reads this env var and auto-sends the
    # 'remove' message when the first window maps; popping forces Qt
    # silent so we control the bounce-stop timing. On Wayland the
    # equivalent token is XDG_ACTIVATION_TOKEN, handled automatically
    # by Qt6 — leave it untouched.
    if will_be_wayland():
        _startup_id = ""
    else:
        _startup_id = os.environ.pop("DESKTOP_STARTUP_ID", "")
    app = QApplication(sys.argv)
    app.setApplicationName("jellytoast")
    app.setApplicationDisplayName("jellytoast")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("jellytoast")
    app.setDesktopFileName("jellytoast")
    app.setWindowIcon(QIcon(make_app_icon(64)))
    app.setQuitOnLastWindowClosed(False)

    # _OPAQUE_BODY is the JT_OPAQUE env diagnostic only (read at module load).
    # There's no user-facing opaque setting: a frosted theme that can't get
    # blur already falls back to a near-opaque body automatically, and the old
    # toggle broke the window's rounded corners by dropping translucency.

    # Push a theme-matched QPalette so widgets Qt paints from the
    # palette (separate-top-level dialogs, menus, tooltips) don't fall
    # back to the desktop's palette — white text on a light theme.
    from jellytoast.ui_helpers import apply_app_palette

    apply_app_palette()

    # Graceful shutdown on terminal-close (SIGHUP), `kill` (SIGTERM),
    # and Ctrl+C (SIGINT). Without this the process is killed before
    # Qt can emit aboutToQuit, so _cleanup never runs — and an active
    # Chromecast / AirPlay session keeps playing with no controller
    # left to stop it (cast receivers play autonomously). Routing the
    # signal to app.quit() unwinds the event loop normally so
    # _cleanup → cast_manager.cleanup() → stop_cast() fires.
    #
    # Armed here, right after the QApplication exists — long before the
    # (potentially slow) window construction — so a signal during boot
    # is still handled.
    def _graceful_shutdown(signum, _frame):
        # Closing a terminal delivers SIGHUP MORE THAN ONCE — the tty
        # hangup and the controlling shell's death each signal the
        # foreground process group. So ignore every shutdown signal
        # from here on: a follow-up SIGHUP must not hard-kill the
        # process mid-cleanup, before stop_cast() has run. (An earlier
        # version re-armed to SIG_DFL, and that second SIGHUP is
        # exactly what was orphaning the cast.) SIGALRM is the escape
        # hatch instead — a hard 5s deadline so a wedged cleanup still
        # can't hang forever.
        for _s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if _s is not None:
                signal.signal(_s, signal.SIG_IGN)
        if hasattr(signal, "alarm"):
            signal.alarm(5)
        _shutdown_log(f"shutdown signal {signum} received -> app.quit()")
        app.quit()

    for _signame in ("SIGINT", "SIGTERM", "SIGHUP"):
        _signum = getattr(signal, _signame, None)
        if _signum is not None:  # SIGHUP is POSIX-only
            signal.signal(_signum, _graceful_shutdown)

    # Python signal handlers only run when the interpreter regains
    # control from Qt's C++ event loop — which, while idle, can be a
    # long wait. A periodic no-op timer wakes the loop often enough
    # that a shutdown signal is acted on within ~200ms.
    _sig_wake = QTimer(app)
    _sig_wake.timeout.connect(lambda: None)
    _sig_wake.start(200)

    # Apply any color-token overrides saved by the user via Settings
    # → Colors BEFORE the main window is constructed, so the first
    # stylesheet stamp sees the overridden values. The override store
    # lives in QSettings, which requires QApplication + the org/app
    # names set above — hence the load happens here, not earlier.
    from jellytoast.color_tokens import load_persisted_overrides

    load_persisted_overrides()

    # App-wide palette override: paint Qt's "Highlight" / "HighlightedText"
    # roles with the user's accent colour so every Qt-style-drawn
    # selection (QListView item highlight, QLineEdit text-selection
    # background, QComboBox dropdown current-item rect, etc.) reads as
    # accent instead of Qt's default Fusion blue. Per-widget palette
    # overrides don't survive Qt's `QStyledItemDelegate` paint pass on
    # KDE Wayland — the delegate reads from the application palette,
    # not the widget palette. Setting it here flows through to every
    # popup / view in the app.
    from PySide6.QtGui import QPalette

    from jellytoast.theme import _hex_to_rgb as _h2r_boot
    from jellytoast.ui_helpers import ACCENT as _ACCENT_BOOT

    _ar, _ag, _ab = _h2r_boot(_ACCENT_BOOT)
    _app_pal = app.palette()
    _accent_qcolor = QColor(_ar, _ag, _ab)
    _app_pal.setColor(QPalette.ColorRole.Highlight, _accent_qcolor)
    _app_pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    app.setPalette(_app_pal)

    # App-wide smooth scrolling. Bound to `app` so it shares the app's
    # lifetime — letting it GC would silently disable the filter.
    from jellytoast.smooth_scroll import SmoothScrollFilter

    app._smooth_scroll = SmoothScrollFilter(app)
    app.installEventFilter(app._smooth_scroll)

    # Single-instance gate. Held by QSharedMemory; the QLocalServer is
    # the message channel for "raise me" pings from subsequent launch
    # attempts. We bind the result to `app` so it shares the app's
    # lifetime — letting it GC would release the shared-memory lock
    # mid-run and effectively disable the check.
    from jellytoast.single_instance import SingleInstance

    app._single_instance = SingleInstance("jellytoast", app)
    if not app._single_instance.acquire():
        # Another instance was already running — signal it to surface
        # and exit cleanly. Print a small breadcrumb so a CLI launcher
        # (terminal, .desktop file, autostart) can see what happened.
        logger.info("already running; raised existing window.")
        sys.exit(0)

    if not MPV_AVAILABLE:
        QMessageBox.critical(
            None,
            "Missing dependency",
            "jellytoast requires libmpv.\n\n"
            "Install mpv from your system package manager, "
            "or download it from https://mpv.io.",
        )
        sys.exit(1)

    settings = get_settings()
    # No first-run URL prompt. The LoginView (jellytoast/login_view.py) is the
    # single entry point for server URL + provider kind + credentials: on a
    # fresh launch server_url is empty, the boot auth check finds no token
    # and drops straight to the LoginView (session_controller._do_boot_auth_
    # check), which collects and persists the URL itself. A separate
    # QInputDialog here just asked for the same field twice, hardcoded
    # "Jellyfin", and couldn't pick the provider kind.
    server_url = settings.server_url.rstrip("/")

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(
            None,
            "No system tray",
            "Your desktop doesn't appear to have a system tray.\n"
            "jellytoast will run, but tray features will be unavailable.",
        )

    bus = PlayerBus.get()

    # Defer the heaviest startup work until after first paint. mpv's
    # codec/audio probe (~100-300ms), MPRIS' DBus name request (up to
    # 3s on a busy session bus), and KWin's mini-player rule install
    # (4-6 kreadconfig/kwriteconfig subprocesses + a qdbus reconfigure)
    # all blocked the user from seeing the window. None of them are
    # needed for chrome to paint or for the WebEngine to start
    # rendering — they only matter once the user actually triggers
    # playback / opens the cast dialog / etc.
    mpv_ctrl: "MpvController | None" = None
    mpris: "MediaControlsService | None" = None

    # Bring up the unified radio-state pipeline BEFORE any surface
    # consuming PlayerBus.radio_state_changed exists. The module owns
    # the parse + cover-lookup plumbing; surfaces just render its
    # emitted RadioState. Idempotent — safe to call again, no-op.
    from jellytoast import radio_state as _radio_state

    _radio_state.init()

    win = JellytoastWindow(server_url)
    # Stash the startup id so _reveal_window (called once the boot
    # auth check has built the initial surface) can fire the KDE
    # _NET_STARTUP_INFO ClientMessage. Eager show + notify in main()
    # would race the deferred auth check and reveal a partially-
    # constructed window for one paint cycle.
    win._startup_id = _startup_id

    # When a duplicate launch attempt pings us, raise + activate the
    # window so the user sees the existing instance instead of confused
    # "did anything happen?" silence. Restore from minimize first so
    # show() actually surfaces it.
    def _raise_existing():
        if win.isMinimized():
            win.showNormal()
        win.show()
        win.raise_()
        win.activateWindow()

    app._single_instance.raise_requested.connect(_raise_existing)
    # Mini player and tray are pure widget construction (no I/O), so
    # they stay up-front — they don't add measurable launch cost.
    mini = FloatingMiniPlayer()
    # Same per-speaker group volume wiring as the main bar's volume btn.
    mini.set_cast_manager(win.cast_manager)
    # Pin to the window so _refresh_provider_refs() can update its
    # cached api reference after sign-out / provider-kind switch.
    # Without this the mini player keeps building stream + cover URLs
    # against the discarded singleton and silently 401s post-login.
    win.mini_player = mini
    bus.show_mini_player.connect(lambda: (mini.show(), mini.raise_(), mini.activateWindow()))
    bus.hide_mini_player.connect(mini.hide)
    # Pin the tray controller to the window so its lifetime tracks
    # `win` rather than relying on Qt's implicit parent-of-`app`
    # retention. Functionally equivalent (both pin past `app.exec()`),
    # but the named attribute reads as intentional rather than as a
    # dangling local.
    win.tray = TrayController(app, mini, win)

    # Dev-only remote-control bridge for live end-to-end testing. OFF
    # unless JT_TEST_BRIDGE=1 is set at launch. Stands up a per-user
    # local socket that evaluates Python on the GUI thread, so a test
    # harness can emit PlayerBus signals, call window methods, and read
    # back state — the deterministic control path on Wayland, where
    # synthetic pointer/key input is unreliable. Never enabled in a
    # shipped build. See jellytoast/test_bridge.py.
    if os.environ.get("JT_TEST_BRIDGE") == "1":
        from jellytoast.player_state import get_now_playing
        from jellytoast.test_bridge import TestBridge

        def _bridge_namespace():
            from PySide6.QtCore import QPoint, Qt
            from PySide6.QtTest import QTest

            import jellytoast.providers as _providers

            return {
                "app": app,
                "win": win,
                "bus": bus,
                "mini": mini,
                "settings": settings,
                "QApplication": QApplication,
                # In-process input: QTest posts internal Qt events (no
                # compositor), so QTest.mouseClick/keyClicks drive real
                # click handlers + hit-testing deterministically on
                # Wayland — the reliable real-interaction path.
                "QTest": QTest,
                "Qt": Qt,
                "QPoint": QPoint,
                "get_now_playing": get_now_playing,
                "get_settings": get_settings,
                "get_provider": _providers.get_provider,
                "cast": getattr(win, "cast_manager", None),
                "qm": getattr(win, "queue_mgr", None),
                "mpv": getattr(win, "mpv_ctrl", None),
            }

        app._test_bridge = TestBridge(app, _bridge_namespace)
        app._test_bridge.start()

    def _post_show_init():
        """Heavy startup work moved here so it runs after the window
        is visible. Order matters: mpv must exist before we wire the
        cast manager, and the volume signal must reach mpv after its
        slot is connected."""
        nonlocal mpv_ctrl, mpris
        mpv_ctrl = MpvController()
        mpv_ctrl.set_cast_manager(win.cast_manager)
        # Pin to the window so _refresh_provider_refs() can update its
        # cached api reference after a sign-out / kind switch — without
        # this, post-login playback would route stream-URL builds
        # through the discarded provider singleton.
        win.mpv_ctrl = mpv_ctrl
        bus.volume_changed.emit(settings.volume)

        # OS media-key / MPRIS integration always-on — the expected
        # behaviour on Linux desktops, and the only way the KDE/GNOME
        # media-control widget surfaces jellytoast.
        mpris = MediaControlsService()
        mpris.start()

        # Keep-above install (mini-player) is idempotent and lands
        # compositor-side any time — doesn't need to be live for first
        # paint. On platforms where Qt's WindowStaysOnTopHint already
        # works, the keep_above backend is a no-op.
        if settings.mini_player_keep_above:
            from jellytoast.keep_above import install_mini_player_rule

            install_mini_player_rule()

        # No-border rules: the mini player + settings dialog are
        # server-side-decorated on KDE Wayland (so KWin keeps their
        # blur alive while they're being dragged — frameless windows
        # lose it); this Force rule strips the visible decoration so
        # they still look frameless. Unconditional (not a user setting)
        # and a no-op off KDE Wayland. Idempotent — re-runs every
        # launch so it self-heals if the rule is ever dropped (e.g. by
        # the System Settings window-rule editor rewriting kwinrulesrc).
        from jellytoast.keep_above import (
            install_main_window_noborder,
            install_noborder_rules,
            remove_main_window_noborder,
        )

        install_noborder_rules()

        # Main window decoration: borderless by default (a KWin
        # `noborder` rule + jellytoast's own blended top bar), or KDE's
        # native server-side titlebar when the user opts into "Use
        # native window border". Reconciled here, before the window
        # maps, so a fresh launch never flashes the wrong chrome; the
        # setting itself takes effect on the next launch.
        if settings.native_window_border:
            remove_main_window_noborder()
        else:
            install_main_window_noborder()

        # Drag-repaint fix: install jellytoast's KWin scripted effect,
        # which forces KWin's full-repaint render path while one of the
        # app's windows is being dragged. That kills the stale-blur
        # "line artifact" KWin leaves on the NVIDIA EGL path (bug
        # 455526/457727). Idempotent, best-effort, a no-op off KDE
        # Wayland; JT_NO_DRAG_REPAINT=1 removes it instead.
        from jellytoast import drag_repaint

        drag_repaint.sync()

        # Open the downloads index (SQLite open + migrate) so the
        # context-menu "Download" action and, later, offline playback
        # have a live DB. Cheap, but deferred here with the rest of the
        # heavy init so it's off the first-paint path.
        from jellytoast import offline

        offline.init()

        # Bring the scrobble manager up — it subscribes to PlayerBus on
        # construction, so the first track that plays can be scrobbled
        # immediately. Drains any pending offline scrobbles from a prior
        # session in the same step.
        from jellytoast.scrobble import get_scrobble_manager, refresh_server_scrobble_flags

        get_scrobble_manager().flush_pending()
        # Re-run double-scrobble detection each launch (best-effort, async):
        # inspects the user's recent ListenBrainz listens for a second
        # scrobbler (the server, e.g. Navidrome) and gates the in-app
        # scrobbler off so the same listen isn't submitted twice. Runs on
        # boot — not just at login — so an existing session picks it up
        # without re-authenticating, and it self-corrects if the server's
        # scrobbling is later turned off.
        refresh_server_scrobble_flags()

    QTimer.singleShot(0, _post_show_init)

    # No eager win.show() — _do_boot_auth_check builds the initial
    # surface (home destination on success, login on failure) and
    # then calls self.show() via _reveal_window. That guarantees
    # first paint shows fully-populated content rather than the dark
    # → fade flicker the loading overlay used to mask.

    if settings.show_mini_on_start:
        mini.show()

    def _cleanup():
        # Hide windows FIRST so the user sees them vanish the instant
        # the terminal closes / shutdown signal arrives — Qt won't
        # repaint a hide until the event loop ticks again, and the
        # cast/mpv teardown below can take a few hundred ms. Without
        # this front-loaded hide the windows linger on screen for the
        # full duration of cleanup before disappearing.
        try:
            win.hide()
        except Exception:
            pass
        try:
            mini.hide()
        except Exception:
            pass
        # Visualizer subprocess (parec / pw-record) and its FFT worker
        # thread — fast-stop variant: skip the 1.0 s + 0.5 s subprocess
        # waits and the 2 s QThread.wait. The process group is dying
        # anyway, so the OS will reap any orphan; this trims up to
        # ~3.5 s off shutdown when the visualizer is active.
        try:
            np_page = getattr(win, "np_page", None)
            vis_engine = getattr(np_page, "_visualizer_engine", None) if np_page else None
            if vis_engine is not None:
                vis_engine.stop(fast=True)
        except Exception:
            pass
        # Stop the cast next. It's the only teardown step with an
        # external, user-visible effect — a Chromecast / AirPlay
        # receiver plays autonomously and keeps going on someone's
        # speakers until told to stop. Doing it before mpv / mpris
        # means that even if a later step hangs or throws, the cast
        # is already stopped.
        # Persist the in-flight eligible scrobble to disk synchronously
        # before teardown. The normal submit goes via run_async, which
        # can't complete during shutdown (the pool worker + the
        # GUI-thread result callback both die once the loop stops) — so
        # a track played past the threshold but quit before track-end was
        # lost. flush_current_on_quit writes it straight to the queue;
        # the next launch's flush_pending sends it. (No-op on the tray
        # path, which calls this itself before its stop.)
        try:
            from jellytoast.scrobble import get_scrobble_manager

            get_scrobble_manager().flush_current_on_quit()
        except Exception:
            pass
        # Flush any pending debounced queue save — see
        # `QueueManager._save` for the why. Runs first so the on-disk
        # queue.json reflects the user's final state even if a later
        # cleanup step throws.
        try:
            win.queue_mgr.flush_pending_save()
        except Exception:
            pass
        _shutdown_log("cleanup: stopping cast")
        try:
            win.cast_manager.cleanup()
            _shutdown_log("cleanup: cast stopped")
        except Exception as e:
            _shutdown_log(f"cleanup: cast stop FAILED — {e!r}")
        # mpv_ctrl / mpris are constructed in the deferred post-show
        # init; if the user closes before that fires they may still be
        # None. None-check before calling shutdown.
        if mpv_ctrl is not None:
            try:
                mpv_ctrl.shutdown()
            except Exception:
                pass
        if mpris is not None:
            try:
                mpris.stop()
            except Exception:
                pass
        _shutdown_log("cleanup: done")

    app.aboutToQuit.connect(_cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

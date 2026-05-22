"""
jellytoast — fully-native Linux desktop Jellyfin client.

Browse / search / suggestions / login / account: native PySide6 surfaces.
Playback engine: mpv via the existing PlayerBus.

The Jellyfin Web embed (QWebEngine) was retired once every user-visible
surface had a native replacement. The REST client (modules/jellyfin_api.py)
talks to the server directly; native auth (modules/login_view.py) calls
api.authenticate. No Chromium runtime, no JF Web shim, no URL interceptor.
"""

import os
import signal
import sys
from pathlib import Path

# libmpv requires LC_NUMERIC=C; Qt's setlocale() undoes Python-side fixes.
# Setting it before any libmpv / Qt import is enough — libmpv reads it
# lazily on first use. (We used to os.execve here for the same effect,
# but that doesn't exist on Windows; mutating os.environ is portable
# and works for our case because nothing has loaded mpv yet.)
os.environ.pop("LC_ALL", None)
os.environ["LC_NUMERIC"] = "C"
os.environ.setdefault("LANG", "C.UTF-8")

from modules.platform_compat import (  # noqa: E402
    IS_LINUX,
    is_kde_wayland,
    will_be_wayland,
)

# Native Wayland by default — Qt picks the platform from WAYLAND_DISPLAY
# / DISPLAY in the usual way. Set QT_QPA_PLATFORM=xcb in the environment
# to fall back to XWayland (escape hatch in case a Wayland regression
# bites). All X11-only code paths (cursor env bootstrap, startup-notify
# ClientMessage, off-screen positioning, taskbar-skip via xprop) are
# gated on the platform — see modules/platform_compat.py.
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

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Slot, QPoint
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter
from modules.design_tokens import RADIUS_WINDOW
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QMainWindow,
    QMessageBox,
    QSystemTrayIcon,
    QWidget,
    QVBoxLayout,
    QStackedLayout,
    QStackedWidget,
    QInputDialog,
    QLineEdit,
    QTextEdit,
)

from modules.player_state import (
    PlayerBus,
    get_now_playing,
    QueueContext,
    QueueKind,
)
from modules.player_backend import MpvController, MPV_AVAILABLE
from modules.queue_manager import QueueManager
from modules.now_playing_bar import NowPlayingBar, CastDialog
from modules.now_playing_page import NowPlayingPage
from modules.mini_player import FloatingMiniPlayer
from modules.tray import TrayController
from modules.media_controls import MediaControlsService
from modules.cast_manager import CastManager
from modules.top_bar import JtTopBar
from modules.settings_dialog import SettingsDialog
from modules.jellyfin_api import get_api
from modules.providers import get_provider
from modules.settings import get_settings
from modules.async_io import run_async
from modules.ui_helpers import (
    make_app_icon,
    GLOBAL_STYLE,
    BODY_COLOR,
)


# Per-intent / per-track-change diagnostics (URL, queue contents,
# cooldown deltas) are gated behind this. Install/skip/error lines stay
# unconditional so a post-mortem from the terminal alone is still possible.
_SHUFFLE_DEBUG = os.environ.get("JT_SHUFFLE_DEBUG") == "1"

# Same shape for the AirPlay 2 / pyatv pairing tracing — noisy by
# default during the LG-webOS pairing investigation, off in normal use.
_AP2_DBG = os.environ.get("JT_AP2_DEBUG") == "1"


def _ap2_dbg(msg: str) -> None:
    if _AP2_DBG:
        print(f"[ap2-dbg] {msg}", flush=True)


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
            for w in QApplication.allWidgets():
                if getattr(w, "_keyboard_mode", False):
                    w._keyboard_mode = False
                    vp = getattr(w, "viewport", None)
                    if callable(vp):
                        vp().update()
        return False


class _ToolTipFilter(QObject):
    """Swallows hover tooltips app-wide when the user has turned them
    off in Settings. Tooltips are set per-widget via setToolTip(), so
    there's no global on/off switch in Qt — but every tooltip is
    delivered as a QEvent.ToolTip first, and eating that event before
    it reaches the widget suppresses the popup. The setting is read per
    event (ToolTip events are rare — one per ~700ms hover-pause) so the
    toggle applies live with no restart and no extra wiring."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ToolTip:
            if not get_settings().show_tooltips:
                return True  # consume — no tooltip shown
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


class JellytoastWindow(QMainWindow):
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
        if not _OPAQUE_BODY:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        if _OPAQUE_BODY:
            print(
                "[jellytoast] JT_OPAQUE=1: skipping WA_TranslucentBackground "
                "on the main window (streaming-flicker diagnostic).",
                file=sys.stderr,
            )
        # Build the body fill QColor once: in opaque mode force alpha to
        # 255 so the body has no compositor blending to grab mid-paint;
        # otherwise honour the theme palette's RGBA tuple.
        if _OPAQUE_BODY:
            self._body_qcolor = QColor(BODY_COLOR[0], BODY_COLOR[1], BODY_COLOR[2], 255)
        else:
            self._body_qcolor = QColor(*BODY_COLOR)

        # Borderless mode: on KDE Wayland, unless the user opted into a
        # native window border, the main window runs under a KWin
        # `noborder` rule (installed at boot) and draws its own rounded
        # body + blended top-bar titlebar + edge resize zones. Off KDE
        # Wayland — or with native borders on — KWin owns the chrome and
        # paintEvent fills a plain rect.
        self._borderless = is_kde_wayland() and not get_settings().native_window_border

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

        # Stacked layout: the chrome (titlebar + top bar + view + np
        # bar) sits underneath a full-window loading overlay used
        # during the deferred boot auth check (see __init__ end and
        # _do_boot_auth_check) so the LoginView never paints for one
        # frame before route_home swaps the active surface.
        central_stack = QStackedLayout(central)
        central_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        central_stack.setContentsMargins(0, 0, 0, 0)

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
        self.bus.playback_started.connect(lambda np: self.bus.notify_track.emit(np))
        # Live-apply for global stylesheet bits (QCheckBox indicator
        # background, QComboBox dropdown items, etc.) when the color
        # editor fires theme_changed. The accent picker's own
        # _on_accent_picked already runs an equivalent cascade; this
        # hook makes the Colors page slider drag behave identically.
        self.bus.theme_changed.connect(self._cascade_global_style)
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
        # opt-in Ctrl+Shift+A) live in modules.hotkeys. The registry
        # there is the single source of truth and the future Settings
        # → Hotkeys page consumes the same list. We hold the returned
        # QShortcut refs on self so Qt doesn't GC them mid-session.
        from modules import hotkeys as _hotkeys

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
        # Hover-tooltip suppression — gated on the show_tooltips setting,
        # applied live (see _ToolTipFilter).
        self._tooltip_filter = _ToolTipFilter(self)
        QApplication.instance().installEventFilter(self._tooltip_filter)
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
        from modules.login_view import LoginView

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

    def _do_boot_auth_check(self):
        """Run the boot-time `is_authenticated` check after the event
        loop is alive — see the deferral comment in __init__. Builds
        the right initial surface (home destination on success, login
        on failure) and *then* shows the window so first paint is
        already populated."""
        if not self.provider.is_authenticated:
            # No credentials — any view-cache JSON left over from a
            # prior signed-in session would render as ghost data on
            # an unauthenticated app (playlists, genres, etc. served
            # straight from disk before the network fetch could
            # fail-401). Drop the cache here so the user lands on
            # empty states instead of stale rows.
            try:
                from modules import disk_cache as _disk_cache

                _disk_cache.clear_all()
            except Exception:
                pass
            self.content_stack.setCurrentWidget(self.login_view)
            self._reveal_window()
            return
        # We have a persisted token from a previous session. Verify
        # it's still valid against the server (the device session may
        # have been revoked by an admin). If it fails, fall back to
        # the LoginView. The verify is async because verify_session
        # is a network call.
        run_async(
            self.provider.verify_session,
            on_result=self._on_verify_session_done,
            on_error=lambda _e: self._on_verify_session_done(False),
        )
        # Render the user's home destination immediately — verify
        # runs in the background; if it fails, _on_verify_session_done
        # swaps to the LoginView.
        self._route_home()
        self._reveal_window()

    def _reveal_window(self):
        """Show the window now that the initial surface has been
        chosen. Idempotent so the verify-session failure path can
        safely call this even though the success path already did."""
        if self.isVisible():
            return
        self.show()
        # Apply compositor blur once the window has a mapped surface
        # (deferred a tick past show()).
        QTimer.singleShot(0, self._apply_blur)
        # Tell KDE the launch is complete so the taskbar entry stops
        # bouncing and transitions from 'launching' to active. The
        # startup id was stashed by main() during construction.
        startup_id = getattr(self, "_startup_id", "")
        if startup_id:
            _send_startup_notification_remove(startup_id)

    def _apply_blur(self):
        """Ask the compositor to blur behind the window when the active
        theme is frosted (blurred glass). Silent no-op where the
        compositor / platform has no blur support.

        Borderless: shape the blur region to the rounded body so it
        doesn't bleed past the corners — squared while maximized to
        match paintEvent. Native-border / non-KDE: KWin clips to its
        own decoration, so a plain rectangle (radius 0) is correct."""
        from modules import blur
        from modules.theme import get_active_theme

        radius = (
            RADIUS_WINDOW
            if self._borderless and not self.isMaximized()
            else 0
        )
        blur.apply(self, get_active_theme().blur, radius)

    def _on_nav_requested(self, action: str):
        # Back / forward walk the jellytoast surface history — every
        # _show_* push is captured in _nav_history.
        if action == "back":
            self._go_back()
            return
        if action == "forward":
            self._go_forward()
            return
        if action == "search":
            self._show_search_view()
            return
        # Home routes to whichever native music surface the user picked
        # in Settings → General → "When Home is pressed, open:". Default
        # is the Albums grid — the canonical music landing.
        if action == "home":
            self._route_home()
            return

    def _on_tab_requested(self, index: int, label: str):
        # Tab dropdown is only populated with the music collection's
        # tabs (the only collection the native chrome ever shows), so
        # every label here maps to a native surface. Unknown labels
        # fall through silently rather than navigating somewhere
        # surprising.
        lab = label.lower()
        if lab == "albums":
            self._show_native_music_grid("album")
        elif lab == "playlists":
            self._show_native_music_grid("playlist")
        elif lab == "smart playlists":
            self._show_smart_playlists_view()
        elif lab in ("artists", "album artists"):
            self._show_native_music_grid("artist")
        elif lab == "songs":
            self._show_songs_view()
        elif lab == "genres":
            self._show_genres_view()
        elif lab == "suggestions":
            self._show_suggestions_view()
        elif lab == "downloads":
            self._show_downloads_library_view()
        elif lab == "radio":
            self._show_radio_view()
        else:
            return
        self.top_bar.set_active_tab(label)

    def _show_downloads_library_view(self):
        """Lazy-build + swap to the standalone Downloads page that
        lists every explicitly-downloaded item. Lives in the same
        ``content_stack`` as the album / artist grids; reusing the
        stack means back/forward navigation Just Works."""
        view = getattr(self, "_downloads_lib_view", None)
        if view is None:
            from modules.downloads_library_view import DownloadsLibraryView

            view = DownloadsLibraryView()
            self._downloads_lib_view = view
            self.content_stack.addWidget(view)
        else:
            view.reload()
        self.content_stack.setCurrentWidget(view)
        self.top_bar.set_library_controls_visible(False)

    def _show_smart_playlists_view(self):
        """Lazy-build + swap to the saved-smart-playlists page. Rules
        resolve to tracks through the active provider's ``query_items``
        on Play; the result installs a regular PLAYLIST queue so all
        existing now-playing UI works without surgery."""
        view = getattr(self, "_smart_playlists_view", None)
        if view is None:
            from modules.smart_playlists_view import SmartPlaylistsView

            view = SmartPlaylistsView(self)
            self._smart_playlists_view = view
            self.content_stack.addWidget(view)
        else:
            view.reload()
        self.content_stack.setCurrentWidget(view)
        self.top_bar.set_library_controls_visible(False)

    def _show_radio_view(self):
        """Lazy-build + swap to the standalone Radio page that lists
        every internet-radio station from the active provider. CRUD
        rides the provider abstraction; clicking a row installs a
        single-item INTERNET_RADIO queue."""
        view = getattr(self, "_radio_view", None)
        if view is None:
            from modules.radio_view import RadioView

            view = RadioView(self.queue_mgr)
            self._radio_view = view
            self.content_stack.addWidget(view)
        else:
            view.reload()
        self.content_stack.setCurrentWidget(view)
        self.top_bar.set_library_controls_visible(False)

    def _show_native_music_grid(self, kind: str = "album"):
        """Lazy-build + swap to a native LibraryGrid for the music
        library context. Albums + artists scope to the music library's
        parent_id (Recursive=True walks its tree); playlists fetch with
        empty parent_id because Jellyfin stores playlists as standalone
        items outside any library — scoping by music_lib_id would
        return nothing."""
        if kind == "playlist":
            parent_id = ""
        else:
            parent_id = self._resolve_library_id("music")
        self._show_library_grid(kind, parent_id)

    def paintEvent(self, e):
        # Fill the body with `_body_qcolor` (computed in __init__ — full
        # alpha in JT_OPAQUE=1 mode so there's no compositor blend for
        # Sunshine's screencopy to race, theme-alpha otherwise).
        #
        # Borderless: KWin draws no decoration, so we round the body
        # ourselves at the host-OS radius — squared while maximized so
        # it sits flush. Native-border / non-KDE: KWin owns the corner
        # radius, so a plain rect fill is correct.
        p = QPainter(self)
        try:
            if self._borderless:
                radius = 0 if self.isMaximized() else RADIUS_WINDOW
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
                from modules.player_state import PlayerBus as _PB

                _PB.get().dpr_changed.emit()
            except Exception as exc:
                print(f"[jellytoast] dpr_changed emit failed: {exc}", file=sys.stderr)
        elif getattr(self, "_borderless", False) and (
            e.type() == _QEvent.Type.WindowStateChange
        ):
            # Maximize / restore flips the corner radius (squared when
            # maximized so the body sits flush against the screen edges)
            # — repaint so paintEvent re-evaluates it, and re-shape the
            # blur region to match. `getattr` guards the early
            # WindowTitleChange that setWindowTitle() fires before
            # __init__ has assigned `_borderless`.
            self.update()
            self._apply_blur()
        super().changeEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Borderless: the rounded blur region is sized to the window —
        # re-shape it once the resize settles (debounced).
        if getattr(self, "_borderless", False) and hasattr(self, "_blur_settle"):
            self._blur_settle.start()

    # Space-to-play is wired through an application-wide event
    # filter (see _SpacePlayFilter) installed in __init__. A plain
    # keyPressEvent override on the window only fires when focus is
    # on the window itself — child widgets like QListView consume
    # Space for their own purposes (selection toggle, page-down)
    # before it ever reaches us. An app-level filter intercepts
    # the key press before any widget can swallow it.

    def _resolve_library_id(self, collection_type: str) -> str:
        # Only return the cache when it actually resolved to an id —
        # caching an empty string would poison the lookup if the very
        # first call landed before credentials were bridged (the
        # request would 401 and we'd remember "" forever, even after
        # auth becomes available).
        cached = self._library_ids.get(collection_type)
        if cached:
            return cached
        try:
            libs = self.provider.get_libraries()
            match = next(
                (lib for lib in libs if lib.get("CollectionType") == collection_type), None
            )
            lib_id = match.get("Id") if match else ""
        except Exception as e:
            print(f"[jellytoast] couldn't resolve {collection_type} library: {e}", flush=True)
            lib_id = ""
        if lib_id:
            self._library_ids[collection_type] = lib_id
        return lib_id or ""

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

        # parent=None so KWin treats Settings as an independent top-
        # level. Passing ``self`` would establish a transient-for
        # relationship under Wayland which keeps the dialog pinned
        # above its parent (clicking the main window can't raise it).
        # The Python reference on ``self._settings_dlg`` keeps the
        # object alive without Qt parenting.
        dlg = SettingsDialog(None)
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
        dlg.show()

    def _on_settings_closed(self, _result=0):
        self._settings_dlg = None

    def _retry_empty_native_views(self):
        """Re-trigger the load for any native surface that exists but
        has no items. Called after fresh credentials arrive — the
        prior fetch likely 401'd against a stale persisted token."""
        if self.songs_view is not None and not self.songs_view._items:
            self.songs_view.load_songs(self._resolve_library_id("music"))
        if self.suggestions_view is not None:
            # SuggestionsView always reloads cleanly (rails handle
            # empty payloads themselves).
            self.suggestions_view.load(self._resolve_library_id("music"))
        if self.album_grid is not None and not self.album_grid._tiles:
            self.album_grid.load_items(self._resolve_library_id("music"), "")
        if self.playlist_grid is not None and not self.playlist_grid._tiles:
            self.playlist_grid.load_items("", "")
        if self.artist_grid is not None and not self.artist_grid._tiles:
            self.artist_grid.load_items(self._resolve_library_id("music"), "")
        if self.genres_view is not None and not self.genres_view._tiles:
            self.genres_view.load_genres()

    def _heartbeat(self):
        """Periodic no-op GET to keep QNAM's TCP+TLS connection to the
        server warm. Cheap (50-byte response), silent on failure (the
        keepalive is best-effort — if it fails, the next real request
        will just pay the handshake cost it would've paid anyway).
        Provider may not implement keep_alive_url yet — guarded so
        older builds don't crash the timer."""
        try:
            url = self.provider.keep_alive_url()
        except AttributeError:
            return
        if not url:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkRequest
        from modules.async_io import get_qnam

        req = QNetworkRequest(QUrl(url))
        req.setTransferTimeout(5000)
        reply = get_qnam().get(req)
        # Drain the response so the connection is freed back to the
        # pool — without finished+deleteLater Qt eventually GCs the
        # reply but holds the socket longer than needed.
        reply.finished.connect(reply.deleteLater)

    def _refresh_provider_refs(self):
        """Re-read the active provider singleton and push it to every
        widget that cached a reference at construction time. Required
        whenever ``reset_provider()`` runs (sign-out, server kind
        switch in LoginView) — without it, surfaces built under the
        previous provider keep dispatching against the discarded
        instance and silently 401, so the user sees an empty grid
        until they restart the app."""
        from modules.providers import get_provider as _gp

        self.provider = _gp()
        for w in (
            self.queue_mgr,
            self.np_bar,
            self.album_grid,
            self.playlist_grid,
            self.artist_grid,
            self.songs_view,
            self.genres_view,
            self.suggestions_view,
            self.search_view,
            self.np_page,
            self.artist_page,
            getattr(self, "mpv_ctrl", None),
        ):
            if w is not None:
                w.api = self.provider

    def _on_sign_out_requested(self):
        # Halt playback first — without this, mpv keeps streaming the
        # current track using the credentials we're about to revoke,
        # the bottom now-playing bar keeps showing the previous user's
        # track, and any next-track advance hits 401. stop_requested
        # makes player_backend stop mpv (which then emits
        # playback_stopped, clearing the bar's cover/title to "Nothing
        # playing"); queue_clear empties the queue so a future sign-in
        # doesn't restore the prior user's queue.
        self.bus.stop_requested.emit()
        self.bus.queue_clear.emit()
        # Tell the server to revoke this device's session BEFORE we
        # clear the token locally — without this the row lingers in
        # the admin Devices dashboard until the user manually deletes
        # it. Synchronous (5s timeout, errors swallowed inside
        # server_logout) so we know the call completed before tearing
        # down credentials.
        self.provider.server_logout()
        settings = get_settings()
        settings.access_token = ""
        settings.user_id = ""
        settings.username = ""
        # Rebuild the provider singleton so its in-memory credential
        # state matches the now-cleared settings — without this the
        # SubsonicProvider would still return is_authenticated=True
        # from cached _username + _password fields. JellyfinAPI's
        # credentials get cleared too because get_api()'s singleton
        # re-reads settings on next access through any new code path.
        from modules.providers import reset_provider

        try:
            self.api.token = ""
            self.api.user_id = ""
        except Exception:
            pass
        reset_provider()
        self._refresh_provider_refs()
        # Drop any cached library ids resolved against the old user.
        self._library_ids = {}
        # Wipe the cover-art disk cache: the next user / server may
        # have different artwork for items that happen to share an
        # id (Subsonic IDs are short strings; collisions are realistic
        # across servers).
        from modules import image_cache as _img_cache

        _img_cache.clear()
        # Also drop the in-memory pixmap / raw-image caches in
        # ui_helpers — same id-collision risk, same fix.
        from modules.ui_helpers import clear_image_caches

        clear_image_caches()
        # Wipe view-cache JSON blobs (library_*, genres, songs,
        # suggestions_*, preview). Without this, navigating to
        # Playlists / Genres / Songs while signed out would render
        # the previous session's data from cache instead of the
        # "no items" empty state.
        from modules import disk_cache as _disk_cache

        _disk_cache.clear_all()
        # Force every lazy-built native view to drop its in-memory
        # model so the next visit re-fetches from the (now empty)
        # cache + the (now unauthenticated) server, landing in the
        # empty state instead of showing the previous session's
        # rows that were last set into the model.
        for surface in (
            self.album_grid,
            self.playlist_grid,
            self.artist_grid,
            self.songs_view,
            self.genres_view,
            self.suggestions_view,
        ):
            if surface is None:
                continue
            try:
                surface._clear()
            except AttributeError:
                pass
        # Show the native sign-in surface.
        self.content_stack.setCurrentWidget(self.login_view)

    def _on_server_change_requested(self):
        current = self.provider.server_url
        url, ok = QInputDialog.getText(
            self,
            "jellytoast — Server URL",
            "Enter your music server URL:",
            text=current or "http://",
        )
        if not ok or not url.strip():
            return
        new_url = url.strip().rstrip("/")
        if new_url == current:
            return
        get_settings().server_url = new_url
        # Switching servers means the old auth is invalid; clear it
        # and fall back to LoginView (now pre-filled with the new URL).
        self._on_sign_out_requested()

    def _library_shuffle(self):
        # Re-entry guard so a rapid double-click of the shuffle button
        # doesn't kick off two parallel REST fetches and two competing
        # queue installs.
        if self._shuffle_in_flight:
            print(
                "[jellytoast] library shuffle skipped — already in flight",
                flush=True,
            )
            return
        self._shuffle_in_flight = True

        # Fast path: a pre-fetched random queue is sitting in the cache.
        # Emit it immediately, then refill the cache in the background.
        if self._random_queue_cache:
            items = self._random_queue_cache
            self._random_queue_cache = []
            self._install_shuffle_queue(items, "library shuffle (cached)")
            self._prime_random_queue_async()
            self._shuffle_in_flight = False
            return

        lib_id = self._resolve_library_id("music")
        if not lib_id:
            print("[jellytoast] no music library resolved; skipping library shuffle", flush=True)
            self._shuffle_in_flight = False
            return
        # Cache miss — fetch on the shared QThreadPool so the GUI
        # doesn't freeze while the random items load. Limit comes from
        # Settings (default 100) — smaller queues commit faster after
        # a drag-reorder since _populate_rows rebuilds every row.
        shuffle_n = get_settings().shuffle_queue_size
        run_async(
            self.provider.get_random_audio_items,
            lib_id,
            limit=shuffle_n,
            on_result=self._on_library_shuffle_loaded,
            on_error=self._on_library_shuffle_error,
        )

    def _on_library_shuffle_loaded(self, items):
        try:
            if not items:
                print("[jellytoast] library shuffle: API returned no tracks", flush=True)
                return
            self._install_shuffle_queue(items, "library shuffle")
            # Prime the cache for the next click while we're already
            # warmed up (lib_id resolved, API connection live).
            self._prime_random_queue_async()
        finally:
            self._shuffle_in_flight = False

    def _on_library_shuffle_error(self, e):
        print(f"[jellytoast] library shuffle fetch failed: {e}", flush=True)
        self._shuffle_in_flight = False

    def _install_shuffle_queue(self, items: list, source_label: str):
        """Install a randomly-ordered library queue and start it. The
        log line gives shuffle diagnostics (item count, unique album
        count) so the per-intent debugging picture stays readable when
        JT_SHUFFLE_DEBUG is on."""
        from modules.player_state import PlayerBus

        unique_albums = {it.get("AlbumId") for it in items if it.get("AlbumId")}
        print(
            f"[jellytoast] queue set via {source_label}: {len(items)} items, "
            f"{len(unique_albums)} unique albums, start=0",
            flush=True,
        )
        ctx = QueueContext(kind=QueueKind.SHUFFLE, source_label="Library shuffle")
        PlayerBus.get().queue_play_now.emit(items, 0, ctx)

    def _prime_random_queue_async(self):
        """Refresh the pre-fetched random queue in the background.
        No-ops if a cache already exists or no music library is known."""
        if self._random_queue_cache:
            return
        lib_id = self._resolve_library_id("music")
        if not lib_id:
            return
        shuffle_n = get_settings().shuffle_queue_size
        run_async(
            self.provider.get_random_audio_items,
            lib_id,
            limit=shuffle_n,
            on_result=self._on_prime_random_queue_loaded,
            on_error=lambda e: print(
                f"[jellytoast] prime random queue failed: {e}",
                flush=True,
            ),
        )

    def _on_prime_random_queue_loaded(self, items):
        if items:
            self._random_queue_cache = items
            print(
                f"[jellytoast] random queue cache primed: {len(items)} items",
                flush=True,
            )

    @Slot()
    def _show_self(self):
        # Drop the minimized bit before showing — show() alone won't un-iconify
        # a window that was minimized to the taskbar.
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def _browse_album(self, album_id: str):
        """Route an album-tile / album-label click. If the clicked
        album is the one currently driving the live queue (queue
        context kind == ALBUM and source_id matches), jump straight to
        the live now-playing page — that's where the user is already
        listening from, so the preview/browse mode would just hide
        the live state. Any other album opens in preview mode.
        """
        ctx = self.queue_mgr.context
        same_album = (
            ctx.kind == QueueKind.ALBUM
            and (ctx.source_id or "").lower() == (album_id or "").lower()
        )
        if same_album and (album_id or ""):
            self._show_now_playing()
        else:
            self._show_now_playing(
                preview_id=album_id,
                preview_kind="album",
            )

    def _browse_playlist(self, playlist_id: str):
        """Route a playlist-tile click. Mirror _browse_album's logic:
        if the clicked playlist is currently driving the live queue,
        drop straight into the now-playing view instead of opening a
        redundant preview."""
        ctx = self.queue_mgr.context
        same_playlist = (
            ctx.kind == QueueKind.PLAYLIST
            and (ctx.source_id or "").lower() == (playlist_id or "").lower()
        )
        if same_playlist and (playlist_id or ""):
            self._show_now_playing()
        else:
            self._show_now_playing(
                preview_id=playlist_id,
                preview_kind="playlist",
            )

    def _show_now_playing(self, preview_id: str = "", preview_kind: str = "album"):
        # Lazy-build on first open. From the second open onward this is
        # just a stack flip; the page subscribes to the bus continuously
        # once it exists, so it stays in sync.
        if self.np_page is None:
            self.np_page = NowPlayingPage(self.queue_mgr, self)
            self.np_page.dismiss_requested.connect(self._dismiss_now_playing)
            # Bottom-bar left cluster (cover + title + artist + heart)
            # follows the page's preview state inversely: visible while
            # previewing (so the currently-playing track stays surfaced
            # in the bottom while the user browses), hidden in live mode
            # (the page itself shows the active track in large).
            self.np_page.preview_changed.connect(
                lambda is_preview: self.np_bar.set_left_cluster_visible(is_preview)
            )

            # Top-bar dropdown follows the preview state too — when the
            # user clicks a track in a previewed album, _on_row_clicked
            # clears the preview flag and starts the queue; the label
            # needs to flip from "Browsing" to "Now Playing" so the
            # nav state is honest.
            def _sync_top_bar_label(is_preview, _self=self):
                if _self.content_stack.currentWidget() is _self.np_page:
                    _self.top_bar.set_now_playing_mode(
                        True,
                        label=("Browsing" if is_preview else "Now Playing"),
                    )

            self.np_page.preview_changed.connect(_sync_top_bar_label)
            self.content_stack.addWidget(self.np_page)
        # preview_id != "" → browse mode (preview an album/playlist
        # without disturbing the live queue). Empty → live mode.
        if preview_id:
            self.np_page.load_preview(preview_id, preview_kind)
        else:
            self.np_page.clear_preview()
        self.content_stack.setCurrentWidget(self.np_page)
        # Top-bar dropdown reflects whether the user is in live
        # playback ("Now Playing") or previewing another album /
        # playlist ("Browsing"). Both modes show the same chevron menu
        # so the user can navigate away without using the back button.
        nav_label = "Browsing" if preview_id else "Now Playing"
        self.top_bar.set_now_playing_mode(True, label=nav_label)
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda pid=preview_id, pk=preview_kind: self._show_now_playing(pid, pk))

    def _on_content_changed(self, _idx: int):
        """Sync top-bar mode with the visible content surface. The
        np_page splits into "Now Playing" (live playback) and
        "Browsing" (preview of an album / playlist that isn't the
        active queue) — surfaced via the dropdown label so the user
        always knows which mode they're in."""
        if self.np_page is None:
            self.top_bar.set_now_playing_mode(False)
            return
        on_np = self.content_stack.currentWidget() is self.np_page
        if not on_np:
            self.top_bar.set_now_playing_mode(False)
            return
        label = "Browsing" if self.np_page._preview_id else "Now Playing"
        self.top_bar.set_now_playing_mode(True, label=label)

    def _dismiss_now_playing(self):
        """Back button on NowPlayingPage — walks the unified nav
        history. Falls back to the home destination if there's nothing
        earlier to return to (only happens at app launch with no
        other surface recorded yet)."""
        if not self._go_back():
            self._route_home()

    def _show_library_grid(
        self, kind: str, parent_id: str = "", genre_id: str = "", year: str = ""
    ):
        """Lazy-build + swap to a native LibraryGrid of the given kind.
        Browse clicks route to NowPlayingPage(preview, kind) for
        playable items, or the ArtistPage for artist tiles; play-
        overlay clicks install the item as the live queue and start it.

        `genre_id` filters the grid to a single genre (Jellyfin's
        ?GenreIds= param) — used by the Genres view's tile-click path
        to drop the user into an album grid scoped by genre."""
        from modules.library_grid import LibraryGrid

        if kind == "playlist":
            if self.playlist_grid is None:
                self.playlist_grid = LibraryGrid(kind="playlist", parent=self)
                self.playlist_grid.browse_requested.connect(self._browse_playlist)
                self.playlist_grid.play_requested.connect(self._on_grid_play_playlist)
                self.content_stack.addWidget(self.playlist_grid)
            grid = self.playlist_grid
        elif kind == "artist":
            if self.artist_grid is None:
                self.artist_grid = LibraryGrid(kind="artist", parent=self)
                # Artist tiles open the dedicated ArtistPage instead
                # of NowPlayingPage's preview — "browse this artist"
                # means see all their albums, not preview a specific
                # collection of tracks.
                self.artist_grid.browse_requested.connect(self._show_artist_page)
                # play_requested is wired but the tile suppresses the
                # play-overlay for kind="artist" (no canonical "play
                # an artist" action — they pick an album from the page).
                self.content_stack.addWidget(self.artist_grid)
            grid = self.artist_grid
        else:
            if self.album_grid is None:
                self.album_grid = LibraryGrid(kind="album", parent=self)
                self.album_grid.browse_requested.connect(self._browse_album)
                self.album_grid.play_requested.connect(self._on_grid_play_album)
                # Subtitle-click on an album tile → ArtistPage. Year-
                # click → re-load the album grid filtered to that year.
                self.album_grid.artist_browse_requested.connect(self._show_artist_page)
                self.album_grid.year_browse_requested.connect(self._show_albums_by_year)
                self.content_stack.addWidget(self.album_grid)
            grid = self.album_grid

        # Re-fetch when scoping changes (parent_id / genre_id / year)
        # — otherwise reuse the loaded tiles to avoid thrashing covers
        # when the user toggles back to the grid from another view.
        prev_year = getattr(grid, "_year", "")
        if (
            not grid._tiles
            or grid._parent_id != parent_id
            or grid._genre_id != genre_id
            or prev_year != year
        ):
            grid.load_items(parent_id, genre_id, year)
        self.content_stack.setCurrentWidget(grid)
        # Deliberately do NOT auto-setFocus on the grid here — that
        # would light up the first album's focus ring on every
        # mouse navigation. The grid's focusProxy makes Tab into
        # the content section land on the inner view, so keyboard
        # users still get the ring when they ask for it.
        # The grid is its own browse surface — no need to also surface
        # the bottom-left now-playing cluster since the grid IS the
        # browsing context. Show it so the user can still see what's
        # playing while they browse.
        self.np_bar.set_left_cluster_visible(True)
        # Surface the library controls (Shuffle / View / Sort) cluster
        # in the top bar — they apply to the native grid only.
        self.top_bar.set_library_controls_visible(True)
        self._push_nav(
            lambda k=kind, pid=parent_id, gid=genre_id, y=year: self._show_library_grid(
                k, pid, gid, y
            )
        )

    def _on_library_sort_changed(self, sort_by: str, sort_order: str):
        # Apply to whichever native surface honors sort and is currently
        # visible. Genres view has no sort (no album-style metadata to
        # sort by).
        current = self.content_stack.currentWidget()
        sortables = (
            self.album_grid,
            self.playlist_grid,
            self.artist_grid,
            self.songs_view,
        )
        for surface in sortables:
            if surface is not None and surface is current:
                surface.set_sort(sort_by, sort_order)
                return

    def _show_songs_view(self):
        """Lazy-build + swap to the native Songs list view."""
        if self.songs_view is None:
            from modules.songs_view import SongsView

            self.songs_view = SongsView(self)
            self.songs_view.play_requested.connect(self._on_songs_play_requested)
            self.songs_view.album_browse_requested.connect(self._browse_album)
            self.content_stack.addWidget(self.songs_view)
            self._kick_load_when_ready(
                lambda: self.songs_view.load_songs(self._resolve_library_id("music"))
            )
        self.content_stack.setCurrentWidget(self.songs_view)
        self.np_bar.set_left_cluster_visible(True)
        # Sort applies to songs; shuffle/view-toggle don't (yet).
        self.top_bar.set_library_controls_visible(True)
        self._push_nav(lambda: self._show_songs_view())

    def _on_songs_play_requested(self, start_idx: int, items: list):
        """Songs view row click → install the visible song list as the
        live queue and start at the clicked index. The QueueContext is
        MANUAL since this isn't an album/playlist/artist source — the
        user is browsing flat tracks."""
        if not items or not (0 <= start_idx < len(items)):
            return
        from modules.player_state import PlayerBus

        ctx = QueueContext(kind=QueueKind.MANUAL, source_label="Songs")
        PlayerBus.get().queue_play_now.emit(list(items), start_idx, ctx)

    def _show_genres_view(self):
        """Lazy-build + swap to the native Genres grid."""
        if self.genres_view is None:
            from modules.genres_view import GenresView

            self.genres_view = GenresView(self)
            self.genres_view.genre_selected.connect(self._on_genre_selected)
            self.content_stack.addWidget(self.genres_view)
            self.genres_view.load_genres()
        self.content_stack.setCurrentWidget(self.genres_view)
        self.np_bar.set_left_cluster_visible(True)
        # Genres don't have a meaningful sort axis; hide the cluster.
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda: self._show_genres_view())

    def _show_suggestions_view(self):
        """Lazy-build + swap to the native Suggestions ("Discover")
        view. Three album rails (Latest / Recently played / Frequently
        played); tile clicks reuse the same browse + play paths as the
        main album grid."""
        if self.suggestions_view is None:
            from modules.suggestions_view import SuggestionsView

            self.suggestions_view = SuggestionsView(self)
            self.suggestions_view.browse_requested.connect(self._browse_album)
            self.suggestions_view.play_requested.connect(self._on_grid_play_album)
            self.suggestions_view.artist_browse_requested.connect(self._show_artist_page)
            self.content_stack.addWidget(self.suggestions_view)
            self._kick_load_when_ready(
                lambda: self.suggestions_view.load(self._resolve_library_id("music"))
            )
        self.content_stack.setCurrentWidget(self.suggestions_view)

        # Qt's auto-focus on a freshly-shown stacked-widget page can
        # land on a rail view inside suggestions, painting the focus
        # ring on the first album at launch ("the app picked an album
        # for you"). The transfer happens deferred — on the next
        # event-loop tick, AFTER setCurrentWidget returns — so a
        # synchronous clearFocus here is too early. Queue it on
        # singleShot(0) instead so we run AFTER Qt has done its thing.
        def _drop_initial_focus(_self=self):
            focused = QApplication.focusWidget()
            if (
                focused is not None
                and _self.suggestions_view is not None
                and _self.suggestions_view.isAncestorOf(focused)
            ):
                focused.clearFocus()

        QTimer.singleShot(0, _drop_initial_focus)
        self.np_bar.set_left_cluster_visible(True)
        # Suggestions is a curated surface — sort/view-toggle controls
        # don't apply, so hide the top-bar cluster.
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda: self._show_suggestions_view())

    @Slot()
    def _cascade_global_style(self):
        """Re-stamp the app-wide stylesheet + repolish indicator-style
        widgets on PlayerBus.theme_changed. The Colors page emits
        theme_changed from its slider drags but doesn't have the
        accent picker's full cascade — without this, QCheckBox
        indicator backgrounds + QComboBox dropdown items stay on the
        old accent until the user reopens the dialog or restarts.

        Idempotent + cheap; safe to run on every theme_changed."""
        from modules import ui_helpers as _uih
        from modules import icons as _icons

        # 1. Rebuild GLOBAL_STYLE from the (already-refreshed) token
        # constants and push it onto QApplication. The emitter
        # (_on_accent_picked / _on_theme_changed / color_tokens) is
        # contractually required to have mutated the ui_helpers tokens
        # *before* emitting theme_changed, so _build_global_style here
        # reads fresh values — re-running refresh_theme would be a
        # redundant QSettings round-trip.
        # Clear-then-set forces Qt to invalidate the QSS evaluation
        # cache — setting the same property value as before can be
        # silently no-op'd, so dropping to "" first guarantees the
        # next set is treated as a real change. Without this, KDE
        # Fusion was caching the old QCheckBox indicator pixmap and
        # the new ACCENT_DEEP background didn't paint until the
        # widget was unpolished individually.
        new_global_style = _uih._build_global_style()
        _uih.GLOBAL_STYLE = new_global_style
        _icons.refresh_theme()
        app = QApplication.instance()
        if app is None:
            return
        app.setStyleSheet("")
        app.setStyleSheet(new_global_style)
        # 2. Refresh the app palette's Highlight role so Qt-style-painted
        # selections (text selection background, QListView highlights)
        # pick up the new accent.
        try:
            from PySide6.QtGui import QPalette
            from modules.theme import _hex_to_rgb as _h2r

            ar, ag, ab = _h2r(_uih.ACCENT)
            pal = app.palette()
            pal.setColor(QPalette.ColorRole.Highlight, QColor(ar, ag, ab))
            pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
            app.setPalette(pal)
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
        from modules.ui_helpers import (
            ACCENT as _ACC,
            BORDER as _BORDER,
            check_url_for_accent as _check_url_fn,
            ink_alpha,
            _hex_to_rgb_safe,
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
        for w in app.allWidgets():
            if isinstance(w, QCheckBox):
                w.setStyleSheet(cb_qss)
                w.style().unpolish(w)
                w.style().polish(w)
                w.update()
            elif isinstance(w, QRadioButton):
                w.style().unpolish(w)
                w.style().polish(w)
                w.update()
        # 5. Repaint the window body. paintEvent fills `_body_qcolor`,
        # which is cached (not read live) — and a theme-mode switch
        # changes the body opacity (frosted ~91% / solid 100% /
        # transparent ~43%). refresh_theme() above already updated
        # ui_helpers.BODY_COLOR; recompute the cached QColor + repaint.
        from modules.ui_helpers import BODY_COLOR as _BC

        if _OPAQUE_BODY:
            self._body_qcolor = QColor(_BC[0], _BC[1], _BC[2], 255)
        else:
            self._body_qcolor = QColor(*_BC)
        self.update()
        # 6. Frosted theme blurs behind the window; Transparent / Solid
        # don't. Re-evaluate on every theme change.
        self._apply_blur()

    def _on_auth_failed(self):
        """Connectivity tracker tripped the auth-failure threshold —
        the persisted credentials are being rejected by the server.
        Drop to LoginView so the user has a path to re-enter creds
        instead of staring at silent empty states. Idempotent: if
        we're already on LoginView (user is actively typing creds
        and getting them wrong) this is a no-op visually."""
        if self.content_stack.currentWidget() is self.login_view:
            return
        print(
            "[jellytoast] auth_failed handler — switching to LoginView",
            flush=True,
        )
        try:
            from modules import disk_cache as _disk_cache

            _disk_cache.clear_all()
        except Exception:
            pass
        self.content_stack.setCurrentWidget(self.login_view)

    def _reinstall_hotkeys(self):
        """Tear down the current QShortcuts and rebuild them from the
        (now-updated) hotkey registry — called on PlayerBus.hotkeys_changed
        so a rebind in Settings takes effect immediately."""
        from modules import hotkeys as _hotkeys

        for sc in getattr(self, "_hotkey_shortcuts", []):
            try:
                sc.setEnabled(False)
                sc.deleteLater()
            except Exception:
                pass
        self._hotkey_shortcuts = _hotkeys.install_shortcuts(self)

    def _on_host_switched(self, label: str):
        """The connectivity engine failed over to (or recovered from) an
        alternate server URL — flash a toast so the user knows which
        address they're on. Lifted clear of the now-playing bar."""
        from modules.toast import show_toast

        name = (label or "").strip() or "Primary"
        if name == "Primary":
            message = "Reconnected to the primary server"
        else:
            message = f"Switched to alternate server · {name}"
        show_toast(self, message, bottom_margin=128)

    def _on_verify_session_done(self, ok: bool):
        """Result of the boot-time verify. If the persisted token was
        rejected, drop the user on the LoginView so they can re-auth.
        Pre-existing settings (server URL, last username) stay so the
        form is partially pre-filled. The token itself stays in
        keyring; api.authenticate will overwrite it on success."""
        if ok:
            return
        print(
            "[jellytoast] persisted token rejected — showing login view",
            flush=True,
        )
        # The persisted token is dead; any cached view payloads from
        # the prior session would now render as ghost data on an
        # unauthenticated app. Drop them so the user lands on empty
        # states across the board instead of stale playlists / genres.
        try:
            from modules import disk_cache as _disk_cache

            _disk_cache.clear_all()
        except Exception:
            pass
        self.content_stack.setCurrentWidget(self.login_view)

    def _on_native_signed_in(self):
        """Called when the LoginView's authenticate round-trip
        succeeded. Credentials are already persisted (provider.
        authenticate wrote them to QSettings + keyring); just route
        to the user's home destination and let the native surfaces
        take over. Library lookups are cleared so they re-resolve
        against the new credentials, and any built native surface
        that's empty gets retried."""
        # LoginView may have called reset_provider() (e.g. user picked
        # a different server kind in the dropdown), so the provider
        # singleton is now a fresh instance — push it to every cached
        # widget BEFORE _route_home triggers any fetches, otherwise
        # surfaces built under the old provider keep using the
        # discarded reference and silently 401.
        self._refresh_provider_refs()
        print(
            f"[jellytoast] native sign-in succeeded (user={self.provider.user_id[:8]}…)",
            flush=True,
        )
        self._library_ids = {}
        # Route to home destination (Albums grid by default). Lazily
        # builds the surface and kicks off its load.
        self._route_home()
        self._retry_empty_native_views()

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

    def _tab_anchors(self):
        """Three section anchors for Tab cycling, in display order:
        top bar (home button) → content (current surface) → bottom
        bar (play button). Home is the right anchor for the top bar
        because back/forward are conditionally disabled (nothing in
        nav history at boot), and a disabled widget rejects setFocus,
        which would silently swallow the Tab cycle."""
        anchors = []
        tb = getattr(self, "top_bar", None)
        if tb is not None and getattr(tb, "home_btn", None) is not None:
            anchors.append(tb.home_btn)
        cs = getattr(self, "content_stack", None)
        cur = cs.currentWidget() if cs is not None else None
        if cur is not None:
            anchors.append(cur)
        npb = getattr(self, "np_bar", None)
        if npb is not None and getattr(npb, "play_btn", None) is not None:
            anchors.append(npb.play_btn)
        return anchors

    def _current_section_index(self, focused, anchors) -> int:
        """Identify which Tab anchor currently owns focus by walking
        the parent chain from the focused widget. Falls back to -1
        when focus is outside all known sections so the next Tab
        press snaps to anchor 0 (the top bar)."""
        if focused is None:
            return -1
        # Each anchor's "section" is rooted at one of: top_bar,
        # content_stack, np_bar. Walk up from focused and match.
        section_roots = []
        tb = getattr(self, "top_bar", None)
        if tb is not None:
            section_roots.append((tb, 0))
        cs = getattr(self, "content_stack", None)
        if cs is not None:
            section_roots.append((cs, 1))
        npb = getattr(self, "np_bar", None)
        if npb is not None:
            section_roots.append((npb, 2))
        w = focused
        while w is not None:
            for root, idx in section_roots:
                if w is root:
                    # Clamp to len(anchors)-1 in case a section is
                    # missing its anchor (e.g., back_btn not built yet).
                    return min(idx, len(anchors) - 1)
            w = w.parentWidget()
        return -1

    def _push_nav(self, thunk):
        """Append a 'show this surface again' thunk to the history. If
        the user navigated from a back state (pos < end), trim the
        forward branch first — same model as a browser history. The
        suppress flag short-circuits this during back/forward replay
        so the replay doesn't itself create a new history entry.
        Capped at _NAV_HISTORY_CAP to keep a long-running session
        from growing the list unbounded."""
        if self._suppress_nav_push:
            return
        # Trim forward history when branching from a back state.
        if self._nav_pos < len(self._nav_history) - 1:
            self._nav_history = self._nav_history[: self._nav_pos + 1]
        self._nav_history.append(thunk)
        # Cap the history — drop oldest entries first. Adjust
        # _nav_pos to stay valid relative to the new (trimmed)
        # list so back/forward still anchors to the right surface.
        if len(self._nav_history) > self._NAV_HISTORY_CAP:
            drop = len(self._nav_history) - self._NAV_HISTORY_CAP
            self._nav_history = self._nav_history[drop:]
        self._nav_pos = len(self._nav_history) - 1
        self._refresh_nav_buttons()

    def _go_back(self) -> bool:
        """Step one entry backward in history. Returns True if the
        replay actually moved; False if there's nothing earlier to go
        to (e.g. the user is at the first entry)."""
        if self._nav_pos <= 0:
            return False
        self._nav_pos -= 1
        self._suppress_nav_push = True
        try:
            self._nav_history[self._nav_pos]()
        finally:
            self._suppress_nav_push = False
        self._refresh_nav_buttons()
        return True

    def _go_forward(self) -> bool:
        if self._nav_pos + 1 >= len(self._nav_history):
            return False
        self._nav_pos += 1
        self._suppress_nav_push = True
        try:
            self._nav_history[self._nav_pos]()
        finally:
            self._suppress_nav_push = False
        self._refresh_nav_buttons()
        return True

    def _refresh_nav_buttons(self):
        """Sync the top-bar back/forward buttons' enabled state with
        the actual reachability in the history stack. Called after
        every push and every back/forward replay."""
        self.top_bar.set_back_enabled(self._nav_pos > 0)
        self.top_bar.set_forward_enabled(self._nav_pos + 1 < len(self._nav_history))

    def _route_home(self):
        """Top-bar Home button. Reads home_destination from Settings
        and swaps to the matching native music surface. Falls back to
        the Albums grid for unknown values (e.g. legacy keys after a
        rename) so Home is always functional."""
        self._apply_music_chrome()
        dest = get_settings().home_destination or "albums"
        if dest == "playlists":
            self._show_native_music_grid("playlist")
            active_tab = "Playlists"
        elif dest == "artists":
            self._show_native_music_grid("artist")
            active_tab = "Artists"
        elif dest == "songs":
            self._show_songs_view()
            active_tab = "Songs"
        elif dest == "genres":
            self._show_genres_view()
            active_tab = "Genres"
        elif dest == "suggestions":
            self._show_suggestions_view()
            active_tab = "Suggestions"
        else:
            self._show_native_music_grid("album")
            active_tab = "Albums"
        # Set after the content swap so set_active_tab runs while
        # the top bar is back in library mode (its guard early-returns
        # while _now_playing_mode is still True).
        self.top_bar.set_active_tab(active_tab)

    def _apply_music_chrome(self):
        """Set the top bar's title + collection so the View dropdown
        appears and the section label reads "Music". Used whenever a
        native music surface becomes the active content widget."""
        self.top_bar.set_title("Music")
        self.top_bar.set_collection("music")

    def _show_search_view(self):
        """Lazy-build + swap to the native Search surface. Remembers the
        surface the user was on so dismiss returns there. The input is
        focused on every open so the user can type immediately."""
        if self.search_view is None:
            from modules.search_view import SearchView

            self.search_view = SearchView(self)
            self.search_view.songs_play_requested.connect(self._on_search_songs_play)
            self.search_view.album_play_requested.connect(self._on_grid_play_album)
            self.search_view.album_browse_requested.connect(self._browse_album)
            self.search_view.artist_browse_requested.connect(self._show_artist_page)
            self.search_view.dismiss_requested.connect(self._dismiss_search_view)
            self.content_stack.addWidget(self.search_view)
        self.content_stack.setCurrentWidget(self.search_view)
        self.np_bar.set_left_cluster_visible(True)
        # Search is its own surface — no library controls apply.
        self.top_bar.set_library_controls_visible(False)
        self.search_view.focus_input()
        self._push_nav(lambda: self._show_search_view())

    def _dismiss_search_view(self):
        """Esc / close button on the SearchView — walks the unified
        nav history back to the previous surface. Falls back to the
        web view only if there's nothing earlier (shouldn't happen in
        practice since search is opened from another surface)."""
        if not self._go_back():
            self._route_home()

    def _on_search_songs_play(self, start_idx: int, items: list):
        """Search → song row click. Installs the visible song results
        as a MANUAL queue starting at the clicked index. Source label
        carries 'Search' so the now-playing kicker reads honestly
        (vs. inheriting an album/playlist label that doesn't match)."""
        if not items or not (0 <= start_idx < len(items)):
            return
        from modules.player_state import PlayerBus

        ctx = QueueContext(kind=QueueKind.MANUAL, source_label="Search")
        PlayerBus.get().queue_play_now.emit(list(items), start_idx, ctx)

    def _on_genre_selected(self, genre_id: str, genre_name: str):
        """Genre tile click → swap to the album grid filtered by genre.
        Uses Jellyfin's ?GenreIds= filter (passed via load_items's
        genre_id arg). ParentId is left empty — the genre filter is
        sufficient and Jellyfin doesn't model genres as parents."""
        self._show_library_grid("album", parent_id="", genre_id=genre_id)

    def _show_albums_by_year(self, year: int):
        """Album-tile year click → swap to the album grid filtered to
        that single ProductionYear. Uses Jellyfin's ?Years= filter on
        Jellyfin (load_items year=...) and Subsonic's byYear/from-toYear
        on Subsonic (handled in SubsonicProvider._get_albums)."""
        if not year:
            return
        self._show_library_grid("album", parent_id="", year=str(year))

    def _show_artist_page(self, artist_id: str):
        """Lazy-build + swap to ArtistPage for the given artist. Click
        an album from there → existing browse path; back button walks
        the unified nav history."""
        if not artist_id:
            return
        if self.artist_page is None:
            from modules.artist_page import ArtistPage

            self.artist_page = ArtistPage(self)
            self.artist_page.dismiss_requested.connect(self._dismiss_artist_page)
            self.artist_page.album_browse_requested.connect(self._browse_album)
            self.artist_page.album_play_requested.connect(self._on_grid_play_album)
            self.content_stack.addWidget(self.artist_page)
        self.artist_page.load_artist(artist_id)
        self.content_stack.setCurrentWidget(self.artist_page)
        self.np_bar.set_left_cluster_visible(True)
        # Top-bar library controls don't apply to a single-artist page.
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda aid=artist_id: self._show_artist_page(aid))

    def _dismiss_artist_page(self):
        """Back button on ArtistPage — walks the unified nav history."""
        if not self._go_back():
            self._route_home()

    def _on_library_view_mode_changed(self, mode: str):
        """Top-bar grid/list toggle → propagate to every native grid
        that's been built. Each LibraryGrid persists the choice via
        `library_view_mode` and re-renders its loaded items in place
        (no re-fetch). The toggle applies globally across albums /
        playlists / artists since one toolbar drives them all."""
        for g in (self.album_grid, self.playlist_grid, self.artist_grid):
            if g is not None:
                g.set_view_mode(mode)

    def _on_grid_play_album(self, album_id: str):
        """Play-overlay click on an album tile — install the full album
        as the live queue, start from track 0."""
        self._grid_play_collection(
            album_id,
            "album",
            self.provider.get_album_tracks,
        )

    def _on_grid_play_playlist(self, playlist_id: str):
        """Play-overlay click on a playlist tile — install the full
        playlist as the live queue, start from track 0."""
        self._grid_play_collection(
            playlist_id,
            "playlist",
            self.provider.get_playlist_items,
        )

    def _grid_play_collection(self, item_id: str, kind: str, fetch_fn):
        """Shared install-and-play path for album/playlist tile play
        clicks. `kind` maps to the QueueKind installed; `fetch_fn` is
        the API call that returns the track list."""
        if not item_id:
            return
        from modules.async_io import run_async
        from modules.player_state import PlayerBus

        queue_kind = QueueKind.PLAYLIST if kind == "playlist" else QueueKind.ALBUM

        def _on_tracks(tracks):
            if not tracks:
                return
            meta = self.provider.get_item(item_id) or {}
            ctx = QueueContext(
                kind=queue_kind,
                source_id=item_id,
                source_label=meta.get("Name", ""),
            )
            PlayerBus.get().queue_play_now.emit(list(tracks), 0, ctx)

        run_async(fetch_fn, item_id, on_result=_on_tracks)

    def _open_currently_playing_album(self):
        """JT_NATIVE_ALBUM shortcut handler — open the *currently-playing*
        track's album in NowPlayingPage's preview mode. Doesn't disrupt
        playback; the user can hit Play in the preview to install + play
        that album as a fresh queue."""
        from modules.player_state import get_now_playing

        np = get_now_playing()
        album_id = (np.raw or {}).get("AlbumId", "") if np else ""
        if album_id:
            self._show_now_playing(preview_id=album_id)

    def _open_cast_dialog(self):
        # Open without gating — picking a device when nothing is playing
        # pre-arms it as the cast target. The next track the user starts
        # will route to that device automatically (MpvController.play
        # checks active_cast and forwards to cast_manager).
        #
        # Non-modal + parent=None, exactly like _open_settings: a modal
        # exec() disables the main window (Qt paints it dimmed), and a
        # Wayland transient-for parent pins the dialog. Singleton-guard
        # a re-click; the picked device arrives via the accepted signal.
        existing = getattr(self, "_cast_dlg", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        dlg = CastDialog(self.cast_manager, None)
        self._cast_dlg = dlg

        def _on_cast_accepted():
            if dlg.selected_device:
                self._cast_to_device(dlg.selected_device)

        dlg.accepted.connect(_on_cast_accepted)
        dlg.finished.connect(lambda _r: setattr(self, "_cast_dlg", None))
        dlg.show()

    def _show_cast_context_menu(self, global_pos):
        """Right-click on the bottom bar's cast button — a quick menu of
        hearted devices (cast straight to them) plus Disconnect, without
        opening the full picker."""
        from modules.ui_helpers import opaque_menu
        from modules.icons import icon as _icon

        menu = opaque_menu(self)
        favs = get_settings().favorite_cast_devices
        # Self-heal legacy entries: the first cut of this feature stored
        # only the uuid, so the migration left name == uuid. If discovery
        # has since turned the device up, adopt its real name/type and
        # persist the upgrade so the menu reads properly from now on.
        upgraded = False
        for fav in favs:
            if fav["name"] == fav["uuid"] or not fav.get("type"):
                live = self._find_cast_device(fav["uuid"])
                if live is not None:
                    fav["name"] = live.name
                    fav["type"] = live.device_type
                    upgraded = True
        if upgraded:
            get_settings().favorite_cast_devices = favs
        if favs:
            for fav in favs:
                glyph = _icon("airplay" if fav.get("type") == "airplay" else "cast")
                act = menu.addAction(glyph, fav.get("name") or "Device")
                act.triggered.connect(lambda _=False, f=fav: self._cast_to_favorite(f))
        else:
            placeholder = menu.addAction("No favorite devices")
            placeholder.setEnabled(False)
        menu.addSeparator()
        disconnect = menu.addAction("Disconnect")
        disconnect.setEnabled(self.cast_manager.active_cast is not None)
        disconnect.triggered.connect(self._disconnect_cast)
        menu.addSeparator()
        open_picker = menu.addAction("Open cast menu…")
        open_picker.triggered.connect(self._open_cast_dialog)
        # Anchor the menu fully above the mini-player / cast / volume
        # icon cluster, horizontally centered on it — rather than at the
        # cursor, which would spill off the bottom-right corner. Clamp
        # the final rect to the window so it can never leave the UI.
        size = menu.sizeHint()
        cluster = [self.np_bar.queue_btn, self.np_bar.cast_btn, self.np_bar.vol_btn]
        tls = [b.mapToGlobal(QPoint(0, 0)) for b in cluster]
        cluster_left = min(p.x() for p in tls)
        cluster_right = max(tls[i].x() + cluster[i].width() for i in range(len(cluster)))
        cluster_top = min(p.y() for p in tls)
        x = (cluster_left + cluster_right) // 2 - size.width() // 2
        y = cluster_top - size.height() - 6
        win = self.frameGeometry()  # global coords
        x = max(win.left() + 4, min(x, win.right() - size.width() - 4))
        y = max(win.top() + 4, min(y, win.bottom() - size.height() - 4))
        menu.exec(QPoint(x, y))

    def _disconnect_cast(self):
        """Stop the active cast session — mirrors CastDialog's
        Disconnect button, for the cast button's right-click menu."""
        self.cast_manager.stop_cast()
        self.bus.cast_stopped.emit()

    def _find_cast_device(self, uuid: str):
        """The live CastDevice for a uuid, or None if discovery hasn't
        turned it up this session."""
        for d in self.cast_manager.get_all_devices():
            if d.uuid == uuid:
                return d
        return None

    def _cast_to_favorite(self, fav: dict):
        """Cast to a hearted device by uuid. If discovery hasn't found
        it yet, kick a scan and poll briefly before giving up."""
        uuid = fav.get("uuid", "")
        dev = self._find_cast_device(uuid)
        if dev is not None:
            self._cast_to_device(dev)
            return
        # Not in the discovery cache yet — scan, then poll for a few
        # seconds (discover_all populates cast_manager's device lists
        # directly, so a plain poll is enough; no callback wiring).
        self.cast_manager.discover_all()
        state = {"tries": 0}
        timer = QTimer(self)
        timer.setInterval(700)

        def _poll():
            state["tries"] += 1
            found = self._find_cast_device(uuid)
            if found is not None:
                timer.stop()
                timer.deleteLater()
                self._cast_to_device(found)
            elif state["tries"] >= 6:
                timer.stop()
                timer.deleteLater()
                QMessageBox.information(
                    self,
                    "Cast",
                    f"Couldn't find “{fav.get('name')}” on the "
                    f"network right now. Open the cast menu to rescan.",
                )

        timer.timeout.connect(_poll)
        timer.start()

    def _cast_to_device(self, dev):
        """Cast to a specific CastDevice — shared by the cast dialog's
        pick and the cast button's right-click quick menu."""
        np = get_now_playing()
        playing_now = bool(np.item_id and np.stream_url)
        # Capture position BEFORE we touch mpv — np.position is updated
        # by MpvController on every time-pos tick. Once we stop, the
        # value still reflects the last-seen position, but we want the
        # cast to resume exactly where the user was.
        resume_seconds = (np.position / 1000.0) if playing_now else 0.0

        # IMPORTANT: stop the local mpv stream BEFORE we set active_cast.
        # MpvController.stop now routes to chromecast_stop when active_cast
        # is set, so emitting stop_requested afterwards would kill the cast
        # session we just initiated. Stop also clears the now-playing UI
        # via playback_stopped — we re-emit playback_started after the
        # cast lands so the bar / mini player re-render the same track.
        if playing_now:
            self.bus.stop_requested.emit()

        # Result handling is shared across transports. For Chromecast it
        # runs from an async callback (the cast call blocks for seconds
        # on cc.wait() + block_until_active); for AirPlay it's invoked
        # inline at the end of this method.
        def _on_cast_result(ok, _dev=dev, _np=np, _playing=playing_now):
            if ok:
                self.bus.cast_started.emit(_dev.name)
                if _playing:
                    # Re-render the now-playing UI so the title, artist,
                    # cover art, and progress bar reflect the track that's
                    # now on the cast device. Without this, the bar shows
                    # "Nothing playing" because of the prior stop_requested.
                    self.bus.playback_started.emit(_np)
            else:
                QMessageBox.warning(self, "Cast failed", f"Could not cast to {_dev.name}.")

        if dev.device_type == "chromecast":
            # Chromecast connect/play block on cc.wait() +
            # block_until_active — run them off the GUI thread so the
            # dialog doesn't freeze while the receiver negotiates, then
            # report back through _on_cast_result.
            if playing_now:
                # Format-detect for direct play (FLAC stays FLAC, etc.)
                container = (np.raw.get("Container") if np.raw else "") or ""
                mime = (
                    self.cast_manager.chromecast_audio_mime_for(container) if np.is_audio else None
                )
                url = np.stream_url
                if np.is_audio and mime is None:
                    # Transcode-fallback URL is provider-specific
                    # (Jellyfin's /Audio/{id}/stream.mp3 vs Subsonic's
                    # /rest/stream?format=mp3). The provider knows.
                    url = get_provider().get_audio_transcode_url(
                        np.item_id,
                        max_bitrate_kbps=320,
                        codec="mp3",
                    )
                    mime = "audio/mpeg"
                self.cast_manager.cast_to_chromecast_async(
                    dev,
                    url,
                    np.title,
                    np.thumb_url,
                    is_audio=np.is_audio,
                    content_type=mime,
                    current_time=resume_seconds,
                    on_done=_on_cast_result,
                )
            else:
                self.cast_manager.connect_to_chromecast_async(dev, on_done=_on_cast_result)
            return
        else:
            # AirPlay v1 has no real "connect without media" handshake;
            # if there's nothing to cast, just record the choice. Calls
            # to play() afterward will issue POST /play to this device.
            #
            # AirPlay 2 receivers that need pairing get intercepted
            # here: launch the pairing modal before attempting cast.
            # If pairing succeeds the credentials are persisted by the
            # dialog itself and the cast retries.
            from modules import airplay2 as _ap2

            is_ap2 = isinstance(dev.cast_object, _ap2.AirPlay2Device)
            _ap2_dbg(
                f"cast handler: dev={dev.name!r} type={dev.device_type} "
                f"is_ap2={is_ap2} playing_now={playing_now}"
            )
            if is_ap2:
                ap2_obj = dev.cast_object  # type: ignore[assignment]
                stored = _ap2.get_stored_credentials(ap2_obj.identifier)
                _ap2_dbg(
                    f"ap2 device: id={ap2_obj.identifier!r} "
                    f"requires_pairing={ap2_obj.requires_pairing} "
                    f"stored_creds_len={len(stored)}"
                )
            if (
                is_ap2
                and dev.cast_object.requires_pairing
                and not _ap2.get_stored_credentials(dev.cast_object.identifier)
            ):
                from modules.airplay_pairing import PairingDialog

                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                _ap2_dbg(f"launching pairing dialog for {ap2_dev.name!r}")
                creds = PairingDialog.run(self, ap2_dev)
                _ap2_dbg(f"pairing dialog returned: creds_len={len(creds)}")
                if not creds:
                    # User cancelled or pairing failed — restore the
                    # local stream so the abandoned cast attempt doesn't
                    # leave the user staring at "Nothing playing".
                    if playing_now:
                        self.bus.playback_started.emit(np)
                    return
                # Successfully paired; fall through into the regular
                # cast path which will pick up the newly-stored creds
                # via _cast_to_airplay2 → play_url_sync.
            if playing_now:
                _ap2_dbg(f"calling cast_to_airplay url_len={len(np.stream_url)} title={np.title!r}")
                ok = self.cast_manager.cast_to_airplay(dev, np.stream_url, np.title)
                _ap2_dbg(f"cast_to_airplay returned ok={ok}")
            else:
                self.cast_manager.active_cast = dev
                ok = True

        _on_cast_result(ok)

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
        from Xlib import display, X
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
        print(f"[jellytoast] startup-notify remove failed: {e}", file=sys.stderr)


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
    # ./jellytoast.py`` works for users with picky displays.
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
    print(f"[jellytoast] {msg}", flush=True)


def main():
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
    from modules.settings import warm_keyring_async

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
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("jellytoast")
    app.setDesktopFileName("jellytoast")
    app.setWindowIcon(QIcon(make_app_icon(64)))
    app.setQuitOnLastWindowClosed(False)

    # Push a theme-matched QPalette so widgets Qt paints from the
    # palette (separate-top-level dialogs, menus, tooltips) don't fall
    # back to the desktop's palette — white text on a light theme.
    from modules.ui_helpers import apply_app_palette

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
    from modules.color_tokens import load_persisted_overrides

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
    from modules.theme import _hex_to_rgb as _h2r_boot
    from modules.ui_helpers import ACCENT as _ACCENT_BOOT

    _ar, _ag, _ab = _h2r_boot(_ACCENT_BOOT)
    _app_pal = app.palette()
    _accent_qcolor = QColor(_ar, _ag, _ab)
    _app_pal.setColor(QPalette.ColorRole.Highlight, _accent_qcolor)
    _app_pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    app.setPalette(_app_pal)

    # App-wide smooth scrolling. Bound to `app` so it shares the app's
    # lifetime — letting it GC would silently disable the filter.
    from modules.smooth_scroll import SmoothScrollFilter

    app._smooth_scroll = SmoothScrollFilter(app)
    app.installEventFilter(app._smooth_scroll)

    # Single-instance gate. Held by QSharedMemory; the QLocalServer is
    # the message channel for "raise me" pings from subsequent launch
    # attempts. We bind the result to `app` so it shares the app's
    # lifetime — letting it GC would release the shared-memory lock
    # mid-run and effectively disable the check.
    from modules.single_instance import SingleInstance

    app._single_instance = SingleInstance("jellytoast", app)
    if not app._single_instance.acquire():
        # Another instance was already running — signal it to surface
        # and exit cleanly. Print a small breadcrumb so a CLI launcher
        # (terminal, .desktop file, autostart) can see what happened.
        print("jellytoast is already running; raised existing window.", flush=True)
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
    server_url = settings.server_url.rstrip("/")
    if not server_url:
        url, ok = QInputDialog.getText(
            None, "jellytoast — Server URL", "Enter your Jellyfin server URL:", text="http://"
        )
        if not ok or not url.strip():
            sys.exit(0)
        server_url = url.strip().rstrip("/")
        settings.server_url = server_url

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
    from modules import radio_state as _radio_state

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
    bus.show_mini_player.connect(lambda: (mini.show(), mini.raise_(), mini.activateWindow()))
    bus.hide_mini_player.connect(mini.hide)
    # Pin the tray controller to the window so its lifetime tracks
    # `win` rather than relying on Qt's implicit parent-of-`app`
    # retention. Functionally equivalent (both pin past `app.exec()`),
    # but the named attribute reads as intentional rather than as a
    # dangling local.
    win.tray = TrayController(app, mini, win)

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

        # Skip MPRIS startup when the user has disabled OS media-key /
        # media-control widget integration. Boot-time only — toggling
        # the setting at runtime takes effect on the next launch.
        if settings.media_controls_enabled:
            mpris = MediaControlsService()
            mpris.start()

        # Keep-above install (mini-player) is idempotent and lands
        # compositor-side any time — doesn't need to be live for first
        # paint. On platforms where Qt's WindowStaysOnTopHint already
        # works, the keep_above backend is a no-op.
        if settings.mini_player_keep_above:
            from modules.keep_above import install_mini_player_rule

            install_mini_player_rule()

        # No-border rules: the mini player + settings dialog are
        # server-side-decorated on KDE Wayland (so KWin keeps their
        # blur alive while they're being dragged — frameless windows
        # lose it); this Force rule strips the visible decoration so
        # they still look frameless. Unconditional (not a user setting)
        # and a no-op off KDE Wayland. Idempotent — re-runs every
        # launch so it self-heals if the rule is ever dropped (e.g. by
        # the System Settings window-rule editor rewriting kwinrulesrc).
        from modules.keep_above import (
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
        from modules import drag_repaint

        drag_repaint.sync()

        # Open the downloads index (SQLite open + migrate) so the
        # context-menu "Download" action and, later, offline playback
        # have a live DB. Cheap, but deferred here with the rest of the
        # heavy init so it's off the first-paint path.
        from modules import offline

        offline.init()

        # Bring the scrobble manager up — it subscribes to PlayerBus on
        # construction, so the first track that plays can be scrobbled
        # immediately. Drains any pending offline scrobbles from a prior
        # session in the same step.
        from modules.scrobble import get_scrobble_manager

        get_scrobble_manager().flush_pending()

    QTimer.singleShot(0, _post_show_init)

    # No eager win.show() — _do_boot_auth_check builds the initial
    # surface (home destination on success, login on failure) and
    # then calls self.show() via _reveal_window. That guarantees
    # first paint shows fully-populated content rather than the dark
    # → fade flicker the loading overlay used to mask.

    if settings.show_mini_on_start:
        mini.show()

    def _cleanup():
        # Stop the cast FIRST. It's the only teardown step with an
        # external, user-visible effect — a Chromecast / AirPlay
        # receiver plays autonomously and keeps going on someone's
        # speakers until told to stop. Doing it before mpv / mpris
        # means that even if a later step hangs or throws, the cast
        # is already stopped.
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

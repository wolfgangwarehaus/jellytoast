"""
JellyToast — Jellyfin Web inside a QtWebEngine shell, with native bit-perfect
mpv playback, mini player, MPRIS2, system tray, and casting.

Browsing UI: Jellyfin Web, loaded straight from your own server (untouched).
Playback engine: mpv via the existing PlayerBus.

The flow on a play action:
  1. Jellyfin Web's HTML5 audio element requests /Audio/{id}/universal
  2. _PlaybackInterceptor extracts the item id and BLOCKS the request
  3. We fetch metadata via our REST client and emit `queue_play_now` on the bus
  4. QueueManager + MpvController play it natively
"""
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

# libmpv requires LC_NUMERIC=C; Qt's setlocale() undoes Python-side fixes.
if os.environ.get("_JELLY_LOCALE_FIXED") != "1":
    if os.environ.get("LC_ALL") is not None or os.environ.get("LC_NUMERIC", "C") != "C":
        new_env = dict(os.environ)
        new_env.pop("LC_ALL", None)
        new_env["LC_NUMERIC"] = "C"
        new_env.setdefault("LANG", "C.UTF-8")
        new_env["_JELLY_LOCALE_FIXED"] = "1"
        os.execve(sys.executable, [sys.executable] + sys.argv, new_env)

# Native Wayland by default — Qt picks the platform from WAYLAND_DISPLAY
# / DISPLAY in the usual way. Set QT_QPA_PLATFORM=xcb in the environment
# to fall back to XWayland (escape hatch in case a Wayland regression
# bites). All X11-only code paths (cursor env bootstrap, startup-notify
# ClientMessage, off-screen positioning, taskbar-skip via xprop) are
# gated on the platform — see _will_be_wayland() and IS_WAYLAND below.
#
# Known Wayland gap: mini player drag/resize uses absolute QWidget.move
# / setGeometry which the protocol forbids; KWin will pick its initial
# position and drag/resize will no-op until those are switched to
# windowHandle().startSystemMove/Resize.


def _will_be_wayland() -> bool:
    """Pre-QApplication Wayland detection. Used by code paths that run
    before QApplication is constructed (env-var bootstraps, etc.).
    Honors an explicit QT_QPA_PLATFORM override — including the xcb force
    above. After QApplication exists, prefer `IS_WAYLAND` from
    `app.platformName() == "wayland"`."""
    plat = os.environ.get("QT_QPA_PLATFORM", "")
    if plat.startswith("xcb"):
        return False
    if plat.startswith("wayland"):
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))

# Make Qt + QtWebEngine pick up the KDE cursor theme + size so the
# cursor doesn't visibly shrink when entering the JellyToast window.
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
    for base in ("/usr/share/icons", os.path.expanduser("~/.icons"),
                 os.path.expanduser("~/.local/share/icons")):
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
                typ, sub, _ = struct.unpack("<III", data[off:off + 12])
                if typ == 0xfffd0002:  # Xcursor IMAGE chunk
                    sizes.add(sub)
            return sorted(sizes)
        except Exception:
            continue
    return []

def _bootstrap_cursor_env():
    # Wayland: KWin renders cursors for all clients itself; XCURSOR_*
    # env vars are X11/XWayland concepts. Skip the bootstrap entirely
    # so we don't leak X-only env into a native-Wayland Qt session.
    if _will_be_wayland():
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
                ["xrdb", "-query"], capture_output=True, text=True, timeout=2
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
                    size = next((s for s in available if s >= requested),
                                available[-1])
                    os.environ["XCURSOR_SIZE"] = str(size)
                else:
                    os.environ["XCURSOR_SIZE"] = str(requested)
    except Exception:
        pass

_bootstrap_cursor_env()

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from PySide6.QtCore import (
    QObject, QUrl, QFile, QIODevice, QTimer, Qt, Slot, Signal,
    QVariantAnimation, QEasingCurve,
)
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPainterPath, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QSystemTrayIcon, QWidget,
    QVBoxLayout, QHBoxLayout, QStackedLayout, QStackedWidget, QLabel,
    QPushButton, QDialog, QInputDialog,
)

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import (
        QWebEngineScript, QWebEngineProfile, QWebEnginePage,
        QWebEngineUrlRequestInterceptor,
    )
    from PySide6.QtWebChannel import QWebChannel
    WEBENGINE_AVAILABLE = True
    _WEBENGINE_ERROR = ""
except ImportError as e:
    WEBENGINE_AVAILABLE = False
    _WEBENGINE_ERROR = str(e)

from modules.player_state import (
    PlayerBus, get_now_playing, QueueContext, QueueKind,
)
from modules.player_backend import MpvController, MPV_AVAILABLE
from modules.queue_manager import QueueManager
from modules.now_playing_bar import NowPlayingBar, CastDialog
from modules.now_playing_page import NowPlayingPage
from modules.mini_player import FloatingMiniPlayer
from modules.tray import TrayController
from modules.mpris import MprisService
from modules.cast_manager import CastManager
from modules.top_bar import JtTopBar
from modules.settings_dialog import SettingsDialog
from modules.jellyfin_api import get_api
from modules.jellyfin_web_bridge import SHIM_JS, Bridge, _LoggingPage
from modules.settings import get_settings
from modules.async_io import run_async
from modules.ui_helpers import (
    make_app_icon, GLOBAL_STYLE, TEXT, TEXT_DIM, BODY_COLOR, enable_kde_blur,
)


# Per-intent / per-track-change diagnostics (URL, JF Web queue contents,
# cooldown deltas) are gated behind this. Install/skip/error lines stay
# unconditional so a post-mortem from the terminal alone is still possible.
_SHUFFLE_DEBUG = os.environ.get("JT_SHUFFLE_DEBUG") == "1"




class _PlaybackInterceptor(QWebEngineUrlRequestInterceptor):
    """
    Intercept HTTP requests for Jellyfin's audio/video streams. Extract the
    item id, signal Python, then BLOCK the request — so Jellyfin Web's HTML5
    player can't play (mpv plays instead).
    """

    intent_detected = Signal(str)  # item_id

    _PATTERN = re.compile(
        r"/(?:Audio|Videos)/([a-f0-9]{32})/(?:universal|stream|master\.m3u8)",
        re.IGNORECASE,
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_id = None
        self._last_at = 0.0

    def interceptRequest(self, info):
        url = info.requestUrl().toString()
        m = self._PATTERN.search(url)
        if not m:
            return
        item_id = m.group(1).lower()
        now = time.time()
        if item_id == self._last_id and (now - self._last_at) < 1.5:
            info.block(True)
            return
        self._last_id = item_id
        self._last_at = now
        self.intent_detected.emit(item_id)
        info.block(True)


class _TitleBar(QWidget):
    """Custom titlebar for the frameless main window. Provides a drag
    region and min/max/close buttons. Drag is delegated to KWin via
    QWindow.startSystemMove() so we don't fight the window manager."""
    def __init__(self, window: QWidget):
        super().__init__()
        self._window = window
        self.setFixedHeight(34)
        self.setObjectName("jtTitleBar")
        # Descendant rule clears the opaque QWidget bg that GLOBAL_STYLE
        # paints onto the JellyToast label and other inner widgets.
        self.setStyleSheet("""
            QWidget#jtTitleBar { background: transparent; }
            QWidget#jtTitleBar QLabel { background: transparent; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 6, 0)
        layout.setSpacing(0)

        self.title = QLabel("JellyToast")
        self.title.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px; font-weight: 500;")
        layout.addWidget(self.title)
        layout.addStretch(1)

        def _btn(symbol: str, hover_color: str):
            b = QPushButton(symbol)
            b.setFixedSize(34, 26)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {TEXT_DIM};
                    border: none; font-size: 11px;
                }}
                QPushButton:hover {{
                    background: {hover_color}; color: {TEXT};
                }}
            """)
            return b

        self.min_btn = _btn("─", "rgba(255,255,255,0.08)")
        self.max_btn = _btn("☐", "rgba(255,255,255,0.08)")
        self.close_btn = _btn("✕", "rgba(239,68,68,0.85)")
        self.min_btn.clicked.connect(lambda: self._window.showMinimized())
        self.max_btn.clicked.connect(self._toggle_max)
        self.close_btn.clicked.connect(self._window.close)
        layout.addWidget(self.min_btn)
        layout.addWidget(self.max_btn)
        layout.addWidget(self.close_btn)

    def _toggle_max(self):
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(e)
        win = self._window
        if win.isMaximized() or win.isFullScreen():
            handle = win.windowHandle()
            if handle is not None:
                handle.startSystemMove()
            return
        # Map the press into window coordinates and ask the host's
        # resize-edge logic if it lands on a top edge / top corner.
        # Without this the titlebar would unconditionally start a
        # system move and the user could never grab the top of the
        # window to resize. Using the host's `_edges_at` keeps the
        # rounded-body corner geometry consistent with the rest of
        # the window's resize affordance.
        win_pos = self.mapTo(win, e.position().toPoint())
        edges = win._edges_at(win_pos)
        if edges != Qt.Edge(0):
            handle = win.windowHandle()
            if handle is not None:
                try:
                    handle.startSystemResize(edges)
                except Exception as ex:
                    print(f"[JellyToast] titlebar startSystemResize failed: {ex}", flush=True)
                return
        handle = win.windowHandle()
        if handle is not None:
            handle.startSystemMove()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()


class _LoadingOverlay(QWidget):
    """Painted overlay shown while Jellyfin Web is loading. Masks
    Chromium's renderer-init paint cycles, the gradual paint of our
    own widgets, and JF Web's lazy-loaded cover art population by
    drawing a frosted dark surface over the entire central widget.
    Fades out (rather than hides instantly) when the page has
    composited — the fade visually covers any final compositor lag."""

    BASE_ALPHA = 220

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        # Override GLOBAL_STYLE's `QWidget { background: BG }` so the
        # default opaque dark background doesn't show through when our
        # painted alpha drops during the fade. With this, only our
        # paintEvent's fillRect paints — at alpha 0 the widget area is
        # genuinely transparent.
        self.setObjectName("jtLoadingOverlay")
        self.setStyleSheet("QWidget#jtLoadingOverlay { background: transparent; }")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._alpha = self.BASE_ALPHA
        self._fade = None

    def paintEvent(self, e):
        if self._alpha <= 0:
            return
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.fillRect(
                self.rect(),
                QColor(BODY_COLOR[0], BODY_COLOR[1], BODY_COLOR[2], self._alpha),
            )
        finally:
            p.end()

    def fade_out(self, duration_ms: int = 500):
        # Animate the painted alpha from current → 0 over duration_ms.
        # Smoother than an instant hide, and the moving transparency
        # naturally masks any compositor cycle that lands during the
        # fade — content reveals through the overlay rather than
        # popping in after.
        if self._fade is not None and self._fade.state() == QVariantAnimation.State.Running:
            return
        anim = QVariantAnimation(self)
        anim.setDuration(duration_ms)
        anim.setStartValue(self._alpha)
        anim.setEndValue(0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(self._on_alpha_step)
        anim.finished.connect(self.hide)
        anim.start()
        self._fade = anim

    def _on_alpha_step(self, value):
        self._alpha = int(value) if value is not None else 0
        self.update()


class JellyToastWindow(QMainWindow):
    # Subtle rounding — small enough that the residual "gap" when KWin
    # snaps the window to a screen edge reads as intentional softness
    # rather than a misaligned border. (We can't get KWin to round our
    # frameless window for us — it only rounds windows it decorates —
    # so we paint our own corners and accept this tradeoff.)
    BODY_RADIUS = 8
    # Hit zones: edges get a tight 8px so the cursor doesn't flip to a
    # resize shape too eagerly when the user is just brushing the
    # window border. Corners get a fatter 16px so the diagonal resize
    # affordance is forgiving — a precise 8×8 corner square is too
    # easy to miss. _edges_at detects corners first using the larger
    # margin, then falls through to the edge check.
    RESIZE_MARGIN = 8
    CORNER_MARGIN = 16

    def __init__(self, server_url: str):
        super().__init__()
        self.setWindowTitle("JellyToast")
        self.setWindowIcon(QIcon(make_app_icon(64)))
        self.setMinimumSize(1100, 720)
        self.resize(1280, 820)
        # GLOBAL_STYLE paints `QWidget { background: BG }` which would cover
        # the translucent body we paint in paintEvent. Override by ID for the
        # central widget and the QMainWindow itself.
        self.setObjectName("jtMain")
        self.setStyleSheet(GLOBAL_STYLE + """
            QMainWindow#jtMain { background: transparent; }
            QWidget#jtCentral { background: transparent; }
        """)

        # Frameless + translucent so we can paint our own rounded body and
        # let KWin blur the desktop behind it (matches the mini player).
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.api = get_api()
        self.bus = PlayerBus.get()
        self.cast_manager = CastManager()
        self.queue_mgr = QueueManager(self)

        central = QWidget()
        central.setObjectName("jtCentral")
        # MouseTracking lets the window receive move events without a button
        # held — needed for the edge-resize cursor feedback.
        central.setMouseTracking(True)
        self.setCentralWidget(central)

        # Stacked layout: the chrome (titlebar + top bar + view + np
        # bar) sits underneath a full-window loading overlay. Until
        # the page signals it's fully rendered, the overlay covers
        # everything — Chromium's renderer-init flicker, JF Web's
        # progressive load, the gradual paint of our own widgets — so
        # the user sees a single calm "loading" surface instead of
        # parts streaming in.
        central_stack = QStackedLayout(central)
        central_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        central_stack.setContentsMargins(0, 0, 0, 0)

        chrome = QWidget()
        chrome.setObjectName("jtChrome")
        chrome.setStyleSheet("QWidget#jtChrome { background: transparent; }")
        chrome.setMouseTracking(True)
        self._chrome_layout = QVBoxLayout(chrome)
        # Outer margins act as the resize hit zone — the WebEngineView swallows
        # mouse events over its own area, so resize is only available on the
        # body's edges (and the bottom-right size grip). Collapsed to zero on
        # any edge touching the screen (maximized / snapped) by
        # _apply_body_margins so the body fills edge-to-edge instead of
        # leaving a transparent gap.
        self._chrome_layout.setSpacing(0)
        self._apply_body_margins()
        layout = self._chrome_layout

        self.titlebar = _TitleBar(self)
        layout.addWidget(self.titlebar)

        self.top_bar = JtTopBar()
        self.top_bar.nav_requested.connect(self._on_nav_requested)
        self.top_bar.drawer_toggle_requested.connect(self._toggle_sidebar)
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

        # Named profile = persistent on disk. The default profile is
        # off-the-record in Qt WebEngine, so localStorage/cookies (and
        # therefore the Jellyfin auth session) are wiped on every launch.
        profile = QWebEngineProfile("jellytoast", self)
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
        )
        # Beef up the on-disk HTTP cache so second+ launches don't
        # re-download Jellyfin Web's bundles, fonts, and album cover
        # art. 256 MB is enough for a couple thousand cards' worth of
        # primary images plus the JS/CSS bundles (~5 MB total).
        # localStorage / IndexedDB are persistent automatically for a
        # named profile; no explicit policy setter needed.
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        profile.setHttpCacheMaximumSize(256 * 1024 * 1024)

        self.interceptor = _PlaybackInterceptor(self)
        # The interceptor blocks Jellyfin Web's hidden HTML5 player
        # from grabbing audio streams behind us. With native browse +
        # native search + native suggestions, no JellyToast surface
        # routes through these URLs — but JF Web is still loaded for
        # credential bridging and could attempt playback if its scripts
        # auto-advance. Blocking is defensive.
        profile.setUrlRequestInterceptor(self.interceptor)

        # Tiny earliest-possible script. Adds `jt-loading` to the html
        # element and a single class-scoped CSS rule that hides body
        # before any browser paint. JF Web doesn't set body opacity,
        # so this rule isn't subject to cascade fights — no need to
        # land after JF Web's stylesheets like the main shim does.
        early_js = (
            "(function(){"
            "  if (window.__jellytoast_early) return;"
            "  window.__jellytoast_early = true;"
            "  document.documentElement.classList.add('jt-loading');"
            "  var s = document.createElement('style');"
            "  s.id = 'jellytoast-early-css';"
            "  s.textContent ="
            "    'html.jt-loading body { opacity: 0 !important; }'"
            "    + 'html:not(.jt-loading) body { transition: opacity 320ms ease-out; }';"
            "  (document.head || document.documentElement).appendChild(s);"
            "})();"
        )
        early_script = QWebEngineScript()
        early_script.setName("jellytoast_shim_early")
        early_script.setSourceCode(early_js)
        early_script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        early_script.setRunsOnSubFrames(False)
        early_script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        profile.scripts().insert(early_script)

        qwc_js = _read_qresource(":/qtwebchannel/qwebchannel.js")
        script = QWebEngineScript()
        script.setName("jellytoast_shim")
        script.setSourceCode(qwc_js + "\n" + SHIM_JS)
        # Main shim runs at DocumentReady so its !important rules
        # land AFTER Jellyfin Web's external stylesheets in the
        # cascade. (Putting it at DocumentCreation reverses that
        # order and JF Web's own !important rules win.) The early
        # body-hide is handled by `early_script` above, which runs
        # at DocumentCreation with a single class-scoped rule that
        # JF Web doesn't override.
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setRunsOnSubFrames(False)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        profile.scripts().insert(script)

        self.view = QWebEngineView(self)
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.view.setStyleSheet("background: transparent;")
        # Force Qt's themed arrow cursor on the WebView. QtWebEngine's
        # bundled Chromium otherwise paints its own internal default
        # cursor over the render surface, which renders at a smaller
        # size than the system Xcursor theme. Setting Qt's cursor on
        # the view + every child widget (including the focusProxy
        # render surface created lazily by Chromium) means Qt owns
        # the cursor and the system Xcursor theme is honored. We
        # apply on a 0ms timer so the focusProxy exists, and re-apply
        # whenever new children are added (Chromium recreates them
        # on profile changes).
        self.view.setCursor(Qt.CursorShape.ArrowCursor)

        def _force_arrow_cursor():
            self.view.setCursor(Qt.CursorShape.ArrowCursor)
            for child in self.view.findChildren(QWidget):
                child.setCursor(Qt.CursorShape.ArrowCursor)

        QTimer.singleShot(0, _force_arrow_cursor)
        QTimer.singleShot(500, _force_arrow_cursor)
        QTimer.singleShot(2000, _force_arrow_cursor)
        self.page = _LoggingPage(profile, self.view)
        self.view.setPage(self.page)
        # Transparent page background so the painted body shows through
        # before/while Jellyfin Web's HTML paints.
        self.page.setBackgroundColor(QColor(0, 0, 0, 0))

        self.bridge = Bridge()
        self.bridge.credentials_received.connect(self._on_credentials_received)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel)

        # The URL interceptor still BLOCKS Jellyfin Web's audio stream
        # requests so its hidden HTML5 player can't play behind us, but
        # nothing reacts to the resulting intent_detected signal anymore
        # — the native browse surfaces install queues directly. The
        # handler that used to consume it (PlaybackIntentHandler) was
        # retired with the bridge cleanup.

        # Content stack: page 0 is the WebEngine library view, page 1
        # is the NowPlayingPage. The transport bar's pill toggles
        # between them; the page's back button flips back to the web
        # view. The QueueManager isn't constructed yet at this point in
        # __init__, so we defer creating the page until after it is
        # (see below, just after `self.queue_mgr = QueueManager(self)`
        # in the existing init flow). We add the WebEngineView now so
        # the layout slot is filled.
        self.content_stack = QStackedWidget()
        # Chrome → content_stack → page must stay transparent so the
        # main window's translucent body (rounded rect + KWin blur)
        # shows through. GLOBAL_STYLE paints every QWidget with the
        # solid BG color by default; this ID rule wins by specificity.
        self.content_stack.setObjectName("jtContentStack")
        self.content_stack.setStyleSheet(
            "QStackedWidget#jtContentStack { background: transparent; }"
        )
        self.content_stack.addWidget(self.view)         # index 0
        layout.addWidget(self.content_stack, 1)

        self.np_bar = NowPlayingBar()
        self.np_bar.show_now_playing_requested.connect(self._show_now_playing)
        self.np_bar.show_queue_requested.connect(lambda: self.bus.show_mini_player.emit())
        self.np_bar.cast_requested.connect(self._open_cast_dialog)
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
        # doesn't tear down + rebuild. Routed by default for Music
        # libraries via _on_collection_resolved + _on_tab_requested.
        self.album_grid = None     # LibraryGrid(kind="album") | None
        self.playlist_grid = None  # LibraryGrid(kind="playlist") | None
        self.artist_grid = None    # LibraryGrid(kind="artist") | None
        # Artist detail page — chronological album grid, opened by
        # clicking an artist tile in the Artists grid.
        self.artist_page = None    # ArtistPage | None
        # Songs (list view) and Genres (tile grid). Songs reuses the
        # standard sort/library controls; Genres has no inline
        # controls (clicking a tile pivots the user into a filtered
        # album grid for that genre).
        self.songs_view = None     # SongsView | None
        self.genres_view = None    # GenresView | None
        self.suggestions_view = None  # SuggestionsView | None
        self.search_view = None    # SearchView | None
        # Browser-style navigation history. Each entry is a thunk that
        # re-shows the surface; back / forward walk the list. Surfaces
        # push themselves at the end of their _show_* method; the
        # _suppress_nav_push flag is set during back/forward replay so
        # the replay doesn't itself add a new history entry.
        self._nav_history: list = []
        self._nav_pos: int = -1
        self._suppress_nav_push: bool = False

        # Now wire the chrome + full-window loading overlay into the
        # central stacked layout. The overlay covers the entire window
        # (titlebar, top bar, view, transport bar) until the page
        # signals it's fully rendered — masks every loading state so
        # the window appears fully populated all at once. Note that
        # main() defers `win.show()` until bridge.page_ready, so the
        # overlay is mostly defense-in-depth for the failsafe path
        # where show() lands before the page is actually ready.
        central_stack.addWidget(chrome)
        # Sidebar drawer — added BEFORE the loading overlay so the
        # overlay still wins during boot, but ABOVE the chrome so the
        # drawer surfaces over the content. Hidden by default; the
        # hamburger toggles it.
        from modules.sidebar import Sidebar
        self.sidebar = Sidebar(self)
        self.sidebar.settings_clicked.connect(self._open_settings)
        central_stack.addWidget(self.sidebar)
        self._loading_overlay = _LoadingOverlay()
        central_stack.addWidget(self._loading_overlay)  # added last → on top

        self.bus.open_main_window.connect(self._show_self)
        self.bus.playback_started.connect(lambda np: self.bus.notify_track.emit(np))

        # JT_NATIVE_ALBUM=1 → register Ctrl+Shift+A to open the currently-
        # playing album in the native page. Lets us A/B-compare against
        # the JF Web album view without rerouting normal navigation.
        if os.getenv("JT_NATIVE_ALBUM"):
            sc = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
            sc.activated.connect(self._open_currently_playing_album)
        # Ctrl+Shift+L → quick path to the native album grid scoped to
        # the user's music library. Useful as a "go to all music" hot
        # key regardless of where the user currently is. The same grid
        # also auto-shows when JF Web navigates to a Music library's
        # default Albums view (see _on_collection_resolved).
        sc_lib = QShortcut(QKeySequence("Ctrl+Shift+L"), self)
        sc_lib.activated.connect(self._show_native_music_grid)

        # Library ids are resolved lazily on first load — the start
        # destination preference picks which one we navigate to.
        self._library_ids: dict[str, str] = {}
        self._first_load_handled = False
        # Pre-fetched random library queue, primed in the background so
        # the first shuffle click after launch can install it instantly
        # instead of waiting for the REST round-trip. Refreshed after
        # each use so the next click also gets a snappy install.
        self._random_queue_cache: list[dict] = []
        # Re-entry guard for the library shuffle button — prevents a
        # double-click from kicking off two parallel installs. Cleared
        # at the end of each shuffle path (cached, async-loaded, error).
        self._shuffle_in_flight: bool = False
        self.page.loadFinished.connect(self._on_first_load)
        # Title sync — Jellyfin Web sets document.title to "<Section> | Jellyfin";
        # strip the suffix and feed the section into the top bar.
        self.view.titleChanged.connect(self._on_title_changed)
        # urlChanged also fires on hash navigation — refresh the View
        # dropdown's available tabs whenever the user enters/leaves a
        # library page.
        self.view.urlChanged.connect(self._on_url_changed)
        self.view.setUrl(QUrl(f"{server_url}/web/"))

        # Native sign-in surface: shown on boot when there are no
        # credentials in our store, or when the persisted token is
        # rejected by the server (admin revoked the device session).
        # The JF Web embed still loads in the background (needed for
        # the Account button until that's natively replaced), but the
        # login view sits above it in the content stack so the user
        # never sees JF Web's own login UI.
        from modules.login_view import LoginView
        self.login_view = LoginView(self)
        self.login_view.signed_in.connect(self._on_native_signed_in)
        self.content_stack.addWidget(self.login_view)
        if not self.api.is_authenticated:
            self.content_stack.setCurrentWidget(self.login_view)
            # Hide the loading overlay since there's nothing to load
            # until the user signs in.
            self._loading_overlay.hide()
        else:
            # We have a persisted token from a previous session.
            # Verify it's still valid against the server (the device
            # session may have been revoked by an admin). If it
            # fails, fall back to the LoginView. The verify is async
            # because verify_session is a network call.
            run_async(
                self.api.verify_session,
                on_result=self._on_verify_session_done,
                on_error=lambda _e: self._on_verify_session_done(False),
            )

    def _on_nav_requested(self, action: str):
        # Back / forward walk the JellyToast surface history (every
        # _show_* push is captured in _nav_history). JF Web's URL
        # history is no longer consulted — we're native-first and the
        # embed isn't usually visible.
        if action == "back":
            self._go_back()
            return
        if action == "forward":
            self._go_forward()
            return
        # Search opens the native SearchView instead of JF Web's
        # /search.html — owns the entire query → result → install path.
        if action == "search":
            self._show_search_view()
            return
        # Home routes to whichever native music surface the user picked
        # in Settings → General → "When Home is pressed, open:". Default
        # is the Albums grid — the canonical music landing.
        if action == "home":
            self._route_home()
            return

    def _toggle_sidebar(self):
        """Hamburger button → toggle the native sidebar drawer.
        Replaces the previous flow that drove Jellyfin Web's own
        drawer; the sidebar now hosts Settings + Account and will
        deepen in the in-progress settings overhaul."""
        self.sidebar.toggle()

    def _on_tab_requested(self, index: int, label: str):
        # Music → Albums and Music → Playlists have native renderings —
        # route to the grid instead of clicking JF Web's hidden tab
        # button. Other tabs (Artists / Songs / Genres) and other
        # library types still go through JF Web until they get native
        # renderings.
        if self.top_bar._view_collection == "music":
            lab = label.lower()
            if lab == "albums":
                self._show_native_music_grid("album")
                self.top_bar.set_active_tab(label)
                return
            if lab == "playlists":
                self._show_native_music_grid("playlist")
                self.top_bar.set_active_tab(label)
                return
            if lab in ("artists", "album artists"):
                self._show_native_music_grid("artist")
                self.top_bar.set_active_tab(label)
                return
            if lab == "songs":
                self._show_songs_view()
                self.top_bar.set_active_tab(label)
                return
            if lab == "genres":
                self._show_genres_view()
                self.top_bar.set_active_tab(label)
                return
            if lab == "suggestions":
                self._show_suggestions_view()
                self.top_bar.set_active_tab(label)
                return
        # Tab change targets the library view, so swap back from the
        # now-playing page (or native grid) if it's currently shown.
        self._show_web_view()
        # Click the corresponding hidden Jellyfin Web tab button. The
        # JS helper looks up by label first, then by index, then falls
        # back to URL-hash manipulation (?tab=N) — robust against
        # Jellyfin Web rearranging or relabeling its tab strip.
        safe = label.replace("'", "\\'")
        self.page.runJavaScript(
            f"window.__jellytoast_switch_tab && window.__jellytoast_switch_tab('{safe}', {index});"
        )
        # Optimistic UI update — don't wait for the DOM poll.
        self.top_bar.set_active_tab(label)

    def _on_url_changed(self, url: QUrl):
        # Pull the collectionType out of the URL so the View dropdown
        # shows the right per-library tabs (or hides on non-library
        # pages). Also drives the auto-route to the native album grid
        # when JF Web lands on a Music library's default view.
        self.page.runJavaScript(
            "window.__jellytoast_collection_type ? window.__jellytoast_collection_type() : '';",
            self._on_collection_resolved,
        )
        # Jellyfin Web renders the tab strip a beat after the URL
        # settles — poll twice with increasing delay so we catch it
        # whether the DOM is fast or slow.
        QTimer.singleShot(400, self._refresh_active_tab)
        QTimer.singleShot(1200, self._refresh_active_tab)
        # Album detail navigation → swap to native preview page. This is
        # the "click the edge of an album tile" path (the centered play
        # overlay on a tile is intent_detected, handled separately).
        self._maybe_intercept_album_detail(url)

    def _on_collection_resolved(self, collection_type):
        """JS callback for window.__jellytoast_collection_type. Used
        only to drive the top-bar View dropdown when the JF Web embed
        IS the visible surface (Account / preferences pages). Native
        surfaces own their own chrome via _apply_music_chrome — and
        we deliberately don't auto-swap content based on JF Web's URL
        anymore: JF Web's reloads (sign-out, server change, account
        navigation) shouldn't yank the user out of a native browse
        surface, and the only path INTO JF Web today is the Account
        button which the user clicks explicitly."""
        if self.content_stack.currentWidget() is self.view:
            self.top_bar.set_collection(collection_type or "")

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

    def _maybe_intercept_album_detail(self, url: QUrl):
        """If the URL is JF Web's album detail page, look up the item
        type and — when it's a MusicAlbum — swap the content stack to
        NowPlayingPage in preview mode. Other Jellyfin item types (Movie,
        Series, MusicArtist, …) pass through to the WebEngine as before."""
        fragment = url.fragment()
        if "/details" not in fragment or "?" not in fragment:
            return
        # URL fragment looks like "!/details?id=ABCDEF&serverId=XYZ".
        qs = fragment.split("?", 1)[1]
        item_id = ""
        for pair in qs.split("&"):
            if pair.startswith("id="):
                item_id = pair[3:]
                break
        if not item_id:
            return
        from modules.async_io import run_async
        run_async(
            self.api.get_item, item_id,
            on_result=lambda item, iid=item_id: self._on_detail_item_check(iid, item),
            on_error=lambda _e: None,
        )

    def _on_detail_item_check(self, item_id: str, item):
        # MusicAlbum + Playlist both share NowPlayingPage's preview mode
        # (the QueueContext model already supports both). Other types
        # (Artist / Movie / Series / …) pass through to the WebEngine
        # until they get their own native renderings.
        if not item:
            return
        kind = {"MusicAlbum": "album", "Playlist": "playlist"}.get(
            item.get("Type", ""), ""
        )
        if not kind:
            return
        # If the user has already navigated away from this URL by the time
        # the item check returns (rapid clicking), skip the swap so we
        # don't yank them out of the page they're now looking at.
        cur_fragment = self.view.url().fragment()
        if "/details" not in cur_fragment or item_id not in cur_fragment:
            return
        self._show_now_playing(preview_id=item_id, preview_kind=kind)

    def _refresh_active_tab(self):
        self.page.runJavaScript(
            "window.__jellytoast_active_tab ? window.__jellytoast_active_tab() : '';",
            self._on_active_tab_response,
        )

    def _on_active_tab_response(self, label):
        if label:
            self.top_bar.set_active_tab(label)

    def _on_title_changed(self, title: str):
        # Jellyfin Web's title only drives our top bar while the JF Web
        # embed is actually the visible surface. With native browse,
        # JF Web sits hidden in the content stack — its reloads (e.g.
        # after a native sign-in) shouldn't repaint our chrome with
        # the server name or whatever JF Web page happens to be open.
        if self.content_stack.currentWidget() is not self.view:
            return
        # Jellyfin Web pages typically set "<Section> | Jellyfin" — strip
        # the brand suffix so our top-bar label reads as just the section.
        if not title:
            self.top_bar.set_title("")
            return
        for suffix in (" | Jellyfin", " - Jellyfin", " — Jellyfin"):
            if title.endswith(suffix):
                title = title[: -len(suffix)]
                break
        if title.lower() == "jellyfin":
            title = ""
        self.top_bar.set_title(title)

    def _screen_touches(self) -> tuple[bool, bool, bool, bool]:
        """Return (left, top, right, bottom) — True iff the window edge
        is flush with the screen's available area.

        Maximize / fullscreen are detected via window-state flags on
        every platform.

        Snap-to-edge is harder. Qt6's `windowState()` enum has no tiled
        flag (KWin sends xdg_toplevel.tiled_left/right/top/bottom but
        QWidget eats them), and xdg-shell forbids clients from reading
        absolute position — so we can't ask "is my left edge at x=0?"
        directly on Wayland.

        - **X11**: position is reliable, use direct edge comparison.
        - **Wayland**: size heuristic. KWin's quick-tile produces
          predictable dimensions (half-w × full-h for Super+Left/Right,
          etc.). When those patterns match we treat all four edges as
          touching — the body fills its window rect, which is the
          desired outcome. Worst false positive: user manually resizes
          to exactly half-screen and the body fills instead of margining.
        """
        if self.isMaximized() or self.isFullScreen():
            return True, True, True, True
        screen = self.screen()
        if screen is None:
            return False, False, False, False
        avail = screen.availableGeometry()
        app = QApplication.instance()
        on_wayland = app is not None and app.platformName() == "wayland"
        if on_wayland:
            ww, wh = self.width(), self.height()
            sw, sh = avail.width(), avail.height()
            tol = 2
            half_w = abs(ww * 2 - sw) <= tol
            full_w = abs(ww - sw) <= tol
            half_h = abs(wh * 2 - sh) <= tol
            full_h = abs(wh - sh) <= tol
            tiled = (
                (half_w and full_h)   # left or right side snap
                or (full_w and half_h) # top or bottom snap
                or (half_w and half_h) # quarter snap (KDE 6+)
            )
            if tiled:
                return True, True, True, True
            return False, False, False, False
        geo = self.geometry()
        return (
            geo.left() <= avail.left(),
            geo.top() <= avail.top(),
            geo.right() >= avail.right(),
            geo.bottom() >= avail.bottom(),
        )

    def _compute_body_margins(self) -> tuple[int, int, int, int]:
        """Per-edge margins for the rounded body. Top is always 0 — the
        titlebar is anchored to the top of the body by design — so the
        only edges that change are L/R/B, which collapse when the
        corresponding edge is flush with the screen."""
        em = self.RESIZE_MARGIN
        L, _T, R, B = self._screen_touches()
        return (
            0 if L else em,
            0,
            0 if R else em,
            0 if B else em,
        )

    def _apply_body_margins(self):
        """Push computed margins into the chrome layout and queue a
        repaint so paintEvent's body rect lines up with where the
        children render."""
        l, t, r, b = self._compute_body_margins()
        cur = self._chrome_layout.contentsMargins()
        if (cur.left(), cur.top(), cur.right(), cur.bottom()) != (l, t, r, b):
            self._chrome_layout.setContentsMargins(l, t, r, b)
            self.update()

    def _build_body_path(self, body, tl: int, tr: int, br: int, bl: int) -> QPainterPath:
        """Body outline with per-corner radii. Corners flush against a
        collapsed (margin=0) edge are square so the rounded curve
        doesn't peel away from the screen edge."""
        x, y = float(body.x()), float(body.y())
        w, h = float(body.width()), float(body.height())
        p = QPainterPath()
        p.moveTo(x + tl, y)
        p.lineTo(x + w - tr, y)
        if tr > 0:
            p.quadTo(x + w, y, x + w, y + tr)
        else:
            p.lineTo(x + w, y)
        p.lineTo(x + w, y + h - br)
        if br > 0:
            p.quadTo(x + w, y + h, x + w - br, y + h)
        else:
            p.lineTo(x + w, y + h)
        p.lineTo(x + bl, y + h)
        if bl > 0:
            p.quadTo(x, y + h, x, y + h - bl)
        else:
            p.lineTo(x, y + h)
        p.lineTo(x, y + tl)
        if tl > 0:
            p.quadTo(x, y, x + tl, y)
        else:
            p.lineTo(x, y)
        p.closeSubpath()
        return p

    def paintEvent(self, e):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Hard-clear alpha first (WA_TranslucentBackground implies
            # WA_NoSystemBackground, so Qt won't auto-fill).
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            l, t, r, b = self._compute_body_margins()
            body = self.rect().adjusted(l, t, -r, -b)
            if body.width() <= 0 or body.height() <= 0:
                return  # mid-resize, nothing to draw
            # A corner is rounded only if NEITHER adjacent edge is flush
            # with the screen — otherwise the curve would peel off the
            # screen edge. Driven by _screen_touches (real edge contact),
            # not the computed margins (top is always 0 by design and
            # would falsely square every top corner).
            L, T, R, B = self._screen_touches()
            rad = self.BODY_RADIUS
            tl = 0 if (L or T) else rad
            tr = 0 if (R or T) else rad
            br = 0 if (R or B) else rad
            bl = 0 if (L or B) else rad
            path = self._build_body_path(body, tl, tr, br, bl)
            # Match the mini player's translucency so the whole app reads
            # as one frosted family. KWin blurs whatever's behind.
            p.setBrush(QColor(*BODY_COLOR))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()

    def showEvent(self, e):
        super().showEvent(e)
        # Reapply once after a short delay so Qt has finalized the X11
        # winId (immediately-after-show is sometimes a placeholder that
        # gets replaced). On native Wayland enable_kde_blur is a no-op,
        # so this is free; on XWayland one xprop subprocess does the job.
        # WindowStateChange below catches the maximize/restore reparent
        # case where the atom would otherwise be dropped.
        QTimer.singleShot(50, lambda: enable_kde_blur(self))

    def changeEvent(self, e):
        super().changeEvent(e)
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.Type.WindowStateChange:
            # Window state changes (maximize / restore / fullscreen) trigger
            # a reparent under KDE Plasma; the EWMH-style blur atom rides on
            # the X11 window and gets cleared in the process. Re-stamp it.
            QTimer.singleShot(50, lambda: enable_kde_blur(self))
            # Collapse / restore body margins so the rounded body fills
            # edge-to-edge when maximized and breathes when restored.
            self._apply_body_margins()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Snap-to-side from KWin doesn't fire a state change — the window
        # stays in Normal state but its geometry now touches a screen edge.
        # Recompute body margins on every resize so the snap edge collapses.
        self._apply_body_margins()

    def moveEvent(self, e):
        super().moveEvent(e)
        # Crossing screens (multi-monitor) or unsnapping via drag changes
        # which edges touch the available area; keep margins in sync.
        self._apply_body_margins()

    def _edges_at(self, pos):
        em = self.RESIZE_MARGIN
        cm = self.CORNER_MARGIN
        r = self.BODY_RADIUS
        w, h = self.width(), self.height()
        x, y = pos.x(), pos.y()

        # The body is painted inside (em, 0, w-em, h-em) with rounded
        # corners of radius r. Anchoring corner hit zones to the
        # window's bounding box puts them in the empty/transparent
        # corner gaps — clicking there feels like grabbing nothing.
        # Instead the hit zones live on the visible body's corners,
        # extending `cm` into the body and a bit `em` out to the
        # outer edge so the cursor finds resize as it approaches the
        # rounded corner from any direction.
        body_l, body_t = em, 0
        body_r, body_b = w - em, h - em
        # Each rounded corner's *arc center* — the resize zone is a
        # box of (em + r + cm) wide centered on it, clipped to that
        # corner's quadrant.
        corners = (
            ((body_l + r, body_t + r),
             Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
             lambda px, py, cx, cy: px <= cx and py <= cy),
            ((body_r - r, body_t + r),
             Qt.Edge.TopEdge | Qt.Edge.RightEdge,
             lambda px, py, cx, cy: px >= cx and py <= cy),
            ((body_l + r, body_b - r),
             Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
             lambda px, py, cx, cy: px <= cx and py >= cy),
            ((body_r - r, body_b - r),
             Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
             lambda px, py, cx, cy: px >= cx and py >= cy),
        )
        # Acceptable distance from the arc center: from `r - cm` (deep
        # into the body, just inside the rounded edge) out to `r + em`
        # (a bit past the visible edge, into the resize margin gap).
        # That gives a forgiving band that hugs the visible curve.
        inner = max(0, r - cm)
        outer = r + em
        for (cx, cy), edges, in_quadrant in corners:
            if not in_quadrant(x, y, cx, cy):
                continue
            dx = x - cx
            dy = y - cy
            d2 = dx * dx + dy * dy
            if inner * inner <= d2 <= outer * outer:
                return edges

        # Single-axis edges — tighter so the resize cursor doesn't
        # appear unnecessarily when the user is just near the edge.
        edges = Qt.Edge(0)
        if x <= em:
            edges |= Qt.Edge.LeftEdge
        elif x >= w - em:
            edges |= Qt.Edge.RightEdge
        if y <= em:
            edges |= Qt.Edge.TopEdge
        elif y >= h - em:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _cursor_for_edges(self, edges):
        if edges == Qt.Edge(0):
            return None
        if edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge,
                     Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in (Qt.Edge.RightEdge | Qt.Edge.TopEdge,
                     Qt.Edge.LeftEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeBDiagCursor
        if edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
            return Qt.CursorShape.SizeHorCursor
        if edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeVerCursor
        return None

    def mouseMoveEvent(self, e):
        if self.isMaximized() or self.isFullScreen():
            self.unsetCursor()
            return super().mouseMoveEvent(e)
        edges = self._edges_at(e.position().toPoint())
        cursor = self._cursor_for_edges(edges)
        if cursor is not None:
            self.setCursor(cursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e):
        if (e.button() == Qt.MouseButton.LeftButton
                and not self.isMaximized() and not self.isFullScreen()):
            edges = self._edges_at(e.position().toPoint())
            if edges != Qt.Edge(0):
                handle = self.windowHandle()
                if handle is not None:
                    try:
                        handle.startSystemResize(edges)
                    except Exception as ex:
                        print(f"[JellyToast] startSystemResize failed: {ex}", flush=True)
                    return
        super().mousePressEvent(e)

    def leaveEvent(self, e):
        self.unsetCursor()
        super().leaveEvent(e)

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
            libs = self.api.get_libraries()
            match = next((l for l in libs if l.get("CollectionType") == collection_type), None)
            lib_id = match.get("Id") if match else ""
        except Exception as e:
            print(f"[JellyToast] couldn't resolve {collection_type} library: {e}", flush=True)
            lib_id = ""
        if lib_id:
            self._library_ids[collection_type] = lib_id
        return lib_id or ""

    @Slot(bool)
    def _on_first_load(self, ok: bool):
        # On first successful page load, drive Jellyfin Web to the
        # music library so the bridge picks up credentials + library
        # state. The `_on_collection_resolved` URL-change handler then
        # auto-swaps to whichever native surface home_destination says
        # — Albums by default. AppRouter.showItem races with auth on
        # Jellyfin Web v10.11.7, so we wait for the My Media tile to
        # appear and click it the way a user would.
        if not ok or self._first_load_handled:
            return
        # Prime the random-queue cache once we have auth — the next
        # shuffle click will install it instantly.
        QTimer.singleShot(800, self._prime_random_queue_async)
        lib_id = self._resolve_library_id("music")
        if not lib_id:
            print("[JellyToast] no music library found; staying on JF home", flush=True)
            self._first_load_handled = True
            self.page.runJavaScript("window.__jellytoast_reveal && window.__jellytoast_reveal();")
            return
        self._first_load_handled = True
        fallback_hash = "#/music.html?topParentId="
        js = f"""
        (function() {{
            var libId = "{lib_id}";
            var fallbackHash = "{fallback_hash}";
            // Wait for the library page to actually render at least a
            // row of cards before revealing the body. Without this the
            // user sees the music page's header/scrubber render in
            // first and album cards pop in progressively, which reads
            // as a flicker. Falls back to a 6s deadline so an empty
            // library can't keep the body permanently hidden.
            function waitForCardsAndReveal() {{
                var deadline = Date.now() + 6000;
                var rev = setInterval(function() {{
                    var cards = document.querySelectorAll(
                        '.itemsContainer .card, .libraryPage .card, '
                        + '.pageTabContent .card, .libraryPage .listItem'
                    );
                    if (cards.length >= 6 || Date.now() > deadline) {{
                        clearInterval(rev);
                        window.__jellytoast_reveal && window.__jellytoast_reveal();
                    }}
                }}, 100);
            }}

            var attempts = 0;
            var iv = setInterval(function() {{
                attempts++;
                var ac = window.ApiClient;
                var signedIn = ac && typeof ac.getCurrentUserId === 'function'
                            && ac.getCurrentUserId();
                if (!signedIn) {{
                    if (attempts > 150) {{
                        console.log('[JellyToast] gave up waiting for sign-in');
                        clearInterval(iv);
                    }}
                    return;
                }}
                // Look for the My Media tile for this library. Jellyfin Web
                // renders these as <a href="#/...?topParentId=<id>"> or
                // <button data-id="<id>"> depending on release.
                var sel = [
                    'a[href*="topParentId=' + libId + '"]',
                    'a[href*="parentId=' + libId + '"]',
                    'a[data-id="' + libId + '"]',
                    'button[data-id="' + libId + '"]',
                    '[data-id="' + libId + '"] a',
                    '.card[data-id="' + libId + '"]',
                    '[data-id="' + libId + '"]',
                ];
                var tile = null;
                for (var i = 0; i < sel.length && !tile; i++) {{
                    tile = document.querySelector(sel[i]);
                }}
                if (tile) {{
                    var href = tile.getAttribute('href');
                    if (href && href.charAt(0) === '#') {{
                        window.location.hash = href;
                        console.log('[JellyToast] navigated via tile href: ' + href);
                    }} else {{
                        tile.click();
                        console.log('[JellyToast] clicked library tile (' + sel[i-1] + ')');
                    }}
                    clearInterval(iv);
                    waitForCardsAndReveal();
                    return;
                }}
                if (attempts > 150) {{
                    console.log('[JellyToast] gave up — no library tile found after 30s');
                    // Last-ditch: jump straight to the library page.
                    window.location.hash = fallbackHash + libId;
                    clearInterval(iv);
                    waitForCardsAndReveal();
                }}
            }}, 200);
        }})();
        """
        self.page.runJavaScript(js)

    def _open_settings(self):
        dlg = SettingsDialog(self)
        dlg.sign_out_requested.connect(self._on_sign_out_requested)
        dlg.server_change_requested.connect(self._on_server_change_requested)
        dlg.exec()

    @Slot(str, str, str)
    def _on_credentials_received(self, server_url: str, user_id: str, token: str):
        # Bridge from JF Web's localStorage session into our Python REST
        # client. JF Web is the source of truth for sign-in; without this
        # any /Users/{user_id}/... call from Python builds a malformed URL
        # (double slash) and 404s. Update unconditionally — re-pushes are
        # idempotent and let us re-sync after a manual sign-out / sign-in
        # in JF Web's UI.
        api_changed = (self.api.user_id != user_id or self.api.token != token
                       or self.api.server_url != server_url.rstrip("/"))
        if not api_changed:
            return
        self.api.server_url = server_url.rstrip("/")
        self.api.user_id = user_id
        self.api.token = token
        settings = get_settings()
        settings.server_url = self.api.server_url
        settings.user_id = user_id
        settings.access_token = token
        # Clear cached library lookups — they were resolved against the
        # previous (empty or stale) credentials and may now be wrong.
        self._library_ids = {}
        print(
            f"[JellyToast] credentials bridged from JF Web "
            f"(user={user_id[:8]}…)",
            flush=True,
        )
        # Retry any built native surface that's currently empty —
        # its first fetch may have used the stale persisted token
        # (api.is_authenticated returns True from boot once user_id +
        # token are restored from QSettings, but the server rejects
        # the call if the token has expired). Now that fresh
        # credentials have been pushed, re-fire the load.
        self._retry_empty_native_views()

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
            self.album_grid.load_items(
                self._resolve_library_id("music"), ""
            )
        if self.playlist_grid is not None and not self.playlist_grid._tiles:
            self.playlist_grid.load_items("", "")
        if self.artist_grid is not None and not self.artist_grid._tiles:
            self.artist_grid.load_items(
                self._resolve_library_id("music"), ""
            )
        if self.genres_view is not None and not self.genres_view._tiles:
            self.genres_view.load_genres()

    def _on_sign_out_requested(self):
        # Wipe persisted credentials in Python and JF Web's session,
        # then surface the native login view so the user can sign in
        # again without restarting the app.
        # Tell the server to revoke this device's session BEFORE we
        # clear the token locally — without this the row lingers in
        # the admin Devices dashboard until the user manually deletes
        # it. Synchronous (5s timeout, errors swallowed inside
        # server_logout) so we know the call completed before tearing
        # down credentials.
        self.api.server_logout()
        settings = get_settings()
        settings.access_token = ""
        settings.user_id = ""
        settings.username = ""
        try:
            self.api.token = ""
            self.api.user_id = ""
        except Exception:
            pass
        profile = self.page.profile()
        try:
            profile.cookieStore().deleteAllCookies()
        except Exception as e:
            print(f"[JellyToast] cookie clear failed: {e}", flush=True)
        # Clear JF Web's localStorage so the embed doesn't keep a
        # stale session in the background.
        self.page.runJavaScript(
            "try { localStorage.clear(); sessionStorage.clear(); } catch(e) {}"
        )
        # Drop any cached library ids resolved against the old user.
        self._library_ids = {}
        # Show the native sign-in surface.
        self.content_stack.setCurrentWidget(self.login_view)
        self._first_load_handled = False  # let _on_first_load run again

    def _on_server_change_requested(self):
        current = self.api.server_url
        url, ok = QInputDialog.getText(
            self, "JellyToast — Server URL",
            "Enter your Jellyfin server URL:",
            text=current or "http://",
        )
        if not ok or not url.strip():
            return
        new_url = url.strip().rstrip("/")
        if new_url == current:
            return
        get_settings().server_url = new_url
        # Switching servers means the old auth is invalid; clear it.
        self._on_sign_out_requested()
        self.api.server_url = new_url
        self.view.setUrl(QUrl(f"{new_url}/web/"))

    def _library_shuffle(self):
        # Re-entry guard so a rapid double-click of the shuffle button
        # doesn't kick off two parallel REST fetches and two competing
        # queue installs.
        if self._shuffle_in_flight:
            print(
                "[JellyToast] library shuffle skipped — already in flight",
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
            print("[JellyToast] no music library resolved; skipping library shuffle", flush=True)
            self._shuffle_in_flight = False
            return
        # Cache miss — fetch on the shared QThreadPool so the GUI
        # doesn't freeze for ~150-200ms while 500 random items load.
        run_async(
            self.api.get_random_audio_items, lib_id, limit=500,
            on_result=self._on_library_shuffle_loaded,
            on_error=self._on_library_shuffle_error,
        )

    def _on_library_shuffle_loaded(self, items):
        try:
            if not items:
                print("[JellyToast] library shuffle: API returned no tracks", flush=True)
                return
            self._install_shuffle_queue(items, "library shuffle")
            # Prime the cache for the next click while we're already
            # warmed up (lib_id resolved, API connection live).
            self._prime_random_queue_async()
        finally:
            self._shuffle_in_flight = False

    def _on_library_shuffle_error(self, e):
        print(f"[JellyToast] library shuffle fetch failed: {e}", flush=True)
        self._shuffle_in_flight = False

    def _install_shuffle_queue(self, items: list, source_label: str):
        """Install a randomly-ordered library queue and start it. The
        log line gives shuffle diagnostics (item count, unique album
        count) so the per-intent debugging picture stays readable when
        JT_SHUFFLE_DEBUG is on."""
        from modules.player_state import QueueContext, QueueKind, PlayerBus
        unique_albums = {it.get("AlbumId") for it in items if it.get("AlbumId")}
        print(
            f"[JellyToast] queue set via {source_label}: {len(items)} items, "
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
        run_async(
            self.api.get_random_audio_items, lib_id, limit=500,
            on_result=self._on_prime_random_queue_loaded,
            on_error=lambda e: print(
                f"[JellyToast] prime random queue failed: {e}", flush=True,
            ),
        )

    def _on_prime_random_queue_loaded(self, items):
        if items:
            self._random_queue_cache = items
            print(
                f"[JellyToast] random queue cache primed: {len(items)} items",
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
            self.content_stack.addWidget(self.np_page)
        # preview_id != "" → browse mode (preview an album/playlist
        # without disturbing the live queue). Empty → live mode.
        if preview_id:
            self.np_page.load_preview(preview_id, preview_kind)
        else:
            self.np_page.clear_preview()
        self.content_stack.setCurrentWidget(self.np_page)
        self._push_nav(lambda pid=preview_id, pk=preview_kind:
                        self._show_now_playing(pid, pk))

    def _dismiss_now_playing(self):
        """Back button on NowPlayingPage — walks the unified nav
        history. Falls back to the web view if there's nothing earlier
        to return to (only happens at app launch with no other surface
        recorded yet, which shouldn't be reachable in practice)."""
        if not self._go_back():
            self._show_web_view()

    def _show_web_view(self):
        self.content_stack.setCurrentWidget(self.view)
        self.np_bar.set_left_cluster_visible(True)
        # JF Web ships its own shuffle/sort/view controls inside its
        # library page, so our top-bar cluster steps out of the way
        # when the embed is what's showing.
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda: self._show_web_view())

    def _show_library_grid(self, kind: str, parent_id: str = "",
                            genre_id: str = ""):
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
                self.playlist_grid.browse_requested.connect(
                    lambda pid: self._show_now_playing(
                        preview_id=pid, preview_kind="playlist",
                    )
                )
                self.playlist_grid.play_requested.connect(
                    self._on_grid_play_playlist
                )
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
                self.album_grid.browse_requested.connect(
                    lambda aid: self._show_now_playing(
                        preview_id=aid, preview_kind="album",
                    )
                )
                self.album_grid.play_requested.connect(self._on_grid_play_album)
                self.content_stack.addWidget(self.album_grid)
            grid = self.album_grid

        # Re-fetch when scoping changes (parent_id OR genre_id) —
        # otherwise reuse the loaded tiles to avoid thrashing covers
        # when the user toggles back to the grid from another view.
        if (not grid._tiles
                or grid._parent_id != parent_id
                or grid._genre_id != genre_id):
            grid.load_items(parent_id, genre_id)
        self.content_stack.setCurrentWidget(grid)
        # The grid is its own browse surface — no need to also surface
        # the bottom-left now-playing cluster since the grid IS the
        # browsing context. Show it so the user can still see what's
        # playing while they browse.
        self.np_bar.set_left_cluster_visible(True)
        # Surface the library controls (Shuffle / View / Sort) cluster
        # in the top bar — they apply to the native grid only.
        self.top_bar.set_library_controls_visible(True)
        self._push_nav(lambda k=kind, pid=parent_id, gid=genre_id:
                        self._show_library_grid(k, pid, gid))

    def _on_library_sort_changed(self, sort_by: str, sort_order: str):
        # Apply to whichever native surface honors sort and is currently
        # visible. Genres view has no sort (no album-style metadata to
        # sort by).
        current = self.content_stack.currentWidget()
        sortables = (
            self.album_grid, self.playlist_grid, self.artist_grid,
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
            self.content_stack.addWidget(self.songs_view)
            self._kick_load_when_ready(
                lambda: self.songs_view.load_songs(
                    self._resolve_library_id("music")
                )
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
        from modules.player_state import QueueContext, QueueKind, PlayerBus
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
            self.suggestions_view.browse_requested.connect(
                lambda aid: self._show_now_playing(
                    preview_id=aid, preview_kind="album",
                )
            )
            self.suggestions_view.play_requested.connect(self._on_grid_play_album)
            self.content_stack.addWidget(self.suggestions_view)
            self._kick_load_when_ready(
                lambda: self.suggestions_view.load(
                    self._resolve_library_id("music")
                )
            )
        self.content_stack.setCurrentWidget(self.suggestions_view)
        self.np_bar.set_left_cluster_visible(True)
        # Suggestions is a curated surface — sort/view-toggle controls
        # don't apply, so hide the top-bar cluster.
        self.top_bar.set_library_controls_visible(False)
        self._push_nav(lambda: self._show_suggestions_view())

    def _on_verify_session_done(self, ok: bool):
        """Result of the boot-time verify. If the persisted token was
        rejected, drop the user on the LoginView so they can re-auth.
        Pre-existing settings (server URL, last username) stay so the
        form is partially pre-filled. The token itself stays in
        keyring; api.authenticate will overwrite it on success."""
        if ok:
            return
        print(
            "[JellyToast] persisted token rejected — showing login view",
            flush=True,
        )
        self.content_stack.setCurrentWidget(self.login_view)
        self._loading_overlay.hide()

    def _on_native_signed_in(self):
        """Called when the LoginView's authenticate round-trip
        succeeded. Credentials are already persisted (api.authenticate
        wrote them to QSettings + keyring); just route to the user's
        home destination and let the existing native flow take over.
        Library lookups are cleared so they re-resolve against the
        new credentials, and any built native surface that's empty
        gets retried (mirrors the credential-bridge handler)."""
        print(
            f"[JellyToast] native sign-in succeeded "
            f"(user={self.api.user_id[:8]}…)",
            flush=True,
        )
        self._library_ids = {}
        # Reload JF Web with the new credentials in place — its embed
        # is still used for the Account button. Without this it would
        # sit on its own login page in the background.
        self.view.setUrl(QUrl(f"{self.api.server_url}/web/"))
        # Route to home destination (Albums grid by default). This
        # also lazily builds the surface and kicks off its load.
        self._route_home()
        # Reveal the body now that we have a real destination to show.
        self._loading_overlay.hide()
        self._retry_empty_native_views()

    def _kick_load_when_ready(self, fn):
        """Run `fn` immediately if the Jellyfin REST credentials are
        bridged, or defer until they arrive. Used by native surfaces
        whose first fetch would otherwise race the cold-launch
        credential push (the JF Web shim signals credentials async,
        and a click on Songs / Genres / Suggestions before that
        lands would 401-fail and the surface would render 'No items'
        permanently because the empty result was cached at the view
        level)."""
        if self.api.is_authenticated:
            fn()
            return
        def _on_creds(*_):
            try:
                self.bridge.credentials_received.disconnect(_on_creds)
            except (TypeError, RuntimeError):
                pass
            fn()
        self.bridge.credentials_received.connect(_on_creds)

    # ── Navigation history ─────────────────────────────────────────────

    def _push_nav(self, thunk):
        """Append a 'show this surface again' thunk to the history. If
        the user navigated from a back state (pos < end), trim the
        forward branch first — same model as a browser history. The
        suppress flag short-circuits this during back/forward replay
        so the replay doesn't itself create a new history entry."""
        if self._suppress_nav_push:
            return
        # Trim forward history when branching from a back state.
        if self._nav_pos < len(self._nav_history) - 1:
            self._nav_history = self._nav_history[:self._nav_pos + 1]
        self._nav_history.append(thunk)
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
        self.top_bar.set_forward_enabled(
            self._nav_pos + 1 < len(self._nav_history)
        )

    def _route_home(self):
        """Top-bar Home button. Reads home_destination from Settings
        and swaps to the matching native music surface. Falls back to
        the Albums grid for unknown values (e.g. legacy keys after a
        rename) so Home is always functional."""
        # Drive the top-bar chrome ourselves — JF Web's URL-change
        # handler isn't reliable for this anymore (JF Web may sit on
        # a non-music page after native sign-in / sign-out, and we
        # don't want its title leaking into our top bar).
        self._apply_music_chrome()
        dest = get_settings().home_destination or "albums"
        if dest == "playlists":
            self._show_native_music_grid("playlist")
        elif dest == "artists":
            self._show_native_music_grid("artist")
        elif dest == "songs":
            self._show_songs_view()
        elif dest == "genres":
            self._show_genres_view()
        elif dest == "suggestions":
            self._show_suggestions_view()
        else:
            self._show_native_music_grid("album")

    def _apply_music_chrome(self):
        """Set the top bar's title + collection so the View dropdown
        appears and the section label reads "Music". Used whenever a
        native music surface becomes the active content widget so the
        chrome is right regardless of what JF Web's URL is doing."""
        self.top_bar.set_title("Music")
        self.top_bar.set_collection("music")

    def _show_search_view(self):
        """Lazy-build + swap to the native Search surface. Remembers the
        surface the user was on so dismiss returns there. The input is
        focused on every open so the user can type immediately."""
        if self.search_view is None:
            from modules.search_view import SearchView
            self.search_view = SearchView(self)
            self.search_view.songs_play_requested.connect(
                self._on_search_songs_play
            )
            self.search_view.album_play_requested.connect(
                self._on_grid_play_album
            )
            self.search_view.album_browse_requested.connect(
                lambda aid: self._show_now_playing(
                    preview_id=aid, preview_kind="album",
                )
            )
            self.search_view.artist_browse_requested.connect(
                self._show_artist_page
            )
            self.search_view.dismiss_requested.connect(
                self._dismiss_search_view
            )
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
            self._show_web_view()

    def _on_search_songs_play(self, start_idx: int, items: list):
        """Search → song row click. Installs the visible song results
        as a MANUAL queue starting at the clicked index. Source label
        carries 'Search' so the now-playing kicker reads honestly
        (vs. inheriting an album/playlist label that doesn't match)."""
        if not items or not (0 <= start_idx < len(items)):
            return
        from modules.player_state import QueueContext, QueueKind, PlayerBus
        ctx = QueueContext(kind=QueueKind.MANUAL, source_label="Search")
        PlayerBus.get().queue_play_now.emit(list(items), start_idx, ctx)

    def _on_genre_selected(self, genre_id: str, genre_name: str):
        """Genre tile click → swap to the album grid filtered by genre.
        Uses Jellyfin's ?GenreIds= filter (passed via load_items's
        genre_id arg). ParentId is left empty — the genre filter is
        sufficient and Jellyfin doesn't model genres as parents."""
        self._show_library_grid("album", parent_id="", genre_id=genre_id)

    def _show_artist_page(self, artist_id: str):
        """Lazy-build + swap to ArtistPage for the given artist. Click
        an album from there → existing browse path; back button walks
        the unified nav history."""
        if not artist_id:
            return
        if self.artist_page is None:
            from modules.artist_page import ArtistPage
            self.artist_page = ArtistPage(self)
            self.artist_page.dismiss_requested.connect(
                self._dismiss_artist_page
            )
            self.artist_page.album_browse_requested.connect(
                lambda aid: self._show_now_playing(
                    preview_id=aid, preview_kind="album",
                )
            )
            self.artist_page.album_play_requested.connect(
                self._on_grid_play_album
            )
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
            self._show_web_view()

    def _on_library_view_mode_changed(self, mode: str):
        # List-view rendering is queued for a follow-up — for now,
        # the toggle is informational only.
        if self.album_grid is None:
            return
        # Future: self.album_grid.set_view_mode(mode)

    def _on_grid_play_album(self, album_id: str):
        """Play-overlay click on an album tile — install the full album
        as the live queue, start from track 0."""
        self._grid_play_collection(
            album_id, "album", self.api.get_album_tracks,
        )

    def _on_grid_play_playlist(self, playlist_id: str):
        """Play-overlay click on a playlist tile — install the full
        playlist as the live queue, start from track 0."""
        self._grid_play_collection(
            playlist_id, "playlist", self.api.get_playlist_items,
        )

    def _grid_play_collection(self, item_id: str, kind: str, fetch_fn):
        """Shared install-and-play path for album/playlist tile play
        clicks. `kind` maps to the QueueKind installed; `fetch_fn` is
        the API call that returns the track list."""
        if not item_id:
            return
        from modules.async_io import run_async
        from modules.player_state import QueueContext, QueueKind, PlayerBus

        queue_kind = (QueueKind.PLAYLIST if kind == "playlist"
                      else QueueKind.ALBUM)

        def _on_tracks(tracks):
            if not tracks:
                return
            meta = self.api.get_item(item_id) or {}
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
        dlg = CastDialog(self.cast_manager, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_device:
            return
        dev = dlg.selected_device
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

        if dev.device_type == "chromecast":
            if playing_now:
                # Format-detect for direct play (FLAC stays FLAC, etc.)
                container = (np.raw.get("Container") if np.raw else "") or ""
                mime = self.cast_manager.chromecast_audio_mime_for(container) if np.is_audio else None
                url = np.stream_url
                if np.is_audio and mime is None:
                    api = get_api()
                    url = (
                        f"{api.server_url}/Audio/{np.item_id}/stream.mp3"
                        f"?api_key={api.token}"
                        f"&MaxStreamingBitrate=320000&AudioCodec=mp3"
                    )
                    mime = "audio/mpeg"
                ok = self.cast_manager.cast_to_chromecast(
                    dev, url, np.title, np.thumb_url,
                    is_audio=np.is_audio, content_type=mime,
                    current_time=resume_seconds,
                )
            else:
                ok = self.cast_manager.connect_to_chromecast(dev)
        else:
            # AirPlay v1 has no real "connect without media" handshake;
            # if there's nothing to cast, just record the choice. Calls
            # to play() afterward will issue POST /play to this device.
            if playing_now:
                ok = self.cast_manager.cast_to_airplay(dev, np.stream_url, np.title)
            else:
                self.cast_manager.active_cast = dev
                ok = True

        if ok:
            self.bus.cast_started.emit(dev.name)
            if playing_now:
                # Re-render the now-playing UI so the title, artist,
                # cover art, and progress bar reflect the track that's
                # now on the cast device. Without this, the bar shows
                # "Nothing playing" because of the prior playback_stopped.
                self.bus.playback_started.emit(np)
        else:
            QMessageBox.warning(self, "Cast failed", f"Could not cast to {dev.name}.")

    def closeEvent(self, e):
        # _quitting is set by the tray's "Quit JellyToast" handler so
        # that path bypasses the minimize-to-tray divert and actually
        # exits. Without this, app.quit() fires the implicit close
        # cascade, this handler ignores the event, and the app stays
        # alive with the mini player still floating + audio still
        # playing.
        if getattr(self, "_quitting", False) or not get_settings().minimize_to_tray:
            QApplication.instance().quit()
        else:
            self.hide()
            e.ignore()


def _read_qresource(path: str) -> str:
    f = QFile(path)
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        return ""
    data = bytes(f.readAll().data()).decode("utf-8", errors="replace")
    f.close()
    return data


def _send_startup_notification_remove(startup_id: str):
    """Tell KDE the startup is complete by sending the freedesktop
    startup-notification 'remove' message via X11 ClientMessage.
    KDE listens for these on the root window and stops the bouncing
    cursor / 'launching' taskbar entry on receipt. Normally Qt sends
    this automatically when the first window maps — we suppress that
    by popping DESKTOP_STARTUP_ID from os.environ before QApplication
    init, then call this when we're actually ready to be seen."""
    if not startup_id:
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
            chunk = msg[i:i + 20].ljust(20, b"\x00")
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
        print(f"[JellyToast] startup-notify remove failed: {e}", file=sys.stderr)


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    # Capture and suppress DESKTOP_STARTUP_ID before QApplication init.
    # X11 only: Qt's xcb plugin reads this env var and auto-sends the
    # 'remove' message when the first window maps; popping forces Qt
    # silent so we control the bounce-stop timing. On Wayland the
    # equivalent token is XDG_ACTIVATION_TOKEN, handled automatically
    # by Qt6 — leave it untouched.
    if _will_be_wayland():
        _startup_id = ""
    else:
        _startup_id = os.environ.pop("DESKTOP_STARTUP_ID", "")
    app = QApplication(sys.argv)
    app.setApplicationName("JellyToast")
    app.setApplicationDisplayName("JellyToast")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("JellyToast")
    app.setDesktopFileName("jellytoast")
    app.setWindowIcon(QIcon(make_app_icon(64)))
    app.setQuitOnLastWindowClosed(False)
    # Authoritative platform check — what Qt actually picked. After this
    # point prefer IS_WAYLAND over _will_be_wayland().
    IS_WAYLAND = (app.platformName() == "wayland")

    # Single-instance gate. Held by QSharedMemory; the QLocalServer is
    # the message channel for "raise me" pings from subsequent launch
    # attempts. We bind the result to `app` so it shares the app's
    # lifetime — letting it GC would release the shared-memory lock
    # mid-run and effectively disable the check.
    from modules.single_instance import SingleInstance
    app._single_instance = SingleInstance("JellyToast", app)
    if not app._single_instance.acquire():
        # Another instance was already running — signal it to surface
        # and exit cleanly. Print a small breadcrumb so a CLI launcher
        # (terminal, .desktop file, autostart) can see what happened.
        print("JellyToast is already running; raised existing window.", flush=True)
        sys.exit(0)

    if not WEBENGINE_AVAILABLE:
        QMessageBox.critical(
            None, "Missing dependency",
            "JellyToast requires PySide6 with QtWebEngine.\n\n"
            "Install with:\n    sudo pacman -S pyside6\n\n"
            f"Original error: {_WEBENGINE_ERROR}"
        )
        sys.exit(1)

    if not MPV_AVAILABLE:
        QMessageBox.critical(
            None, "Missing dependency",
            "JellyToast requires libmpv.\nInstall with:\n    sudo pacman -S mpv"
        )
        sys.exit(1)

    settings = get_settings()
    server_url = settings.server_url.rstrip("/")
    if not server_url:
        url, ok = QInputDialog.getText(
            None, "JellyToast — Server URL",
            "Enter your Jellyfin server URL:",
            text="http://"
        )
        if not ok or not url.strip():
            sys.exit(0)
        server_url = url.strip().rstrip("/")
        settings.server_url = server_url

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(
            None, "No system tray",
            "Your desktop doesn't appear to have a system tray.\n"
            "JellyToast will run, but tray features will be unavailable."
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
    mpris: "MprisService | None" = None

    win = JellyToastWindow(server_url)
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
    bus.show_mini_player.connect(lambda: (mini.show(), mini.raise_(), mini.activateWindow()))
    bus.hide_mini_player.connect(mini.hide)
    tray = TrayController(app, mini, win)

    def _post_show_init():
        """Heavy startup work moved here so it runs after the window
        is visible. Order matters: mpv must exist before we wire the
        cast manager, and the volume signal must reach mpv after its
        slot is connected."""
        nonlocal mpv_ctrl, mpris
        mpv_ctrl = MpvController()
        mpv_ctrl.set_cast_manager(win.cast_manager)
        bus.volume_changed.emit(settings.volume)

        mpris = MprisService()
        mpris.start()

        # KWin rule install (mini-player keep-above) is idempotent and
        # lands compositor-side any time — doesn't need to be live for
        # first paint.
        if settings.mini_player_keep_above:
            from modules.kwin_rules import install_mini_player_rule
            install_mini_player_rule()

    QTimer.singleShot(0, _post_show_init)

    # Compute target on-screen position before we go anywhere weird.
    screen_geom = app.primaryScreen().availableGeometry()
    target_w, target_h = win.width(), win.height()
    target_x = screen_geom.x() + (screen_geom.width() - target_w) // 2
    target_y = screen_geom.y() + (screen_geom.height() - target_h) // 2

    if IS_WAYLAND:
        # Wayland: setWindowOpacity is unreliable on top-level surfaces
        # (the protocol exposes no portable per-surface alpha for shells)
        # and absolute QWidget.move() is forbidden. The X11 "show
        # invisibly, wait for art, then reveal" trick collapses on
        # Wayland — try to wait for art before show() and the JS
        # loop counts blurhash placeholders as "loaded" (they finish
        # decoding instantly because they're tiny inline SVG), so
        # pageRendered fires before the real images swap in and the
        # window appears with placeholder grit.
        # Instead: show at pageReady (DOM laid out, cards in place)
        # with the loading overlay already hidden, and accept a brief
        # image-fill phase. Real images load via JF Web's normal
        # viewport-driven path — slightly progressive but no grit.
        _shown = {"done": False}

        def _wl_show():
            if _shown["done"]:
                return
            _shown["done"] = True
            win._loading_overlay.hide()
            win.show()

        win.bridge.page_ready.connect(_wl_show)
        # Failsafe: never let auth/network hangs leave the user with
        # nothing on screen. 8s matches the SHIM_JS reveal failsafe.
        QTimer.singleShot(8000, _wl_show)
    else:
        # X11 path: opacity 0 hides pixels but the window is still
        # mapped, so KWin/XWayland routes hover events to it. Without
        # moving off-screen the user sees the system cursor "react" to
        # the invisible window edges and titlebar. setGeometry() before
        # show() doesn't help — KWin's placement policy overrides
        # pre-map geometry for frameless+translucent windows. Workaround:
        # show first, then move off-screen on the next event-loop tick
        # when KWin honors configure requests. Chromium still treats
        # the view as visible because opacity is a compositor effect,
        # not a visibility signal.
        win.setWindowOpacity(0.0)
        win.show()
        QTimer.singleShot(0, lambda: win.move(-50000, -50000))

        _revealed = {"done": False}

        def _reveal():
            if _revealed["done"]:
                return
            _revealed["done"] = True
            win.move(target_x, target_y)
            win.setWindowOpacity(1.0)
            win._loading_overlay.hide()
            # Tell KDE the launch is complete via _NET_STARTUP_INFO
            # ClientMessage — bounce stops, taskbar entry transitions
            # from 'launching' to active.
            _send_startup_notification_remove(_startup_id)

        win.bridge.page_rendered.connect(_reveal)
        # Failsafe — always reveal eventually, even if the bridge
        # round-trip never lands. Cold-cache image loads can legitimately
        # take 3-5s; 15s is comfortable headroom.
        QTimer.singleShot(15000, _reveal)

    if settings.show_mini_on_start:
        mini.show()

    def _cleanup():
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
        try:
            win.cast_manager.cleanup()
        except Exception:
            pass

    app.aboutToQuit.connect(_cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

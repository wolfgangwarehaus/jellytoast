"""
JellyToast — fully-native Linux desktop Jellyfin client.

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

from modules.platform_compat import is_wayland, will_be_wayland  # noqa: E402

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
    if will_be_wayland():
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

from PySide6.QtCore import QTimer, Qt, Slot
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QSystemTrayIcon, QWidget,
    QVBoxLayout, QStackedLayout, QStackedWidget,
    QDialog, QInputDialog,
)

from modules.player_state import (
    PlayerBus, get_now_playing, QueueContext, QueueKind,
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
    make_app_icon, GLOBAL_STYLE, BODY_COLOR,
)


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




class JellyToastWindow(QMainWindow):
    # Server-side decorations: KWin renders the titlebar, window
    # controls, corner radius, and resize handles. The class keeps
    # WA_TranslucentBackground so the body card-color reads at the
    # correct alpha, but no longer paints its own corners or resize
    # edges.

    def __init__(self, server_url: str):
        super().__init__()
        self.setWindowTitle("JellyToast")
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
        self.setMinimumSize(720, 440)
        self.resize(1280, 820)
        # Restore previous window geometry if persisted. Done after
        # the default resize so an empty / corrupt blob falls back to
        # the 1280x820 default cleanly. restoreGeometry returns False
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
        self.setStyleSheet(GLOBAL_STYLE + """
            QMainWindow#jtMain { background: transparent; }
            QWidget#jtCentral { background: transparent; }
        """)

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
                "[JellyToast] JT_OPAQUE=1: skipping WA_TranslucentBackground "
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
        QTimer.singleShot(4000, self.cast_manager.discover_all)
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

        self.top_bar = JtTopBar(chrome)
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

        # Wire the chrome into the central stacked layout. The
        # window stays hidden until _do_boot_auth_check finishes
        # building the initial surface (see __init__ end + main());
        # there's no boot-time loading overlay because the user
        # never sees a partially-constructed window.
        central_stack.addWidget(chrome)

        self.bus.open_main_window.connect(self._show_self)
        self.bus.playback_started.connect(lambda np: self.bus.notify_track.emit(np))

        # JT_NATIVE_ALBUM=1 → register Ctrl+Shift+A to open the currently-
        # playing track's album in NowPlayingPage's preview. Opt-in
        # because there are already several paths to the album (song
        # row click, Now-Playing-bar tap) and a default Ctrl+Shift+A
        # would conflict with users' other muscle memory.
        if os.getenv("JT_NATIVE_ALBUM"):
            sc = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
            sc.activated.connect(self._open_currently_playing_album)
        # Ctrl+Shift+L → quick path to the native album grid scoped to
        # the user's music library. Useful as a "go to all music" hot
        # key regardless of where the user currently is.
        sc_lib = QShortcut(QKeySequence("Ctrl+Shift+L"), self)
        sc_lib.activated.connect(self._show_native_music_grid)
        # Search hotkeys — Ctrl+F (find) and / (vim/Slack convention).
        # Both open the native SearchView and focus its input. If
        # search is already the current surface, focus_input is
        # idempotent — selectAll lets the user retype over the prior
        # query in one motion.
        for keyseq in ("Ctrl+F", "/"):
            sc = QShortcut(QKeySequence(keyseq), self)
            sc.activated.connect(self._show_search_view)

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
        # Tell KDE the launch is complete so the taskbar entry stops
        # bouncing and transitions from 'launching' to active. The
        # startup id was stashed by main() during construction.
        startup_id = getattr(self, "_startup_id", "")
        if startup_id:
            _send_startup_notification_remove(startup_id)

    def _on_nav_requested(self, action: str):
        # Back / forward walk the JellyToast surface history — every
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
        elif lab in ("artists", "album artists"):
            self._show_native_music_grid("artist")
        elif lab == "songs":
            self._show_songs_view()
        elif lab == "genres":
            self._show_genres_view()
        elif lab == "suggestions":
            self._show_suggestions_view()
        else:
            return
        self.top_bar.set_active_tab(label)

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
        # Fill the body inside the client area; KWin's server-side
        # decoration handles the corner radius, snap edges, and resize
        # affordances. `_body_qcolor` was computed in __init__ — full
        # alpha in JT_OPAQUE=1 mode (no compositor blend → no buffer-
        # attach race for Sunshine's screencopy to grab), theme-alpha
        # otherwise.
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), self._body_qcolor)
        finally:
            p.end()

    def changeEvent(self, e):
        # Catch Qt 6's authoritative cross-DPR event so subscribers
        # (LibraryGrid, NowPlayingBar, MiniPlayer, NowPlayingPage) can
        # re-issue cover loads sized for the new physical target. Fires
        # when the user drags JellyToast between monitors of different
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
                print(f"[JellyToast] dpr_changed emit failed: {exc}",
                      file=sys.stderr)
        super().changeEvent(e)

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
            match = next((lib for lib in libs if lib.get("CollectionType") == collection_type), None)
            lib_id = match.get("Id") if match else ""
        except Exception as e:
            print(f"[JellyToast] couldn't resolve {collection_type} library: {e}", flush=True)
            lib_id = ""
        if lib_id:
            self._library_ids[collection_type] = lib_id
        return lib_id or ""

    @Slot(bool)
    def _open_settings(self):
        dlg = SettingsDialog(self)
        # Close the dialog before tearing down credentials so the
        # LoginView underneath becomes visible immediately — otherwise
        # the modal sits on top of it until the user dismisses it.
        dlg.sign_out_requested.connect(dlg.accept)
        dlg.sign_out_requested.connect(self._on_sign_out_requested)
        dlg.server_change_requested.connect(dlg.accept)
        dlg.server_change_requested.connect(self._on_server_change_requested)
        dlg.exec()

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
            self.queue_mgr, self.np_bar,
            self.album_grid, self.playlist_grid, self.artist_grid,
            self.songs_view, self.genres_view, self.suggestions_view,
            self.search_view, self.np_page, self.artist_page,
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
        # Show the native sign-in surface.
        self.content_stack.setCurrentWidget(self.login_view)

    def _on_server_change_requested(self):
        current = self.provider.server_url
        url, ok = QInputDialog.getText(
            self, "JellyToast — Server URL",
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
        # doesn't freeze while the random items load. Limit comes from
        # Settings (default 100) — smaller queues commit faster after
        # a drag-reorder since _populate_rows rebuilds every row.
        shuffle_n = get_settings().shuffle_queue_size
        run_async(
            self.provider.get_random_audio_items, lib_id, limit=shuffle_n,
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
        from modules.player_state import PlayerBus
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
        shuffle_n = get_settings().shuffle_queue_size
        run_async(
            self.provider.get_random_audio_items, lib_id, limit=shuffle_n,
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
                preview_id=album_id, preview_kind="album",
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
        self._push_nav(lambda pid=preview_id, pk=preview_kind:
                        self._show_now_playing(pid, pk))

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

    def _show_library_grid(self, kind: str, parent_id: str = "",
                            genre_id: str = "", year: str = ""):
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
                    self._browse_album
                )
                self.album_grid.play_requested.connect(self._on_grid_play_album)
                # Subtitle-click on an album tile → ArtistPage. Year-
                # click → re-load the album grid filtered to that year.
                self.album_grid.artist_browse_requested.connect(
                    self._show_artist_page
                )
                self.album_grid.year_browse_requested.connect(
                    self._show_albums_by_year
                )
                self.content_stack.addWidget(self.album_grid)
            grid = self.album_grid

        # Re-fetch when scoping changes (parent_id / genre_id / year)
        # — otherwise reuse the loaded tiles to avoid thrashing covers
        # when the user toggles back to the grid from another view.
        prev_year = getattr(grid, "_year", "")
        if (not grid._tiles
                or grid._parent_id != parent_id
                or grid._genre_id != genre_id
                or prev_year != year):
            grid.load_items(parent_id, genre_id, year)
        self.content_stack.setCurrentWidget(grid)
        # The grid is its own browse surface — no need to also surface
        # the bottom-left now-playing cluster since the grid IS the
        # browsing context. Show it so the user can still see what's
        # playing while they browse.
        self.np_bar.set_left_cluster_visible(True)
        # Surface the library controls (Shuffle / View / Sort) cluster
        # in the top bar — they apply to the native grid only.
        self.top_bar.set_library_controls_visible(True)
        self._push_nav(lambda k=kind, pid=parent_id, gid=genre_id, y=year:
                        self._show_library_grid(k, pid, gid, y))

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
            self.songs_view.album_browse_requested.connect(
                self._browse_album
            )
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
            self.suggestions_view.browse_requested.connect(
                self._browse_album
            )
            self.suggestions_view.play_requested.connect(self._on_grid_play_album)
            self.suggestions_view.artist_browse_requested.connect(
                self._show_artist_page
            )
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
            f"[JellyToast] native sign-in succeeded "
            f"(user={self.provider.user_id[:8]}…)",
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
            self.search_view.songs_play_requested.connect(
                self._on_search_songs_play
            )
            self.search_view.album_play_requested.connect(
                self._on_grid_play_album
            )
            self.search_view.album_browse_requested.connect(
                self._browse_album
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
            self.artist_page.dismiss_requested.connect(
                self._dismiss_artist_page
            )
            self.artist_page.album_browse_requested.connect(
                self._browse_album
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
            album_id, "album", self.provider.get_album_tracks,
        )

    def _on_grid_play_playlist(self, playlist_id: str):
        """Play-overlay click on a playlist tile — install the full
        playlist as the live queue, start from track 0."""
        self._grid_play_collection(
            playlist_id, "playlist", self.provider.get_playlist_items,
        )

    def _grid_play_collection(self, item_id: str, kind: str, fetch_fn):
        """Shared install-and-play path for album/playlist tile play
        clicks. `kind` maps to the QueueKind installed; `fetch_fn` is
        the API call that returns the track list."""
        if not item_id:
            return
        from modules.async_io import run_async
        from modules.player_state import PlayerBus

        queue_kind = (QueueKind.PLAYLIST if kind == "playlist"
                      else QueueKind.ALBUM)

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
                    # Transcode-fallback URL is provider-specific
                    # (Jellyfin's /Audio/{id}/stream.mp3 vs Subsonic's
                    # /rest/stream?format=mp3). The provider knows.
                    url = get_provider().get_audio_transcode_url(
                        np.item_id, max_bitrate_kbps=320, codec="mp3",
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
            #
            # AirPlay 2 receivers that need pairing get intercepted
            # here: launch the pairing modal before attempting cast.
            # If pairing succeeds the credentials are persisted by the
            # dialog itself and the cast retries.
            from modules import airplay2 as _ap2
            is_ap2 = isinstance(dev.cast_object, _ap2.AirPlay2Device)
            print(
                f"[ap2-dbg] cast handler: dev={dev.name!r} type={dev.device_type} "
                f"is_ap2={is_ap2} playing_now={playing_now}",
                flush=True,
            )
            if is_ap2:
                ap2_obj = dev.cast_object  # type: ignore[assignment]
                stored = _ap2.get_stored_credentials(ap2_obj.identifier)
                print(
                    f"[ap2-dbg] ap2 device: id={ap2_obj.identifier!r} "
                    f"requires_pairing={ap2_obj.requires_pairing} "
                    f"stored_creds_len={len(stored)}",
                    flush=True,
                )
            if (is_ap2
                    and dev.cast_object.requires_pairing
                    and not _ap2.get_stored_credentials(dev.cast_object.identifier)):
                from modules.airplay_pairing import PairingDialog
                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                print(f"[ap2-dbg] launching pairing dialog for {ap2_dev.name!r}", flush=True)
                creds = PairingDialog.run(self, ap2_dev)
                print(f"[ap2-dbg] pairing dialog returned: creds_len={len(creds)}", flush=True)
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
                print(
                    f"[ap2-dbg] calling cast_to_airplay url_len={len(np.stream_url)} "
                    f"title={np.title!r}",
                    flush=True,
                )
                ok = self.cast_manager.cast_to_airplay(dev, np.stream_url, np.title)
                print(f"[ap2-dbg] cast_to_airplay returned ok={ok}", flush=True)
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


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
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
    app.setApplicationName("JellyToast")
    app.setApplicationDisplayName("JellyToast")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("JellyToast")
    app.setDesktopFileName("jellytoast")
    app.setWindowIcon(QIcon(make_app_icon(64)))
    app.setQuitOnLastWindowClosed(False)

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
    app._single_instance = SingleInstance("JellyToast", app)
    if not app._single_instance.acquire():
        # Another instance was already running — signal it to surface
        # and exit cleanly. Print a small breadcrumb so a CLI launcher
        # (terminal, .desktop file, autostart) can see what happened.
        print("JellyToast is already running; raised existing window.", flush=True)
        sys.exit(0)

    if not MPV_AVAILABLE:
        QMessageBox.critical(
            None, "Missing dependency",
            "JellyToast requires libmpv.\n\n"
            "Install mpv from your system package manager, "
            "or download it from https://mpv.io."
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
    mpris: "MediaControlsService | None" = None

    win = JellyToastWindow(server_url)
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

    QTimer.singleShot(0, _post_show_init)

    # No eager win.show() — _do_boot_auth_check builds the initial
    # surface (home destination on success, login on failure) and
    # then calls self.show() via _reveal_window. That guarantees
    # first paint shows fully-populated content rather than the dark
    # → fade flicker the loading overlay used to mask.

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

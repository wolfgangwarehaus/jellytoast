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

# mpv `wid` embedding requires XWayland on Wayland sessions
if "WAYLAND_DISPLAY" in os.environ and "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "xcb"

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from PyQt6.QtCore import (
    QObject, QUrl, QFile, QIODevice, QTimer, Qt, pyqtSlot, pyqtSignal,
)
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QSystemTrayIcon, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialog, QInputDialog,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import (
        QWebEngineScript, QWebEngineProfile, QWebEnginePage,
        QWebEngineUrlRequestInterceptor,
    )
    from PyQt6.QtWebChannel import QWebChannel
    WEBENGINE_AVAILABLE = True
    _WEBENGINE_ERROR = ""
except ImportError as e:
    WEBENGINE_AVAILABLE = False
    _WEBENGINE_ERROR = str(e)

from modules.player_state import PlayerBus, get_now_playing
from modules.player_backend import MpvController, MPV_AVAILABLE
from modules.queue_manager import QueueManager
from modules.now_playing_bar import NowPlayingBar, CastDialog
from modules.mini_player import FloatingMiniPlayer
from modules.tray import TrayController
from modules.mpris import MprisService
from modules.cast_manager import CastManager
from modules.top_bar import JtTopBar
from modules.settings_dialog import SettingsDialog
from modules.jellyfin_api import get_api
from modules.settings import get_settings
from modules.ui_helpers import make_app_icon, GLOBAL_STYLE, TEXT, TEXT_DIM, BODY_COLOR


# JS shim: hides Jellyfin Web's now-playing bar and confirms the bridge is up.
# Playback detection happens in Python via the URL request interceptor below.
SHIM_JS = r"""
(function() {
  if (window.__jellytoast_installed) return;
  window.__jellytoast_installed = true;
  console.log('[JellyToast] shim loaded at', location.href);

  // Hide the page until we've finished navigating to the music library.
  // _on_first_load (Python) calls window.__jellytoast_reveal() once it's done.
  document.documentElement.classList.add('jt-loading');
  window.__jellytoast_reveal = function() {
    document.documentElement.classList.remove('jt-loading');
  };
  // Failsafe: never stay hidden for more than 8s, even if navigation hangs.
  setTimeout(window.__jellytoast_reveal, 8000);

  function bind() {
    if (typeof QWebChannel === 'undefined' || !window.qt || !qt.webChannelTransport) {
      return setTimeout(bind, 50);
    }
    new QWebChannel(qt.webChannelTransport, function(channel) {
      window.jellytoast = channel.objects.bridge;
      console.log('[JellyToast] bridge ready');
    });
  }

  function injectCSS() {
    if (document.getElementById('jellytoast-css')) return;
    const style = document.createElement('style');
    style.id = 'jellytoast-css';
    style.textContent = `
      /* Make Jellyfin Web's app shell transparent so the host window's
         frosted body shows through. Library cards keep their own fills.
         (.mainDrawer is intentionally NOT in this list — see below; it
         gets a frosted background of its own.) */
      html, body,
      .skinBody, .skinBody-withBackdrop,
      .mainAnimatedPages, .mainAnimatedPage,
      .page, .libraryPage, .itemDetailPage, .homePage, .homePage-content,
      .pageContainer, .dialogBackdrop,
      .backdropContainer, .backgroundContainer,
      .mainAnimatedPagesContainer { background: transparent !important; }
      /* Hide Jellyfin Web's .skinHeader entirely — JellyToast paints its
         own native top bar (modules/top_bar.py) above the WebView. Then
         reclaim the empty top strip the .skinHeader used to occupy by
         pulling .skinBody up to top: 0. Without this, content would
         start ~7em down the WebView with empty space above it. */
      .skinHeader,
      .skinHeader-withBackground,
      .skinHeader.semiTransparent { display: none !important; }
      .skinBody,
      .skinBody-withBackdrop { top: 0 !important; }
      /* Pages bake spacing in for the now-hidden .skinHeader. Hit
         padding-top, margin-top, AND top: in case it's an absolute. */
      .page,
      .libraryPage,
      .itemDetailPage,
      .homePage,
      .padded-top-page,
      .padded-top,
      .pageContainer,
      .mainAnimatedPagesContainer,
      .mainAnimatedPages,
      .libraryHeader,
      .headerSpacer,
      .headerSection,
      .padded-top-section,
      .padded-top-headroom,
      .pageWithAbsoluteTabs,
      .withTabs {
        padding-top: 0 !important;
        margin-top: 0 !important;
        top: 0 !important;
      }
      .libraryPage > .pageTabContent,
      .absolutePageTabContent,
      .libraryPage .padded-top { top: 0 !important; }
      .mainDrawer,
      .mainDrawer-scrollContainer {
        background: rgba(24, 24, 24, 0.72) !important;
        box-shadow: none !important;
        border-right: none !important;
      }
      /* Hide all scrollbars — Jellyfin Web has its own letter scrubber
         and infinite-scroll cards, the chrome scrollbar is just noise. */
      ::-webkit-scrollbar { width: 0 !important; height: 0 !important;
        background: transparent !important; }
      ::-webkit-scrollbar-thumb,
      ::-webkit-scrollbar-track,
      ::-webkit-scrollbar-corner { background: transparent !important; }
      * { scrollbar-width: none !important; }
      html.jt-loading body { opacity: 0 !important; }
      html:not(.jt-loading) body { transition: opacity 200ms ease-in; }
      .nowPlayingBar,
      .nowPlayingBarTop,
      .nowPlayingBarBottom { display: none !important; }
      .mainAnimatedPages { padding-bottom: 0 !important; }
      /* Cast button in the header — non-functional in JellyToast (we have
         our own cast manager in the now-playing bar). */
      .headerCastButton,
      .btnCast,
      button[is="paper-icon-button-light"].headerCastButton { display: none !important; }
      /* Hide dialogs/toasts until our observer has approved them.
         This kills the "Playback failed" flash before it can paint. */
      dialog:not(.jt-checked),
      .dialog:not(.jt-checked),
      .dialogContainer:not(.jt-checked),
      .paperDialog:not(.jt-checked),
      .formDialog:not(.jt-checked),
      .actionSheet:not(.jt-checked),
      .actionsheet:not(.jt-checked),
      .toast:not(.jt-checked),
      .toast-message:not(.jt-checked),
      [role="alertdialog"]:not(.jt-checked),
      [role="dialog"]:not(.jt-checked) { visibility: hidden !important; }
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  // Suppress Jellyfin Web's "Playback failed / not supported by this client"
  // alert that fires after our URL interceptor blocks the audio request.
  // Acts only when phrase is inside a known dialog container — never walks
  // up into the main app shell.
  const KILL_PHRASES = [
    'not supported by this client',
    'no compatible streams',
    'playback error',
  ];
  const TOAST_SELECTOR = [
    'dialog', '.dialog', '.dialogContainer', '.paperDialog', '.formDialog',
    '.actionsheet', '.actionSheet', '.toast', '.toast-message',
    '[role="alertdialog"]', '[role="dialog"]',
  ].join(', ');

  // Wrap window.alert in case Jellyfin Web ever uses it directly.
  const _origAlert = window.alert;
  window.alert = function(msg) {
    if (typeof msg === 'string' &&
        KILL_PHRASES.some(p => msg.toLowerCase().includes(p))) {
      console.log('[JellyToast] suppressed alert:', msg);
      return;
    }
    return _origAlert.apply(this, arguments);
  };

  function isKillText(text) {
    text = (text || '').toLowerCase();
    return KILL_PHRASES.some(p => text.includes(p));
  }
  function findToastAncestor(node) {
    let cur = node;
    for (let i = 0; cur && i < 12; i++) {
      if (cur.matches && cur.matches(TOAST_SELECTOR)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  // Decide each new dialog: kill it (if it carries a kill phrase) or mark
  // it jt-checked so the CSS reveals it. We check on requestAnimationFrame,
  // which runs *before* the next paint — so a killed dialog never paints.
  // Some dialogs are inserted empty and populated a tick later; we re-check
  // for up to 30 frames before giving up and revealing.
  function decide(toast) {
    let attempts = 0;
    function check() {
      if (!toast.isConnected) return;
      attempts++;
      const text = (toast.textContent || '').trim();
      if (isKillText(text)) {
        try {
          const btn = toast.querySelector(
            'button[data-id="ok"], .button-submit, .btnCloseDialog, .raised-cancel, .dialog-button'
          );
          if (btn) btn.click(); else toast.remove();
        } catch (e) { /* ignore */ }
        console.log('[JellyToast] suppressed playback dialog');
        return;
      }
      if (text || attempts > 30) {
        toast.classList.add('jt-checked');
        return;
      }
      requestAnimationFrame(check);
    }
    requestAnimationFrame(check);
  }
  function handleNode(n) {
    if (!n || n.nodeType !== 1) return;
    if (n.matches && n.matches(TOAST_SELECTOR)) { decide(n); return; }
    if (n.querySelectorAll) {
      n.querySelectorAll(TOAST_SELECTOR).forEach(decide);
    }
    // Lazy-populated case: text appears inside a pre-existing dialog.
    if (isKillText(n.textContent)) {
      const toast = findToastAncestor(n);
      if (toast) decide(toast);
    }
  }
  function watchDialogs() {
    // Pre-existing dialogs (shouldn't be any, but defensive).
    document.querySelectorAll(TOAST_SELECTOR).forEach(decide);
    const obs = new MutationObserver(muts => {
      for (const m of muts) {
        for (const n of m.addedNodes) handleNode(n);
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  // Runtime gap-killer: Jellyfin Web's pages bake spacing into
  // .libraryPage / .itemDetailPage / .homePage / .padded-top to clear
  // the (now-hidden) .skinHeader. Static CSS misses some classes /
  // inline styles; this re-applies on every page change. Hits all
  // three vectors — padding, margin, and absolute top.
  function killTopPadding() {
    const sels = [
      '.libraryPage', '.itemDetailPage', '.homePage', '.page',
      '.padded-top-page', '.padded-top', '.padded-top-section',
      '.padded-top-headroom', '.libraryHeader', '.headerSpacer',
      '.mainAnimatedPagesContainer', '.mainAnimatedPages',
      '.pageWithAbsoluteTabs', '.withTabs',
    ];
    for (const s of sels) {
      document.querySelectorAll(s).forEach(el => {
        // setProperty with 'important' is the only way to beat
        // Jellyfin Web's `.pageWithAbsoluteTabs { padding-top: 7em
        // !important }`. Plain inline el.style.paddingTop loses to any
        // external !important rule.
        el.style.setProperty('padding-top', '0', 'important');
        el.style.setProperty('margin-top', '0', 'important');
      });
    }
  }
  window.addEventListener('hashchange', () => setTimeout(killTopPadding, 50));
  setInterval(killTopPadding, 750);

  // Library tab switcher — finds Jellyfin Web's tab button by its
  // visible label and clicks it. Used by JtTopBar's "View" dropdown
  // to drive Albums/Songs/Genres/etc. without our own routing logic.
  // Falls back to URL-hash manipulation (?tab=N) if no button matches,
  // since Jellyfin Web's library pages parse the tab index from there.
  const _TAB_BUTTON_SELECTORS = [
    'button.emby-tabs-button',
    'button[is="emby-tab-button"]',
    '.libraryPage .emby-tabs-button',
    '.libraryPage button[role="tab"]',
    '.headerTabs button',
    '.emby-tabs button',
    '.libraryPage [role="tablist"] button',
    'button.headerTab',
  ];
  function _findAllTabButtons() {
    const seen = new Set();
    const out = [];
    for (const sel of _TAB_BUTTON_SELECTORS) {
      document.querySelectorAll(sel).forEach(btn => {
        if (!seen.has(btn)) { seen.add(btn); out.push(btn); }
      });
    }
    return out;
  }
  function _fireClick(el) {
    // Synthesize the full mouse cycle — some Jellyfin Web tab handlers
    // listen on mousedown rather than click, so a bare .click() misses.
    ['mousedown', 'mouseup', 'click'].forEach(type => {
      try {
        el.dispatchEvent(new MouseEvent(type, {
          bubbles: true, cancelable: true, view: window, button: 0,
        }));
      } catch (_) { /* ignore */ }
    });
  }
  function _setHashTab(index) {
    const hash = location.hash || '';
    let next;
    if (/[?&]tab=\d+/.test(hash)) {
      next = hash.replace(/([?&])tab=\d+/, '$1tab=' + index);
    } else {
      const sep = hash.includes('?') ? '&' : '?';
      next = hash + sep + 'tab=' + index;
    }
    if (next !== hash) location.hash = next;
  }
  window.__jellytoast_switch_tab = function(label, index) {
    const target = (label || '').trim().toLowerCase();
    const buttons = _findAllTabButtons();
    console.log('[JellyToast] switch_tab:', label, 'index:', index,
                '— found', buttons.length, 'tab buttons');
    if (target) {
      for (const btn of buttons) {
        const txt = (btn.textContent || '').trim().toLowerCase();
        if (txt === target) {
          console.log('[JellyToast] clicking matched tab button:', txt);
          _fireClick(btn);
          return true;
        }
      }
    }
    if (typeof index === 'number' && index >= 0 && index < buttons.length) {
      console.log('[JellyToast] no label match — clicking by index', index);
      _fireClick(buttons[index]);
      return true;
    }
    if (typeof index === 'number' && index >= 0) {
      console.log('[JellyToast] no buttons matched — falling back to URL ?tab=' + index);
      _setHashTab(index);
      return true;
    }
    console.warn('[JellyToast] could not switch to tab:', label);
    return false;
  };

  // Returns the label of the currently-active tab, or '' if no tab
  // strip is rendered. Used by Python to keep the View dropdown's
  // label in sync with whatever Jellyfin Web is showing.
  window.__jellytoast_active_tab = function() {
    const sels = [
      'button.emby-tabs-button.is-active',
      'button[is="emby-tab-button"].is-active',
      'button.emby-tabs-button[aria-selected="true"]',
      'button[is="emby-tab-button"][aria-selected="true"]',
      '.libraryPage .emby-tabs-button.is-active',
      '.headerTabs button.is-active',
    ];
    for (const sel of sels) {
      const btn = document.querySelector(sel);
      if (btn) {
        const t = (btn.textContent || '').trim();
        if (t) return t;
      }
    }
    return '';
  };
  // Detect the current library's collectionType from the URL hash
  // (#/music?…&collectionType=music) so the native top bar can show
  // the right View dropdown items. Returns "" off library pages.
  window.__jellytoast_collection_type = function() {
    const m = (location.hash || '').match(/[?&]collectionType=([^&]+)/);
    return m ? decodeURIComponent(m[1]).toLowerCase() : '';
  };

  // Drawer toggle helper — JtTopBar's hamburger calls this via JS.
  // Jellyfin Web's actual drawer trigger lives inside .skinHeader (which
  // we now hide), but the underlying button still receives clicks if we
  // can find it. Try a few likely selectors before giving up.
  window.__jellytoast_toggle_drawer = function() {
    const sels = [
      '.headerButton.mainDrawerButton',
      '.mainDrawerButton',
      '.headerDrawerButton',
      'button.headerButton[title="Menu"]',
      'button[is="paper-icon-button-light"].mainDrawerButton',
    ];
    for (const s of sels) {
      const btn = document.querySelector(s);
      if (btn) { btn.click(); return true; }
    }
    return false;
  };

  function init() {
    injectCSS();
    if (document.body) { watchDialogs(); killTopPadding(); }
    else document.addEventListener('DOMContentLoaded', () => {
      watchDialogs(); killTopPadding();
    });
  }

  init();
  bind();
})();
"""


class Bridge(QObject):
    """Reserved for future JS→Python calls (settings UI, navigation hints, etc.)."""

    @pyqtSlot(str)
    def diagnostic(self, msg: str):
        print(f"[JellyToast/JS] {msg}", flush=True)


class _LoggingPage(QWebEnginePage):
    """Routes Jellyfin Web's JS console to the terminal."""
    _LEVEL_NAMES = {0: "INFO", 1: "WARN", 2: "ERROR"}

    def javaScriptConsoleMessage(self, level, message, line, source):
        try:
            lvl = level.value if hasattr(level, "value") else int(level)
        except Exception:
            lvl = 0
        name = self._LEVEL_NAMES.get(lvl, str(lvl))
        src = source.rsplit("/", 1)[-1] if source else "?"
        print(f"[js {name}] {message}  ({src}:{line})", flush=True)


class _PlaybackInterceptor(QWebEngineUrlRequestInterceptor):
    """
    Intercept HTTP requests for Jellyfin's audio/video streams. Extract the
    item id, signal Python, then BLOCK the request — so Jellyfin Web's HTML5
    player can't play (mpv plays instead).
    """

    intent_detected = pyqtSignal(str)  # item_id

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
            b.setCursor(Qt.CursorShape.PointingHandCursor)
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
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()


class JellyToastWindow(QMainWindow):
    BODY_RADIUS = 14
    RESIZE_MARGIN = 10  # px hit zone around the window for edge-resize

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
        layout = QVBoxLayout(central)
        # Outer margins act as the resize hit zone — the WebEngineView swallows
        # mouse events over its own area, so resize is only available on the
        # body's edges (and the bottom-right size grip).
        layout.setContentsMargins(
            self.RESIZE_MARGIN, 0, self.RESIZE_MARGIN, self.RESIZE_MARGIN
        )
        layout.setSpacing(0)

        self.titlebar = _TitleBar(self)
        layout.addWidget(self.titlebar)

        self.top_bar = JtTopBar()
        self.top_bar.nav_requested.connect(self._on_nav_requested)
        self.top_bar.drawer_toggle_requested.connect(self._toggle_jf_drawer)
        self.top_bar.cast_requested.connect(self._open_cast_dialog)
        self.top_bar.settings_requested.connect(self._open_settings)
        self.top_bar.tab_requested.connect(self._on_tab_requested)
        layout.addWidget(self.top_bar)

        # Named profile = persistent on disk. The default profile is
        # off-the-record in PyQt6, so localStorage/cookies (and therefore
        # the Jellyfin auth session) are wiped on every launch.
        profile = QWebEngineProfile("jellytoast", self)
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
        )

        self.interceptor = _PlaybackInterceptor(self)
        self.interceptor.intent_detected.connect(self._on_intent)
        profile.setUrlRequestInterceptor(self.interceptor)

        qwc_js = _read_qresource(":/qtwebchannel/qwebchannel.js")
        script = QWebEngineScript()
        script.setName("jellytoast_shim")
        script.setSourceCode(qwc_js + "\n" + SHIM_JS)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setRunsOnSubFrames(False)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        profile.scripts().insert(script)

        self.view = QWebEngineView(self)
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.view.setStyleSheet("background: transparent;")
        self.page = _LoggingPage(profile, self.view)
        self.view.setPage(self.page)
        # Transparent page background so the painted body shows through
        # before/while Jellyfin Web's HTML paints.
        self.page.setBackgroundColor(QColor(0, 0, 0, 0))

        self.bridge = Bridge()
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel)

        layout.addWidget(self.view, 1)

        self.np_bar = NowPlayingBar()
        self.np_bar.show_now_playing_requested.connect(self._show_now_playing)
        self.np_bar.show_queue_requested.connect(lambda: self.bus.show_mini_player.emit())
        self.np_bar.cast_requested.connect(self._open_cast_dialog)
        layout.addWidget(self.np_bar)

        self.bus.open_main_window.connect(self._show_self)
        self.bus.playback_started.connect(lambda np: self.bus.notify_track.emit(np))

        # Library ids are resolved lazily on first load — the start
        # destination preference picks which one we navigate to.
        self._library_ids: dict[str, str] = {}
        self._first_load_handled = False
        self.page.loadFinished.connect(self._on_first_load)
        # Title sync — Jellyfin Web sets document.title to "<Section> | Jellyfin";
        # strip the suffix and feed the section into the top bar.
        self.view.titleChanged.connect(self._on_title_changed)
        # urlChanged also fires on hash navigation — refresh the View
        # dropdown's available tabs whenever the user enters/leaves a
        # library page.
        self.view.urlChanged.connect(self._on_url_changed)
        self.view.setUrl(QUrl(f"{server_url}/web/"))

    def _on_nav_requested(self, action: str):
        if action == "back":
            self.view.back()
        elif action == "forward":
            self.view.forward()
        elif action == "home":
            self.view.setUrl(QUrl(f"{self.api.server_url}/web/#/home.html"))
        elif action == "search":
            self.view.setUrl(QUrl(f"{self.api.server_url}/web/#/search.html"))
        elif action == "preferences":
            self.view.setUrl(
                QUrl(f"{self.api.server_url}/web/#/mypreferencesmenu.html")
            )

    def _toggle_jf_drawer(self):
        # Jellyfin Web's drawer trigger lives in the (hidden) .skinHeader.
        # The button still exists in the DOM and accepts clicks. SHIM_JS
        # exposes the helper that finds and clicks it.
        self.page.runJavaScript("window.__jellytoast_toggle_drawer && window.__jellytoast_toggle_drawer();")

    def _on_tab_requested(self, index: int, label: str):
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
        # shows the right per-library tabs (or hides on non-library pages).
        self.page.runJavaScript(
            "window.__jellytoast_collection_type ? window.__jellytoast_collection_type() : '';",
            self.top_bar.set_collection,
        )
        # Jellyfin Web renders the tab strip a beat after the URL
        # settles — poll twice with increasing delay so we catch it
        # whether the DOM is fast or slow.
        QTimer.singleShot(400, self._refresh_active_tab)
        QTimer.singleShot(1200, self._refresh_active_tab)

    def _refresh_active_tab(self):
        self.page.runJavaScript(
            "window.__jellytoast_active_tab ? window.__jellytoast_active_tab() : '';",
            self._on_active_tab_response,
        )

    def _on_active_tab_response(self, label):
        if label:
            self.top_bar.set_active_tab(label)

    def _on_title_changed(self, title: str):
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

    def paintEvent(self, e):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Hard-clear alpha first (WA_TranslucentBackground implies
            # WA_NoSystemBackground, so Qt won't auto-fill).
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            m = self.RESIZE_MARGIN
            body = self.rect().adjusted(m, 0, -m, -m)
            if body.width() <= 0 or body.height() <= 0:
                return  # mid-resize, nothing to draw
            path = QPainterPath()
            path.addRoundedRect(
                float(body.x()), float(body.y()),
                float(body.width()), float(body.height()),
                self.BODY_RADIUS, self.BODY_RADIUS,
            )
            # Match the mini player's translucency so the whole app reads
            # as one frosted family. KWin blurs whatever's behind.
            p.setBrush(QColor(*BODY_COLOR))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()

    def _edges_at(self, pos):
        m = self.RESIZE_MARGIN
        w, h = self.width(), self.height()
        edges = Qt.Edge(0)
        if pos.x() <= m:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= w - m:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= m:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= h - m:
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
        if collection_type in self._library_ids:
            return self._library_ids[collection_type]
        try:
            libs = self.api.get_libraries()
            match = next((l for l in libs if l.get("CollectionType") == collection_type), None)
            lib_id = match.get("Id") if match else ""
        except Exception as e:
            print(f"[JellyToast] couldn't resolve {collection_type} library: {e}", flush=True)
            lib_id = ""
        self._library_ids[collection_type] = lib_id or ""
        return lib_id or ""

    @pyqtSlot(bool)
    def _on_first_load(self, ok: bool):
        # On the first successful page load, navigate to the user's
        # preferred starting destination (Music / Movies / TV / Home).
        # AppRouter.showItem races with auth and produced "Failed to
        # fetch item" on Jellyfin Web v10.11.7 — so we wait for the
        # corresponding "My Media" tile to appear in the DOM and click
        # it, the way a user would.
        if not ok or self._first_load_handled:
            return
        dest = get_settings().start_destination or "music"
        if dest == "home":
            # Stay on the home page Jellyfin Web already loaded; just
            # reveal the page once auth has settled.
            self._first_load_handled = True
            self.page.runJavaScript(
                "(function(){"
                "  var iv = setInterval(function(){"
                "    var ac = window.ApiClient;"
                "    if (ac && typeof ac.getCurrentUserId === 'function' && ac.getCurrentUserId()) {"
                "      clearInterval(iv);"
                "      setTimeout(window.__jellytoast_reveal, 200);"
                "    }"
                "  }, 200);"
                "  setTimeout(function(){clearInterval(iv); window.__jellytoast_reveal && window.__jellytoast_reveal();}, 8000);"
                "})();"
            )
            return
        lib_id = self._resolve_library_id(dest)
        if not lib_id:
            print(f"[JellyToast] no library found for {dest}; staying on home", flush=True)
            self._first_load_handled = True
            self.page.runJavaScript("window.__jellytoast_reveal && window.__jellytoast_reveal();")
            return
        self._first_load_handled = True
        # The fallback hash differs per collection type — Jellyfin Web
        # picks a different page filename based on what's being shown.
        fallback_hash = {
            "music":   "#/music.html?topParentId=",
            "movies":  "#/movies.html?topParentId=",
            "tvshows": "#/tv.html?topParentId=",
        }.get(dest, "#/list.html?topParentId=")
        js = f"""
        (function() {{
            var libId = "{lib_id}";
            var fallbackHash = "{fallback_hash}";
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
                    // For <a> tags, navigate via the href directly — click()
                    // sometimes gets eaten by Jellyfin's own handlers when
                    // the page just rendered.
                    var href = tile.getAttribute('href');
                    if (href && href.charAt(0) === '#') {{
                        window.location.hash = href;
                        console.log('[JellyToast] navigated via tile href: ' + href);
                    }} else {{
                        tile.click();
                        console.log('[JellyToast] clicked library tile (' + sel[i-1] + ')');
                    }}
                    clearInterval(iv);
                    // Reveal the page after the view has had a chance to
                    // render (one frame of layout + a small buffer).
                    setTimeout(window.__jellytoast_reveal, 250);
                    return;
                }}
                if (attempts > 150) {{
                    console.log('[JellyToast] gave up — no library tile found after 30s');
                    // Last-ditch: jump straight to the library page.
                    window.location.hash = fallbackHash + libId;
                    clearInterval(iv);
                    setTimeout(window.__jellytoast_reveal, 250);
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

    def _on_sign_out_requested(self):
        # Wipe Jellyfin Web's stored session (cookies + localStorage) and
        # the python REST client's saved token, then reload back to the
        # login page.
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
        # Clear Jellyfin Web's localStorage token + reload.
        self.page.runJavaScript(
            "try { localStorage.clear(); sessionStorage.clear(); } catch(e) {}"
            "location.replace(location.origin + '/web/');"
        )
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

    @pyqtSlot(str)
    def _on_intent(self, item_id: str):
        try:
            item = self.api.get_item(item_id)
        except Exception as e:
            print(f"[JellyToast] metadata fetch failed for {item_id}: {e}", flush=True)
            return
        if not item:
            return
        items, start_idx = self._expand_context(item)
        self.bus.queue_play_now.emit(items, start_idx)

    def _expand_context(self, item: dict):
        """For an audio track, queue the full album so Next/Prev work."""
        if item.get("Type") == "Audio" and item.get("AlbumId"):
            try:
                tracks = self.api.get_album_tracks(item["AlbumId"])
                if tracks:
                    # get_album_tracks doesn't return AlbumId by default —
                    # propagate it from the original item so every track's
                    # _build_now_playing can resolve the album art
                    # reliably (the track's own /Items/{id}/Images/Primary
                    # is inconsistent across Jellyfin versions).
                    album_id = item["AlbumId"]
                    for t in tracks:
                        t.setdefault("AlbumId", album_id)
                    for i, t in enumerate(tracks):
                        if t.get("Id") == item.get("Id"):
                            return tracks, i
                    return tracks, 0
            except Exception as e:
                print(f"[JellyToast] album expand failed: {e}", flush=True)
        return [item], 0

    @pyqtSlot()
    def _show_self(self):
        # Drop the minimized bit before showing — show() alone won't un-iconify
        # a window that was minimized to the taskbar.
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def _show_now_playing(self):
        np = get_now_playing()
        if np.item_id:
            self.view.setUrl(QUrl(f"{self.api.server_url}/web/#/details?id={np.item_id}"))

    def _open_cast_dialog(self):
        np = get_now_playing()
        if not np.item_id:
            QMessageBox.information(
                self, "Cast",
                "Start playing something first, then choose a device to cast to."
            )
            return
        dlg = CastDialog(self.cast_manager, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_device:
            return
        dev = dlg.selected_device
        if dev.device_type == "chromecast":
            ok = self.cast_manager.cast_to_chromecast(
                dev, np.stream_url, np.title, np.thumb_url, is_audio=np.is_audio,
            )
        else:
            ok = self.cast_manager.cast_to_airplay(dev, np.stream_url, np.title)
        if ok:
            self.bus.cast_started.emit(dev.name)
            self.bus.stop_requested.emit()
            QMessageBox.information(self, "Casting", f"Now casting to {dev.name}.")
        else:
            QMessageBox.warning(self, "Cast failed", f"Could not cast to {dev.name}.")

    def closeEvent(self, e):
        if get_settings().minimize_to_tray:
            self.hide()
            e.ignore()
        else:
            QApplication.instance().quit()


def _read_qresource(path: str) -> str:
    f = QFile(path)
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        return ""
    data = bytes(f.readAll().data()).decode("utf-8", errors="replace")
    f.close()
    return data


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    app.setApplicationName("JellyToast")
    app.setApplicationDisplayName("JellyToast")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("JellyToast")
    app.setDesktopFileName("jellytoast")
    app.setWindowIcon(QIcon(make_app_icon(64)))
    app.setQuitOnLastWindowClosed(False)

    if not WEBENGINE_AVAILABLE:
        QMessageBox.critical(
            None, "Missing dependency",
            "JellyToast requires PyQt6-WebEngine.\n\n"
            "Install with:\n    sudo pacman -S python-pyqt6-webengine\n\n"
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
    mpv_ctrl = MpvController()
    bus.volume_changed.emit(settings.volume)

    win = JellyToastWindow(server_url)

    mini = FloatingMiniPlayer()
    bus.show_mini_player.connect(lambda: (mini.show(), mini.raise_(), mini.activateWindow()))
    bus.hide_mini_player.connect(mini.hide)

    tray = TrayController(app, mini, win)
    mpris = MprisService()
    mpris.start()

    win.show()

    if settings.show_mini_on_start:
        mini.show()

    def _cleanup():
        try:
            mpv_ctrl.shutdown()
        except Exception:
            pass
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

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
import threading
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

from PyQt6.QtCore import (
    QObject, QUrl, QFile, QIODevice, QTimer, Qt, pyqtSlot, pyqtSignal,
    QVariantAnimation, QEasingCurve,
)
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QSystemTrayIcon, QWidget,
    QVBoxLayout, QHBoxLayout, QStackedLayout, QLabel, QPushButton,
    QDialog, QInputDialog,
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


# Per-intent / per-track-change diagnostics (URL, JF Web queue contents,
# cooldown deltas) are gated behind this. Install/skip/error lines stay
# unconditional so a post-mortem from the terminal alone is still possible.
_SHUFFLE_DEBUG = os.environ.get("JT_SHUFFLE_DEBUG") == "1"


# JS shim: hides Jellyfin Web's now-playing bar and confirms the bridge is up.
# Playback detection happens in Python via the URL request interceptor below.
SHIM_JS = r"""
(function() {
  if (window.__jellytoast_installed) return;
  window.__jellytoast_installed = true;
  console.log('[JellyToast] shim loaded at', location.href);

  // Hide the page until we've finished navigating to the music library.
  // _on_first_load (Python) calls window.__jellytoast_reveal() once it's done.
  // The reveal pings the host TWICE via QWebChannel:
  //   pageReady    — DOM is fully laid out; host can show the window.
  //                  Wayland uses this signal (since it can't show
  //                  invisibly while waiting for art to load).
  //   pageRendered — page has actually composited to screen (we know
  //                  because document.visibilityState went 'visible'
  //                  AND a few rAF callbacks fired at full speed);
  //                  host can hide the loading overlay. X11 uses this
  //                  to flip opacity 0 → 1 in one go.
  // Splitting this lets the X11 overlay stay up through Chromium's
  // post-show compositor cycle so chrome and albums appear together.
  document.documentElement.classList.add('jt-loading');
  function pingHost(method) {
    try {
      if (window.jellytoast && typeof window.jellytoast[method] === 'function') {
        window.jellytoast[method]();
      }
    } catch (e) { /* bridge not ready; failsafe timers will catch up */ }
  }
  window.__jellytoast_reveal = function() {
    document.documentElement.classList.remove('jt-loading');
    pingHost('pageReady');
    // After the host shows the window (in response to pageReady),
    // the page becomes 'visible' and JF Web's lazy-loader starts
    // pulling cover art. We don't ping pageRendered until those
    // images have actually arrived AND the page has had a frame to
    // composite them — otherwise the host hides the overlay while
    // album cards still have empty placeholders, and the art "pops
    // in" after.
    function waitForVisibleAndArt() {
      if (document.visibilityState !== 'visible') {
        setTimeout(waitForVisibleAndArt, 50);
        return;
      }
      // Wait for the batch of visible cover-art images to finish
      // loading. JF Web's lazy loader fires them in waves once the
      // page is visible; we look for the count of fully-loaded
      // images to STOP increasing for 600ms (6 ticks at 100ms),
      // which signals that the current viewport's images are all in.
      // Without this, pageRendered fires after the first batch and
      // the user sees the rest of the grid pop in afterwards.
      // Filter naturalWidth > 100 to exclude blurhash placeholders
      // (typically ~20×20) — counting them would ping pageRendered
      // before the real images swap in.
      var deadline = Date.now() + 8000;
      var prev = 0;
      var stableTicks = 0;
      var iv = setInterval(function() {
        var images = document.querySelectorAll(
          '.itemsContainer img, .libraryPage img, '
          + '.pageTabContent img, .homePage img'
        );
        var loaded = 0;
        for (var i = 0; i < images.length; i++) {
          if (images[i].complete && images[i].naturalWidth > 100) loaded++;
        }
        if (loaded >= 4 && loaded === prev) {
          stableTicks++;
        } else {
          stableTicks = 0;
          prev = loaded;
        }
        if (stableTicks >= 6 || Date.now() > deadline) {
          clearInterval(iv);
          // A handful of rAFs so the loaded images have actually
          // composited to the screen surface before we yank the
          // overlay.
          var frames = 0;
          (function loop() {
            if (++frames >= 4) { pingHost('pageRendered'); return; }
            requestAnimationFrame(loop);
          })();
        }
      }, 100);
    }
    waitForVisibleAndArt();
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
      /* Force the system default cursor everywhere inside the embedded
         view. Jellyfin Web sets cursor:pointer on every card, link, and
         button — that creates a visible pointer-finger hop every time
         the cursor crosses an album. We override to default so the
         cursor stays the system arrow over the whole web view. */
      *, *::before, *::after { cursor: default !important; }
      input, textarea, [contenteditable='true'] { cursor: text !important; }
      html.jt-loading body { opacity: 0 !important; }
      html:not(.jt-loading) body { transition: opacity 320ms ease-out; }
      .nowPlayingBar,
      .nowPlayingBarTop,
      .nowPlayingBarBottom { display: none !important; }
      .mainAnimatedPages { padding-bottom: 0 !important; }
      /* Cast button in the header — non-functional in JellyToast (we have
         our own cast manager in the now-playing bar). */
      .headerCastButton,
      .btnCast,
      button[is="paper-icon-button-light"].headerCastButton { display: none !important; }
      /* Loading spinners — JF Web shows .docspinner while fetching its
         own playback queue (300 random items, prefetch metadata, etc.)
         and trying to set up its audio element. We've already taken
         over via mpv, the queue is already in our cache, so this
         spinner is just visual noise that lingers for ~3s before its
         audio.src finally errors out. Hide it; legitimate page-load
         spinners on the main view aren't using this class. */
      .docspinner,
      .mdl-spinner,
      .spinnerContainer { display: none !important; }
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

  // Stamp a timestamp every time the user clicks anything that looks
  // like a Shuffle button. Python reads this stamp via __jellytoast_
  // queue_state and, if the click was within the last 3s, forces a
  // library-wide shuffle (overriding Jellyfin Web's "shuffle one
  // album" behavior). Captured in the capture phase so we observe
  // the click even if Jellyfin's own handler stops propagation.
  window.__jellytoast_shuffle_clicked_at = 0;
  // Push the shuffle event to Python directly via the QWebChannel
  // bridge — saves ~250ms of JF Web round-trip (metadata fetch +
  // audio request + intercept + queue-state callback) before library
  // shuffle starts. The intercept-driven path stays as a fallback for
  // when the bridge isn't ready yet at click time.
  function notifyShuffle(via) {
    window.__jellytoast_shuffle_clicked_at = Date.now();
    console.log('[JellyToast] shuffle button clicked (' + via + ')');
    try {
      if (window.jellytoast
          && typeof window.jellytoast.shuffleClicked === 'function') {
        window.jellytoast.shuffleClicked();
      }
    } catch (_) { /* bridge not ready; stamp will be used by intercept path */ }
  }
  (function installShuffleClickHook() {
    var SHUFFLE_MATCHERS = [
      '.btnShuffle',
      '.btnShuffleAll',
      'button[is="paper-icon-button-light"].btnShuffle',
      'button[title*="Shuffle" i]',
      'button[aria-label*="Shuffle" i]',
      '[data-action*="shuffle" i]',
    ];
    document.addEventListener('click', function(e) {
      var el = e.target;
      for (var i = 0; el && i < 8; i++, el = el.parentElement) {
        if (!el.matches) continue;
        for (var j = 0; j < SHUFFLE_MATCHERS.length; j++) {
          try {
            if (el.matches(SHUFFLE_MATCHERS[j])) {
              notifyShuffle('matched ' + SHUFFLE_MATCHERS[j]);
              return;
            }
          } catch (_) { /* invalid selector — skip */ }
        }
        // Fall back: a span.material-icons.shuffle inside any button.
        if (el.tagName === 'BUTTON' || el.tagName === 'A') {
          var icon = el.querySelector
            && el.querySelector('.material-icons.shuffle, [class*="shuffle" i]');
          if (icon) {
            notifyShuffle('icon descendant');
            return;
          }
        }
      }
    }, true);
  })();

  // Snapshot Jellyfin Web's current playback queue + index plus a
  // shuffle-intent flag. Returned as JSON-encoded {items, index,
  // shuffle} or null if the manager isn't ready. Used right after
  // a /Audio/{id}/stream interception so Python can decide whether
  // to use Jellyfin Web's queue, override with library shuffle,
  // or fall back to manual context expansion. The shuffle stamp is
  // consumed on first read — JF Web error-advances through its own
  // queue after we block playback, generating extra intent fires;
  // we don't want those re-triggering library shuffle.
  window.__jellytoast_queue_state = function() {
    var stamp = window.__jellytoast_shuffle_clicked_at || 0;
    var shuffleIntent = (Date.now() - stamp) < 3000;
    if (shuffleIntent) window.__jellytoast_shuffle_clicked_at = 0;
    try {
      var pm = window.playbackManager;
      if (!pm || typeof pm.playlist !== 'function') {
        return JSON.stringify({ items: null, index: 0, shuffle: shuffleIntent });
      }
      var list = pm.playlist();
      if (!list || !list.length) {
        return JSON.stringify({ items: null, index: 0, shuffle: shuffleIntent });
      }
      var idx = (typeof pm.currentPlaylistIndex === 'function')
        ? pm.currentPlaylistIndex() : 0;
      var items = list.map(function(it) {
        return {
          Id: it.Id, Name: it.Name, Type: it.Type,
          Album: it.Album, AlbumId: it.AlbumId,
          AlbumPrimaryImageTag: it.AlbumPrimaryImageTag,
          AlbumArtist: it.AlbumArtist, Artists: it.Artists,
          ArtistItems: it.ArtistItems,
          RunTimeTicks: it.RunTimeTicks,
          IndexNumber: it.IndexNumber,
          ParentIndexNumber: it.ParentIndexNumber,
          ImageTags: it.ImageTags,
          MediaType: it.MediaType,
          UserData: it.UserData,
        };
      });
      return JSON.stringify({
        items: items, index: idx, shuffle: shuffleIntent,
      });
    } catch (e) {
      console.warn('[JellyToast] queue_state error:', e);
      return JSON.stringify({ items: null, index: 0, shuffle: shuffleIntent });
    }
  };

  // Stop Jellyfin Web's playbackManager dead. Called by Python every
  // time we install our own queue — without this, JF Web's player
  // error-advances through *its* queue (300 random items after a
  // shuffle click) and each retry generates a /Audio/{id}/... request
  // that our interceptor catches. Eventually one of those leaks past
  // the cooldown and overwrites our queue. Calling pm.stop() empties
  // its playlist and halts the storm at the source.
  window.__jellytoast_silence_jfweb = function() {
    try {
      var pm = window.playbackManager;
      if (!pm) return;
      if (typeof pm.stop === 'function') pm.stop();
      // Best-effort: clear the internal queue too in case stop() leaves
      // the array intact for replay. Field name varies by version.
      ['_playlist', '_currentPlaylistIndex'].forEach(function(k) {
        if (pm[k] !== undefined) {
          if (Array.isArray(pm[k])) pm[k].length = 0;
          else pm[k] = -1;
        }
      });
      console.log('[JellyToast] silenced JF Web playbackManager');
    } catch (e) {
      console.warn('[JellyToast] silence error:', e);
    }
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
    """JS→Python calls. Wired through QWebChannel as `window.jellytoast`."""

    shuffle_requested = pyqtSignal()
    page_ready = pyqtSignal()
    page_rendered = pyqtSignal()

    @pyqtSlot(str)
    def diagnostic(self, msg: str):
        print(f"[JellyToast/JS] {msg}", flush=True)

    @pyqtSlot()
    def shuffleClicked(self):
        # Fired the instant the JS click hook detects a shuffle button
        # press — lets us start library shuffle immediately instead of
        # waiting for JF Web's metadata + audio-request round-trip.
        self.shuffle_requested.emit()

    @pyqtSlot()
    def pageReady(self):
        # Fired by __jellytoast_reveal() when the page DOM is laid out.
        # Tells the host it's safe to show the window — JF Web has
        # finished bootstrapping, the music library has navigated, and
        # the album cards are present in the DOM.
        self.page_ready.emit()

    @pyqtSlot()
    def pageRendered(self):
        # Fired after the page has actually composited to screen
        # (visibilitystate went visible + a few rAF callbacks fired
        # at full frame rate). Tells the host to hide the loading
        # overlay — at this point chrome and album content are both
        # painted, so they appear together rather than in stages.
        self.page_rendered.emit()


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
    # Cross-thread silence trigger. _emit_queue can be called from a
    # worker thread (library shuffle worker), but page.runJavaScript
    # only runs on the main thread. Emitting this signal queues the
    # call onto the GUI thread automatically.
    silence_jfweb_signal = pyqtSignal()

    BODY_RADIUS = 14
    RESIZE_MARGIN = 10  # px hit zone around the window for edge-resize

    # Matches a Jellyfin Web details-page id in the URL hash (e.g.
    # `#/details?id=<32hex>&context=playlists`). Used to recover the
    # surrounding context (playlist / album / artist) when the user
    # plays a track from a details page.
    _URL_CONTEXT_ID = re.compile(r"[?&]id=([a-f0-9]{32})", re.IGNORECASE)

    # Set to time.time() whenever we successfully install a queue. Used
    # to suppress JF Web's stale-request storm — when we block its
    # audio request, JF Web's pipeline (REST + bitrate test + prefetch
    # + audio.src load) can take 3-4 seconds before the in-flight load
    # finally errors and a fresh intent fires. Plus the player's
    # error-advance through *its* queue. Those arrive in a window we
    # need to ignore so our shuffle queue isn't overwritten.
    _QUEUE_COOLDOWN_S = 5.0
    # Long-tail guard for the destructive `_intent_via_metadata` path.
    # That path expands a single intercepted track to its album, which
    # is the wrong move once we already own a queue — regardless of
    # whether the cooldown caught the intent or not.
    _METADATA_FALLBACK_SKIP_S = 30.0

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
        layout = QVBoxLayout(chrome)
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
        # Beef up the on-disk HTTP cache so second+ launches don't
        # re-download Jellyfin Web's bundles, fonts, and album cover
        # art. 256 MB is enough for a couple thousand cards' worth of
        # primary images plus the JS/CSS bundles (~5 MB total).
        # localStorage / IndexedDB are persistent automatically for a
        # named profile; no explicit policy setter needed.
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        profile.setHttpCacheMaximumSize(256 * 1024 * 1024)

        self.interceptor = _PlaybackInterceptor(self)
        self.interceptor.intent_detected.connect(self._on_intent)
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
        self.bridge.shuffle_requested.connect(self._library_shuffle)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel)

        # Cross-thread silence trigger — see signal definition above.
        self.silence_jfweb_signal.connect(self._do_silence_jfweb)

        layout.addWidget(self.view, 1)

        self.np_bar = NowPlayingBar()
        self.np_bar.show_now_playing_requested.connect(self._show_now_playing)
        self.np_bar.show_queue_requested.connect(lambda: self.bus.show_mini_player.emit())
        self.np_bar.cast_requested.connect(self._open_cast_dialog)
        layout.addWidget(self.np_bar)

        # Now wire the chrome + full-window loading overlay into the
        # central stacked layout. The overlay covers the entire window
        # (titlebar, top bar, view, transport bar) until the page
        # signals it's fully rendered — masks every loading state so
        # the window appears fully populated all at once. Note that
        # main() defers `win.show()` until bridge.page_ready, so the
        # overlay is mostly defense-in-depth for the failsafe path
        # where show() lands before the page is actually ready.
        central_stack.addWidget(chrome)
        self._loading_overlay = _LoadingOverlay()
        central_stack.addWidget(self._loading_overlay)  # added second → on top

        self.bus.open_main_window.connect(self._show_self)
        self.bus.playback_started.connect(lambda np: self.bus.notify_track.emit(np))

        # Library ids are resolved lazily on first load — the start
        # destination preference picks which one we navigate to.
        self._library_ids: dict[str, str] = {}
        self._first_load_handled = False
        # Pre-fetched random library queue, primed in the background so
        # the first shuffle click after launch can install it instantly
        # instead of waiting for the REST round-trip. Refreshed after
        # each use so the next click also gets a snappy install.
        self._random_queue_cache: list[dict] = []
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
        # Prime the random-queue cache once we have auth — the next
        # shuffle click will install it instantly.
        QTimer.singleShot(800, self._prime_random_queue_async)
        dest = get_settings().start_destination or "music"
        if dest == "home":
            # Stay on the home page Jellyfin Web already loaded; reveal
            # once auth has settled AND we've actually rendered some
            # cards (Continue Watching / Latest / etc.). Falls back to
            # an 8s timeout failsafe so an empty home page can't keep
            # the body permanently hidden.
            self._first_load_handled = True
            self.page.runJavaScript(
                "(function(){"
                "  var deadline = Date.now() + 8000;"
                "  var iv = setInterval(function(){"
                "    var ac = window.ApiClient;"
                "    var signedIn = ac && typeof ac.getCurrentUserId === 'function' && ac.getCurrentUserId();"
                "    var cards = document.querySelectorAll('.card, .homePage .card, .section .card');"
                "    if ((signedIn && cards.length >= 4) || Date.now() > deadline) {"
                "      clearInterval(iv);"
                "      window.__jellytoast_reveal && window.__jellytoast_reveal();"
                "    }"
                "  }, 100);"
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
        # Suppress JF Web's auto-advance retries. After we block the
        # audio request for our intercepted track, JF Web's player
        # errors and advances through *its* queue, firing a fresh
        # intent for each retried track. Without this guard the last
        # one wins and our shuffle queue gets overwritten by the
        # original album.
        since_set = time.time() - getattr(self, "_queue_set_at", 0.0)
        if _SHUFFLE_DEBUG:
            print(
                f"[JellyToast] _on_intent: item={item_id[:8]} "
                f"since_queue_set={since_set:.2f}s cooldown={self._QUEUE_COOLDOWN_S}s",
                flush=True,
            )
        if since_set < self._QUEUE_COOLDOWN_S:
            print(
                f"[JellyToast] suppressing intent {item_id[:8]} "
                f"(queue set {since_set:.2f}s ago)",
                flush=True,
            )
            return
        # Prefer Jellyfin Web's own playback queue — it reflects whatever
        # the user actually triggered (shuffle library / album / playlist /
        # search result / single track). Falls back to manual context
        # expansion if the queue isn't reachable.
        self.page.runJavaScript(
            "window.__jellytoast_queue_state ? window.__jellytoast_queue_state() : null;",
            lambda result: self._on_queue_state(item_id, result),
        )

    def _emit_queue(self, items: list, start: int, source: str):
        """Centralized queue-set: stamps the cooldown timer, emits to
        the bus, then tells Jellyfin Web to halt its own playback so
        its auto-advance doesn't keep firing intents. Safe to call
        from worker threads — bus.queue_play_now and the silence
        signal both auto-queue across threads."""
        if not items:
            return
        unique_albums = {it.get("AlbumId") for it in items if it.get("AlbumId")}
        print(
            f"[JellyToast] queue set via {source}: {len(items)} items, "
            f"{len(unique_albums)} unique albums, start={start}",
            flush=True,
        )
        self._queue_set_at = time.time()
        self.bus.queue_play_now.emit(items, start)
        self.silence_jfweb_signal.emit()

    def _do_silence_jfweb(self):
        self.page.runJavaScript(
            "window.__jellytoast_silence_jfweb && window.__jellytoast_silence_jfweb();"
        )

    def _on_queue_state(self, item_id: str, payload):
        # _on_intent's cooldown gate runs before runJavaScript is
        # dispatched, but the runJavaScript itself is async — by the
        # time this callback fires, a queue may have been installed
        # by the bridge fast-path. Re-check cooldown here so we don't
        # overwrite a fresh queue with stale state. Also avoids the
        # metadata-fallback path: when we silenced JF Web, its
        # pm.playlist() returns null and we'd otherwise fetch the
        # intercepted track and expand it to its album.
        since_set = time.time() - getattr(self, "_queue_set_at", 0.0)
        if since_set < self._QUEUE_COOLDOWN_S:
            print(
                f"[JellyToast] queue_state callback within cooldown "
                f"({since_set:.2f}s) — discarding",
                flush=True,
            )
            return
        url = self.view.url().toString()
        if _SHUFFLE_DEBUG:
            print(f"[JellyToast] intent on URL: {url}", flush=True)
        if payload:
            try:
                data = json.loads(payload)
                shuffle_intent = bool(data.get("shuffle"))
                items = data.get("items") or []
                idx = int(data.get("index") or 0)

                # Primary signal: user just clicked a Shuffle button.
                # Forced library-wide shuffle. Stamp is consumed JS-side
                # so JF Web's auto-advance retries don't re-trigger.
                if shuffle_intent:
                    print("[JellyToast] shuffle click detected", flush=True)
                    self._library_shuffle()
                    return

                if items:
                    if _SHUFFLE_DEBUG:
                        unique_albums = {it.get("AlbumId") for it in items if it.get("AlbumId")}
                        print(
                            f"[JellyToast] JF Web queue: {len(items)} tracks, "
                            f"{len(unique_albums)} unique album(s), "
                            f"library_view={self._is_library_view()}",
                            flush=True,
                        )
                    target = item_id.lower()
                    if 0 <= idx < len(items) and (items[idx].get("Id") or "").lower() == target:
                        start = idx
                    else:
                        start = next(
                            (i for i, it in enumerate(items)
                             if (it.get("Id") or "").lower() == target),
                            -1,
                        )
                    if start >= 0:
                        self._emit_queue(items, start, "JF Web queue")
                        return
            except Exception as e:
                print(f"[JellyToast] queue_state parse failed: {e}", flush=True)
        self._intent_via_metadata(item_id)

    def _is_library_view(self) -> bool:
        url = self.view.url().toString()
        if "#" not in url:
            return False
        hash_part = url.split("#", 1)[1].lower()
        # A details page is never a library view, even if a topParentId
        # query param tags along for breadcrumbs.
        if "/details" in hash_part:
            return False
        # Library list pages across Jellyfin Web versions use one of:
        #   #/music.html?topParentId=<id>   (10.10 and earlier)
        #   #/list.html?type=MusicAlbum&parentId=<id>
        #   #/music?topParentId=<id>        (newer routing)
        # Any of these markers identifies a list page.
        markers = (
            "music.html", "movies.html", "tv.html", "tvshows.html",
            "list.html",
            "topparentid=", "parentid=",
        )
        return any(m in hash_part for m in markers)

    def _library_shuffle(self):
        # Two paths can land here for the same shuffle click — the
        # bridge (fast, direct from JS) and the intercept-driven
        # queue-state callback (slower, via JF Web's audio request).
        # The cooldown stamp is the same lock that suppresses JF Web's
        # auto-advance retries; if it's active here, a shuffle is
        # already in flight or just installed, so skip.
        since = time.time() - getattr(self, "_queue_set_at", 0.0)
        if since < self._QUEUE_COOLDOWN_S:
            print(
                f"[JellyToast] library shuffle skipped — already in flight "
                f"({since:.2f}s ago)",
                flush=True,
            )
            return
        # Stamp the cooldown the instant the request comes in, before
        # any work starts. Any auto-advance intent that fires in the
        # meantime gets suppressed instead of racing us.
        self._queue_set_at = time.time()

        # Fast path: a pre-fetched random queue is sitting in the cache.
        # Emit it immediately, then refill the cache in the background.
        if self._random_queue_cache:
            items = self._random_queue_cache
            self._random_queue_cache = []
            self._emit_queue(items, 0, "library shuffle (cached)")
            self._prime_random_queue_async()
            return

        lib_id = self._resolve_library_id("music")
        if not lib_id:
            print("[JellyToast] no music library resolved; skipping library shuffle", flush=True)
            return
        # Cache miss — fetch on a worker thread so the GUI doesn't
        # freeze for ~150-200ms while 500 random items load.
        threading.Thread(
            target=self._library_shuffle_worker, args=(lib_id,), daemon=True,
        ).start()

    def _library_shuffle_worker(self, lib_id: str):
        try:
            items = self.api.get_random_audio_items(lib_id, limit=500)
        except Exception as e:
            print(f"[JellyToast] library shuffle fetch failed: {e}", flush=True)
            return
        if not items:
            print("[JellyToast] library shuffle: API returned no tracks", flush=True)
            return
        # bus.queue_play_now and the silence signal are both Qt signals;
        # emit() across threads is safe — Qt auto-uses QueuedConnection
        # so the slots run on the main thread.
        self._emit_queue(items, 0, "library shuffle")
        # Prime the cache for the next click while we're already
        # warmed up (lib_id resolved, API connection live).
        self._prime_random_queue_async()

    def _prime_random_queue_async(self):
        """Refresh the pre-fetched random queue in the background.
        No-ops if a cache already exists or no music library is known."""
        if self._random_queue_cache:
            return
        lib_id = self._resolve_library_id("music")
        if not lib_id:
            return
        threading.Thread(
            target=self._prime_random_queue_worker, args=(lib_id,), daemon=True,
        ).start()

    def _prime_random_queue_worker(self, lib_id: str):
        try:
            items = self.api.get_random_audio_items(lib_id, limit=500)
        except Exception as e:
            print(f"[JellyToast] prime random queue failed: {e}", flush=True)
            return
        if items:
            self._random_queue_cache = items
            print(
                f"[JellyToast] random queue cache primed: {len(items)} items",
                flush=True,
            )

    def _intent_via_metadata(self, item_id: str):
        # Metadata fallback expands the intercepted track to its album.
        # That's the right move when we have no queue yet (first launch,
        # post-stop), but destructive once we already own one — JF Web's
        # silenced pm.playlist returns empty, and we'd otherwise replace
        # our shuffle queue with a single-album expansion.
        since_set = time.time() - getattr(self, "_queue_set_at", 0.0)
        if 0 < since_set < self._METADATA_FALLBACK_SKIP_S:
            print(
                f"[JellyToast] metadata fallback skipped — queue installed "
                f"{since_set:.1f}s ago",
                flush=True,
            )
            return
        try:
            item = self.api.get_item(item_id)
        except Exception as e:
            print(f"[JellyToast] metadata fetch failed for {item_id}: {e}", flush=True)
            return
        if not item:
            return
        context_item = self._fetch_url_context(exclude_id=item_id)
        items, start_idx = self._expand_context(item, context_item)
        self._emit_queue(items, start_idx, "metadata fallback")

    def _fetch_url_context(self, exclude_id: str = "") -> dict | None:
        url = self.view.url().toString()
        if "#" not in url:
            return None
        m = self._URL_CONTEXT_ID.search(url.split("#", 1)[1])
        if not m:
            return None
        ctx_id = m.group(1).lower()
        if exclude_id and ctx_id == exclude_id.lower():
            # The played item *is* the context (e.g. user clicked Play on
            # a single track's own details page). Nothing extra to fetch.
            return None
        try:
            return self.api.get_item(ctx_id)
        except Exception as e:
            print(f"[JellyToast] context fetch failed for {ctx_id}: {e}", flush=True)
            return None

    def _expand_context(self, item: dict, context_item: dict | None = None):
        """For an audio track, queue the surrounding playlist or album so
        Next/Prev walk the right context. Falls back to a single-item
        queue for video / unknown contexts."""
        if item.get("Type") != "Audio":
            return [item], 0

        # Playlist context — user clicked a track from a playlist's
        # details page. Queue the whole playlist starting at this track.
        if context_item and context_item.get("Type") == "Playlist":
            try:
                tracks = self.api.get_playlist_items(context_item["Id"])
                if tracks:
                    return self._index_starting_at(tracks, item.get("Id"))
            except Exception as e:
                print(f"[JellyToast] playlist expand failed: {e}", flush=True)

        # Album context (default) — queue the track's own album.
        if item.get("AlbumId"):
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
                    return self._index_starting_at(tracks, item.get("Id"))
            except Exception as e:
                print(f"[JellyToast] album expand failed: {e}", flush=True)

        return [item], 0

    @staticmethod
    def _index_starting_at(tracks: list[dict], item_id: str) -> tuple[list[dict], int]:
        for i, t in enumerate(tracks):
            if t.get("Id") == item_id:
                return tracks, i
        return tracks, 0

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
    # Hand the cast manager to mpv so transport signals (play/pause/
    # seek/volume) route to the receiver when a cast session is active.
    mpv_ctrl.set_cast_manager(win.cast_manager)

    mini = FloatingMiniPlayer()
    bus.show_mini_player.connect(lambda: (mini.show(), mini.raise_(), mini.activateWindow()))
    bus.hide_mini_player.connect(mini.hide)

    tray = TrayController(app, mini, win)
    mpris = MprisService()
    mpris.start()

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

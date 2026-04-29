"""
JellyToast — prototype.

Embeds Jellyfin Web in QtWebEngine, intercepts playback intents, and
forwards them to Python (so a future version can route through mpv,
the mini player, MPRIS, casting, etc.).

Milestone 1: load Jellyfin Web, detect when the user clicks "Play",
and print the intercepted item id to the terminal. No mpv wiring yet.

Run:
    python3 jellytoast.py

Reads server URL from the existing JellyPlayer config so you don't need
to log in again — sign in via JellyPlayer first if you haven't yet.
"""

import os
import sys
from pathlib import Path

# Same locale / Qt-platform safeguards JellyPlayer needs (libmpv + Wayland)
if os.environ.get("_JELLY_LOCALE_FIXED") != "1":
    if os.environ.get("LC_ALL") is not None or os.environ.get("LC_NUMERIC", "C") != "C":
        new_env = dict(os.environ)
        new_env.pop("LC_ALL", None)
        new_env["LC_NUMERIC"] = "C"
        new_env.setdefault("LANG", "C.UTF-8")
        new_env["_JELLY_LOCALE_FIXED"] = "1"
        os.execve(sys.executable, [sys.executable] + sys.argv, new_env)

if "WAYLAND_DISPLAY" in os.environ and "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "xcb"

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from PyQt6.QtCore import QObject, QUrl, QFile, QIODevice, pyqtSlot
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineScript, QWebEngineProfile
    from PyQt6.QtWebChannel import QWebChannel
    WEBENGINE_AVAILABLE = True
    _WEBENGINE_ERROR = ""
except ImportError as e:
    WEBENGINE_AVAILABLE = False
    _WEBENGINE_ERROR = str(e)

from modules.settings import get_settings


# Injected into every page in Jellyfin Web. Two phases:
#   1. Connect to QWebChannel and stash the bridge on `window.jellytoast`.
#   2. Monkey-patch `playbackManager.play` once it appears, and fall back
#      to a click listener for older/odd builds.
SHIM_JS = r"""
(function() {
  if (window.__jellytoast_installed) return;
  window.__jellytoast_installed = true;

  function bind() {
    if (typeof QWebChannel === 'undefined' || !window.qt || !qt.webChannelTransport) {
      return setTimeout(bind, 50);
    }
    new QWebChannel(qt.webChannelTransport, function(channel) {
      window.jellytoast = channel.objects.bridge;
      console.log('[JellyToast] bridge ready');
      hookPlaybackManager();
    });
  }

  function hookPlaybackManager() {
    let attempts = 0;
    const tick = setInterval(function() {
      attempts++;
      const pm = window.playbackManager
              || (window.require && (function(){try{return require('playbackManager');}catch(e){return null;}})());
      if (pm && typeof pm.play === 'function' && !pm.__jellytoast_hooked) {
        pm.__jellytoast_hooked = true;
        const origPlay = pm.play.bind(pm);
        pm.play = function(options) {
          try {
            console.log('[JellyToast] playbackManager.play()', options);
            window.jellytoast && window.jellytoast.play_intercepted(JSON.stringify(options || {}));
          } catch (e) { console.error('[JellyToast]', e); }
          return origPlay(options);
        };
        clearInterval(tick);
      } else if (attempts > 100) {
        clearInterval(tick);
        console.warn('[JellyToast] playbackManager not found, falling back to click hook');
        installClickFallback();
      }
    }, 200);
  }

  function installClickFallback() {
    document.addEventListener('click', function(e) {
      const btn = e.target.closest('[data-action="resume"], [data-action="play"], .btnPlay, button[title*="Play"]');
      if (!btn) return;
      const card = btn.closest('[data-id]');
      const id = card && card.getAttribute('data-id');
      if (id && window.jellytoast) {
        window.jellytoast.play_intercepted(JSON.stringify({ItemIds: [id], _source: 'click'}));
      }
    }, true);
  }

  bind();
})();
"""


class Bridge(QObject):
    """JS calls these slots; we just print for now."""

    @pyqtSlot(str)
    def play_intercepted(self, options_json: str):
        print(f"\n>>> play intent: {options_json}\n", flush=True)


def _read_qresource(path: str) -> str:
    f = QFile(path)
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        return ""
    data = bytes(f.readAll().data()).decode("utf-8", errors="replace")
    f.close()
    return data


class JellyToastWindow(QMainWindow):
    def __init__(self, server_url: str):
        super().__init__()
        self.setWindowTitle("JellyToast")
        self.resize(1280, 800)

        self.view = QWebEngineView(self)
        self.setCentralWidget(self.view)

        self.bridge = Bridge()
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        # Inject qwebchannel.js + our shim into every page at DocumentReady
        qwc_js = _read_qresource(":/qtwebchannel/qwebchannel.js")
        if not qwc_js:
            print("WARNING: qwebchannel.js not found in Qt resources", file=sys.stderr)

        script = QWebEngineScript()
        script.setName("jellytoast_shim")
        script.setSourceCode(qwc_js + "\n" + SHIM_JS)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setRunsOnSubFrames(False)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        QWebEngineProfile.defaultProfile().scripts().insert(script)

        self.view.setUrl(QUrl(f"{server_url}/web/"))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("JellyToast")

    if not WEBENGINE_AVAILABLE:
        QMessageBox.critical(
            None, "Missing dependency",
            "JellyToast requires PyQt6-WebEngine to embed Jellyfin Web.\n\n"
            "Install with:\n    sudo pacman -S python-pyqt6-webengine\n\n"
            f"Original error: {_WEBENGINE_ERROR}"
        )
        sys.exit(1)

    settings = get_settings()
    server_url = settings.server_url.rstrip("/")
    if not server_url:
        QMessageBox.critical(
            None, "No server configured",
            "JellyToast reads the Jellyfin server URL from JellyPlayer's config.\n\n"
            "Run JellyPlayer (`bash run.sh`) and sign in once first."
        )
        sys.exit(1)

    win = JellyToastWindow(server_url)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

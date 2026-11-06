"""Qt Quick phone front end (see dev/QT_QUICK_PLAN.md).

``main()`` is the phone UI's own entry (``jellytoast.launch`` picks it on a
phone shell); ``run(app)`` loads ``jellytoast/quick/qml/Main.qml``, exposes
the bridge objects in this package to it, and once the first frame is up
builds the shared core (queue, mpv player, cast manager, media controls)
exactly like the Widgets path does. ``JT_UI=quick`` / ``widgets`` force
the choice.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, Slot

logger = logging.getLogger(__name__)

QML_DIR = Path(__file__).resolve().parent / "qml"


def available() -> bool:
    """Whether this build can run the Qt Quick UI at all. The PyInstaller
    bundles (AppImage / deb / Windows / macOS) prune QtQuick + QtQml for
    size, so there the beta switch is hidden and the classic UI is used."""
    try:
        import PySide6.QtQml  # noqa: F401
        import PySide6.QtQuick  # noqa: F401
        import PySide6.QtQuickControls2  # noqa: F401
    except Exception:
        return False
    return (QML_DIR / "Main.qml").is_file()


def wanted() -> bool:
    """Whether to start the Qt Quick UI instead of the Widgets window:
    ``JT_UI=quick`` / ``JT_UI=widgets`` force it; otherwise an undocked
    phone shell gets it unless "Phone interface" was turned off (Settings →
    "Use the classic interface")."""
    forced = os.environ.get("JT_UI", "").strip().lower()
    if forced in ("quick", "widgets"):
        return forced == "quick"
    try:
        from jellytoast.settings import get_settings
        from jellytoast.touch import phone_ui

        if not (bool(get_settings().phone_ui_quick) and phone_ui()):
            return False
    except Exception as e:
        logger.warning("front-end choice failed, using the classic UI: %s", e)
        return False
    # Last: this imports QtQuick, which a desktop launch needn't load.
    return available()


def _silence_play_reports() -> None:
    """Dev rigs: don't tell the server about plays (no "now playing", no
    play counts / scrobbles from test runs)."""
    from jellytoast.providers import get_provider

    prov = get_provider()
    for name in ("report_playback_start", "report_playback_progress", "report_playback_stopped"):
        setattr(prov, name, lambda *a, **k: None)
    logger.warning("play reports to the server are OFF (JT_NO_PLAY_REPORTS=1)")


def _start_services(app, core: dict, *, scrobble: bool = True) -> None:
    """The window-free services the Widgets app starts after first paint
    (app.main → _post_show_init), and the same flushes on quit."""

    def _try(what, fn):
        try:
            return fn()
        except Exception as e:  # a service must never take the UI down
            logger.warning("%s unavailable: %s", what, e)

    from jellytoast import offline

    _try("offline / connectivity", offline.init)

    def _sleep():
        from jellytoast.power import SleepInhibitor

        inh = SleepInhibitor()
        inh.start()
        core["sleep"] = inh

    _try("sleep inhibitor", _sleep)

    def _notify():
        from jellytoast.notifications.nowplaying import NowPlayingNotifier

        n = NowPlayingNotifier(app)
        n.start()
        core["notifier"] = n

    _try("now-playing notifications", _notify)

    if scrobble:

        def _scrobble():
            from jellytoast.scrobble import get_scrobble_manager, refresh_server_scrobble_flags

            get_scrobble_manager().flush_pending()
            refresh_server_scrobble_flags()

        _try("scrobbling", _scrobble)
    else:
        logger.warning("Last.fm / ListenBrainz scrobbling is OFF (dev rig)")

    def _cleanup():
        if scrobble:
            _try(
                "scrobble flush",
                lambda: (
                    __import__("jellytoast.scrobble", fromlist=["get_scrobble_manager"])
                    .get_scrobble_manager()
                    .flush_current_on_quit()
                ),
            )
        _try(
            "download suspend",
            lambda: __import__("jellytoast.offline.manager", fromlist=["suspend"]).suspend(),
        )
        if core.get("queue") is not None:
            _try("queue save", core["queue"].flush_pending_save)
        _try(
            "image cache flush",
            lambda: __import__(
                "jellytoast.image_cache", fromlist=["flush_pending_writes"]
            ).flush_pending_writes(timeout_s=1.0),
        )
        if core.get("cast") is not None:
            _try("cast cleanup", core["cast"].cleanup)
        if core.get("mpv") is not None:
            _try("mpv shutdown", core["mpv"].shutdown)
        if core.get("mpris") is not None:
            _try("media controls stop", core["mpris"].stop)
        if core.get("sleep") is not None:
            _try("sleep inhibitor stop", core["sleep"].stop)

    app.aboutToQuit.connect(_cleanup)


def _apply_font(app) -> None:
    """Settings → Display → Font, else the desktop app's built-in stack
    (Inter → Noto Sans → sans-serif; the Widgets UI gets it from QSS)."""
    from PySide6.QtGui import QFont

    from jellytoast.settings import get_settings

    fam = (get_settings().font_family or "").strip()
    f = QFont(app.font())
    f.setFamilies([fam] if fam else ["Inter", "Segoe UI", "Noto Sans", "sans-serif"])
    app.setFont(f)


def _start_core(app, core: dict) -> None:
    """Queue, mpv, cast, media controls and background services — the same
    objects the Widgets window builds."""
    from jellytoast.cast_manager import CastManager
    from jellytoast.media_controls import MediaControlsService
    from jellytoast.player_backend import MpvController
    from jellytoast.player_state import PlayerBus
    from jellytoast.queue_manager import QueueManager
    from jellytoast.settings import get_settings

    settings = get_settings()
    if os.environ.get("JT_NO_PLAY_REPORTS") == "1":
        _silence_play_reports()
    core["cast"] = CastManager()
    core["queue"] = QueueManager(app)
    mpv = MpvController()
    mpv.set_cast_manager(core["cast"])
    core["mpv"] = mpv
    PlayerBus.get().volume_changed.emit(settings.volume)
    if settings.media_integration_enabled:
        try:
            mpris = MediaControlsService()
            mpris.start(None)
            core["mpris"] = mpris
        except Exception as e:  # never block the UI on the session bus
            logger.warning("media controls unavailable: %s", e)
    _start_services(app, core, scrobble=os.environ.get("JT_NO_PLAY_REPORTS") != "1")


def build_engine(app, *, start_core: bool = True):
    """Create the core + bridge objects and a loaded QQmlApplicationEngine.
    ``start_core=False`` skips mpv / MPRIS (tests)."""
    from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
    from PySide6.QtQuickControls2 import QQuickStyle

    from jellytoast.providers import get_provider
    from jellytoast.quick.actions import Actions
    from jellytoast.quick.cast import Cast
    from jellytoast.quick.covers import CoverPixmapProvider, Covers
    from jellytoast.quick.icons import IconProvider
    from jellytoast.quick.libraries import Libraries
    from jellytoast.quick.models import (
        DownloadsModel,
        LibraryModel,
        LyricsModel,
        QueueModel,
        RadioModel,
        SearchModel,
        TracksModel,
    )
    from jellytoast.quick.player import PlayerController
    from jellytoast.quick.playlists import Playlists
    from jellytoast.quick.prefs import Prefs
    from jellytoast.quick.scrobbling import Scrobbling
    from jellytoast.quick.session import Session
    from jellytoast.quick.theme import Theme

    # Our own components + theme; never the platform style — Plasma exports
    # QT_QUICK_CONTROLS_STYLE=org.kde.breeze, which pip's PySide6 can't load.
    # JT_QUICK_STYLE overrides for experiments.
    style = os.environ.get("JT_QUICK_STYLE") or "Basic"
    if QQuickStyle.name() != style:  # once per process (a 2nd engine can't change it)
        QQuickStyle.setStyle(style)
    _apply_font(app)
    signed_in = bool(get_provider().is_authenticated)

    qmlRegisterType(TracksModel, "Jellytoast", 1, 0, "TracksModel")
    qmlRegisterType(LibraryModel, "Jellytoast", 1, 0, "LibraryModel")
    qmlRegisterType(SearchModel, "Jellytoast", 1, 0, "SearchModel")
    qmlRegisterType(DownloadsModel, "Jellytoast", 1, 0, "DownloadsModel")
    qmlRegisterType(RadioModel, "Jellytoast", 1, 0, "RadioModel")
    engine = QQmlApplicationEngine()
    bridge = {
        "albums": LibraryModel(engine),
        "queue": QueueModel(engine),
        "lyrics": LyricsModel(engine),
        "actions": Actions(engine),
        "session": Session(engine),
        "prefs": Prefs(engine),
        "scrobbling": Scrobbling(engine),
        "libraries": Libraries(engine),
        "playlists": Playlists(engine),
        "cast": Cast(None, engine),
        "player": PlayerController(engine),
        "theme": Theme(engine),
        "covers": Covers(engine),
    }
    engine.addImageProvider("icon", IconProvider())
    engine.addImageProvider("coverpx", CoverPixmapProvider(bridge["covers"]))
    ctx = engine.rootContext()
    for name, obj in bridge.items():
        ctx.setContextProperty(name, obj)
    core: dict = {}
    engine._jt_keep = (core, bridge)  # keep Python wrappers alive with the engine
    from jellytoast.boot_timing import mark as _mark

    _mark("quick: bridge objects built")
    engine.load(str(QML_DIR / "Main.qml"))
    _mark("quick: QML loaded")
    # Only once the UI is up: start the player core. A failed load starts
    # nothing, so the caller can fall back to the classic window cleanly.
    if start_core and engine.rootObjects():
        _start_core_and_attach(app, engine)
        if signed_in:
            bridge["albums"].reload()
    return engine


def _start_core_and_attach(app, engine) -> None:
    """Start the player core and hand it to the bridge objects (which QML
    has been using since load — their properties are valid without it)."""
    from jellytoast.boot_timing import mark as _mark

    core, bridge = engine._jt_keep
    _start_core(app, core)
    bridge["player"].attach_queue(core.get("queue"))
    bridge["queue"].attach(core.get("queue"))
    bridge["cast"].attach(core.get("cast"))
    _mark("quick: core started")


def _start_bridge(app, engine) -> None:
    """Dev-only eval socket (``JT_TEST_BRIDGE=1``) with the rig in scope —
    see jellytoast/test_bridge.py and dev/qctl.py."""
    from jellytoast.player_state import PlayerBus, get_now_playing
    from jellytoast.providers import get_provider
    from jellytoast.quick.rig import Rig, pump
    from jellytoast.settings import get_settings
    from jellytoast.test_bridge import TestBridge

    rig = Rig(engine)
    core, bridge = engine._jt_keep

    def namespace():
        return {
            "app": app,
            "engine": engine,
            "rig": rig,
            "win": rig.window,
            "pump": pump,
            "bus": PlayerBus.get(),
            "get_now_playing": get_now_playing,
            "get_provider": get_provider,
            "get_settings": get_settings,
            "qm": core.get("queue"),
            "mpv": core.get("mpv"),
            **bridge,
        }

    app._test_bridge = TestBridge(app, namespace)
    app._test_bridge.start()


def run(app) -> "int | None":
    """Start the Qt Quick front end and run the event loop. ``None`` if it
    couldn't load — the caller falls back to the classic window."""
    from jellytoast.boot_timing import mark as _mark

    _mark("quick: run() entered")
    try:
        engine = build_engine(app, start_core=False)
    except Exception:
        logger.exception("Qt Quick UI failed to start — using the classic interface")
        return None
    if not engine.rootObjects():
        logger.error("Qt Quick UI failed to load (QML errors above) — using the classic interface")
        return None
    if os.environ.get("JT_TEST_BRIDGE") == "1":
        _start_bridge(app, engine)
    size = os.environ.get("JT_QUICK_SIZE", "")  # e.g. 402x874 — rig / desktop dev
    if "x" in size:
        w, h = (int(v) for v in size.lower().split("x", 1))
        engine.rootObjects()[0].resize(w, h)
    app.setQuitOnLastWindowClosed(True)
    win = engine.rootObjects()[0]
    _keep_running_in_background(app, win)
    # The library's first page goes out now, so it loads while the first
    # frame is drawn; the core (mpv, media controls, cast discovery — the
    # slow imports) starts only once that frame is on screen.
    from jellytoast.providers import get_provider

    if get_provider().is_authenticated:
        engine._jt_keep[1]["albums"].reload()

    def _after_first_frame():
        _mark("quick: first frame rendered")
        _start_core_and_attach(app, engine)

    _FirstFrameRelay(win, _after_first_frame)
    return app.exec()


class _FirstFrameRelay(QObject):
    """Run ``fn`` on the GUI thread once the window has rendered a frame.
    ``afterRendering`` is emitted on the render thread; the signal hop
    (queued, to this object, which lives on the GUI thread) crosses back."""

    hit = Signal()

    def __init__(self, window, fn):
        super().__init__(window)
        self._window = window
        self._fn = fn
        self._done = False
        self.hit.connect(self._run, Qt.ConnectionType.QueuedConnection)
        window.afterRendering.connect(self._rendered, Qt.ConnectionType.DirectConnection)

    def _rendered(self):  # render thread
        if not self._done:
            self._done = True
            self.hit.emit()

    @Slot()
    def _run(self):  # GUI thread
        try:
            self._window.afterRendering.disconnect(self._rendered)
        except Exception:
            pass
        self._fn()


def main() -> None:
    """The phone UI's own entry (``jellytoast.launch`` picks it): the boot
    steps the classic window's ``app.main`` does, without importing
    ``jellytoast.app`` — that pulls in the whole desktop UI, mpv and D-Bus
    at import time, over a second of a phone's start before anything
    shows. Same settings, sign-in, logging, single instance and core."""
    import sys

    from jellytoast import boot_env as boot
    from jellytoast.boot_timing import arm_stall_tracebacks, mark

    mark("main() entered (phone UI)")
    arm_stall_tracebacks()
    boot._enable_faulthandler()
    boot._setup_hidpi()
    from jellytoast.settings import warm_keyring_async

    warm_keyring_async()
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    mark("QApplication constructed")
    from jellytoast.platform_compat import desktop_entry_id, is_mobile_shell
    from jellytoast.version import __version__

    app.setApplicationName("jellytoast")
    app.setApplicationDisplayName("jellytoast")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("jellytoast")
    app.setDesktopFileName(desktop_entry_id() if is_mobile_shell() else "jellytoast")
    app.setQuitOnLastWindowClosed(False)
    from jellytoast import log as _jt_log

    _jt_log.install()
    mark("file log installed")
    from jellytoast import i18n

    i18n.install(app)
    from jellytoast.ui_helpers import apply_app_palette, make_app_icon, set_boot_default_font

    app.setWindowIcon(QIcon(make_app_icon(64)))
    set_boot_default_font(app.font())
    apply_app_palette()
    boot.install_signal_handlers(app)
    from jellytoast.color_tokens import load_persisted_overrides

    load_persisted_overrides()
    try:
        from jellytoast.settings import get_settings
        from jellytoast.theme_presets import apply_theme_family, family_has_both

        s = get_settings()
        if (
            s.theme_mode == "auto"
            and s.theme_family not in ("", "jellytoast")
            and family_has_both(s.theme_family)
        ):
            apply_theme_family(s.theme_family, "auto", s.frosted)
    except Exception:
        pass
    from jellytoast import touch

    touch.install(app)
    from jellytoast.single_instance import SingleInstance

    app._single_instance = SingleInstance(os.environ.get("JT_INSTANCE_KEY", "jellytoast"), app)
    if not app._single_instance.acquire():
        logger.info("already running; raised existing window.")
        sys.exit(0)
    from jellytoast import radio_state

    radio_state.init()
    mark("boot done, loading the UI")
    rc = run(app)
    if rc is None:
        # The Quick UI couldn't load. A second QApplication can't be made in
        # this process: start over into the classic window.
        logger.error("starting the classic interface instead")
        os.environ["JT_UI"] = "widgets"
        os.execv(sys.executable, [sys.executable, "-m", "jellytoast"])
    sys.exit(rc)


class _CloseToBackground(QObject):
    """Closing the window while music plays hides it and keeps playing —
    swiping the app away on a phone shouldn't cut the song off, and the
    shell's media controls stay live. Closing while paused / stopped quits
    as before (there's no tray to come back from). Only a close the user
    or the shell asked for (a *spontaneous* one) is intercepted: a quit the
    app itself requests (MPRIS Quit, the session ending) goes through."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window

    @staticmethod
    def should_hide() -> bool:
        from jellytoast.player_state import get_now_playing

        np = get_now_playing()
        return bool(np.item_id) and not np.is_paused

    def eventFilter(self, obj, event):  # noqa: N802
        from PySide6.QtCore import QEvent

        if (
            obj is self._window
            and event.type() == QEvent.Type.Close
            and event.spontaneous()
            and self.should_hide()
        ):
            event.ignore()
            self._window.hide()
            logger.info("window closed while playing — kept running in the background")
            return True
        return False


def _keep_running_in_background(app, window) -> None:
    """Hide-on-close while playing, and the two ways back: launching the
    app again (the single-instance ping) and the media controls' "open"
    (MPRIS Raise → ``open_main_window``)."""
    keeper = _CloseToBackground(window)
    window.installEventFilter(keeper)
    window._jt_close_keeper = keeper

    def _bring_back():
        from jellytoast.single_instance import use_activation_token

        use_activation_token(getattr(app, "_single_instance", None))
        window.show()
        window.raise_()
        window.requestActivate()

    from jellytoast.player_state import PlayerBus

    window._jt_bring_back = _bring_back
    PlayerBus.get().open_main_window.connect(_bring_back)
    single = getattr(app, "_single_instance", None)
    if single is not None:
        single.raise_requested.connect(_bring_back)

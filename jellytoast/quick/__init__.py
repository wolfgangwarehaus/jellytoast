"""Qt Quick phone front end (see dev/QT_QUICK_PLAN.md).

``run(app)`` builds the shared core (queue, mpv player, cast manager, media
controls) exactly like the Widgets path does, exposes it to QML through the
bridge objects in this package, and loads ``jellytoast/quick/qml/Main.qml``.
Opt-in for now: ``JT_UI=quick``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

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
    ``JT_UI=quick`` / ``JT_UI=widgets`` force it; otherwise the opt-in
    "New phone interface (beta)" setting, on an undocked phone shell."""
    forced = os.environ.get("JT_UI", "").strip().lower()
    if forced in ("quick", "widgets"):
        return forced == "quick"
    if not available():
        return False
    try:
        from jellytoast.settings import get_settings
        from jellytoast.touch import phone_ui

        return bool(get_settings().phone_ui_quick) and phone_ui()
    except Exception as e:
        logger.warning("front-end choice failed, using the classic UI: %s", e)
        return False


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
        SearchModel,
        TracksModel,
    )
    from jellytoast.quick.player import PlayerController
    from jellytoast.quick.prefs import Prefs
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
    engine = QQmlApplicationEngine()
    bridge = {
        "albums": LibraryModel(engine),
        "queue": QueueModel(engine),
        "lyrics": LyricsModel(engine),
        "actions": Actions(engine),
        "session": Session(engine),
        "prefs": Prefs(engine),
        "libraries": Libraries(engine),
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
    engine.load(str(QML_DIR / "Main.qml"))
    # Only once the UI is up: start the player core. A failed load starts
    # nothing, so the caller can fall back to the classic window cleanly.
    if start_core and engine.rootObjects():
        _start_core(app, core)
        bridge["player"].attach_queue(core.get("queue"))
        bridge["queue"].attach(core.get("queue"))
        bridge["cast"].attach(core.get("cast"))
        if signed_in:
            bridge["albums"].reload()
    return engine


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
    try:
        engine = build_engine(app)
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
    return app.exec()

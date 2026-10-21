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


def wanted() -> bool:
    """Whether to start the Qt Quick UI instead of the Widgets window."""
    return os.environ.get("JT_UI", "").strip().lower() == "quick"


def build_engine(app, *, start_core: bool = True):
    """Create the core + bridge objects and a loaded QQmlApplicationEngine.
    ``start_core=False`` skips mpv / MPRIS (tests)."""
    from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
    from PySide6.QtQuickControls2 import QQuickStyle

    from jellytoast.player_state import PlayerBus
    from jellytoast.providers import get_provider
    from jellytoast.quick.icons import IconProvider
    from jellytoast.quick.models import AlbumsModel, TracksModel
    from jellytoast.quick.player import PlayerController
    from jellytoast.quick.theme import Theme

    core = {}
    if start_core:
        from jellytoast.cast_manager import CastManager
        from jellytoast.media_controls import MediaControlsService
        from jellytoast.player_backend import MpvController
        from jellytoast.queue_manager import QueueManager
        from jellytoast.settings import get_settings

        settings = get_settings()
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

    # Our own components + theme; never the platform style — Plasma exports
    # QT_QUICK_CONTROLS_STYLE=org.kde.breeze, which pip's PySide6 can't load.
    # JT_QUICK_STYLE overrides for experiments.
    QQuickStyle.setStyle(os.environ.get("JT_QUICK_STYLE") or "Basic")
    signed_in = bool(get_provider().is_authenticated)

    qmlRegisterType(TracksModel, "Jellytoast", 1, 0, "TracksModel")
    engine = QQmlApplicationEngine()
    bridge = {
        "albums": AlbumsModel(engine),
        "player": PlayerController(engine, queue=core.get("queue")),
        "theme": Theme(engine),
    }
    engine.addImageProvider("icon", IconProvider())
    ctx = engine.rootContext()
    for name, obj in bridge.items():
        ctx.setContextProperty(name, obj)
    ctx.setContextProperty("signedIn", signed_in)
    engine._jt_keep = (core, bridge)  # keep Python wrappers alive with the engine
    engine.load(str(QML_DIR / "Main.qml"))
    if signed_in and start_core:
        bridge["albums"].reload()
    return engine


def run(app) -> int:
    """Start the Qt Quick front end and run the event loop."""
    engine = build_engine(app)
    if not engine.rootObjects():
        logger.error("Qt Quick UI failed to load — see QML errors above")
        return 1
    app.setQuitOnLastWindowClosed(True)
    return app.exec()

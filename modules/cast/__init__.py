"""Cast subsystem sub-package.

Houses cast-target backends that don't fit the legacy
``modules/cast_manager.py`` shape — currently just the DLNA / UPnP-AV
control point. Chromecast + AirPlay 2 still live at the repo root in
``modules/cast_manager.py`` and ``modules/airplay2.py`` and are wired
through ``CastManager``; this package is where new backends land so
the top-level cast file doesn't keep growing.

No re-exports — import ``modules.cast.dlna.DlnaController`` directly.
"""

__all__ = ["dlna"]

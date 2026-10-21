"""Phone settings for the Qt Quick front end: the handful of options that
matter on a phone, written through the same settings keys and PlayerBus
signals the Widgets Settings dialog uses."""

from __future__ import annotations

from PySide6.QtCore import Property, QObject, Signal, Slot

from jellytoast.player_state import PlayerBus


def _settings():
    from jellytoast.settings import get_settings

    return get_settings()


class Prefs(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        bus = PlayerBus.get()
        bus.replaygain_changed.connect(lambda _m: self.changed.emit())
        bus.offline_mode_changed.connect(lambda _on: self.changed.emit())

    # ── option lists ({label, value}) ──
    def _qualities(self):
        return [
            {"label": self.tr("Original"), "value": "original"},
            *({"label": f"{k} kbps", "value": k} for k in ("320", "256", "192", "128", "96")),
        ]

    def _normalizations(self):
        return [
            {"label": self.tr("Off"), "value": "no"},
            {"label": self.tr("Per song"), "value": "track"},
            {"label": self.tr("Per album"), "value": "album"},
        ]

    qualities = Property("QVariantList", _qualities, constant=True)
    normalizations = Property("QVariantList", _normalizations, constant=True)

    # ── values ──
    def _get_quality(self) -> str:
        return _settings().audio_quality or "original"

    def _get_download_quality(self) -> str:
        return _settings().download_quality or "original"

    def _get_normalization(self) -> str:
        return _settings().replaygain or "no"

    def _get_media_keys(self) -> bool:
        return bool(_settings().media_integration_enabled)

    quality = Property(str, _get_quality, notify=changed)
    downloadQuality = Property(str, _get_download_quality, notify=changed)
    normalization = Property(str, _get_normalization, notify=changed)
    mediaKeys = Property(bool, _get_media_keys, notify=changed)

    @Slot(str)
    def setQuality(self, v: str) -> None:  # noqa: N802
        _settings().audio_quality = v
        # The backend recomputes its bit-perfect contract on this.
        PlayerBus.get().audio_quality_changed.emit(v)
        self.changed.emit()

    @Slot(str)
    def setDownloadQuality(self, v: str) -> None:  # noqa: N802
        _settings().download_quality = v
        self.changed.emit()

    @Slot(str)
    def setNormalization(self, v: str) -> None:  # noqa: N802
        PlayerBus.get().replaygain_changed.emit(v)  # the backend persists it
        _settings().replaygain = v
        self.changed.emit()

    @Slot(bool)
    def setMediaKeys(self, on: bool) -> None:  # noqa: N802
        _settings().media_integration_enabled = bool(on)  # applies next launch
        self.changed.emit()

    # ── offline ──
    def _offline_mode(self) -> bool:
        from jellytoast import offline

        try:
            return bool(offline.is_offline_mode())
        except Exception:
            return False

    def _wifi_only(self) -> bool:
        from jellytoast import offline

        try:
            return bool(offline.is_wifi_only())
        except Exception:
            return False

    offlineMode = Property(bool, _offline_mode, notify=changed)
    wifiOnly = Property(bool, _wifi_only, notify=changed)

    @Slot(bool)
    def setOfflineMode(self, on: bool) -> None:  # noqa: N802
        from jellytoast import offline

        offline.set_offline_mode(bool(on))
        self.changed.emit()

    @Slot(bool)
    def setWifiOnly(self, on: bool) -> None:  # noqa: N802
        from jellytoast import offline

        offline.set_wifi_only(bool(on))
        self.changed.emit()

    # ── casting (opt-in per protocol) ──
    _CAST = (
        ("chromecast", "Chromecast / Google Cast"),
        ("airplay", "AirPlay"),
        ("dlna", "DLNA / UPnP"),
        ("sonos", "Sonos"),
    )

    def _cast_types(self):
        st = _settings()
        return [
            {"key": k, "label": label, "on": bool(getattr(st, f"cast_{k}_enabled"))}
            for k, label in self._CAST
        ]

    castTypes = Property("QVariantList", _cast_types, notify=changed)

    @Slot(str, bool)
    def setCastType(self, key: str, on: bool) -> None:  # noqa: N802
        if key in dict(self._CAST):
            setattr(_settings(), f"cast_{key}_enabled", bool(on))
            self.changed.emit()

    @Slot(result=str)
    def version(self) -> str:
        try:
            from jellytoast.version import __version__

            return str(__version__)
        except Exception:
            return ""

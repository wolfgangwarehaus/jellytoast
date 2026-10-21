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
        PlayerBus.get().replaygain_changed.connect(lambda _m: self.changed.emit())

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

    @Slot(result=str)
    def version(self) -> str:
        try:
            from jellytoast.version import __version__

            return str(__version__)
        except Exception:
            return ""

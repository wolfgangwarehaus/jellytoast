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
        bus.bit_perfect_active_changed.connect(lambda _on: self.changed.emit())

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

    # ── theme ──
    def _themes(self):
        from jellytoast.theme_presets import THEME_FAMILIES

        out = [{"label": "jellytoast", "value": "jellytoast"}]
        out += [{"label": fam.label, "value": key} for key, fam in THEME_FAMILIES.items()]
        return out

    def _modes(self):
        return [
            {"label": self.tr("Dark"), "value": "dark"},
            {"label": self.tr("Light"), "value": "light"},
            {"label": self.tr("Follow the system"), "value": "auto"},
        ]

    themes = Property("QVariantList", _themes, constant=True)
    themeModes = Property("QVariantList", _modes, constant=True)

    def _theme(self) -> str:
        return _settings().theme_family or "jellytoast"

    def _theme_mode(self) -> str:
        return _settings().theme_mode or "dark"

    def _theme_has_modes(self) -> bool:
        from jellytoast.theme_presets import family_has_both

        return bool(family_has_both(self._theme()))

    theme = Property(str, _theme, notify=changed)
    themeMode = Property(str, _theme_mode, notify=changed)
    themeHasModes = Property(bool, _theme_has_modes, notify=changed)

    # ── accent (the desktop's accent swatches; the built-in theme only —
    #    a preset family brings its own accent) ──
    def _accent_presets(self):
        from jellytoast.theme import ACCENT_PRESETS

        return [{"label": self.tr(name), "value": hex_} for name, hex_ in ACCENT_PRESETS]

    accentPresets = Property("QVariantList", _accent_presets, constant=True)
    accent = Property(str, lambda self: (_settings().accent_color or "").lower(), notify=changed)

    @Slot(str)
    def setAccent(self, hex_value: str) -> None:  # noqa: N802
        """Same steps as the desktop's swatch pick (settings_page_display.
        _on_accent_picked): persist, drop accent-derived colour overrides,
        refresh the theme constants, broadcast."""
        from jellytoast import icons as _icons
        from jellytoast import ui_helpers as _uih
        from jellytoast.settings_migration import open_qsettings

        st = _settings()
        st.accent_color = hex_value
        st.follow_system_accent = False  # a pick replaces following the system
        qs = open_qsettings()
        for tok in ("ACCENT", "ACCENT_DEEP", "BORDER_ACCENT"):
            qs.remove(f"debug/colors/{tok}")
        _uih.refresh_theme()
        _icons.refresh_theme()
        PlayerBus.get().theme_changed.emit()
        self.changed.emit()

    @Slot(str)
    def setTheme(self, family: str) -> None:  # noqa: N802
        from jellytoast.theme_presets import apply_theme_family

        st = _settings()
        apply_theme_family(family, st.theme_mode or "dark", bool(st.frosted))
        self.changed.emit()

    # Frosted glass — the desktop's Frosted / Opaque switch. On the phone it
    # turns the frosted top bar, mini player and sheets plus the blurred
    # album backdrop on Now Playing on or off (off is lighter on a slow GPU).
    frosted = Property(bool, lambda self: bool(_settings().frosted), notify=changed)

    @Slot(bool)
    def setFrosted(self, on: bool) -> None:  # noqa: N802
        from jellytoast.theme_presets import apply_theme_family

        st = _settings()
        st.frosted = bool(on)
        apply_theme_family(st.theme_family or "jellytoast", st.theme_mode or "dark", bool(on))
        self.changed.emit()

    @Slot(str)
    def setThemeMode(self, mode: str) -> None:  # noqa: N802
        from jellytoast.theme_presets import apply_theme_family

        st = _settings()
        apply_theme_family(st.theme_family or "jellytoast", mode, bool(st.frosted))
        self.changed.emit()

    # ── sound: equalizer + crossfade (the desktop Playback / EQ pages' keys
    #    and signals; both are off while bit-perfect playback is on) ──
    def _bit_perfect(self) -> bool:
        return bool(getattr(PlayerBus.get(), "bit_perfect_active", False))

    bitPerfect = Property(bool, _bit_perfect, notify=changed)

    def _eq_presets(self):
        from jellytoast.eq_presets import PRESETS

        names = list(PRESETS) + sorted(_settings().eq_user_presets or {})
        return names + (["Custom"] if self._eq_preset() == "Custom" else [])

    def _eq_preset(self) -> str:
        return _settings().eq_preset or "Flat"

    def _eq_bands(self):
        return [float(b) for b in (_settings().eq_bands or [0.0] * 10)]

    eqEnabled = Property(bool, lambda self: bool(_settings().eq_enabled), notify=changed)
    eqPreset = Property(str, _eq_preset, notify=changed)
    eqPresets = Property("QVariantList", _eq_presets, notify=changed)
    eqBands = Property("QVariantList", _eq_bands, notify=changed)
    eqFrequencies = Property(
        "QVariantList",
        lambda self: list(__import__("jellytoast.eq_presets", fromlist=["x"]).BAND_FREQUENCIES),
        constant=True,
    )

    def _emit_eq(self) -> None:
        st = _settings()
        PlayerBus.get().eq_changed.emit(bool(st.eq_enabled), list(st.eq_bands))
        self.changed.emit()

    @Slot(bool)
    def setEqEnabled(self, on: bool) -> None:  # noqa: N802
        _settings().eq_enabled = bool(on)
        self._emit_eq()

    @Slot(str)
    def setEqPreset(self, name: str) -> None:  # noqa: N802
        from jellytoast.eq_presets import PRESETS

        st = _settings()
        bands = PRESETS.get(name) or (st.eq_user_presets or {}).get(name)
        if isinstance(bands, dict):  # user presets carry {bands, preamp}
            st.eq_preamp = float(bands.get("preamp") or 0.0)
            bands = bands.get("bands")
        if not bands:
            return
        st.eq_bands = [float(b) for b in bands]
        st.eq_preset = name
        self._emit_eq()

    @Slot(int, float)
    def setEqBand(self, index: int, db: float) -> None:  # noqa: N802
        from jellytoast.eq_presets import GAIN_LIMIT_DB

        st = _settings()
        bands = self._eq_bands()
        if not (0 <= index < len(bands)):
            return
        bands[index] = max(-GAIN_LIMIT_DB, min(GAIN_LIMIT_DB, round(float(db), 1)))
        st.eq_bands = bands
        st.eq_preset = "Custom"
        self._emit_eq()

    crossfade = Property(bool, lambda self: bool(_settings().crossfade_enabled), notify=changed)
    crossfadeSeconds = Property(
        float, lambda self: _settings().crossfade_duration_ms / 1000.0, notify=changed
    )

    @Slot(bool)
    def setCrossfade(self, on: bool) -> None:  # noqa: N802
        _settings().crossfade_enabled = bool(on)
        PlayerBus.get().crossfade_changed.emit(bool(on))
        self.changed.emit()

    @Slot(float)
    def setCrossfadeSeconds(self, seconds: float) -> None:  # noqa: N802
        _settings().crossfade_duration_ms = int(round(seconds * 1000))
        self.changed.emit()

    # ── lyrics size ──
    def _lyrics_sizes(self):
        return [
            {"label": self.tr("Small"), "value": "small"},
            {"label": self.tr("Default"), "value": "default"},
            {"label": self.tr("Large"), "value": "large"},
            {"label": self.tr("Largest"), "value": "largest"},
        ]

    lyricsSizes = Property("QVariantList", _lyrics_sizes, constant=True)
    lyricsSize = Property(
        str, lambda self: _settings().lyrics_font_size or "default", notify=changed
    )

    @Slot(str)
    def setLyricsSize(self, size: str) -> None:  # noqa: N802
        _settings().lyrics_font_size = size
        PlayerBus.get().lyrics_font_size_changed.emit(size)
        self.changed.emit()

    # ── library view (grid / list), shared with the desktop library ──
    def _view_mode(self) -> str:
        return _settings().library_view_mode or "grid"

    viewMode = Property(str, _view_mode, notify=changed)

    @Slot(str)
    def setViewMode(self, mode: str) -> None:  # noqa: N802
        _settings().library_view_mode = "list" if mode == "list" else "grid"
        self.changed.emit()

    # The Artists section has its own grid / list choice.
    artistViewMode = Property(str, lambda self: _settings().artist_view_mode, notify=changed)

    @Slot(str)
    def setArtistViewMode(self, mode: str) -> None:  # noqa: N802
        _settings().artist_view_mode = mode
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

    @Slot()
    def useClassicUi(self) -> None:  # noqa: N802
        """Back to the classic (Widgets) interface from next launch."""
        _settings().phone_ui_quick = False

    @Slot(result=str)
    def version(self) -> str:
        try:
            from jellytoast.version import __version__

            return str(__version__)
        except Exception:
            return ""

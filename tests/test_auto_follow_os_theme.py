"""Tests for the "Auto (follow OS)" theme + the cross-platform OS
light/dark detection it rides on.

Covers:
  * the `Theme.dark` flag (dark vs light family — drives the Windows Mica
    variant),
  * `theme.os_color_scheme()` returning a valid `"dark"`/`"light"` token,
  * `get_active_theme()` resolving `theme_mode == "auto"` to
    frosted-light / frosted-dark by the OS scheme,
  * `blur.apply(dark=None)` resolving the Mica variant from the active
    theme (so every call site gets the right one for free),
  * the Windows "no body" default (`_win_glass_alpha()` == 0).

QSettings is process-wide in test mode (conftest), so the
`clean_theme_settings` fixture clears `ui/theme_mode` before/after each
test that resolves `get_active_theme()`.
"""

from __future__ import annotations

import pytest

from modules import blur
from modules import theme as th
from modules.theme import THEMES


def _settings_handle():
    from modules.settings import get_settings

    return get_settings()._s


@pytest.fixture
def clean_theme_settings():
    def _wipe():
        s = _settings_handle()
        s.remove("ui/theme_mode")
        s.remove("ui/accent_color")
        s.sync()

    _wipe()
    yield
    _wipe()


def _set_theme_mode(mode: str):
    s = _settings_handle()
    s.setValue("ui/theme_mode", mode)
    s.sync()


# ── Theme.dark flag ──────────────────────────────────────────────────


class TestDarkFlag:
    def test_dark_family_is_dark(self):
        assert THEMES["frosted_dark"].dark is True
        assert THEMES["dark"].dark is True

    def test_light_family_is_light(self):
        assert THEMES["frosted_light"].dark is False
        assert THEMES["light"].dark is False


# ── os_color_scheme() ────────────────────────────────────────────────


class TestOsColorScheme:
    def test_returns_a_valid_token(self, qapp):
        # On a real platform it's whatever the OS reports; either way it
        # must be one of the two tokens get_active_theme() understands.
        assert th.os_color_scheme() in {"dark", "light"}

    def test_safe_default_without_qapplication(self, monkeypatch):
        # No QGuiApplication.instance() → conservative "dark", never raises.
        from PySide6.QtGui import QGuiApplication

        monkeypatch.setattr(QGuiApplication, "instance", staticmethod(lambda: None))
        assert th.os_color_scheme() == "dark"

    def test_never_raises(self, monkeypatch):
        from PySide6.QtGui import QGuiApplication

        def boom():
            raise RuntimeError("no style hints")

        monkeypatch.setattr(QGuiApplication, "instance", staticmethod(boom))
        assert th.os_color_scheme() == "dark"


# ── get_active_theme() resolves "auto" ───────────────────────────────


class TestAutoResolution:
    def test_auto_light_resolves_frosted_light(
        self, clean_theme_settings, monkeypatch
    ):
        monkeypatch.setattr(th, "os_color_scheme", lambda: "light")
        _set_theme_mode("auto")
        assert th.get_active_theme().name == "frosted_light"

    def test_auto_dark_resolves_frosted_dark(
        self, clean_theme_settings, monkeypatch
    ):
        monkeypatch.setattr(th, "os_color_scheme", lambda: "dark")
        _set_theme_mode("auto")
        assert th.get_active_theme().name == "frosted_dark"

    def test_explicit_theme_ignores_os_scheme(
        self, clean_theme_settings, monkeypatch
    ):
        # An explicit pick must NOT follow the OS.
        monkeypatch.setattr(th, "os_color_scheme", lambda: "light")
        _set_theme_mode("frosted_dark")
        assert th.get_active_theme().name == "frosted_dark"


# ── blur.apply(dark=None) resolves the Mica variant from the theme ────


class TestBlurDarkResolution:
    def _capture_dark(self, monkeypatch):
        seen = {}

        def fake_backend_apply(widget, enabled, corner_radius=0, dark=True):
            seen["dark"] = dark
            return True

        monkeypatch.setattr(blur._backend, "apply", fake_backend_apply)
        return seen

    def test_dark_theme_yields_dark_mica(
        self, qapp, clean_theme_settings, monkeypatch
    ):
        monkeypatch.setattr(th, "os_color_scheme", lambda: "dark")
        _set_theme_mode("frosted_dark")
        seen = self._capture_dark(monkeypatch)
        blur.apply(object(), True, corner_radius=12)  # dark=None → resolve
        assert seen["dark"] is True

    def test_light_theme_yields_light_mica(
        self, qapp, clean_theme_settings, monkeypatch
    ):
        _set_theme_mode("frosted_light")
        seen = self._capture_dark(monkeypatch)
        blur.apply(object(), True, corner_radius=12)
        assert seen["dark"] is False

    def test_auto_light_yields_light_mica(
        self, qapp, clean_theme_settings, monkeypatch
    ):
        monkeypatch.setattr(th, "os_color_scheme", lambda: "light")
        _set_theme_mode("auto")
        seen = self._capture_dark(monkeypatch)
        blur.apply(object(), True, corner_radius=12)
        assert seen["dark"] is False

    def test_explicit_dark_arg_is_not_overridden(self, qapp, monkeypatch):
        seen = self._capture_dark(monkeypatch)
        blur.apply(object(), True, corner_radius=12, dark=False)
        assert seen["dark"] is False


# ── Windows "no body" default ────────────────────────────────────────


class TestWindowsNoBodyDefault:
    def test_win_glass_alpha_defaults_to_zero(self, monkeypatch):
        monkeypatch.delenv("JT_WIN_GLASS_ALPHA", raising=False)
        assert th._win_glass_alpha() == 0

    def test_env_override_still_works(self, monkeypatch):
        monkeypatch.setenv("JT_WIN_GLASS_ALPHA", "30")
        assert th._win_glass_alpha() == 30

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

from jellytoast import blur
from jellytoast import theme as th
from jellytoast.theme import THEMES


def _settings_handle():
    from jellytoast.settings import get_settings

    return get_settings()._s


# clean_theme_settings now lives in conftest.py (one canonical copy).


def _set_theme_mode(mode: str):
    # These tests drive the luminance axis; frosted defaults True (wiped by the
    # fixture), so a "frosted_*" arg self-heals to its luminance and still
    # resolves to the frosted variant — matching the pre-split behaviour.
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

        def fake_backend_apply(widget, enabled, corner_radius=0, dark=True, elevated=False):
            seen["dark"] = dark
            seen["elevated"] = elevated
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

    def test_elevated_flag_reaches_the_backend(self, qapp, monkeypatch):
        """apply(elevated=True) must reach the backend — on Windows it
        drops Acrylic's built-in tint so popups (which carry their own
        QSS frost fill) aren't double-veiled (warmer + more opaque than
        the same popup on Linux, 2026-06-10). Default stays False so
        the main window / dialogs keep the tinted material."""
        seen = self._capture_dark(monkeypatch)
        blur.apply(object(), True, corner_radius=8, elevated=True)
        assert seen["elevated"] is True
        blur.apply(object(), True, corner_radius=8)
        assert seen["elevated"] is False


# ── Windows "no body" default + click-through floor ──────────────────


class TestWindowsBodyAlpha:
    def test_default_is_dark_but_not_zero(self, monkeypatch):
        # Dark-but-translucent default tint (so the dark theme reads as dark
        # on Windows), but NEVER alpha-0 (click-through would break window
        # dragging + popups on Windows).
        monkeypatch.delenv("JT_WIN_GLASS_ALPHA", raising=False)
        assert th._win_glass_alpha() == th._WIN_BODY_DEFAULT_ALPHA
        assert th._WIN_BODY_DEFAULT_ALPHA >= th._WIN_BODY_FLOOR_ALPHA > 0

    def test_env_can_raise_above_floor(self, monkeypatch):
        monkeypatch.setenv("JT_WIN_GLASS_ALPHA", "40")
        assert th._win_glass_alpha() == 40

    def test_env_below_floor_is_clamped_up(self, monkeypatch):
        # Even an explicit 0 / low value can't reach the click-through zone.
        monkeypatch.setenv("JT_WIN_GLASS_ALPHA", "0")
        assert th._win_glass_alpha() == th._WIN_BODY_FLOOR_ALPHA
        monkeypatch.setenv("JT_WIN_GLASS_ALPHA", "5")
        assert th._win_glass_alpha() == th._WIN_BODY_FLOOR_ALPHA

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("JT_WIN_GLASS_ALPHA", "notanumber")
        assert th._win_glass_alpha() == th._WIN_BODY_DEFAULT_ALPHA


# ── Windows Acrylic tint (real frosted-glass blur; opt out via
# JT_NO_WIN_BLUR) ─────────────────────────────────────────────────────


class TestAcrylicTint:
    def test_dark_and_light_differ(self):
        from jellytoast.blur import _dwm

        # Dark = 0xBE (190): eyeball-calibrated on the Win11 laptop
        # (2026-06-10) — the shared 0x99 read too transparent for a dark
        # theme. Light keeps qframelesswindow's default.
        assert _dwm._acrylic_tint(True) == 0xBE202020
        assert _dwm._acrylic_tint(False) == 0x99F2F2F2

    def test_alpha_override_replaces_only_alpha(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setenv("JT_WIN_BLUR_ALPHA", "60")
        # alpha byte swapped to 0x3C, the BBGGRR (0x202020) preserved
        assert _dwm._acrylic_tint(True) == 0x3C202020

    def test_garbage_alpha_falls_back(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setenv("JT_WIN_BLUR_ALPHA", "nope")
        assert _dwm._acrylic_tint(True) == 0xBE202020

    def test_alpha_clamped(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setenv("JT_WIN_BLUR_ALPHA", "999")
        assert (_dwm._acrylic_tint(True) >> 24) == 255

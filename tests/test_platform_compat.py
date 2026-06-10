"""Tests for cross-platform detection (jellytoast/platform_compat.py).

Covers the pre-QApplication Wayland probe (env-var precedence), the
KDE-desktop / KDE-Wayland gates, and the non-Linux short-circuits that
keep every helper safe to call on Windows / macOS.
"""

from __future__ import annotations

import pytest

from jellytoast import platform_compat as pc


@pytest.fixture
def linux(monkeypatch):
    """Force the IS_LINUX gate on so the Linux-only branches run
    regardless of the host the suite executes on."""
    monkeypatch.setattr(pc, "IS_LINUX", True)


@pytest.fixture
def not_linux(monkeypatch):
    monkeypatch.setattr(pc, "IS_LINUX", False)


class TestWillBeWayland:
    def test_false_off_linux(self, not_linux, monkeypatch):
        # Even with WAYLAND_DISPLAY set, a non-Linux host is never Wayland.
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert pc.will_be_wayland() is False

    def test_wayland_display_env(self, linux, monkeypatch):
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert pc.will_be_wayland() is True

    def test_no_env_is_false(self, linux, monkeypatch):
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert pc.will_be_wayland() is False

    def test_qt_platform_override_wayland_wins(self, linux, monkeypatch):
        # QT_QPA_PLATFORM=wayland forces True even with no WAYLAND_DISPLAY.
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
        assert pc.will_be_wayland() is True

    def test_qt_platform_override_xcb_wins(self, linux, monkeypatch):
        # QT_QPA_PLATFORM=xcb forces False even though WAYLAND_DISPLAY is set.
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
        assert pc.will_be_wayland() is False


class TestIsKdeDesktop:
    def test_false_off_linux(self, not_linux, monkeypatch):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        assert pc.is_kde_desktop() is False

    def test_true_when_kde(self, linux, monkeypatch):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        assert pc.is_kde_desktop() is True

    def test_case_insensitive(self, linux, monkeypatch):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "kde")
        assert pc.is_kde_desktop() is True

    def test_substring_match(self, linux, monkeypatch):
        # Some sessions report "KDE:plasma" or similar composites.
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE:plasma")
        assert pc.is_kde_desktop() is True

    def test_false_for_other_desktop(self, linux, monkeypatch):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
        assert pc.is_kde_desktop() is False

    def test_false_when_unset(self, linux, monkeypatch):
        monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
        assert pc.is_kde_desktop() is False


class TestIsKdeWayland:
    def test_requires_both_kde_and_wayland(self, linux, monkeypatch):
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert pc.is_kde_wayland() is True

    def test_false_when_kde_but_not_wayland(self, linux, monkeypatch):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        assert pc.is_kde_wayland() is False

    def test_false_when_wayland_but_not_kde(self, linux, monkeypatch):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        assert pc.is_kde_wayland() is False


class TestNonLinuxShortCircuits:
    def test_is_wayland_false_off_linux(self, not_linux):
        # Short-circuits before ever touching QApplication.
        assert pc.is_wayland() is False

    def test_is_x11_false_off_linux(self, not_linux):
        assert pc.is_x11() is False

    def test_platform_constants_mutually_exclusive(self):
        # Exactly one of the three OS constants is True on any host.
        assert sum([pc.IS_LINUX, pc.IS_WINDOWS, pc.IS_MACOS]) == 1

"""Tests for jellytoast.kde_qt_plugin_fix — the KDE/pipx blur self-heal.

The heal appends the system Qt6 plugin dir to QT_PLUGIN_PATH when (and only
when) jellytoast runs under a pip/pipx-bundled PySide6 on a KDE session, so
KWindowSystem can load its platform backend and frosted blur works. The pure
decision function `_choose_plugin_dir` takes an injected `isdir` predicate, so
the whole gate matrix is exercised here without a real filesystem, a real
PySide6 install, or a real KDE session.
"""

from __future__ import annotations

import os

from jellytoast import kde_qt_plugin_fix as fix

# A pretend bundled (pipx) PySide6 location and the system plugin dir that
# the first candidate check looks for.
PIPX_PYSIDE = "/home/u/.local/share/pipx/venvs/jellytoast/lib/python3.14/site-packages/PySide6"
ARCH_PLUGINS = "/usr/lib/qt6/plugins"


def _isdir_with(*existing):
    """Build an isdir predicate that returns True only for the given paths."""
    wanted = set(existing)
    return lambda p: p in wanted


# The kf6/kwindowsystem dir under the first (Arch) candidate.
ARCH_KF6 = os.path.join(ARCH_PLUGINS, "kf6", "kwindowsystem")


class TestChoosePluginDir:
    def _call(self, **over):
        base = dict(
            platform="linux",
            desktop="KDE",
            disabled=False,
            pyside_dir=PIPX_PYSIDE,
            existing_path="",
            isdir=_isdir_with(ARCH_KF6),
        )
        base.update(over)
        return fix._choose_plugin_dir(**base)

    def test_happy_path_returns_system_plugin_dir(self):
        assert self._call() == ARCH_PLUGINS

    def test_non_linux_returns_none(self):
        assert self._call(platform="win32") is None
        assert self._call(platform="darwin") is None

    def test_disabled_escape_hatch_returns_none(self):
        assert self._call(disabled=True) is None

    def test_non_kde_returns_none(self):
        assert self._call(desktop="GNOME") is None
        assert self._call(desktop="") is None

    def test_kde_match_is_case_insensitive(self):
        # XDG_CURRENT_DESKTOP can be "KDE", "plasma:KDE", etc.
        assert self._call(desktop="plasma:kde") == ARCH_PLUGINS

    def test_system_pyside_returns_none(self):
        # A distro / --system-site-packages PySide6 already sees the plugins.
        assert self._call(pyside_dir="/usr/lib/python3.14/site-packages/PySide6") is None
        assert self._call(pyside_dir="/usr/lib64/python3.14/site-packages/PySide6") is None

    def test_empty_pyside_dir_returns_none(self):
        assert self._call(pyside_dir="") is None

    def test_no_system_plugin_dir_returns_none(self):
        # isdir matches nothing → no candidate exists.
        assert self._call(isdir=lambda p: False) is None

    def test_already_present_returns_none(self):
        assert self._call(existing_path=ARCH_PLUGINS) is None
        # Present among several entries also counts.
        joined = os.pathsep.join(["/some/other", ARCH_PLUGINS])
        assert self._call(existing_path=joined) is None

    def test_falls_through_to_later_candidate(self):
        # First candidate dir absent, a later one present → return the later.
        lib64 = "/usr/lib64/qt6/plugins"
        kf6_64 = os.path.join(lib64, "kf6", "kwindowsystem")
        assert self._call(isdir=_isdir_with(kf6_64)) == lib64


class TestHealQtPluginPath:
    """heal_qt_plugin_path() wiring: env read, env mutation, return value."""

    def test_appends_when_chosen(self, monkeypatch):
        monkeypatch.setattr(fix, "_pyside_location", lambda: PIPX_PYSIDE)
        monkeypatch.setattr(fix.os.path, "isdir", _isdir_with(ARCH_KF6))
        monkeypatch.setattr(fix.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        monkeypatch.delenv("JT_NO_QT_PLUGIN_FIX", raising=False)
        monkeypatch.delenv("QT_PLUGIN_PATH", raising=False)

        added = fix.heal_qt_plugin_path()
        assert added == ARCH_PLUGINS
        assert os.environ["QT_PLUGIN_PATH"] == ARCH_PLUGINS

    def test_appends_after_existing_path(self, monkeypatch):
        monkeypatch.setattr(fix, "_pyside_location", lambda: PIPX_PYSIDE)
        monkeypatch.setattr(fix.os.path, "isdir", _isdir_with(ARCH_KF6))
        monkeypatch.setattr(fix.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        monkeypatch.delenv("JT_NO_QT_PLUGIN_FIX", raising=False)
        monkeypatch.setenv("QT_PLUGIN_PATH", "/pre/existing")

        added = fix.heal_qt_plugin_path()
        assert added == ARCH_PLUGINS
        # Existing entry preserved and kept at HIGHER priority (earlier).
        assert os.environ["QT_PLUGIN_PATH"] == os.pathsep.join(["/pre/existing", ARCH_PLUGINS])

    def test_noop_leaves_env_untouched(self, monkeypatch):
        # Non-KDE → no change, QT_PLUGIN_PATH untouched.
        monkeypatch.setattr(fix, "_pyside_location", lambda: PIPX_PYSIDE)
        monkeypatch.setattr(fix.os.path, "isdir", _isdir_with(ARCH_KF6))
        monkeypatch.setattr(fix.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
        monkeypatch.delenv("QT_PLUGIN_PATH", raising=False)

        added = fix.heal_qt_plugin_path()
        assert added is None
        assert "QT_PLUGIN_PATH" not in os.environ

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setattr(fix, "_pyside_location", lambda: PIPX_PYSIDE)
        monkeypatch.setattr(fix.os.path, "isdir", _isdir_with(ARCH_KF6))
        monkeypatch.setattr(fix.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        monkeypatch.setenv("JT_NO_QT_PLUGIN_FIX", "1")
        monkeypatch.delenv("QT_PLUGIN_PATH", raising=False)

        assert fix.heal_qt_plugin_path() is None
        assert "QT_PLUGIN_PATH" not in os.environ

    def test_never_raises_without_pyside(self, monkeypatch):
        # _pyside_location swallows import errors → returns "" → no-op.
        monkeypatch.setattr(fix, "_pyside_location", lambda: "")
        monkeypatch.setattr(fix.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        assert fix.heal_qt_plugin_path() is None

"""Coverage for the window chrome-mode decision.

``_resolve_chrome_mode`` is the pure platform→chrome decision behind the main
window: which of KDE-KWin-rule / Windows-frameless / GNOME-(non-KDE-Wayland)-
frameless applies, and whether the window is "borderless" (draws its own
chrome). ``is_linux_wayland`` gates the GNOME path and the Settings "native
window border" opt-out. Both are pure (no Qt / no settings), so the whole
platform matrix is exercised directly."""
from __future__ import annotations

import jellytoast.platform_compat as pc
from jellytoast.app import _resolve_chrome_mode


def _mode(**kw):
    base = dict(
        is_windows=False,
        kde_wayland=False,
        linux_wayland=False,
        native_border=False,
        no_win_chrome=False,
        no_linux_chrome=False,
    )
    base.update(kw)
    return _resolve_chrome_mode(**base)


def test_kde_wayland_borderless_via_kwin_rule():
    win, lin, borderless = _mode(kde_wayland=True, linux_wayland=True)
    assert borderless is True
    # KDE strips the decoration with a KWin rule, not the Qt frameless flag
    assert win is False and lin is False


def test_gnome_wayland_is_linux_frameless():
    win, lin, borderless = _mode(linux_wayland=True)  # non-KDE Wayland
    assert lin is True and borderless is True
    assert win is False


def test_windows_is_win_frameless():
    win, lin, borderless = _mode(is_windows=True)
    assert win is True and borderless is True
    assert lin is False


def test_x11_or_non_wayland_linux_is_native():
    assert _mode() == (False, False, False)


def test_native_border_forces_native_everywhere():
    assert _mode(kde_wayland=True, linux_wayland=True, native_border=True) == (False, False, False)
    assert _mode(linux_wayland=True, native_border=True) == (False, False, False)
    assert _mode(is_windows=True, native_border=True) == (False, False, False)


def test_env_hatches_force_native():
    assert _mode(linux_wayland=True, no_linux_chrome=True) == (False, False, False)
    assert _mode(is_windows=True, no_win_chrome=True) == (False, False, False)


def test_is_linux_wayland_predicate(monkeypatch):
    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "will_be_wayland", lambda: True)
    assert pc.is_linux_wayland() is True
    monkeypatch.setattr(pc, "will_be_wayland", lambda: False)
    assert pc.is_linux_wayland() is False
    monkeypatch.setattr(pc, "IS_LINUX", False)
    monkeypatch.setattr(pc, "will_be_wayland", lambda: True)
    assert pc.is_linux_wayland() is False

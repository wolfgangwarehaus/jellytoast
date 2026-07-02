"""Tests for jellytoast.blur — the compositor "blur behind" subsystem.

Covers the public facade (`is_supported` / `apply`), the KWin
backend's `_rounded_region` region-shaping helper, and the no-op
`_unsupported` backend. All paths are best-effort: nothing here
should raise regardless of whether KWindowSystem is installed or
the test runs headless.
"""

from __future__ import annotations

import sys

import pytest

from jellytoast import blur
from jellytoast.blur import _kwin, _unsupported

# ── Public facade ─────────────────────────────────────────────────────


class TestFacade:
    def test_is_supported_returns_bool(self):
        result = blur.is_supported()
        assert isinstance(result, bool)

    def test_is_supported_does_not_raise(self):
        blur.is_supported()  # must not raise

    def test_apply_unshown_widget_returns_false(self, qapp):
        """A widget that was never shown has no windowHandle() — apply
        must return False (no platform window to blur) and not raise."""
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        assert w.windowHandle() is None  # never shown
        assert blur.apply(w, True, 0) is False

    def test_apply_disable_unshown_widget_returns_false(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        assert blur.apply(w, False, 0) is False

    def test_apply_with_corner_radius_does_not_raise(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.resize(300, 200)
        # Unshown -> False, but the rounded-region path must not raise.
        assert blur.apply(w, True, 16) is False

    def test_apply_returns_bool(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        assert isinstance(blur.apply(w, True, 0), bool)


# ── KWin backend: _rounded_region ─────────────────────────────────────


class TestRoundedRegion:
    def test_bounding_rect_matches_widget_size(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.resize(300, 200)
        region = _kwin._rounded_region(w, 24)
        rect = region.boundingRect()
        assert rect.width() == 300
        assert rect.height() == 200

    def test_corner_point_not_contained(self, qapp):
        """The (0,0) top-left pixel sits in the rounded-off corner —
        it must NOT be inside the region."""
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.resize(300, 200)
        region = _kwin._rounded_region(w, 40)
        assert not region.contains(_point(0, 0))

    def test_center_point_contained(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.resize(300, 200)
        region = _kwin._rounded_region(w, 40)
        assert region.contains(_point(150, 100))

    def test_zero_size_widget_returns_empty_region(self, qapp):
        """A not-yet-laid-out widget (0x0) yields an empty QRegion —
        KWindowSystem reads empty as 'blur the whole window'."""
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.resize(0, 0)
        region = _kwin._rounded_region(w, 16)
        assert region.isEmpty()

    def test_does_not_raise(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.resize(120, 80)
        _kwin._rounded_region(w, 12)  # must not raise


# ── KWin backend: is_supported / apply ────────────────────────────────


class TestKWinBackend:
    def test_is_supported_returns_bool(self):
        assert isinstance(_kwin.is_supported(), bool)

    def test_apply_unshown_widget_returns_false(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        assert _kwin.apply(w, True, 0) is False

    def test_apply_never_raises(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w.resize(100, 100)
        _kwin.apply(w, True, 16)  # must not raise


# ── Unsupported (no-op) backend ───────────────────────────────────────


class TestUnsupportedBackend:
    def test_is_supported_is_false(self):
        assert _unsupported.is_supported() is False

    def test_apply_is_false(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        assert _unsupported.apply(w, True, 0) is False

    def test_apply_disable_is_false(self, qapp):
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        assert _unsupported.apply(w, False, 24) is False

    def test_apply_accepts_none_widget(self):
        """The unsupported backend is a pure no-op — it never touches
        the widget, so even None is safe."""
        assert _unsupported.apply(None, True, 0) is False

    def test_probe_is_unsupported(self):
        assert _unsupported.probe() is blur.BlurStatus.UNSUPPORTED


# ── BlurStatus enum ───────────────────────────────────────────────────


class TestBlurStatus:
    def test_has_the_four_members(self):
        assert {s.name for s in blur.BlurStatus} == {
            "ACTIVE",
            "REQUESTED_UNVERIFIABLE",
            "UNSUPPORTED",
            "DISABLED",
        }


# ── status() facade — verified-blur capability ────────────────────────


class TestStatus:
    def test_returns_blurstatus_and_never_raises(self, qapp, monkeypatch):
        monkeypatch.setattr(blur, "_status_cache", None)
        s = blur.status()
        assert isinstance(s, blur.BlurStatus)
        # status() reports a machine capability, never the theme's DISABLED.
        assert s is not blur.BlurStatus.DISABLED

    def test_result_is_cached(self, monkeypatch):
        calls = {"n": 0}

        def fake_probe():
            calls["n"] += 1
            return blur.BlurStatus.ACTIVE

        monkeypatch.setattr(blur, "_FORCE", "")  # ignore any JT_BLUR_FORCE
        monkeypatch.setattr(blur._backend, "probe", fake_probe)
        monkeypatch.setattr(blur, "_status_cache", None)
        assert blur.status() is blur.BlurStatus.ACTIVE
        assert blur.status() is blur.BlurStatus.ACTIVE
        assert calls["n"] == 1  # probed once, then cached

    def test_force_reprobes(self, monkeypatch):
        calls = {"n": 0}

        def fake_probe():
            calls["n"] += 1
            return blur.BlurStatus.ACTIVE

        monkeypatch.setattr(blur, "_FORCE", "")  # ignore any JT_BLUR_FORCE
        monkeypatch.setattr(blur._backend, "probe", fake_probe)
        monkeypatch.setattr(blur, "_status_cache", None)
        blur.status()
        blur.status(force=True)
        assert calls["n"] == 2

    def test_probe_exception_yields_conservative_status(self, monkeypatch):
        def boom():
            raise RuntimeError("probe blew up")

        monkeypatch.setattr(blur, "_FORCE", "")  # ignore any JT_BLUR_FORCE
        monkeypatch.setattr(blur._backend, "probe", boom)
        monkeypatch.setattr(blur, "_status_cache", None)
        # A failed probe must never crash and must stay conservative
        # (near-opaque body) rather than gamble on a see-through window.
        assert blur.status() is blur.BlurStatus.REQUESTED_UNVERIFIABLE

    def test_force_override_pins_status(self, monkeypatch):
        monkeypatch.setattr(blur, "_FORCE", "unsupported")
        monkeypatch.setattr(blur, "_status_cache", None)
        assert blur.status() is blur.BlurStatus.UNSUPPORTED

    def test_force_disabled_is_ignored(self, monkeypatch):
        # status() reports a machine capability and never returns DISABLED,
        # so JT_BLUR_FORCE=disabled falls through to a normal probe.
        monkeypatch.setattr(blur, "_FORCE", "disabled")
        monkeypatch.setattr(
            blur._backend, "probe", lambda: blur.BlurStatus.UNSUPPORTED
        )
        monkeypatch.setattr(blur, "_status_cache", None)
        s = blur.status()
        assert s is blur.BlurStatus.UNSUPPORTED
        assert s is not blur.BlurStatus.DISABLED


# ── KWin backend: probe + capability helpers ──────────────────────────


class TestKWinProbe:
    def test_probe_returns_blurstatus(self, qapp):
        assert isinstance(_kwin.probe(), blur.BlurStatus)

    def test_probe_never_raises(self, qapp):
        _kwin.probe()  # must not raise regardless of env

    def test_resolve_avail_returns_callable_or_none(self):
        fn = _kwin._resolve_avail()
        assert fn is None or callable(fn)

    def test_blur_effect_active_is_tri_state(self, qapp):
        r = _kwin._blur_effect_active()
        assert r is None or isinstance(r, bool)

    def test_blur_disabled_helpers_are_bool_and_never_raise(self, qapp):
        assert isinstance(_kwin._blur_disabled_in_kwinrc(), bool)
        assert isinstance(_kwin._blur_disabled(), bool)

    def test_probe_demotes_to_unsupported_when_blur_disabled(self, monkeypatch):
        """A True capability bit + a positive "blur is off" signal (KWin's
        Blur effect toggled off) must demote to UNSUPPORTED, not ACTIVE —
        this is the see-through guard the QtDBus-only path missed."""
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(_kwin, "_resolve_avail", lambda: (lambda effect: True))
        monkeypatch.setattr(pc, "is_x11", lambda: False)
        monkeypatch.setattr(_kwin, "_blur_disabled", lambda: True)
        assert _kwin.probe() is blur.BlurStatus.UNSUPPORTED

    def test_probe_active_when_capable_and_not_disabled(self, monkeypatch):
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(_kwin, "_resolve_avail", lambda: (lambda effect: True))
        monkeypatch.setattr(pc, "is_x11", lambda: False)
        monkeypatch.setattr(_kwin, "_blur_disabled", lambda: False)
        assert _kwin.probe() is blur.BlurStatus.ACTIVE

    def test_probe_unsupported_when_capability_false(self, monkeypatch):
        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(_kwin, "_resolve_avail", lambda: (lambda effect: False))
        assert _kwin.probe() is blur.BlurStatus.UNSUPPORTED


# ── Windows Mica backend (_dwm) ───────────────────────────────────────


class TestDwmBackend:
    """The DWM Mica calls only run on Windows, but the build-version +
    transparency gating that decides ACTIVE vs the near-opaque fallback is
    unit-testable cross-platform by importing the module and mocking
    its IS_WINDOWS gate + the build/registry reads."""

    def test_degrades_off_windows_and_never_raises(self, monkeypatch):
        from jellytoast.blur import _dwm

        # Emulate a non-Windows host so this also runs on the Windows box.
        monkeypatch.setattr(_dwm, "IS_WINDOWS", False)
        assert _dwm.is_supported() is False
        assert _dwm.probe() is blur.BlurStatus.UNSUPPORTED
        assert _dwm.apply(None, True, 0) is False  # must not touch winId/DWM

    def test_active_on_win11_22h2_with_transparency(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setattr(_dwm, "IS_WINDOWS", True)
        monkeypatch.setattr(_dwm, "_build", lambda: 22631)
        monkeypatch.setattr(_dwm, "_transparency_enabled", lambda: True)
        assert _dwm.is_supported() is True
        assert _dwm.probe() is blur.BlurStatus.ACTIVE

    def test_active_on_win11_21h2_legacy_build(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setattr(_dwm, "IS_WINDOWS", True)
        monkeypatch.setattr(_dwm, "_build", lambda: 22000)
        monkeypatch.setattr(_dwm, "_transparency_enabled", lambda: True)
        assert _dwm.probe() is blur.BlurStatus.ACTIVE

    def test_unsupported_on_windows10(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setattr(_dwm, "IS_WINDOWS", True)
        monkeypatch.setattr(_dwm, "_build", lambda: 19045)  # Win10 22H2
        monkeypatch.setattr(_dwm, "_transparency_enabled", lambda: True)
        assert _dwm.is_supported() is False
        assert _dwm.probe() is blur.BlurStatus.UNSUPPORTED

    def test_unsupported_when_transparency_disabled(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setattr(_dwm, "IS_WINDOWS", True)
        monkeypatch.setattr(_dwm, "_build", lambda: 22631)
        monkeypatch.setattr(_dwm, "_transparency_enabled", lambda: False)
        # Mica won't render → near-opaque body, never see-through.
        assert _dwm.probe() is blur.BlurStatus.UNSUPPORTED

    def test_apply_never_raises_when_dwm_unreachable(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setattr(_dwm, "IS_WINDOWS", True)
        monkeypatch.setattr(_dwm, "_build", lambda: 22631)

        class _FakeWidget:
            def winId(self):
                return 12345

        # ctypes.windll is absent on the test host → apply must catch and
        # return False, never raise.
        assert _dwm.apply(_FakeWidget(), True, 0) is False

    def test_apply_false_below_min_build(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setattr(_dwm, "IS_WINDOWS", True)
        monkeypatch.setattr(_dwm, "_build", lambda: 19045)
        assert _dwm.apply(object(), True, 0) is False

    def test_acrylic_branch_propagates_apply_acrylic_result(self, monkeypatch):
        """The default (Acrylic) path returns the accent-policy result, NOT an
        unconditional True — symmetric with the Mica branch's `_set_attr == 0`.
        No caller reads it, but the 'issued' return must be honest on both
        paths (this is the #138 backlog item)."""
        from jellytoast.blur import _dwm

        monkeypatch.setattr(_dwm, "IS_WINDOWS", True)
        monkeypatch.setattr(_dwm, "_build", lambda: 22631)
        monkeypatch.setattr(_dwm, "_set_attr", lambda *a: 0)  # neutralise DWM calls
        monkeypatch.delenv("JT_NO_WIN_BLUR", raising=False)  # take the Acrylic path

        class _FakeWidget:
            def windowHandle(self):
                return object()  # "shown" — apply()'s not-yet-shown guard passes

            def winId(self):
                return 12345

        seen = {}

        def fake_acrylic(hwnd, dark, enabled=True, elevated=False):
            seen["hwnd"] = hwnd
            return False  # the accent call reports "not issued"

        monkeypatch.setattr(_dwm, "apply_acrylic", fake_acrylic)
        assert _dwm.apply(_FakeWidget(), True, 0) is False  # propagated, not True
        assert seen["hwnd"] == 12345
        # …and True when the accent call is issued.
        monkeypatch.setattr(_dwm, "apply_acrylic", lambda *a, **k: True)
        assert _dwm.apply(_FakeWidget(), True, 0) is True

    def test_apply_acrylic_propagates_set_wca_result(self, monkeypatch):
        from jellytoast.blur import _dwm

        monkeypatch.setattr(_dwm, "_set_wca", lambda *a: True)
        assert _dwm.apply_acrylic(123, dark=True, enabled=True) is True
        monkeypatch.setattr(_dwm, "_set_wca", lambda *a: False)
        assert _dwm.apply_acrylic(123, dark=True, enabled=False) is False

    def test_set_wca_off_windows_returns_false_bool(self):
        """No windll on the (Linux) test host → _set_wca catches and returns a
        bool False, not None — so apply_acrylic always propagates a real bool."""
        from jellytoast.blur import _dwm

        result = _dwm._set_wca(0, _dwm._WCA_ACCENT_POLICY, _dwm._ACCENT_POLICY())
        assert result is False


# ── reason() — human-readable status explanation ──────────────────────


class TestReason:
    def test_facade_is_nonempty_str_and_never_raises(self, qapp):
        r = blur.reason()
        assert isinstance(r, str) and r

    def test_active_reason(self):
        assert _kwin.reason(blur.BlurStatus.ACTIVE) == "KWin blur active"

    def test_non_kde_desktop_blames_the_desktop(self, monkeypatch):
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(pc, "is_kde_desktop", lambda: False)
        monkeypatch.setattr(pc, "desktop_name", lambda: "GNOME")
        r = _kwin.reason(blur.BlurStatus.UNSUPPORTED)
        assert "GNOME" in r and "no app-controllable" in r

    def test_x11_reason(self, monkeypatch):
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(pc, "is_kde_desktop", lambda: True)
        monkeypatch.setattr(pc, "desktop_name", lambda: "KDE")
        monkeypatch.setattr(pc, "is_x11", lambda: True)
        assert "X11" in _kwin.reason(blur.BlurStatus.REQUESTED_UNVERIFIABLE)

    def test_blur_effect_off_reason(self, monkeypatch):
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(pc, "is_kde_desktop", lambda: True)
        monkeypatch.setattr(pc, "desktop_name", lambda: "KDE")
        monkeypatch.setattr(pc, "is_x11", lambda: False)
        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(_kwin, "_blur_disabled", lambda: True)
        assert "Blur effect is off" in _kwin.reason(blur.BlurStatus.UNSUPPORTED)

    def test_missing_kwindowsystem_reason(self, monkeypatch):
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(pc, "is_kde_desktop", lambda: True)
        monkeypatch.setattr(pc, "desktop_name", lambda: "KDE")
        monkeypatch.setattr(pc, "is_x11", lambda: False)
        monkeypatch.setattr(_kwin, "_resolve", lambda: None)
        assert "kwindowsystem" in _kwin.reason(blur.BlurStatus.UNSUPPORTED)

    def test_unsupported_backend_reason_is_str(self):
        assert isinstance(_unsupported.reason(blur.BlurStatus.UNSUPPORTED), str)


# ── macOS backend (_macos) — sibling-below vibrancy ───────────────────


class TestMacosBackend:
    """The macOS vibrancy backend is live on real macOS, but off-platform
    (this CI runs on Linux) AppKit is absent, so it must safely degrade:
    is_supported() False → probe() UNSUPPORTED → near-opaque body, never
    see-through, and apply() never raises."""

    @pytest.mark.skipif(
        sys.platform == "darwin",
        reason="on real macOS AppKit is present, so the backend IS supported",
    )
    def test_degrades_to_unsupported_off_platform_and_never_raises(self):
        from jellytoast.blur import _macos

        assert _macos.is_supported() is False
        assert _macos.apply(None, True, 0) is False
        assert _macos.probe() is blur.BlurStatus.UNSUPPORTED
        assert isinstance(_macos.reason(blur.BlurStatus.UNSUPPORTED), str)


# ── helper ────────────────────────────────────────────────────────────


def _point(x, y):
    from PySide6.QtCore import QPoint

    return QPoint(x, y)

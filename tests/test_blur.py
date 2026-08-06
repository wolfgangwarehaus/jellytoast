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
        monkeypatch.delenv("FLATPAK_ID", raising=False)
        assert _kwin.probe() is blur.BlurStatus.ACTIVE

    def _flatpak_kde_probe(self, monkeypatch, *, effect, kde=True):
        """probe() under a simulated flatpak-on-KDE-Wayland with a True
        capability bit; `effect` is what the D-Bus cross-check returns."""
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(_kwin, "_resolve_avail", lambda: (lambda e: True))
        monkeypatch.setattr(pc, "is_x11", lambda: False)
        monkeypatch.setattr(pc, "is_kde_desktop", lambda: kde)
        monkeypatch.setattr(_kwin, "_blur_disabled", lambda: False)
        monkeypatch.setattr(_kwin, "_blur_effect_active", lambda: effect)
        monkeypatch.setenv("FLATPAK_ID", "io.github.wolfgangwarehaus.jellytoast")
        return _kwin.probe()

    def test_probe_flatpak_kde_inconclusive_effect_is_unverifiable(self, monkeypatch):
        """The 0.2.0 Steam Deck bug: inside a sandbox that can't reach
        org.kde.KWin, a host with the Blur effect OFF looks identical to one
        with it on — the capability bit stays True either way. An
        inconclusive effect check in a flatpak on KDE must NOT earn ACTIVE
        (full-transparency glass over an unblurred desktop); it demotes to
        the near-opaque frosted fallback."""
        st = self._flatpak_kde_probe(monkeypatch, effect=None)
        assert st is blur.BlurStatus.REQUESTED_UNVERIFIABLE

    def test_probe_flatpak_kde_verified_effect_stays_active(self, monkeypatch):
        # With the --talk-name=org.kde.KWin grant the cross-check works and
        # a genuinely-on Blur effect keeps real glass.
        st = self._flatpak_kde_probe(monkeypatch, effect=True)
        assert st is blur.BlurStatus.ACTIVE

    def test_probe_flatpak_nonkde_inconclusive_stays_active(self, monkeypatch):
        # niri/COSMIC honestly advertise the blur protocol; the KDE-only
        # doubt must not demote them.
        st = self._flatpak_kde_probe(monkeypatch, effect=None, kde=False)
        assert st is blur.BlurStatus.ACTIVE

    def test_probe_unsupported_when_capability_false(self, monkeypatch):
        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(_kwin, "_resolve_avail", lambda: (lambda effect: False))
        assert _kwin.probe() is blur.BlurStatus.UNSUPPORTED


# ── The request never reaches the compositor (#229) ───────────────────


class TestBlurDelivery:
    """A True capability bit says the COMPOSITOR can blur. It says nothing
    about whether our request gets there. On a Qt/KWindowSystem version skew
    enableBlurBehind silently drops it (Qt refuses KF6 the Wayland native
    interface) while isEffectAvailable() still answers True — which is how
    0.2.0 came to paint clear glass over an unblurred desktop."""

    def _capable_probe(self, monkeypatch, *, delivered):
        """probe() on a KDE Wayland box where every compositor-side signal is
        green; `delivered` is what the delivery self-test reports."""
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(_kwin, "_resolve_avail", lambda: (lambda e: True))
        monkeypatch.setattr(pc, "is_x11", lambda: False)
        monkeypatch.setattr(_kwin, "_blur_disabled", lambda: False)
        monkeypatch.setattr(
            _kwin, "_blur_request_reaches_compositor", lambda: delivered
        )
        monkeypatch.delenv("FLATPAK_ID", raising=False)
        return _kwin.probe()

    def test_dropped_request_demotes_to_unverifiable(self, monkeypatch):
        # The #229 regression: capability True, Blur effect on, and yet
        # nothing we send lands. Full glass here is see-through-broken.
        st = self._capable_probe(monkeypatch, delivered=False)
        assert st is blur.BlurStatus.REQUESTED_UNVERIFIABLE

    def test_delivered_request_earns_active(self, monkeypatch):
        st = self._capable_probe(monkeypatch, delivered=True)
        assert st is blur.BlurStatus.ACTIVE

    def test_inconclusive_test_does_not_demote(self, monkeypatch):
        # None = "couldn't run the test" (no QGuiApplication, no symbol).
        # Absence of a verdict is not evidence of failure — a box that
        # blurred fine before must keep its glass.
        st = self._capable_probe(monkeypatch, delivered=None)
        assert st is blur.BlurStatus.ACTIVE

    def test_selftest_is_tri_state_and_never_raises(self, qapp, monkeypatch):
        monkeypatch.setattr(_kwin, "_delivery_tested", False)
        monkeypatch.setattr(_kwin, "_delivery_ok", None)
        r = _kwin._blur_request_reaches_compositor()
        assert r is None or isinstance(r, bool)

    def test_selftest_is_cached(self, monkeypatch):
        """One throwaway window per process, not one per status() call."""
        calls = []
        monkeypatch.setattr(_kwin, "_delivery_tested", False)
        monkeypatch.setattr(_kwin, "_delivery_ok", None)

        def counted():
            calls.append(1)
            raise RuntimeError("boom")  # forces the inconclusive path

        monkeypatch.setattr(_kwin, "_resolve", counted)
        assert _kwin._blur_request_reaches_compositor() is None
        assert _kwin._blur_request_reaches_compositor() is None
        assert len(calls) == 1

    def test_selftest_restores_the_previous_message_handler(self, qapp, monkeypatch):
        """The handler is a PROCESS-WIDE hook — leaving ours installed would
        swallow every later Qt warning in the app."""
        from PySide6.QtCore import qInstallMessageHandler

        sentinel_seen = []

        def sentinel(mode, ctx, msg):
            sentinel_seen.append(msg)

        prev = qInstallMessageHandler(sentinel)
        try:
            monkeypatch.setattr(_kwin, "_delivery_tested", False)
            monkeypatch.setattr(_kwin, "_delivery_ok", None)
            _kwin._blur_request_reaches_compositor()
            # Ours must be gone; the sentinel must be back in charge.
            from PySide6.QtCore import qCritical

            qCritical("jellytoast-selftest-canary")
            assert any("canary" in m for m in sentinel_seen)
        finally:
            qInstallMessageHandler(prev)

    def test_reason_explains_a_dropped_request(self, monkeypatch):
        import jellytoast.platform_compat as pc

        monkeypatch.setattr(pc, "is_x11", lambda: False)
        monkeypatch.setattr(pc, "is_kde_desktop", lambda: True)
        monkeypatch.setattr(pc, "desktop_name", lambda: "KDE")
        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(_kwin, "_blur_disabled", lambda: False)
        monkeypatch.setattr(
            _kwin, "_blur_request_reaches_compositor", lambda: False
        )
        msg = _kwin.reason(blur.BlurStatus.REQUESTED_UNVERIFIABLE)
        assert "skew" in msg.lower()
        assert "near-opaque" in msg


# ── KWindowSystem's platform plugin: the venv-PySide6 shim ────────────


class TestPlatformPluginShim:
    """A pip/pipx-installed PySide6 bundles its own Qt, whose library paths
    don't include the distro's /usr/lib/qt6/plugins — so KWindowSystem's
    platform-integration plugin (the piece that actually speaks the Wayland
    blur protocols) is invisible, isEffectAvailable() reads False on a
    blur-capable KWin, and the app paints grainy faux frost. The shim exposes
    ONLY the kwindowsystem plugin family, never the whole system plugin tree
    (which would let a second Qt build's platform/image plugins shadow
    PySide6's own). Fix ported from the dough app base (dough d20562c).

    Everything here fakes the filesystem + library paths — nothing depends on
    the host actually having KF6 installed."""

    @staticmethod
    def _hide_real_plugin_paths():
        """Drop any library path where the plugin is ALREADY discoverable —
        the host may have KF6 installed, and an earlier test in this session
        may have installed the real shim. Returns them for restoration."""
        from pathlib import Path

        from PySide6.QtCore import QCoreApplication

        hidden = [
            p
            for p in QCoreApplication.libraryPaths()
            if (Path(p) / _kwin._KF_PLUGIN_SUBDIR).is_dir()
        ]
        for p in hidden:
            QCoreApplication.removeLibraryPath(p)
        return hidden

    def test_noop_when_already_discoverable(self, qapp, tmp_path, monkeypatch):
        """Distro PySide6 (shared Qt prefix) — and the flatpak, whose KF6
        plugin rides the runtime's Qt plugin path — already see the plugin,
        so no shim is added."""
        from PySide6.QtCore import QCoreApplication

        monkeypatch.setattr(_kwin, "_plugin_path_ensured", False)
        visible = tmp_path / "visible"
        (visible / _kwin._KF_PLUGIN_SUBDIR).mkdir(parents=True)
        QCoreApplication.addLibraryPath(str(visible))
        try:
            before = list(QCoreApplication.libraryPaths())
            _kwin._ensure_platform_plugin()
            assert list(QCoreApplication.libraryPaths()) == before
        finally:
            QCoreApplication.removeLibraryPath(str(visible))

    def test_exposes_only_the_kwindowsystem_plugins(self, qapp, tmp_path, monkeypatch):
        """A system plugin dir that Qt can't see earns a shim library path
        whose tree holds JUST the kf6 kwindowsystem symlink."""
        from pathlib import Path

        from PySide6.QtCore import QCoreApplication

        monkeypatch.setattr(_kwin, "_plugin_path_ensured", False)
        fake_root = tmp_path / "plugins"
        src = fake_root / _kwin._KF_PLUGIN_SUBDIR
        src.mkdir(parents=True)
        (src / "KF6WindowSystemKWaylandPlugin.so").write_bytes(b"")
        # Also plant a platform plugin dir the shim must NOT expose — the
        # whole-tree mistake would let it shadow PySide6's own Qt plugins.
        (fake_root / "platforms").mkdir()
        monkeypatch.setattr(_kwin, "_SYSTEM_PLUGIN_ROOTS", (str(fake_root),))

        hidden = self._hide_real_plugin_paths()
        before = list(QCoreApplication.libraryPaths())
        _kwin._ensure_platform_plugin()
        added = [p for p in QCoreApplication.libraryPaths() if p not in before]
        try:
            assert len(added) == 1
            shim = Path(added[0])
            link = shim / _kwin._KF_PLUGIN_SUBDIR
            assert link.is_symlink() and link.resolve() == src.resolve()
            # nothing else rides along — no platforms/, no imageformats/
            assert [p.name for p in shim.iterdir()] == ["kf6"]
        finally:
            for p in added:
                QCoreApplication.removeLibraryPath(p)
            for p in hidden:
                QCoreApplication.addLibraryPath(p)

    def test_noop_when_no_system_plugin_anywhere(self, qapp, monkeypatch):
        """No distro plugin on the box (CI, or a non-KDE Linux) → silent
        no-op; blur just stays a no-op too."""
        from PySide6.QtCore import QCoreApplication

        monkeypatch.setattr(_kwin, "_plugin_path_ensured", False)
        monkeypatch.setattr(_kwin, "_SYSTEM_PLUGIN_ROOTS", ())
        hidden = self._hide_real_plugin_paths()
        before = list(QCoreApplication.libraryPaths())
        try:
            _kwin._ensure_platform_plugin()
            assert list(QCoreApplication.libraryPaths()) == before
        finally:
            for p in hidden:
                QCoreApplication.addLibraryPath(p)

    def test_is_cached_after_the_first_call(self, qapp, tmp_path, monkeypatch):
        """One shim per process — probe() and every apply() call it."""
        from PySide6.QtCore import QCoreApplication

        monkeypatch.setattr(_kwin, "_plugin_path_ensured", False)
        fake_root = tmp_path / "plugins"
        (fake_root / _kwin._KF_PLUGIN_SUBDIR).mkdir(parents=True)
        monkeypatch.setattr(_kwin, "_SYSTEM_PLUGIN_ROOTS", (str(fake_root),))
        hidden = self._hide_real_plugin_paths()
        before = list(QCoreApplication.libraryPaths())
        try:
            _kwin._ensure_platform_plugin()
            after_first = list(QCoreApplication.libraryPaths())
            assert len(after_first) == len(before) + 1
            _kwin._ensure_platform_plugin()
            _kwin._ensure_platform_plugin()
            assert list(QCoreApplication.libraryPaths()) == after_first
        finally:
            for p in list(QCoreApplication.libraryPaths()):
                if p not in before:
                    QCoreApplication.removeLibraryPath(p)
            for p in hidden:
                QCoreApplication.addLibraryPath(p)

    def test_never_raises_when_the_shim_cannot_be_built(self, qapp, monkeypatch):
        """Read-only /tmp, a symlink refusal, a Qt that won't answer — all
        must resolve to a silent no-op. Blur is progressive enhancement."""
        import tempfile

        monkeypatch.setattr(_kwin, "_plugin_path_ensured", False)
        monkeypatch.setattr(_kwin, "_SYSTEM_PLUGIN_ROOTS", ("/",))  # "/kf6/…" absent
        monkeypatch.setattr(
            tempfile, "mkdtemp", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        _kwin._ensure_platform_plugin()  # must not raise

        monkeypatch.setattr(_kwin, "_plugin_path_ensured", False)

        def _boom():
            raise RuntimeError("no Qt here")

        from PySide6.QtCore import QCoreApplication

        monkeypatch.setattr(QCoreApplication, "libraryPaths", staticmethod(_boom))
        _kwin._ensure_platform_plugin()  # must not raise

    def test_probe_ensures_the_plugin_before_the_capability_gate(self, monkeypatch):
        """Ordering is the whole point: isEffectAvailable() answers False on a
        blur-capable KWin while the plugin is still invisible."""
        order = []
        monkeypatch.setattr(_kwin, "_resolve", lambda: object())
        monkeypatch.setattr(
            _kwin, "_ensure_platform_plugin", lambda: order.append("shim")
        )

        def _avail():
            order.append("avail")
            return lambda effect: False

        monkeypatch.setattr(_kwin, "_resolve_avail", _avail)
        _kwin.probe()
        assert order == ["shim", "avail"]


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

"""Tests for modules.blur — the compositor "blur behind" subsystem.

Covers the public facade (`is_supported` / `apply`), the KWin
backend's `_rounded_region` region-shaping helper, and the no-op
`_unsupported` backend. All paths are best-effort: nothing here
should raise regardless of whether KWindowSystem is installed or
the test runs headless.
"""

from __future__ import annotations

from modules import blur
from modules.blur import _kwin, _unsupported


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


# ── helper ────────────────────────────────────────────────────────────


def _point(x, y):
    from PySide6.QtCore import QPoint

    return QPoint(x, y)

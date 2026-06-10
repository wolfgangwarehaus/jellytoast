"""Theme-swap performance helpers: the icon rasterization cache
(`icons._svg_pix_cached`) and the `ui_helpers.theme_swap_guard` context
manager (busy cursor + batched repaints around a live theme/accent swap)."""

from __future__ import annotations


class TestIconCache:
    def test_repeat_render_hits_cache(self, qapp):
        from jellytoast import icons as ic

        ic._svg_pix_cached.cache_clear()
        ic._svg_pix("cast", "#ffffff", 20)
        before = ic._svg_pix_cached.cache_info()
        ic._svg_pix("cast", "#ffffff", 20)
        ic._svg_pix("cast", "#ffffff", 20)
        after = ic._svg_pix_cached.cache_info()
        # Two more reads, zero new renders.
        assert after.hits == before.hits + 2
        assert after.misses == before.misses

    def test_distinct_color_is_a_distinct_entry(self, qapp):
        from jellytoast import icons as ic

        ic._svg_pix_cached.cache_clear()
        ic._svg_pix("cast", "#ffffff", 20)
        ic._svg_pix("cast", "#000000", 20)
        # Two colours = two distinct renders (color is in the key).
        assert ic._svg_pix_cached.cache_info().misses == 2

    def test_returns_dpr_tagged_pixmap(self, qapp):
        from jellytoast import icons as ic

        pix = ic._svg_pix("cast", "#ffffff", 20)
        assert not pix.isNull()
        assert pix.devicePixelRatio() >= 1.0

    def test_unknown_icon_is_transparent_not_a_crash(self, qapp):
        from jellytoast import icons as ic

        pix = ic._svg_pix("definitely-not-an-icon", "#ffffff", 16)
        assert not pix.isNull()  # transparent placeholder


class TestThemeSwapGuard:
    def test_busy_cursor_set_then_restored(self, qapp):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from jellytoast.ui_helpers import theme_swap_guard

        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()  # clean any leaked override

        with theme_swap_guard():
            cur = QApplication.overrideCursor()
            assert cur is not None
            assert cur.shape() == Qt.CursorShape.BusyCursor
        assert QApplication.overrideCursor() is None

    def test_suspends_and_restores_visible_top_level_updates(self, qapp):
        from PySide6.QtWidgets import QWidget

        from jellytoast.ui_helpers import theme_swap_guard

        w = QWidget()
        w.show()
        qapp.processEvents()
        assert w.updatesEnabled() is True
        with theme_swap_guard():
            assert w.updatesEnabled() is False  # batched during the swap
        assert w.updatesEnabled() is True  # restored after
        w.hide()

    def test_restores_even_if_body_raises(self, qapp):
        from PySide6.QtWidgets import QApplication

        from jellytoast.ui_helpers import theme_swap_guard

        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        try:
            with theme_swap_guard():
                raise ValueError("boom")
        except ValueError:
            pass
        assert QApplication.overrideCursor() is None  # cursor restored on error

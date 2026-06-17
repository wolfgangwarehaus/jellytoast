"""Coverage for the faux-frost no-real-blur fallback backdrop.

``FauxFrost`` generates a cached dark frosted texture sized to a surface;
``ui_helpers.frosted_fallback_active`` decides when a frosted surface should use
it (frosted theme + no verified compositor blur). Both are exercised headlessly
via the session ``qapp`` fixture (QPixmap construction needs a QApplication)."""
from __future__ import annotations

import types

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QColor, QPainter, QPixmap

from jellytoast.blur._faux_frost import FauxFrost

BASE = QColor(14, 15, 18, 235)


def test_texture_has_target_size_and_caches(qapp):
    ff = FauxFrost()
    pm = ff._ensure(QSize(800, 600), BASE)
    assert pm is not None
    assert pm.size() == QSize(800, 600)
    # identical args → same cached pixmap, no rebuild
    assert ff._ensure(QSize(800, 600), BASE) is pm


def test_cache_rebuilds_on_size_or_base_change(qapp):
    ff = FauxFrost()
    pm = ff._ensure(QSize(400, 300), BASE)
    assert ff._ensure(QSize(401, 300), BASE) is not pm  # size changed
    pm2 = ff._ensure(QSize(400, 300), BASE)
    assert ff._ensure(QSize(400, 300), QColor(40, 20, 30, 200)) is not pm2  # base changed


def test_zero_size_returns_none(qapp):
    ff = FauxFrost()
    assert ff._ensure(QSize(0, 100), BASE) is None
    assert ff._ensure(QSize(100, 0), BASE) is None


def test_grain_tile_cached_and_sized(qapp):
    tile = FauxFrost._grain_tile()
    assert tile.width() == FauxFrost._GRAIN_TILE
    assert tile.height() == FauxFrost._GRAIN_TILE
    assert FauxFrost._grain_tile() is tile  # class-level cache


def test_paint_draws_and_reports_true(qapp):
    ff = FauxFrost()
    canvas = QPixmap(200, 150)
    canvas.fill(QColor(0, 0, 0))
    p = QPainter(canvas)
    try:
        assert ff.paint(p, QRect(0, 0, 200, 150), BASE, radius=8) is True
        assert ff.paint(p, QRect(0, 0, 200, 150), BASE, radius=0) is True
    finally:
        p.end()


def _theme(blur: bool):
    return types.SimpleNamespace(blur=blur)


def test_frosted_fallback_active_gating(qapp, monkeypatch):
    import jellytoast.theme as thememod
    from jellytoast import blur as blurmod
    from jellytoast import ui_helpers

    # frosted theme, real blur NOT active → fallback ON
    monkeypatch.setattr(thememod, "get_active_theme", lambda: _theme(True))
    monkeypatch.setattr(blurmod, "status", lambda **k: blurmod.BlurStatus.UNSUPPORTED)
    assert ui_helpers.frosted_fallback_active() is True

    # frosted theme, real blur active → fallback OFF (desktop shows through)
    monkeypatch.setattr(blurmod, "status", lambda **k: blurmod.BlurStatus.ACTIVE)
    assert ui_helpers.frosted_fallback_active() is False

    # non-frosted (Solid/Transparent) theme → fallback OFF regardless of blur
    monkeypatch.setattr(thememod, "get_active_theme", lambda: _theme(False))
    assert ui_helpers.frosted_fallback_active() is False

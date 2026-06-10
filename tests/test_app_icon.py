"""Tests for the bundled app-icon loader (jellytoast.ui_helpers).

The brand mark must ship inside the wheel: it's loaded via
``importlib.resources`` from ``jellytoast.assets`` so an installed build
finds it (the old ``Path(__file__).parent.parent / "packaging"`` path is
wheel-excluded → blank icon). The loader also guards against a
missing/invalid SVG with a drawn placeholder.

`qapp` (conftest.py) provides the QApplication QPixmap construction
needs.
"""

import importlib.resources as ir

from PySide6.QtGui import QPixmap

import jellytoast.ui_helpers as ui_helpers


def test_icon_resource_resolves_in_package():
    # The wheel layout resolves the SVG through importlib.resources,
    # not a filesystem-relative packaging/ path.
    res = ir.files("jellytoast.assets").joinpath("jellytoast.svg")
    assert res.is_file()
    data = res.read_bytes()
    assert data, "bundled SVG must be non-empty"
    assert b"<svg" in data


def test_loader_reads_svg_bytes():
    assert ui_helpers._load_app_icon_svg_bytes()


def test_make_app_icon_returns_non_null_pixmap(qapp):
    # Reset the cached renderer so we exercise the real load path.
    ui_helpers._APP_ICON_RENDERER = None
    ui_helpers._APP_ICON_CACHE.clear()
    pix = ui_helpers.make_app_icon(64)
    assert isinstance(pix, QPixmap)
    assert not pix.isNull()
    assert pix.size().width() == 64
    assert pix.size().height() == 64


def test_placeholder_used_when_svg_missing(qapp, monkeypatch):
    # If the SVG can't be loaded the loader must still return a
    # non-null pixmap (drawn placeholder), never a blank icon.
    monkeypatch.setattr(ui_helpers, "_load_app_icon_svg_bytes", lambda: b"")
    ui_helpers._APP_ICON_RENDERER = None
    ui_helpers._APP_ICON_CACHE.clear()
    pix = ui_helpers.make_app_icon(64)
    assert isinstance(pix, QPixmap)
    assert not pix.isNull()
    # Reset so later tests get the real renderer.
    ui_helpers._APP_ICON_RENDERER = None
    ui_helpers._APP_ICON_CACHE.clear()

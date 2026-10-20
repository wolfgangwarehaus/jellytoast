"""A cover fetch that lands after the grid reloaded must not paint the old
album's art onto whatever album now sits at that row.

First sign-in reloads the album grid several times while covers are still
coming in (``_route_home`` + ``_retry_empty_native_views`` + the library
selection refresh). Covers were landed by row alone, so for a while art
showed up doubled over the wrong albums until loading settled.
"""

from __future__ import annotations

from jellytoast import disk_cache as _disk_cache
from jellytoast import library_grid as lg


def _grid_with_captured_loads(monkeypatch):
    g = lg.LibraryGrid("album")
    monkeypatch.setattr(g.api, "get_image_url", lambda *a, **k: "http://x/cover")
    captured = []
    monkeypatch.setattr(
        lg,
        "load_image_async",
        lambda key, url, w, h, on_pix, on_error=None, **kw: captured.append((on_pix, on_error)),
    )
    return g, captured


def test_stale_cover_dropped_after_reload(qapp, monkeypatch):
    from PySide6.QtGui import QPixmap

    g, captured = _grid_with_captured_loads(monkeypatch)
    g._model.set_items([{"Id": "old"}])
    g._fire_cover_load(0)
    # The grid reloads with a different album at row 0 before the fetch lands.
    g._model.set_items([{"Id": "new"}])
    pix = QPixmap(4, 4)
    pix.fill()
    captured[0][0](pix)
    assert not g._model.has_cover(0)


def test_stale_cover_error_does_not_spend_retries(qapp, monkeypatch):
    g, captured = _grid_with_captured_loads(monkeypatch)
    g._model.set_items([{"Id": "old"}])
    g._fire_cover_load(0)
    g._model.set_items([{"Id": "new"}])
    captured[0][1]()
    assert 0 not in g._cover_retries


def test_current_cover_still_lands(qapp, monkeypatch):
    from PySide6.QtGui import QPixmap

    g, captured = _grid_with_captured_loads(monkeypatch)
    g._model.set_items([{"Id": "a1"}])
    g._fire_cover_load(0)
    pix = QPixmap(4, 4)
    pix.fill()
    captured[0][0](pix)
    assert g._model.has_cover(0)


def test_cold_load_shows_spinner_until_items_land(qapp, monkeypatch):
    monkeypatch.setattr(_disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(_disk_cache, "save", lambda *a, **k: None)
    g = lg.LibraryGrid("album")
    pending = []
    monkeypatch.setattr(
        "jellytoast.library_paginator.run_async",
        lambda fn, *a, on_result=None, on_error=None, **k: pending.append(on_result),
    )
    monkeypatch.setattr(g, "_load_visible_covers", lambda *a, **k: None)
    monkeypatch.setattr(g, "_fire_cover_load", lambda *a, **k: None)

    g.load_items("lib", "")
    assert g._content_stack.currentWidget() is g._loading_state

    pending[0]({"Items": [{"Id": "a1", "Name": "A"}]})
    assert g._content_stack.currentIndex() == 0


def test_phone_rail_is_quiet_until_touched_and_centres_columns(qapp, monkeypatch):
    from jellytoast import platform_compat as pc

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    g = lg.LibraryGrid("album")
    rail = g._alphabet
    assert rail._quiet and not rail._revealed and rail._amt == 0.0
    # Buttons stay invisible hit targets; the rail paints letters / line and
    # cross-fades between them.
    assert "transparent;" in rail._buttons["A"].styleSheet().split("color:")[1]
    rail._set_revealed(True)
    assert rail._anim.endValue() == 1.0
    rail._anim.setCurrentTime(rail._anim.duration())
    assert rail._amt == 1.0
    rail._set_revealed(False)
    assert rail._anim.endValue() == 0.0
    assert rail._anim.duration() > rail.REVEAL_IN_MS  # settles out slower

    # Rail shown → it floats over the view's right margin (not a layout
    # column), equal side margins centre the columns, and the view drops its
    # own scroll bar (it would sit on the rail's track).
    assert rail.parentWidget() is g._body_widget
    rail.setVisible(True)
    left, right = g._view._hmargins()
    assert left == right and right >= rail.width() * 0.75
    assert g._tile_delegate._fine_steps
    assert g._view.verticalScrollBarPolicy() == lg.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    rail.setVisible(False)
    assert g._view._hmargins() == (g._view._BASE_HMARGIN, g._view._RIGHT_HMARGIN)


def test_desktop_rail_unchanged(qapp, monkeypatch):
    from jellytoast import platform_compat as pc

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
    g = lg.LibraryGrid("album")
    assert not g._alphabet._quiet and g._alphabet._revealed
    g._alphabet.setVisible(True)
    assert g._view._hmargins() == (g._view._BASE_HMARGIN, g._view._RIGHT_HMARGIN)


def test_quiet_rail_pill_tracks_scroll_position(qapp, monkeypatch):
    from jellytoast import platform_compat as pc

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    rail = lg._AlphabetIndex()
    rail.set_scroll(50, 0, 100, 100)
    assert rail._scroll_frac == 0.5 and rail._page_frac == 0.5
    rail.set_scroll(0, 0, 0, 100)  # nothing to scroll → no pill
    assert rail._page_frac == 1.0

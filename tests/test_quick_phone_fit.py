"""Phone UI fit, QA 2026-10-27 (august's phone + the mobile QA pass):
whole tiles on the Home rails, Radio's actions fitting 360 px, icons at
their own size, empty artist pages without dead buttons, landscape
Now Playing side by side and grids that fit the height."""

from __future__ import annotations


def _settle(quick, w, h):
    quick.window.resize(w, h)
    quick.wait_idle()


def _section(quick, key):
    quick.eval(f"win.showSection('{key}')")
    quick.wait_idle()


def test_home_rails_fit_whole_tiles(quick):
    for w, cols in ((360, 2), (412, 3)):
        _settle(quick, w, 800)
        _section(quick, "home")
        rail = quick.wait_for("rail_latest")
        assert rail.property("cols") == cols
        tile = rail.property("tileW")
        # n tiles + gaps + side margins == the full width: no cut-off tile.
        span = cols * tile + (cols - 1) * rail.property("spacing")
        assert abs(span + rail.property("leftMargin") + rail.property("rightMargin") - rail.width()) < 1
        assert rail.property("spacing") == rail.property("rightMargin")


def test_radio_actions_fit_a_360px_phone(quick):
    _settle(quick, 360, 720)
    _section(quick, "radio")
    a, b = quick.wait_for("browsePopular"), quick.wait_for("addStation")
    assert a.property("compact") and b.property("compact")
    left = a.mapToItem(None, 0, 0).x()
    right = b.mapToItem(None, b.width(), 0).x()
    assert left >= 0 and right <= 360
    _settle(quick, 600, 900)
    assert not quick.get("browsePopular").property("compact")  # room again


def test_icons_report_their_own_size_to_layouts(quick):
    # A bare Image reported size × screen scale; at 2.5× the cast sheet's
    # icons laid out at ~60 px. Icon is an Item now: implicit == size.
    icons = [i for i in quick.items() if i.metaObject().className().startswith("Icon_")]
    assert icons
    for i in icons:
        assert i.implicitWidth() == i.property("size")


def test_now_playing_is_side_by_side_in_landscape(quick):
    quick.fake_now_playing("Song", "Someone", "Album 004", album_id="al4")
    quick.eval("nowPlaying.open()")
    quick.wait_idle()
    _settle(quick, 780, 360)
    cover = quick.get("npCover")
    seek = quick.get("seekBar")
    c = cover.mapToItem(None, 0, 0)
    s = seek.mapToItem(None, 0, 0)
    assert cover.width() > 120  # the cover is there, not squeezed out
    assert s.x() > c.x() + cover.width()  # controls to its right
    _settle(quick, 402, 874)
    s = quick.get("seekBar").mapToItem(None, 0, 0)
    c = quick.get("npCover").mapToItem(None, 0, 0)
    assert s.y() > c.y() + quick.get("npCover").height()  # portrait: below it


def test_landscape_grid_tiles_fit_the_height(quick):
    _settle(quick, 780, 360)
    _section(quick, "album")
    grid = quick.wait_for("albumGrid")
    # Old: 3 columns, ~298 px tiles in a ~304 px page — titles off screen.
    assert grid.property("cellHeight") < 0.85 * grid.height()

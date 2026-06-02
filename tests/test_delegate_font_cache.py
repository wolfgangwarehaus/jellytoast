"""Per-paint QFont / QFontMetrics caching across the three library-side
delegates (AT-9). Pre-AT-9 the paint methods allocated 2-5 fresh QFont +
QFontMetrics objects per row per repaint to elide identical titles
against identical widths — measurable cost on long lists / drag-resize.

The contract under test:

1. Each delegate constructs its font cache in ``__init__`` via
   ``_build_fonts()`` and exposes ``_fm_*`` QFontMetrics + ``_*_font``
   QFont attributes. The presence test proves init populated them.

2. ``_build_fonts`` is idempotent and is wired to
   ``PlayerBus.theme_changed`` so a future runtime theme-driven font
   change refreshes the cache (matches the live-accent contract).

3. The paint methods do NOT construct fresh QFontMetrics — they read
   the cached attributes. Verified by spying on each module's
   ``QFontMetrics`` constructor reference and asserting zero growth
   across N driven paint() calls.
"""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QFontMetrics, QImage, QPainter
from PySide6.QtWidgets import QStyle

_NO_STATE = QStyle.StateFlag.State_None


# ── _TileDelegate (library_grid) ──────────────────────────────────────


def test_tile_delegate_caches_fonts_in_init(qapp):
    from modules.library_grid import _TileDelegate

    d = _TileDelegate("album")
    assert isinstance(d._title_font, QFont)
    assert isinstance(d._fm_title, QFontMetrics)
    assert isinstance(d._caption_font, QFont)
    assert isinstance(d._fm_caption, QFontMetrics)


def test_tile_delegate_paint_does_not_reconstruct_metrics(qapp, monkeypatch):
    import modules.library_grid as lg
    from modules.library_grid import _LibraryItemsModel

    construct_count = [0]
    orig = lg.QFontMetrics

    class _Spy(orig):
        def __init__(self, *a, **kw):
            construct_count[0] += 1
            super().__init__(*a, **kw)

    monkeypatch.setattr(lg, "QFontMetrics", _Spy)

    d = lg._TileDelegate("album")
    after_init = construct_count[0]
    assert after_init >= 2, "Init must build the cache via the spied class"

    img = QImage(400, 400, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)

    class _Opt:
        rect = QRect(0, 0, 200, 240)
        state = _NO_STATE

    class _Idx:
        def data(self, role):
            if role == _LibraryItemsModel.ItemRole:
                return {"Name": "Whatever", "Artists": ["X"]}
            return None

    for _ in range(10):
        d.paint(painter, _Opt(), _Idx())
    painter.end()

    assert construct_count[0] == after_init, (
        f"Paint allocated {construct_count[0] - after_init} fresh "
        "QFontMetrics across 10 paints — cache is broken."
    )


# ── _RowDelegate (library_grid) ───────────────────────────────────────


def test_row_delegate_caches_fonts_in_init(qapp):
    from modules.library_grid import _RowDelegate

    d = _RowDelegate("album")
    assert isinstance(d._title_font, QFont)
    assert isinstance(d._fm_title, QFontMetrics)
    assert isinstance(d._sub_font, QFont)
    assert isinstance(d._fm_sub, QFontMetrics)
    assert isinstance(d._year_font, QFont)


def test_row_delegate_paint_does_not_reconstruct_metrics(qapp, monkeypatch):
    import modules.library_grid as lg
    from modules.library_grid import _LibraryItemsModel

    construct_count = [0]
    orig = lg.QFontMetrics

    class _Spy(orig):
        def __init__(self, *a, **kw):
            construct_count[0] += 1
            super().__init__(*a, **kw)

    monkeypatch.setattr(lg, "QFontMetrics", _Spy)

    d = lg._RowDelegate("album")
    after_init = construct_count[0]
    assert after_init >= 2

    img = QImage(600, 80, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)

    class _Opt:
        rect = QRect(0, 0, 600, 56)
        state = _NO_STATE

    class _Idx:
        def data(self, role):
            if role == _LibraryItemsModel.ItemRole:
                return {"Name": "Row title", "ProductionYear": 2024}
            return None

    for _ in range(10):
        d.paint(painter, _Opt(), _Idx())
    painter.end()

    assert construct_count[0] == after_init


# ── _SongRowDelegate (songs_view) ─────────────────────────────────────


def test_song_row_delegate_caches_fonts_in_init(qapp):
    from modules.songs_view import _SongRowDelegate

    d = _SongRowDelegate()
    assert isinstance(d._body_font, QFont)
    assert isinstance(d._fm_body, QFontMetrics)
    assert isinstance(d._caption_font, QFont)
    assert isinstance(d._fm_caption, QFontMetrics)
    assert isinstance(d._mono_font, QFont)
    assert d._mono_font.family() == "JetBrains Mono"


def test_song_row_delegate_paint_does_not_reconstruct_metrics(qapp, monkeypatch):
    import modules.songs_view as sv
    from modules.songs_view import _SongsListModel

    construct_count = [0]
    orig = sv.QFontMetrics

    class _Spy(orig):
        def __init__(self, *a, **kw):
            construct_count[0] += 1
            super().__init__(*a, **kw)

    monkeypatch.setattr(sv, "QFontMetrics", _Spy)

    d = sv._SongRowDelegate()
    after_init = construct_count[0]
    assert after_init >= 2

    img = QImage(800, 80, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)

    class _Opt:
        rect = QRect(0, 0, 800, 56)
        state = _NO_STATE

    class _Idx:
        def data(self, role):
            if role == _SongsListModel.ItemRole:
                return {
                    "Name": "Track",
                    "Artists": ["A"],
                    "Album": "Alb",
                    "RunTimeTicks": 1800000000,
                }
            return None

    for _ in range(10):
        d.paint(painter, _Opt(), _Idx())
    painter.end()

    assert construct_count[0] == after_init


# ── _TrackDelegate (now_playing_page) ─────────────────────────────────


def test_track_delegate_caches_fonts_in_init(qapp):
    from modules.np_track_list import _TrackDelegate

    d = _TrackDelegate()
    # Divider
    assert isinstance(d._divider_font, QFont)
    assert isinstance(d._fm_divider, QFontMetrics)
    # Index column — both bold-state variants pre-built
    assert isinstance(d._idx_font_regular, QFont)
    assert isinstance(d._idx_font_bold, QFont)
    assert d._idx_font_regular.family() == "JetBrains Mono"
    assert d._idx_font_bold.bold() is True
    assert d._idx_font_regular.bold() is False
    # Title — both bold-state variants + matching metrics
    assert isinstance(d._title_font_regular, QFont)
    assert isinstance(d._title_font_bold, QFont)
    assert isinstance(d._fm_title_regular, QFontMetrics)
    assert isinstance(d._fm_title_bold, QFontMetrics)
    assert d._title_font_bold.bold() is True
    assert d._title_font_regular.bold() is False
    # Subtitle + duration — single-variant
    assert isinstance(d._sub_font, QFont)
    assert isinstance(d._fm_sub, QFontMetrics)
    assert isinstance(d._dur_font, QFont)
    assert d._dur_font.family() == "JetBrains Mono"


def test_track_delegate_paint_does_not_reconstruct_metrics(qapp, monkeypatch):
    import modules.np_track_list as npp
    from modules.np_track_list import _TracksModel

    construct_count = [0]
    orig = npp.QFontMetrics

    class _Spy(orig):
        def __init__(self, *a, **kw):
            construct_count[0] += 1
            super().__init__(*a, **kw)

    monkeypatch.setattr(npp, "QFontMetrics", _Spy)

    d = npp._TrackDelegate()
    after_init = construct_count[0]
    # Init builds 4 QFontMetrics (divider + title-regular + title-bold + sub).
    assert after_init >= 4

    img = QImage(600, 80, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)

    class _Opt:
        rect = QRect(0, 0, 600, 44)
        state = _NO_STATE

    def _data_for(role, *, current: bool):
        if role == _TracksModel.KindRole:
            return "track"
        if role == _TracksModel.ItemRole:
            return {
                "Name": "Song",
                "Artists": ["A"],
                "IndexNumber": 3,
                "RunTimeTicks": 2400000000,
            }
        if role == _TracksModel.IsCurrentRole:
            return current
        if role == _TracksModel.ShowArtistRole:
            return True
        if role == _TracksModel.PlayIndexRole:
            return 2
        if role == _TracksModel.IsDownloadedRole:
            return False
        if role == _TracksModel.IsDragGhostRole:
            return False
        if role == _TracksModel.AnimYOffsetRole:
            return 0.0
        if role == _TracksModel.SuppressHoverRole:
            return False
        if role == _TracksModel.IsPressedRole:
            return False
        if role == _TracksModel.IsKbCursorRole:
            return False
        if role == _TracksModel.DiscInfoRole:
            return None
        return None

    class _IdxCurrent:
        def data(self, role):
            return _data_for(role, current=True)

    class _IdxNotCurrent:
        def data(self, role):
            return _data_for(role, current=False)

    # Alternate is_current so both bold-conditional cache branches run.
    for i in range(10):
        d.paint(painter, _Opt(), _IdxCurrent() if i % 2 else _IdxNotCurrent())
    painter.end()

    assert construct_count[0] == after_init, (
        f"Paint allocated {construct_count[0] - after_init} fresh "
        "QFontMetrics across 10 alternating-current paints — "
        "bold-variant cache is broken."
    )


# ── theme_changed wiring ──────────────────────────────────────────────


def test_delegates_rebuild_fonts_on_theme_changed(qapp):
    """Each delegate's _build_fonts hooks up to PlayerBus.theme_changed
    so a future runtime theme/font-scale shift refreshes the cache.
    Emit the signal and verify each delegate's _build_fonts ran (proven
    by the cached attribute identity changing)."""
    from modules.genres_view import _GenreDelegate
    from modules.library_grid import _RowDelegate, _TileDelegate
    from modules.np_track_list import _TrackDelegate
    from modules.player_state import PlayerBus
    from modules.songs_view import _SongRowDelegate

    delegates = [
        _TileDelegate("album"),
        _RowDelegate("album"),
        _SongRowDelegate(),
        _TrackDelegate(),
        _GenreDelegate(),
    ]

    def _probe(d):
        # Pick one cached attribute per delegate to identity-check.
        for name in ("_title_font", "_title_font_regular", "_body_font"):
            if hasattr(d, name):
                return id(getattr(d, name))
        raise AssertionError(f"no cached title font on {type(d).__name__}")

    before = [_probe(d) for d in delegates]
    PlayerBus.get().theme_changed.emit()
    after = [_probe(d) for d in delegates]
    for d, b, a in zip(delegates, before, after, strict=False):
        assert b != a, (
            f"{type(d).__name__}: theme_changed did not refresh the "
            "cached font — _build_fonts was not invoked."
        )


# ── _GenreDelegate (genres_view) — AT-13 ──────────────────────────────


def test_genre_delegate_caches_fonts_in_init(qapp):
    from modules.genres_view import _GenreDelegate

    d = _GenreDelegate()
    assert isinstance(d._title_font, QFont)
    assert isinstance(d._fm_title, QFontMetrics)
    assert d._title_font.bold() is True


def test_genre_delegate_paint_does_not_reconstruct_metrics(qapp, monkeypatch):
    import modules.genres_view as gv
    from modules.genres_view import _GenresModel

    construct_count = [0]
    orig = gv.QFontMetrics

    class _Spy(orig):
        def __init__(self, *a, **kw):
            construct_count[0] += 1
            super().__init__(*a, **kw)

    monkeypatch.setattr(gv, "QFontMetrics", _Spy)

    d = gv._GenreDelegate()
    after_init = construct_count[0]
    assert after_init >= 1, "Init must build the cache via the spied class"

    img = QImage(300, 120, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)

    class _Opt:
        rect = QRect(0, 0, 220, 108)
        # Hover-on so the gradient-swap branch runs too — the hover
        # repaint is the hot path this cache protects.
        state = QStyle.StateFlag.State_MouseOver

    class _Idx:
        def data(self, role):
            if role == _GenresModel.ItemRole:
                return {"Name": "Progressive Metal", "Id": "g1"}
            return None

    for _ in range(10):
        d.paint(painter, _Opt(), _Idx())
    painter.end()

    assert construct_count[0] == after_init, (
        f"Paint allocated {construct_count[0] - after_init} fresh "
        "QFontMetrics across 10 paints — genres font cache is broken."
    )


# ── _RowDelegate cover cache (library_grid) — AT-13 ───────────────────


def test_row_delegate_caches_scaled_cover(qapp, monkeypatch):
    """The list-mode row delegate must scale + centre-crop the cover
    thumb once per (cacheKey, size, dpr), not on every paint. Spy on
    the module-level scale_pixmap_for_dpr and assert it runs exactly
    once across N paints of the same cover, and that the cache holds
    the result."""
    from PySide6.QtGui import QPixmap

    import modules.library_grid as lg
    from modules.library_grid import _LibraryItemsModel

    scale_calls = [0]
    orig_scale = lg.scale_pixmap_for_dpr

    def _spy_scale(*a, **kw):
        scale_calls[0] += 1
        return orig_scale(*a, **kw)

    monkeypatch.setattr(lg, "scale_pixmap_for_dpr", _spy_scale)

    d = lg._RowDelegate("album")

    # A real, non-null cover so the paint hits the scale branch.
    cover = QPixmap(180, 180)
    cover.fill(Qt.GlobalColor.red)

    img = QImage(600, 80, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)

    class _Opt:
        rect = QRect(0, 0, 600, 48)
        state = _NO_STATE

    class _Idx:
        def data(self, role):
            if role == _LibraryItemsModel.ItemRole:
                return {"Name": "Row title", "ProductionYear": 2024}
            if role == _LibraryItemsModel.CoverRole:
                return cover
            return None

    for _ in range(10):
        d.paint(painter, _Opt(), _Idx())
    painter.end()

    assert scale_calls[0] == 1, (
        f"scale_pixmap_for_dpr ran {scale_calls[0]} times across 10 "
        "paints of the same cover — the row thumb cache is not hit."
    )
    assert len(d._scaled_cover_cache) == 1, (
        "Expected exactly one cached thumb entry after repeated paints."
    )

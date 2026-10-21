"""Qt Quick phone front end (M0): bridge models + QML smoke load."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jellytoast.quick import models as qm


def _albums(n, start=0):
    return [
        {"Id": f"a{i}", "Name": f"Album {i}", "AlbumArtist": "X", "ProductionYear": 2000}
        for i in range(start, start + n)
    ]


@pytest.fixture
def prov(monkeypatch):
    p = MagicMock()
    p.get_items.side_effect = lambda *a, **k: {
        "Items": _albums(min(qm.AlbumsModel.PAGE, 130 - a[3]), a[3])
    }
    p.get_image_url.side_effect = lambda i, kind, px: f"http://s/{i}.jpg"
    monkeypatch.setattr(qm, "_provider", lambda: p)
    # Run "async" work inline.
    monkeypatch.setattr(
        qm.async_io, "run_async", lambda fn, *a, on_result=None, on_error=None: on_result(fn(*a))
    )
    return p


def test_albums_pages_until_short_page(qapp, prov):
    m = qm.AlbumsModel()
    m.reload()
    assert m.rowCount() == 100 and m.canFetchMore()
    m.fetchMore()
    assert m.rowCount() == 130 and not m.canFetchMore()
    idx = m.index(5)
    assert m.data(idx, m.NameRole) == "Album 5"
    assert m.data(idx, m.CoverRole) == "http://s/a5.jpg"
    assert m.data(idx, m.YearRole) == "2000"


def test_stale_page_dropped_after_reload(qapp, prov, monkeypatch):
    pending = []
    monkeypatch.setattr(
        qm.async_io,
        "run_async",
        lambda fn, *a, on_result=None, on_error=None: pending.append(on_result),
    )
    m = qm.AlbumsModel()
    m.reload()
    old = pending.pop()
    m.reload()
    old(_albums(3))  # first generation lands late
    assert m.rowCount() == 0


def test_qml_loads_offscreen(qapp, monkeypatch):
    from jellytoast import quick
    from jellytoast.providers import get_provider

    monkeypatch.setattr(type(get_provider()), "is_authenticated", property(lambda self: False))
    engine = quick.build_engine(qapp, start_core=False)
    roots = engine.rootObjects()
    assert roots, "Main.qml failed to load"
    win = roots[0]
    assert win.findChild(object, "signInPage") is not None  # signed out → sign in
    win.close()
    engine.deleteLater()


# ── M1: album tracks, player controls, icons ──


def _tracks():
    return [
        {
            "Id": "t1",
            "Name": "One",
            "IndexNumber": 1,
            "RunTimeTicks": 1_850_000_000,
            "Artists": ["X"],
            "AlbumArtist": "X",
        },
        {
            "Id": "t2",
            "Name": "Two",
            "IndexNumber": 2,
            "RunTimeTicks": 600_000_000,
            "Artists": ["X", "Guest"],
            "AlbumArtist": "X",
        },
    ]


@pytest.fixture
def bus_spy(qapp):
    from jellytoast.player_state import PlayerBus

    bus = PlayerBus.get()
    got = {"play": [], "shuffle": [], "repeat": [], "seek": []}
    slots = {
        bus.queue_play_now: lambda items, i, ctx: got["play"].append((items, i, ctx)),
        bus.shuffle_changed: lambda on: got["shuffle"].append(on),
        bus.repeat_changed: lambda m: got["repeat"].append(m),
        bus.seek_requested: lambda ms: got["seek"].append(ms),
    }
    for sig, fn in slots.items():
        sig.connect(fn)
    yield got
    for sig, fn in slots.items():
        sig.disconnect(fn)


def test_tracks_model_roles_and_play(qapp, monkeypatch, bus_spy):
    p = MagicMock()
    p.get_album_tracks.return_value = _tracks()
    monkeypatch.setattr(qm, "_provider", lambda: p)
    monkeypatch.setattr(
        qm.async_io, "run_async", lambda fn, *a, on_result=None, on_error=None: on_result(fn(*a))
    )
    m = qm.TracksModel()
    m.load("alb", "The Album")
    assert m.rowCount() == 2
    assert m.data(m.index(0), m.LengthRole) == "3:05"
    assert m.data(m.index(0), m.ArtistRole) == ""  # same as album artist → hidden
    assert m.data(m.index(1), m.ArtistRole) == "X, Guest"
    assert m.summary == "2 songs · 4 min"

    m.play(1)
    items, idx, ctx = bus_spy["play"][-1]
    assert [t["Id"] for t in items] == ["t1", "t2"] and idx == 1
    assert ctx.source_id == "alb" and ctx.source_label == "The Album"

    m.shuffle()
    assert bus_spy["shuffle"][-1] is True and len(bus_spy["play"]) == 2


def test_player_controller_modes_and_seek(qapp, bus_spy):
    from jellytoast.quick.player import PlayerController

    pc = PlayerController()
    pc._on_repeat("off")
    pc.cycleRepeat()
    assert bus_spy["repeat"][-1] == "all"
    pc._on_repeat("one")
    pc.cycleRepeat()
    assert bus_spy["repeat"][-1] == "off"
    pc._on_shuffle(False)
    pc.toggleShuffle()
    assert bus_spy["shuffle"][-1] is True
    pc.seek(42_000)
    assert bus_spy["seek"][-1] == 42_000 and pc.position == 42_000


def test_icon_provider_renders_tinted_glyph(qapp):
    from PySide6.QtCore import QSize

    from jellytoast.quick.icons import IconProvider

    size = QSize()
    pix = IconProvider().requestPixmap("play/ff0000", size, QSize(40, 40))
    assert not pix.isNull() and size == QSize(40, 40)
    img = pix.toImage()
    reds = [
        img.pixelColor(x, y)
        for x in range(40)
        for y in range(40)
        if img.pixelColor(x, y).alpha() > 200
    ]
    assert reds and all(c.red() > 200 and c.green() < 60 for c in reds)


def test_reload_with_view_attached_fetches_page_once(qapp, prov, monkeypatch):
    """endResetModel() makes a view call fetchMore() re-entrantly; reload()
    must not then fetch the same page again (duplicate albums)."""
    pending = []
    monkeypatch.setattr(
        qm.async_io,
        "run_async",
        lambda fn, *a, on_result=None, on_error=None: pending.append((fn, on_result)),
    )
    m = qm.AlbumsModel()
    m.modelReset.connect(lambda: m.fetchMore())  # what GridView does
    m.reload()
    assert len(pending) == 1
    fn, done = pending.pop()
    done(fn())
    assert m.rowCount() == 100


def test_seek_letter_pages_until_found(qapp, monkeypatch):
    """A–Z scrubber: a letter past the loaded rows pages more in, then answers."""
    names = [f"A{i:03d}" for i in range(150)] + [f"M{i:03d}" for i in range(20)]
    p = MagicMock()
    p.get_items.side_effect = lambda *a, **k: {
        "Items": [{"Id": n, "Name": n} for n in names[a[3] : a[3] + a[2]]]
    }
    p.get_image_url.return_value = ""
    monkeypatch.setattr(qm, "_provider", lambda: p)
    monkeypatch.setattr(
        qm.async_io, "run_async", lambda fn, *a, on_result=None, on_error=None: on_result(fn(*a))
    )
    m = qm.LibraryModel(kind="album")
    found = []
    m.letterFound.connect(lambda letter, row: found.append((letter, row)))
    m.reload()
    assert m.rowCount() == 100
    m.seekLetter("M")
    assert found == [("M", 150)] and m.rowCount() == 170
    m.seekLetter("Z")  # past the end → last row
    assert found[-1] == ("Z", 169)

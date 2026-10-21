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
    assert win.findChild(object, "albumGrid") is not None
    win.close()
    engine.deleteLater()

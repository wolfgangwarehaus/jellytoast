"""RadioView top-level objectName regression (testability gap N1).

The per-station / preset rows already carry ``jtStationRow`` /
``jtPresetRow``, but the top-level ``RadioView`` had no objectName, so a
test (or QSS) couldn't address the view itself. ``RadioView.__init__``
fires ``reload()`` → ``run_async(get_internet_radio_stations)``; we
neutralise ``run_async`` so no thread pool / network work runs here — the
objectName is set before ``reload()`` regardless.
"""

from __future__ import annotations


def test_radio_view_has_object_name(qapp, monkeypatch):
    import jellytoast.radio_view as rv

    monkeypatch.setattr(rv, "run_async", lambda *a, **k: None)
    view = rv.RadioView(queue_mgr=None)
    assert view.objectName() == "radioView"

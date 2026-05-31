"""LibraryGrid cover self-heal across an offline blip.

A connectivity flap (the Jellyfin→Navidrome swap symptom) flips auto-offline
on/off during the initial album load. While offline, the image gate fails
every uncached cover SYNCHRONOUSLY. Previously a tile was added to
``_covers_loaded`` before the fetch and, once it burned its retry budget,
stayed marked loaded with no pixmap forever — the "only some album art came
in" symptom. Now an offline failure is treated as a deferral (no retry
spend) and re-fetched on the offline→online transition.
"""

from __future__ import annotations

from modules import library_grid as lg
from modules import offline as _offline


def test_offline_cover_failure_is_deferred_not_capped(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": "a1"}, {"Id": "a2"}])
    monkeypatch.setattr(g.api, "get_image_url", lambda *a, **k: "http://x/cover")
    # Stand in for the image gate's synchronous offline failure.
    monkeypatch.setattr(
        lg,
        "load_image_async",
        lambda key, url, w, h, on_pix, on_error=None, **kw: on_error and on_error(),
    )
    monkeypatch.setattr(_offline, "is_offline_mode", lambda: True)

    g._fire_cover_load(0)

    # Deferred: remembered for re-fetch, retry budget NOT spent.
    assert 0 in g._cover_failed
    assert 0 not in g._cover_retries
    # Still marked loaded so the normal visible/prefetch passes skip it.
    assert 0 in g._covers_loaded


def test_online_cover_failure_still_counts_toward_cap(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": "a1"}])
    monkeypatch.setattr(g.api, "get_image_url", lambda *a, **k: "http://x/cover")
    monkeypatch.setattr(
        lg,
        "load_image_async",
        lambda key, url, w, h, on_pix, on_error=None, **kw: on_error and on_error(),
    )
    monkeypatch.setattr(_offline, "is_offline_mode", lambda: False)
    # Don't let the retry actually re-spawn fetches in the test.
    monkeypatch.setattr(g._prefetch_timer, "isActive", lambda: True)

    g._fire_cover_load(0)

    # Online failure: real retry, counted toward the cap, not deferred.
    assert g._cover_retries.get(0) == 1
    assert 0 not in g._cover_failed


def test_rearm_reissues_deferred_covers(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": "a1"}, {"Id": "a2"}])
    g._covers_loaded.update({0, 1})
    g._cover_failed.add(0)
    g._cover_retries[0] = 0
    fired = {"n": 0}
    monkeypatch.setattr(g, "_load_visible_covers", lambda: fired.__setitem__("n", fired["n"] + 1))

    g._rearm_failed_covers()

    assert g._cover_failed == set()
    assert 0 not in g._covers_loaded  # re-armed → eligible to re-fetch
    assert 1 in g._covers_loaded  # untouched
    assert fired["n"] == 1  # visible pass re-issued


def test_online_transition_rearms_offline_transition_does_not(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": "a1"}])
    monkeypatch.setattr(g, "isVisible", lambda: False)  # skip the full reload
    calls = []
    monkeypatch.setattr(g, "_rearm_failed_covers", lambda: calls.append(True))

    g._on_offline_mode_changed(True)  # going OFFLINE → no rearm
    assert calls == []

    g._on_offline_mode_changed(False)  # back ONLINE → rearm
    assert calls == [True]


def test_rearm_is_noop_when_nothing_deferred(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": "a1"}])
    fired = {"n": 0}
    monkeypatch.setattr(g, "_load_visible_covers", lambda: fired.__setitem__("n", fired["n"] + 1))
    g._rearm_failed_covers()
    assert fired["n"] == 0  # no failed covers → no work

"""Regression tests for the large-library cover-pipeline fixes.

The first external user ran jellytoast (Win11 installer) against a Navidrome
with ~5,200 albums / ~73,000 tracks / 580GB — far past anything tested in
house — and hit three faces of one bug: covers loaded a few then STALLED,
already-loaded covers all DISAPPEARED, and it reproduced across restarts.

Root causes + the fixes pinned here:

- ``ui_helpers``: NORMAL-priority cover loads now go through the concurrency
  gate (were ungated → flooded QNAM's internal queue → transfer-timeout →
  abandoned), so visible tiles wait HERE without a live timer.
- ``library_grid``: a visible cover EVICTED from the model LRU (or abandoned
  after retries) is re-armed + reloaded instead of staying a placeholder.
- ``library_paginator``: an AUTO offline flip no longer blanks a populated
  server-backed grid (the "all images disappear" / flapping).
- ``subsonic``: a ReadTimeout (slow-but-alive server) does NOT trip
  auto-offline; only a connect-level failure does.
- ``songs_view`` / ``image_cache``: caches bounded / sized for a real library.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QPixmap

from jellytoast import library_grid as lg
from jellytoast import offline as _offline_pkg
from jellytoast import ui_helpers as uih
from jellytoast.offline import connectivity as _conn


def _pix() -> QPixmap:
    p = QPixmap(2, 2)
    p.fill()  # non-null content → passes set_cover's isNull guard
    return p


# ── offline_source: auto vs user ────────────────────────────────────────────


def test_offline_source_reflects_auto_vs_user():
    _conn._reset_for_tests()
    assert _conn.offline_source() is None
    assert _offline_pkg.offline_source() is None
    _conn._set_offline_mode_internal(True, source="auto")
    assert _conn.offline_source() == "auto"
    assert _offline_pkg.offline_source() == "auto"  # package re-export agrees
    _conn._set_offline_mode_internal(False, source=None)
    assert _conn.offline_source() is None


# ── subsonic: slow-but-alive server is not an outage ─────────────────────────


def _subsonic_provider():
    from jellytoast.providers.subsonic import SubsonicProvider

    p = SubsonicProvider()
    p._username = "tester"
    p._password = "secret"
    p._server_url = "http://example.local"
    return p


def test_subsonic_read_timeout_is_not_a_network_failure(monkeypatch):
    import requests

    p = _subsonic_provider()

    class _SlowSession:
        def get(self, *a, **k):
            raise requests.exceptions.ReadTimeout("server busy generating art")

    p.session = _SlowSession()
    counts = {"fail": 0, "ok": 0}
    monkeypatch.setattr(
        _offline_pkg, "note_request_failure", lambda: counts.__setitem__("fail", counts["fail"] + 1)
    )
    monkeypatch.setattr(
        _offline_pkg, "note_request_success", lambda: counts.__setitem__("ok", counts["ok"] + 1)
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        p._request("getAlbumList2", {"type": "alphabeticalByName"})

    # A read timeout means we CONNECTED — the server is reachable, just slow.
    # It must count as neither a failure (would trip auto-offline) nor a
    # success (it returned no data).
    assert counts["fail"] == 0
    assert counts["ok"] == 0


def test_subsonic_connect_failure_still_counts_as_outage(monkeypatch):
    import requests

    p = _subsonic_provider()

    class _DeadSession:
        def get(self, *a, **k):
            raise requests.exceptions.ConnectionError("host unreachable")

    p.session = _DeadSession()
    counts = {"fail": 0}
    monkeypatch.setattr(
        _offline_pkg, "note_request_failure", lambda: counts.__setitem__("fail", counts["fail"] + 1)
    )

    with pytest.raises(requests.exceptions.ConnectionError):
        p._request("getAlbumList2", {"type": "alphabeticalByName"})

    assert counts["fail"] == 1  # a genuinely dead host DOES trip the tracker


# ── ui_helpers: the concurrency gate covers NORMAL priority ──────────────────


def test_normal_priority_load_is_gated_high_bypasses(monkeypatch):
    monkeypatch.setattr(_offline_pkg, "is_offline_mode", lambda: False)
    fired = []
    monkeypatch.setattr(uih, "_fire_image_request", lambda *a: fired.append(a))
    # Saturate the gate, clean state.
    uih._gated_in_flight = uih._GATED_MAX_INFLIGHT
    uih._deferred_normal.clear()
    uih._deferred_low.clear()
    uih._inflight_subscribers.clear()
    try:
        uih._after_disk_miss(
            "kN|1x1|r=0", "kN", "http://x", 1, 1, 0, lambda p: None, None, "normal"
        )
        # Over the cap → NORMAL is deferred HERE (no QNAM timer), not fired.
        assert fired == []
        assert len(uih._deferred_normal) == 1
        assert len(uih._deferred_low) == 0

        uih._after_disk_miss(
            "kL|1x1|r=0", "kL", "http://x", 1, 1, 0, lambda p: None, None, "low"
        )
        assert len(uih._deferred_low) == 1  # LOW defers to its own queue

        # HIGH priority bypasses the gate entirely (rare, user-facing).
        uih._after_disk_miss(
            "kH|1x1|r=0", "kH", "http://x", 1, 1, 0, lambda p: None, None, "high"
        )
        assert len(fired) == 1
    finally:
        uih._gated_in_flight = 0
        uih._deferred_normal.clear()
        uih._deferred_low.clear()
        uih._inflight_subscribers.clear()


# ── library_grid: an evicted/abandoned visible cover is re-armed ─────────────


def test_evicted_visible_cover_is_rearmed(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": f"a{i}"} for i in range(3)])
    monkeypatch.setattr(g.api, "get_image_url", lambda *a, **k: "http://x/cover")
    monkeypatch.setattr(g, "_visible_row_range", lambda: (0, 3))
    fired = []
    monkeypatch.setattr(
        lg,
        "load_image_async",
        lambda key, url, w, h, on_pix, on_error=None, **kw: fired.append(key),
    )

    # First pass: nothing resident yet → all three fire.
    g._load_visible_covers()
    assert sorted(fired) == ["a0|albumtile", "a1|albumtile", "a2|albumtile"]
    assert g._covers_loaded == {0, 1, 2}

    # Simulate the model LRU evicting row 1's pixmap while it's still marked
    # loaded (rows 0/2 resident). Old behaviour: row 1 stays a placeholder
    # forever. New behaviour: the visible pass re-arms + reloads ONLY row 1.
    monkeypatch.setattr(g._model, "has_cover", lambda r: r != 1)
    fired.clear()
    g._load_visible_covers()
    assert fired == ["a1|albumtile"]


def test_artless_row_does_not_spin_on_revisit(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": "a0"}, {"Id": ""}])  # row 1 has no art id
    monkeypatch.setattr(g.api, "get_image_url", lambda *a, **k: "http://x/cover")
    monkeypatch.setattr(g, "_visible_row_range", lambda: (0, 2))
    fired = []
    monkeypatch.setattr(
        lg,
        "load_image_async",
        lambda key, url, w, h, on_pix, on_error=None, **kw: fired.append(key),
    )
    g._load_visible_covers()
    # Row 1 (no art id) is marked retry-exhausted so the re-arm pass skips it.
    assert g._cover_retries.get(1) == g.COVER_RETRY_LIMIT
    fired.clear()
    # Second pass with nothing resident: row 0 re-arms; row 1 (exhausted /
    # art-less) is NOT re-fired — no spin, no wasted request.
    monkeypatch.setattr(g._model, "has_cover", lambda r: False)
    g._load_visible_covers()
    assert fired == ["a0|albumtile"]


# ── library_paginator: AUTO offline preserves a populated grid ───────────────


def test_auto_offline_preserves_populated_grid(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": "a1"}, {"Id": "a2"}])
    monkeypatch.setattr(g, "isVisible", lambda: True)
    reloaded = []
    monkeypatch.setattr(g, "load_items", lambda *a, **k: reloaded.append(True))

    # AUTO flip + populated grid → preserve what's on screen, no re-render.
    monkeypatch.setattr(_offline_pkg, "offline_source", lambda: "auto")
    g._on_offline_mode_changed(True)
    assert reloaded == []  # grid + covers preserved (no wipe)

    # A deliberate USER toggle still renders the downloads-only view.
    monkeypatch.setattr(_offline_pkg, "offline_source", lambda: "user")
    g._on_offline_mode_changed(True)
    assert reloaded == [True]


def test_auto_offline_renders_downloads_when_grid_empty(qapp, monkeypatch):
    g = lg.LibraryGrid("album")
    g._model.set_items([])  # nothing loaded yet
    monkeypatch.setattr(g, "isVisible", lambda: True)
    reloaded = []
    monkeypatch.setattr(g, "load_items", lambda *a, **k: reloaded.append(True))
    monkeypatch.setattr(_offline_pkg, "offline_source", lambda: "auto")
    g._on_offline_mode_changed(True)
    # An empty grid has nothing to preserve → fall through to render downloads.
    assert reloaded == [True]


# ── songs_view: bounded thumb cache ──────────────────────────────────────────


def test_songs_cover_cache_is_bounded(qapp):
    from jellytoast.songs_view import _SongsListModel

    m = _SongsListModel()
    cap = _SongsListModel._COVER_CACHE_MAX
    n = cap + 50
    m.set_items([{"Id": f"s{i}", "AlbumId": f"al{i}"} for i in range(n)])
    pix = _pix()
    for row in range(n):
        m.set_cover(row, pix)
    assert len(m._covers) == cap  # never grows past the cap (was unbounded)
    assert not m.has_cover(0)  # earliest → evicted
    assert m.has_cover(n - 1)  # newest → resident


# ── image_cache: disk cap sized for a real library ───────────────────────────


def test_disk_cache_cap_holds_a_real_library():
    from jellytoast import image_cache

    # A ~5,000-album library stores ~2 PNGs/album (~1.2GB). The cap must be
    # well above the old 200MB so the disk tier converges between launches
    # instead of evicting faster than it fills ("rebooted twice, still broken").
    assert image_cache._DISK_CACHE_MAX_BYTES >= 1024 * 1024 * 1024

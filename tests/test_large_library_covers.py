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

        uih._after_disk_miss("kL|1x1|r=0", "kL", "http://x", 1, 1, 0, lambda p: None, None, "low")
        assert len(uih._deferred_low) == 1  # LOW defers to its own queue

        # HIGH priority bypasses the gate entirely (rare, user-facing).
        uih._after_disk_miss("kH|1x1|r=0", "kH", "http://x", 1, 1, 0, lambda p: None, None, "high")
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
    g.show()  # the loader defers while hidden (tray-restore fix)
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
    g.show()  # the loader defers while hidden (tray-restore fix)
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


# ── tray-restore blank art: clear-while-hidden re-arms on show ───────────────


def test_dpr_clear_while_hidden_rearms_on_show(qapp, monkeypatch):
    """The restore-from-tray bug: a DevicePixelRatioChange delivered while
    the window is hidden clears every decoded cover and the reload pass
    can't see any rows — before the fix, nothing re-armed on show and the
    grid sat on placeholders until a resize/scroll."""
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": f"a{i}"} for i in range(3)])
    g.show()
    monkeypatch.setattr(g.api, "get_image_url", lambda *a, **k: "http://x/cover")
    monkeypatch.setattr(g, "_visible_row_range", lambda: (0, 3))
    fired = []
    monkeypatch.setattr(
        lg,
        "load_image_async",
        lambda key, url, w, h, on_pix, on_error=None, **kw: fired.append(key),
    )
    g._load_visible_covers()
    assert len(fired) == 3  # baseline pass fired

    g.hide()
    fired.clear()
    g._on_dpr_changed()  # clears covers; hidden → defers, burns no retries
    assert fired == []
    assert g._covers_dirty_while_hidden
    assert getattr(g, "_visible_retry_tries", 0) == 0

    g.show()  # showEvent schedules the coalesced pass via singleShot(0)
    qapp.processEvents()
    # Set, not list: the DPR handler also restarts the prefetch timer,
    # which can re-fire rows during processEvents — production coalesces
    # duplicates on cache_key, so coverage is the contract here.
    assert set(fired) == {"a0|albumtile", "a1|albumtile", "a2|albumtile"}


# ── 2026-07 art audit: viewport-first scheduling ─────────────────────────────


def _entry(key, fired):
    return (64, 64, 0, lambda: fired.append(key))


def test_visible_coalesce_promotes_parked_key(qapp):
    """The K's-stall fix: a normal-priority repeat for a key parked deep
    in the deferred queue jumps it to the FRONT — the viewport must not
    wait behind rows the user scrolled past."""
    fired = []
    uih._gated_in_flight = uih._GATED_MAX_INFLIGHT  # gate full
    try:
        for i in range(6):
            uih._inflight_subscribers[f"stale{i}"] = [(lambda p: None, None)]
            uih._deferred_normal[f"stale{i}"] = _entry(f"stale{i}", fired)
        uih._inflight_subscribers["k-row"] = [(lambda p: None, None)]
        uih._deferred_normal["k-row"] = _entry("k-row", fired)
        # The user scrolls k-row into view → the visible pass re-requests it
        # and coalesces; the coalesce path promotes.
        uih._after_disk_miss("k-row", "k", "http://x", 64, 64, 0, lambda p: None, None, "normal")
        uih._gated_in_flight -= 1  # a slot frees
        uih._promote_next_deferred()
        assert fired == ["k-row"]  # promoted past all six stale rows
    finally:
        uih._gated_in_flight = 0
        uih._deferred_normal.clear()
        uih._deferred_low.clear()
        uih._inflight_subscribers.clear()


def test_low_prefetch_upgrades_when_scrolled_to(qapp):
    """A row prefetched at LOW that the user scrolls to must upgrade to
    the front of NORMAL — the second starvation path."""
    fired = []
    uih._gated_in_flight = uih._GATED_MAX_INFLIGHT
    try:
        uih._inflight_subscribers["prefetched"] = [(lambda p: None, None)]
        uih._deferred_low["prefetched"] = _entry("prefetched", fired)
        uih._inflight_subscribers["other"] = [(lambda p: None, None)]
        uih._deferred_normal["other"] = _entry("other", fired)
        uih._after_disk_miss(
            "prefetched", "p", "http://x", 64, 64, 0, lambda p: None, None, "normal"
        )
        assert "prefetched" not in uih._deferred_low
        uih._gated_in_flight -= 1
        uih._promote_next_deferred()
        assert fired == ["prefetched"]  # beat the earlier normal entry
    finally:
        uih._gated_in_flight = 0
        uih._deferred_normal.clear()
        uih._deferred_low.clear()
        uih._inflight_subscribers.clear()


def test_low_queue_cap_drops_oldest_silently(qapp):
    """The low backlog is bounded; overflow forgets the longest-parked
    entry AND its subscribers (no callbacks — the visible re-arm covers
    dropped rows), so the key can't wedge as phantom-in-flight."""
    called = []
    uih._gated_in_flight = uih._GATED_MAX_INFLIGHT
    try:
        for i in range(uih._DEFERRED_LOW_MAX + 3):
            key = f"low{i}"
            uih._after_disk_miss(
                key,
                key,
                "http://x",
                64,
                64,
                0,
                called.append,
                lambda: called.append("err"),
                "low",
            )
        assert len(uih._deferred_low) == uih._DEFERRED_LOW_MAX
        # The three oldest were dropped silently — no callbacks, keys freed.
        assert called == []
        for i in range(3):
            assert f"low{i}" not in uih._inflight_subscribers
            assert f"low{i}" not in uih._deferred_low
        assert f"low{uih._DEFERRED_LOW_MAX + 2}" in uih._deferred_low
    finally:
        uih._gated_in_flight = 0
        uih._deferred_normal.clear()
        uih._deferred_low.clear()
        uih._inflight_subscribers.clear()


# ── 2026-07 art audit: capped-row forgiveness ────────────────────────────────


def test_capped_row_forgiven_after_cooldown(qapp, monkeypatch):
    """A row benched by 4 genuine failures gets a fresh chance when it's
    visible again and its last failure is old — server recovery must not
    leave session-permanent blank tiles."""
    import time

    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": "a0"}])
    g.show()
    monkeypatch.setattr(g.api, "get_image_url", lambda *a, **k: "http://x/cover")
    monkeypatch.setattr(g, "_visible_row_range", lambda: (0, 1))
    fired = []
    monkeypatch.setattr(
        lg,
        "load_image_async",
        lambda key, url, w, h, on_pix, on_error=None, **kw: fired.append(key),
    )
    # Bench the row: at cap, with a failure wall stamped just now.
    g._cover_retries[0] = g.COVER_RETRY_LIMIT
    g._cover_retry_wall[0] = time.monotonic()
    g._covers_loaded.add(0)
    g._load_visible_covers()
    assert fired == []  # cooldown not elapsed — stays benched

    # Age the failure past the forgiveness window.
    g._cover_retry_wall[0] = time.monotonic() - g.COVER_RETRY_FORGIVE_SEC - 1
    g._load_visible_covers()
    assert fired == ["a0|albumtile"]  # forgiven and re-fired
    assert 0 not in g._cover_retries


def test_artless_row_is_never_forgiven(qapp, monkeypatch):
    """Rows with no art id bench WITHOUT a wall timestamp — forgiveness
    must not spin on them."""
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": ""}])
    g.show()
    monkeypatch.setattr(g, "_visible_row_range", lambda: (0, 1))
    fired = []
    monkeypatch.setattr(
        lg,
        "load_image_async",
        lambda key, url, w, h, on_pix, on_error=None, **kw: fired.append(key),
    )
    g._load_visible_covers()  # benches it (no id)
    g._load_visible_covers()  # revisit: no wall → stays benched
    assert fired == []


# ── 2026-07 field report: deep-scroll visible range (the K's stall) ──────────


def _pin_geometry(g, monkeypatch, *, scroll, vp_h=950, vp_w=900, cell_h=246, cols=4):
    """Pin the exact geometry the live app reported during the stall:
    4 columns of 246px cells, 950px viewport, scrolled deep. Fakes the
    viewport/scrollbar so the range math is exercised without depending
    on offscreen layout."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        g._view, "viewport", lambda: SimpleNamespace(height=lambda: vp_h, width=lambda: vp_w)
    )
    monkeypatch.setattr(g._view, "verticalScrollBar", lambda: SimpleNamespace(value=lambda: scroll))
    monkeypatch.setattr(g, "_cell_metrics", lambda: (cell_h, cols))


def test_deep_scroll_range_is_not_blind(qapp, monkeypatch):
    """THE regression: at a deep scroll offset the old corner-probe
    implementation returned (0, 0) — the app could not see what was on
    screen, so every cover trigger downstream went dead and art died at
    a fixed row (the prefetch ceiling). Cell math must report the real
    window."""
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": f"a{i}"} for i in range(314)])
    g.show()
    _pin_geometry(g, monkeypatch, scroll=15468)

    first, last = g._visible_row_range()

    # Live-verified numbers: rows 248..267 were on screen (row 256 sat at
    # viewport y=276), plus the 12-row prewarm buffer on each side.
    assert (first, last) != (0, 0)
    assert first <= 248 and last >= 268
    assert 256 in range(first, last)


def test_range_tracks_scroll_position(qapp, monkeypatch):
    """Different scroll offsets must yield different windows — a range
    that ignores scroll is the same blindness in another costume."""
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": f"a{i}"} for i in range(314)])
    g.show()
    _pin_geometry(g, monkeypatch, scroll=0)
    top = g._visible_row_range()
    _pin_geometry(g, monkeypatch, scroll=15468)
    deep = g._visible_row_range()
    assert top[0] == 0
    assert deep[0] > top[1]  # windows don't overlap — it really moved


def test_overscroll_clamps_to_tail(qapp, monkeypatch):
    """Scrolled past the last row (over-scroll, or a stale offset after
    the model shrank): clamp to the final screenful instead of handing
    back an out-of-range window that loads nothing."""
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": f"a{i}"} for i in range(314)])
    g.show()
    _pin_geometry(g, monkeypatch, scroll=99999)

    first, last = g._visible_row_range()

    assert last == 314  # exclusive end == rowCount
    assert first < 314


def test_geometry_not_ready_stays_empty(qapp, monkeypatch):
    """Before first layout the cell height is unknown — return the empty
    range so callers retry, and NEVER (0, rc): treating unknown as 'all
    rows' fires a cover load for the whole library."""
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": f"a{i}"} for i in range(314)])
    g.show()
    _pin_geometry(g, monkeypatch, scroll=0, cell_h=0)

    assert g._visible_row_range() == (0, 0)


def test_alphabet_rail_shares_the_same_metrics(qapp, monkeypatch):
    """The rail highlight and the cover window read one helper, so they
    can't drift apart (they were duplicate implementations before)."""
    g = lg.LibraryGrid("album")
    g._model.set_items([{"Id": f"a{i}", "Name": f"N{i}"} for i in range(314)])
    cell_h, cols = g._cell_metrics()
    assert cols >= 1
    assert cell_h == g._view._tile_delegate.CELL_H


# ── 2026-08 pipeline optimization: pooled decode + bounded raw cache ─────────


def _jpeg_bytes(side: int) -> bytes:
    """An encoded image of a given pixel size (PNG — format is irrelevant,
    the decode path is the same; what matters is the decoded dimensions)."""
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtGui import QImage

    img = QImage(side, side, QImage.Format.Format_RGB32)
    img.fill(0x336699)
    # Hold a Python ref to the QByteArray: QBuffer does NOT own it, and a
    # temporary would be freed while the buffer still points at it.
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


class TestPooledDecode:
    def test_decode_and_scale_runs_without_a_gui_thread(self, qapp):
        """The pooled worker returns plain QImages (thread-safe); no
        QPixmap is constructed off the GUI thread."""
        from PySide6.QtGui import QImage

        res = uih._decode_and_scale(_jpeg_bytes(600), 180, 180)
        assert res is not None
        raw, scaled = res
        assert isinstance(raw, QImage) and isinstance(scaled, QImage)
        assert scaled.width() == 180 and scaled.height() == 180

    def test_undecodable_body_returns_none(self, qapp):
        """A server error page must read as a failure, not a blank cover."""
        assert uih._decode_and_scale(b"<html>500</html>", 180, 180) is None

    def test_oversized_source_is_capped_before_caching(self, qapp):
        """The raw kept for L2 is capped; the delivered target is not."""
        res = uih._decode_and_scale(_jpeg_bytes(uih._RAW_MAX_DIM * 2), 180, 180)
        assert res is not None
        raw, scaled = res
        assert max(raw.width(), raw.height()) == uih._RAW_MAX_DIM
        assert scaled.width() == 180  # target unaffected by the cap

    def test_source_within_cap_is_kept_whole(self, qapp):
        res = uih._decode_and_scale(_jpeg_bytes(600), 180, 180)
        raw, _ = res
        assert raw.width() == 600  # no needless rescale


class TestRawCacheBudget:
    def _fresh(self):
        uih._raw_image_cache.clear()
        uih._raw_cache_bytes = 0

    def test_budget_bounds_memory_not_entry_count(self, qapp, monkeypatch):
        """The regression that motivated this: 32 entries of a 3000px
        master is ~1.1 GB. Bytes, not entries, must be the bound."""
        from PySide6.QtGui import QImage

        self._fresh()
        monkeypatch.setattr(uih, "_RAW_CACHE_BUDGET_BYTES", 8 * 1024 * 1024)
        big = QImage(1024, 1024, QImage.Format.Format_ARGB32)  # 4 MB each
        big.fill(0)
        for i in range(10):
            uih._store_raw(f"sem{i}", big.copy())
        assert uih._raw_cache_bytes <= uih._RAW_CACHE_BUDGET_BYTES
        assert len(uih._raw_image_cache) < 10  # evicted well before 32
        self._fresh()

    def test_small_sources_still_get_many_slots(self, qapp):
        """Byte-budgeting must not punish the normal case — ordinary
        540px thumbnails should keep MORE entries resident than the old
        32-entry cap allowed."""
        from PySide6.QtGui import QImage

        self._fresh()
        small = QImage(540, 540, QImage.Format.Format_ARGB32)  # ~1.1 MB
        small.fill(0)
        for i in range(40):
            uih._store_raw(f"small{i}", small.copy())
        assert len(uih._raw_image_cache) >= 32
        assert uih._raw_cache_bytes <= uih._RAW_CACHE_BUDGET_BYTES
        self._fresh()

    def test_legacy_oversized_disk_raw_is_capped_on_store(self, qapp):
        """Raws read back from an older on-disk cache bypass the pooled
        pre-cap, so _store_raw guards too."""
        from PySide6.QtGui import QImage

        self._fresh()
        huge = QImage(uih._RAW_MAX_DIM * 2, uih._RAW_MAX_DIM * 2, QImage.Format.Format_ARGB32)
        huge.fill(0)
        uih._store_raw("legacy", huge)
        stored = uih._raw_image_cache["legacy"]
        assert max(stored.width(), stored.height()) == uih._RAW_MAX_DIM
        self._fresh()

    def test_accounting_survives_replacement(self, qapp):
        """Replacing a key with a bigger source must not double-count."""
        from PySide6.QtGui import QImage

        self._fresh()
        for side in (300, 600):
            img = QImage(side, side, QImage.Format.Format_ARGB32)
            img.fill(0)
            uih._store_raw("same", img)
        assert uih._raw_cache_bytes == 600 * 600 * 4
        self._fresh()

    def test_cap_covers_the_largest_consumer(self):
        """_RAW_MAX_DIM must stay above the biggest target any surface
        requests (mini player: 320 × 3 DPR = 960) — below it, that
        surface would miss this tier forever and refetch every time."""
        assert uih._RAW_MAX_DIM >= 960


# ── 2026-08: stale art after the server's cover changes ─────────────────────


class TestArtVersionKeying:
    """Covers were cached by item id, which does NOT change when the
    artwork behind it does — so re-tagging an album left the old cover on
    screen forever. Both providers hand us a version token in
    ImageTags.Primary (Jellyfin: a content hash; Navidrome: coverArt
    `al-<id>_<hash>`), which is now folded into the cache stem."""

    def test_stem_without_a_token_is_the_bare_id(self):
        assert uih.art_stem("alb1", "") == "alb1"
        assert uih.art_stem("alb1", "   ") == "alb1"

    def test_stem_folds_in_the_token(self):
        assert uih.art_stem("alb1", "tag9") == "alb1@tag9"

    def test_new_art_produces_a_different_stem(self):
        """The whole point: same album, new artwork → new cache identity."""
        before = uih.art_stem("alb1", "al-alb1_6894c3ee")
        after = uih.art_stem("alb1", "al-alb1_69ff0000")
        assert before != after

    def test_stale_pixmap_is_not_served_after_the_art_changes(self, qapp, monkeypatch):
        """End-to-end at the cache layer: a cover cached under the OLD
        token must not satisfy a request carrying the NEW one."""
        from PySide6.QtGui import QPixmap

        old_key = f"{uih.art_stem('alb1', 'tagOLD')}|albumtile|64x64|r=0"
        stale = QPixmap(64, 64)
        stale.fill()
        uih._image_cache[old_key] = stale
        served = []
        # A request under the NEW token must miss the memory tier and go
        # looking (disk/network), not hand back the stale pixmap.
        monkeypatch.setattr(uih, "_after_disk_miss", lambda *a, **k: served.append("miss"))
        import jellytoast.async_io as aio

        monkeypatch.setattr(
            aio,
            "run_async",
            lambda fn, *a, on_result=None, on_error=None, **k: (
                on_result(fn()) if on_result else fn()
            ),
        )
        monkeypatch.setattr(uih._disk_image_cache, "get_raw", lambda *a, **k: None)
        monkeypatch.setattr(uih._disk_image_cache, "get_image", lambda *a, **k: None)

        uih.load_image_async(
            f"{uih.art_stem('alb1', 'tagNEW')}|albumtile",
            "http://x/cover",
            64,
            64,
            lambda p: served.append("pixmap"),
        )
        assert served == ["miss"]  # refetched, not served stale
        uih._image_cache.clear()

    def test_np_stem_uses_image_id_and_tag(self, qapp):
        from jellytoast.player_state import NowPlaying

        np = NowPlaying(item_id="track1", image_id="album1", art_tag="tagX")
        assert uih.np_art_stem(np) == "album1@tagX"

    def test_np_stem_falls_back_to_item_id(self, qapp):
        from jellytoast.player_state import NowPlaying

        np = NowPlaying(item_id="track1")
        assert uih.np_art_stem(np) == "track1"

    def test_queue_manager_populates_the_art_tag(self, qapp, monkeypatch):
        """The player surfaces can only version their cache if the token
        survives into NowPlaying."""
        import jellytoast.providers as providers_mod
        from jellytoast.queue_manager import QueueManager

        class _P:
            kind = "fake"

            def get_audio_stream_url(self, i):
                return f"stream://{i}"

            def get_video_stream_url(self, i):
                return f"stream://{i}"

            def get_image_url(self, i, k="Primary", w=400):
                return f"img://{i}"

        monkeypatch.setattr(providers_mod, "_PROVIDER", _P())
        qm = QueueManager()
        np = qm._build_now_playing(
            {
                "Id": "trk1",
                "Name": "T",
                "Type": "Audio",
                "AlbumId": "alb1",
                "AlbumPrimaryImageTag": "al-alb1_deadbeef",
            }
        )
        assert np.art_tag == "al-alb1_deadbeef"
        assert uih.np_art_stem(np) == "alb1@al-alb1_deadbeef"

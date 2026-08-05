#!/usr/bin/env python3
"""Synthetic large-library cover-load stress harness.

Reproduces the first external user's bug class (Navidrome, ~5,200 albums /
580GB on Win11: covers "load a few then stop", "all disappear", reproduces
across restarts) WITHOUT a real 600GB server, so the fixes on
``fix/large-library-covers`` can be verified locally on Linux.

It drives a real ``LibraryGrid`` through the REAL ``ui_helpers`` fetch path
(disk tiers, the concurrency gate, the reply finish handler, the grid's
re-arm logic) but swaps QNAM for a fake that simulates a SLOW, LOSSY server:
each "cover" completes after an artificial latency, a small fraction fail.

It checks the three things the fixes are about:

  1. GATE — even when the whole library is requested at once, the app never
     has more than ``_GATED_MAX_INFLIGHT`` requests live at QNAM (the old
     code flooded QNAM's queue and the tail timed out → "loads a few then
     stops").
  2. NO PERMANENT STALL — every row that wasn't a simulated hard-failure
     ends up with a cover (re-arm reloads evicted/abandoned ones).
  3. NO WIPE / BOUNDED MEMORY — an AUTO offline flip preserves the populated
     grid, and the model's resident pixmap set stays bounded (no OOM).

Usage:  python dev/stress_large_library.py [N_ALBUMS] [LATENCY_MS] [FAIL_PCT]
        python dev/stress_large_library.py 5000 50 2
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import QApplication

random.seed(1234)  # deterministic run-to-run

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
LATENCY_MS = int(sys.argv[2]) if len(sys.argv) > 2 else 50
FAIL_PCT = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0

app = QApplication.instance() or QApplication(sys.argv)

from jellytoast import library_grid as lg  # noqa: E402
from jellytoast import ui_helpers as uih  # noqa: E402


def _png_bytes() -> QByteArray:
    img = QImage(8, 8, QImage.Format.Format_ARGB32)
    img.fill(0xFF335577)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return ba


_PNG = _png_bytes()

stats = {"live": 0, "peak": 0, "ok": 0, "fail": 0, "requested": 0}


class _FakeReply(QObject):
    """Minimal stand-in for QNetworkReply: emits ``finished`` after a
    simulated server latency; ``error()`` / ``readAll()`` mimic success or
    a transient failure so the real finish handler exercises both paths."""

    finished = Signal()

    def __init__(self, fail: bool):
        super().__init__()
        self._fail = fail

    def error(self):
        return (
            QNetworkReply.NetworkError.TimeoutError
            if self._fail
            else QNetworkReply.NetworkError.NoError
        )

    def readAll(self) -> QByteArray:
        return QByteArray() if self._fail else QByteArray(_PNG)

    def attribute(self, _attr):
        # HttpStatusCodeAttribute — the finish handler logs it on failure.
        return None if self._fail else 200

    def deleteLater(self):  # noqa: D401 - match QObject API used by the handler
        super().deleteLater()


class _FakeQNAM:
    def get(self, req):
        stats["requested"] += 1
        stats["live"] += 1
        stats["peak"] = max(stats["peak"], stats["live"])
        fail = random.random() < (FAIL_PCT / 100.0)
        reply = _FakeReply(fail)
        jitter = random.randint(0, LATENCY_MS)

        def _complete():
            stats["live"] -= 1
            stats["fail" if fail else "ok"] += 1
            reply.finished.emit()

        QTimer.singleShot(LATENCY_MS + jitter, _complete)
        return reply


def _spin(predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return predicate()


def main() -> int:
    # Route the real ui_helpers fetch path at our fake server; start clean.
    uih.get_qnam = lambda: _FakeQNAM()
    uih._image_cache.clear()
    uih._raw_image_cache.clear()
    # Force a COLD cache so every load goes through the gate → fake server
    # (otherwise a prior run's warm disk cache serves everything and the gate
    # isn't exercised). Stub the disk tier in-memory so we neither read a warm
    # cache nor pollute the user's real cover cache on disk.
    from jellytoast import image_cache as _ic

    _ic.get = lambda *a, **k: None
    _ic.get_image = lambda *a, **k: None
    _ic.get_raw = lambda *a, **k: None
    _ic.put = lambda *a, **k: None
    _ic.put_raw = lambda *a, **k: None
    uih._gated_in_flight = 0
    uih._deferred_normal.clear()
    uih._deferred_low.clear()
    uih._inflight_subscribers.clear()
    uih._pending_replies.clear()

    print(f"albums={N} latency~{LATENCY_MS}-{2*LATENCY_MS}ms fail={FAIL_PCT}% gate_cap={uih._GATED_MAX_INFLIGHT}")

    grid = lg.LibraryGrid("album")
    grid.api.get_image_url = lambda cid, *a, **k: f"http://fake.local/cover?id={cid}"
    items = [{"Id": f"alb{i}"} for i in range(N)]
    grid._model.set_items(items)

    # Record EVERY row that ever received a cover (cumulative): the model only
    # keeps the last 512 resident (LRU), so "no permanent stall" means every
    # row loaded at SOME point, not that 5000 are resident at once.
    ever_loaded: set = set()
    _orig_set_cover = grid._model.set_cover

    def _wrapped_set_cover(row, pix):
        ever_loaded.add(row)
        return _orig_set_cover(row, pix)

    grid._model.set_cover = _wrapped_set_cover

    # Flood: request EVERY row at once (the worst case the old code choked on —
    # an unbounded viewport burst). The gate must keep QNAM in-flight bounded;
    # the grid's own retry path heals the simulated failures over time.
    for row in range(N):
        grid._fire_cover_load(row, priority="normal")

    # Drain budget scales with the simulated work: N requests, ~1.5×LATENCY
    # each, GATED_MAX_INFLIGHT at a time, plus headroom for retries/healing.
    budget = max(30.0, N * (1.5 * LATENCY_MS / 1000.0) / uih._GATED_MAX_INFLIGHT * 2.0)
    drained = _spin(lambda: len(ever_loaded) >= N * 0.99, timeout_s=budget)
    grid._prefetch_timer.stop()

    resident = len(grid._model._covers)
    covered = len(ever_loaded)
    # Byte-budgeted LRU (audit #234 finding 9): derive the effective
    # entry cap from the budget and the rig's cover size.
    _one = next(iter(grid._model._covers.values()), None)
    _per = lg._LibraryItemsModel._pix_bytes(_one) if _one is not None else 1
    cap = max(1, lg._LibraryItemsModel._COVER_CACHE_BUDGET_BYTES // _per)

    # AUTO offline flip must preserve the populated grid (no wipe).
    from jellytoast import offline as _offline

    _orig_src = _offline.offline_source
    _offline.offline_source = lambda: "auto"
    reloaded = {"n": 0}
    grid.load_items = lambda *a, **k: reloaded.__setitem__("n", reloaded["n"] + 1)
    grid.isVisible = lambda: True
    grid._on_offline_mode_changed(True)
    _offline.offline_source = _orig_src

    print("─" * 56)
    print(f"requested QNAM gets : {stats['requested']}")
    print(f"peak concurrent     : {stats['peak']}  (gate cap {uih._GATED_MAX_INFLIGHT})")
    print(f"completed ok/fail   : {stats['ok']}/{stats['fail']}")
    print(f"model resident covs : {resident}  (LRU cap {cap})")
    print(f"rows ever covered   : {covered}/{N}  (cumulative; LRU keeps {cap})")
    print(f"auto-offline reloads: {reloaded['n']}  (expect 0 = grid preserved)")
    print("─" * 56)

    ok = True

    def check(label, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

    check("converged without hanging (≥99% rows covered)", drained)
    check(f"gate bounded QNAM in-flight ≤ {uih._GATED_MAX_INFLIGHT}", stats["peak"] <= uih._GATED_MAX_INFLIGHT)
    check("model resident covers stayed within the LRU cap", resident <= cap)
    check("every row loaded at some point (no permanent stall)", covered >= N * 0.99)
    check("auto-offline did NOT wipe/reload the populated grid", reloaded["n"] == 0)
    print("─" * 56)
    print("RESULT:", "PASS ✓" if ok else "FAIL ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

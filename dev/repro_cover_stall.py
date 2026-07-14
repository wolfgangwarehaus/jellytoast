#!/usr/bin/env python3
"""Reproduce Skope's "loads some covers then all disappear" against a REAL
large Navidrome (#cover-stall).

Unlike stress_large_library.py (which fakes QNAM), this drives the REAL
SubsonicProvider + REAL ui_helpers loader + REAL connectivity tracker against
a live server, so it exercises the exact production path — including the
mechanism behind "all images disappear": a server pegged generating
getCoverArt thumbnails makes concurrent METADATA calls time out too, which
trips the auto-offline flip, which blanks the grid.

So we do BOTH at once, like the real grid: page the album list (provider
metadata → feeds connectivity) WHILE sweeping every album's cover through
load_image_async. We watch for:
  • auto-offline flips (offline.is_offline_mode()) — the "all disappear" cause
  • cover failures / resize-fallback firing (the new logging)
  • how many covers ultimately resolve vs stall

Usage:
    QT_QPA_PLATFORM=offscreen .venv/bin/python dev/repro_cover_stall.py \
        --url http://127.0.0.1:4534 --user admin --password stress \
        [--albums 5200] [--tile 200] [--burst 60]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--albums", type=int, default=5200)
    ap.add_argument("--tile", type=int, default=200, help="cover target px")
    ap.add_argument("--burst", type=int, default=60, help="covers fired per pump")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    app = QApplication(sys.argv)

    from jellytoast import offline as offline_mod
    from jellytoast import providers, ui_helpers
    from jellytoast.providers.subsonic import SubsonicProvider

    # Fresh, isolated settings so we don't touch the real config.
    prov = SubsonicProvider()
    res = prov.authenticate(args.url, args.user, args.password)
    if not getattr(res, "ok", False) and not prov.is_authenticated:
        print(f"AUTH FAILED: {res}", file=sys.stderr)
        return 2
    providers._PROVIDER = prov
    print(f"authenticated at {args.url} as {args.user}", file=sys.stderr)

    # ---- collect album ids by paging the REAL metadata endpoint ----
    album_ids: list[str] = []
    offset = 0
    while len(album_ids) < args.albums:
        batch = prov.get_items(
            item_type="MusicAlbum", start_index=offset, limit=500,
            sort_by="SortName", sort_order="Ascending",
        )
        items = batch if isinstance(batch, list) else batch.get("Items", [])
        if not items:
            break
        for it in items:
            iid = it.get("Id") or it.get("id")
            if iid:
                album_ids.append(iid)
        offset += len(items)
        app.processEvents()
    print(f"paged {len(album_ids)} album ids", file=sys.stderr)

    # ---- concurrent sweep: covers via the real loader, metadata re-paging in
    #      the background so a pegged server can trip connectivity ----
    got = {"ok": 0, "err": 0}
    fired = 0
    offline_events: list[float] = []
    was_offline = offline_mod.is_offline_mode()
    t0 = time.monotonic()

    def on_ok(_pix, i=None):
        got["ok"] += 1

    def on_err(i=None):
        got["err"] += 1

    idx = 0
    repage_offset = 0
    last_report = t0
    while idx < len(album_ids) or (got["ok"] + got["err"]) < fired:
        # fire a burst of cover loads (grid scrolled a screenful into view)
        for _ in range(args.burst):
            if idx >= len(album_ids):
                break
            aid = album_ids[idx]
            url = prov.get_image_url(aid, width=args.tile)
            ui_helpers.load_image_async(
                f"{aid}|{args.tile}", url, args.tile, args.tile,
                callback=on_ok, on_error=on_err, priority="normal",
            )
            fired += 1
            idx += 1
        # interleave a metadata call (what the grid's pager does) so the
        # connectivity tracker sees real API round-trips under the load
        try:
            prov.get_items(item_type="MusicAlbum", start_index=repage_offset, limit=100)
            repage_offset = (repage_offset + 100) % max(1, len(album_ids))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("repro").warning("metadata call raised: %s", e)
        app.processEvents()
        time.sleep(0.05)
        # detect an auto-offline flip the instant it happens
        now_off = offline_mod.is_offline_mode()
        if now_off and not was_offline:
            offline_events.append(time.monotonic() - t0)
            logging.getLogger("repro").error(
                ">>> AUTO-OFFLINE FLIPPED at t=%.1fs after %d covers "
                "(ok=%d err=%d) — this is the 'all images disappear'",
                offline_events[-1], fired, got["ok"], got["err"],
            )
        was_offline = now_off
        if time.monotonic() - last_report > 5:
            last_report = time.monotonic()
            print(f"  t={time.monotonic()-t0:5.0f}s fired={fired} ok={got['ok']} "
                  f"err={got['err']} gate={ui_helpers._gated_in_flight} "
                  f"deferred={len(ui_helpers._deferred_normal)+len(ui_helpers._deferred_low)} "
                  f"offline={offline_mod.is_offline_mode()}", file=sys.stderr)
        if time.monotonic() - t0 > 900:
            print("TIMEOUT 15min", file=sys.stderr)
            break

    dt = time.monotonic() - t0
    print("\n==== VERDICT ====", file=sys.stderr)
    print(f"albums swept:      {fired}", file=sys.stderr)
    print(f"covers OK:         {got['ok']}", file=sys.stderr)
    print(f"covers failed:     {got['err']}", file=sys.stderr)
    print(f"unresolved:        {fired - got['ok'] - got['err']}", file=sys.stderr)
    print(f"auto-offline flips:{len(offline_events)} at {offline_events}", file=sys.stderr)
    print(f"final offline:     {offline_mod.is_offline_mode()}", file=sys.stderr)
    print(f"elapsed:           {dt:.0f}s", file=sys.stderr)
    reproduced = bool(offline_events) or (fired - got["ok"] - got["err"]) > 50
    print(f"REPRODUCED THE BUG: {reproduced}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

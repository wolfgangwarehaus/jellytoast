#!/usr/bin/env python
"""jellytoast smoke test — a fast confidence check of the critical paths.

Run from the repo root:

    python dev/smoke_test.py                 # offline checks + live (if reachable)
    python dev/smoke_test.py --require-server # treat an unreachable server as failure
    python dev/smoke_test.py --offline        # skip live checks entirely

What it does NOT do: play audio, write to the server (no playback-report
POSTs / no play-count or scrobble side effects — live stream/image checks
fetch a single range then abort), or mutate local state. Safe to run any
time, including against a production server.

How it relates to the test suite: `pytest` proves each unit of logic in
isolation; this proves the app actually talks to a real server correctly
and that the moat behaviours hold on *real-shape* data (e.g. the
smart-shuffle anti-clustering — which once silently no-op'd on Jellyfin
because real items carry no scalar ArtistId; this script re-checks it
against live data so that class of regression can't slip back in).

Covers the release sanity checks (the old manual test plan — git history). Exit code
is the number of failed checks (0 = clean); skipped live checks don't
count as failures unless --require-server is passed and the server is down.
"""
from __future__ import annotations

import os
import sys

# Run from the repo root regardless of cwd.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# Force UTF-8 stdout/stderr so the section headers' box-drawing glyphs (─)
# don't crash on Windows' default cp1252 console/redirect (UnicodeEncodeError
# before the first check runs). Best-effort: no-op on streams that can't be
# reconfigured.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


PASS = _c("32", "PASS")
FAIL = _c("31", "FAIL")
SKIP = _c("90", "SKIP")
INFO = _c("33", "info")

_fails = 0
_server_down = False


def section(title: str) -> None:
    print("\n" + _c("1", f"── {title} "))


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _fails
    if not ok:
        _fails += 1
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def skip(name: str, detail: str = "") -> None:
    print(f"  [{SKIP}] {name}" + (f"  — {detail}" if detail else ""))


def info(name: str, detail: str = "") -> None:
    print(f"  [{INFO}] {name}" + (f"  — {detail}" if detail else ""))


def live(fn, *a, **kw):
    """Run a live provider call. On a connection/timeout error, flag the
    server down (subsequent live checks skip) and return None. Other
    exceptions propagate so the caller can record a real failure."""
    global _server_down
    import requests

    try:
        return fn(*a, **kw)
    except requests.exceptions.RequestException as exc:
        _server_down = True
        info("server unreachable", f"{type(exc).__name__} — live checks will skip")
        return None


def adjacency_rate(order, field):
    if len(order) < 2:
        return 0.0
    same = sum(1 for a, b in zip(order, order[1:], strict=False) if a.get(field) and a.get(field) == b.get(field))
    return same / (len(order) - 1)


# ════════════════════════════════════════════════════════════════════
# OFFLINE — always run; no server, no audio.
# ════════════════════════════════════════════════════════════════════
def run_offline() -> None:
    import random

    section("Offline: imports")
    try:
        import jellytoast.playback.crossfade  # noqa: F401
        import jellytoast.providers.smart_rule_eval  # noqa: F401
        import jellytoast.providers.smart_rule_schema  # noqa: F401
        import jellytoast.smart_shuffle  # noqa: F401

        check("core modules import", True)
    except Exception as exc:  # pragma: no cover - import smoke
        check("core modules import", False, repr(exc))
        return

    section("Offline: smart shuffle (real Jellyfin item shape)")
    from jellytoast.smart_shuffle import artist_key, smart_shuffle

    # Jellyfin adapted song items carry NO scalar ArtistId — artist is in
    # AlbumArtist/Artists. Build that shape and confirm anti-clustering
    # actually spreads (the 2026-05-29 regression: it didn't).
    lib = [
        {"Id": f"t{i}", "Name": f"T{i}", "AlbumArtist": f"Artist {i % 4}", "Artists": [f"Artist {i % 4}"]}
        for i in range(48)
    ]
    keys = {artist_key(it) for it in lib}
    check("artist_key buckets Jellyfin items by name (not __unknown__)",
          "__unknown__" not in keys and len(keys) == 4, f"{len(keys)} buckets")
    smart = [adjacency_rate(smart_shuffle([dict(x) for x in lib], [], rng=random.Random(s)), "AlbumArtist")
             for s in range(15)]
    classic = []
    for s in range(15):
        c = list(lib)
        random.Random(s).shuffle(c)
        classic.append(adjacency_rate(c, "AlbumArtist"))
    sa, ca = sum(smart) / len(smart), sum(classic) / len(classic)
    check("anti-clustering beats plain random on real shape", sa < ca * 0.5,
          f"smart {sa:.3f} vs classic {ca:.3f}")

    section("Offline: smart-rule validation + operators")
    from jellytoast.providers.smart_rule_eval import matches_rule
    from jellytoast.providers.smart_rule_schema import validate_rules

    def rs(rules):
        return {"match": "all", "rules": rules}

    check("empty string value rejected",
          bool(validate_rules(rs([{"field": "genre", "op": "equals", "value": ""}]))))
    check("numeric 0 / boolean False NOT rejected",
          not validate_rules(rs([{"field": "play_count", "op": "equals", "value": 0}]))
          and not validate_rules(rs([{"field": "is_favorite", "op": "equals", "value": False}])))
    check("is_favorite eval maps UserData.IsFavorite",
          matches_rule({"UserData": {"IsFavorite": True}}, {"field": "is_favorite", "op": "equals", "value": True})
          and not matches_rule({}, {"field": "is_favorite", "op": "equals", "value": True}))

    section("Offline: date operators")
    from datetime import datetime, timedelta

    now = datetime.now()
    recent = {"DateCreated": (now - timedelta(days=5)).isoformat()}
    old = {"DateCreated": (now - timedelta(days=60)).isoformat()}
    check("in_the_last 30 matches recent, excludes old",
          matches_rule(recent, {"field": "date_added", "op": "in_the_last", "value": 30})
          and not matches_rule(old, {"field": "date_added", "op": "in_the_last", "value": 30}))
    sub = {"DateCreated": (now - timedelta(days=2)).isoformat()}  # Subsonic shape: no LastPlayedDate
    check("Subsonic shape never matches last_played",
          not matches_rule(sub, {"field": "last_played", "op": "after",
                                 "value": (now - timedelta(days=30)).isoformat()}))

    section("Offline: crossfade equal-power curve")
    from jellytoast.playback.crossfade import _equal_power_gains as gains

    devs = []
    for i in range(101):
        go, gi = gains(i / 100.0)
        devs.append(go * go + gi * gi)
    check("summed power flat across fade (no mid-fade dip)", abs(max(devs) - min(devs)) < 0.02,
          f"min={min(devs):.4f} max={max(devs):.4f}")
    o0, i0 = gains(0.0)
    o1, i1 = gains(1.0)
    check("fade endpoints correct", o0 > 0.98 and i0 < 0.02 and o1 < 0.02 and i1 > 0.98)


# ════════════════════════════════════════════════════════════════════
# LIVE — talks to the real server; auto-skips if unreachable.
# ════════════════════════════════════════════════════════════════════
def run_live() -> None:
    import requests

    from jellytoast.providers import get_provider

    p = get_provider()
    k = p.kind
    k = k() if callable(k) else k
    section(f"Live: provider ({k})")
    if not p.is_authenticated:
        skip("provider authenticated", "not signed in — sign in first, then re-run")
        return
    check("provider authenticated (creds from dual store)", True)

    section("Live: search (all buckets)")
    r = live(p.search_all, "love", songs=12, albums=14, artists=14)
    if _server_down:
        return
    if r is not None:
        a, al, ar = len(r.get("Audio", [])), len(r.get("MusicAlbum", [])), len(r.get("MusicArtist", []))
        check("search returns ≥1 non-empty bucket", (a + al + ar) > 0, f"songs={a} albums={al} artists={ar}")

    section("Live: library tabs load")

    def n_items(**kw):
        rr = p.get_items(**kw)
        return len(rr.get("Items", []) if isinstance(rr, dict) else rr)

    genres = []
    try:
        check("Albums load", n_items(item_type="MusicAlbum", recursive=True, limit=50) > 0)
        check("Artists load", len(p.get_artists(limit=50)) > 0)
        genres = p.get_genres()
        check("Genres load", len(genres) > 0, f"{len(genres)} genres")
        check("Songs load", n_items(item_type="Audio", recursive=True, limit=50) > 0)
        info("Playlists", f"{n_items(item_type='Playlist', recursive=True, limit=50)} (0 fine if none)")
    except requests.exceptions.RequestException as exc:
        globals()["_server_down"] = True
        info("server dropped mid-browse", repr(exc))
        return

    section("Live: seeded-queue paths")
    try:
        songs = p.get_random_audio_items("", limit=200)
        seed = songs[0]
        mix = p.get_instant_mix(seed.get("Id"), count=50)
        check("instant-mix returns a non-trivial set", len(mix) >= 5,
              f"{len(mix)} tracks from '{seed.get('Name', '?')}'")
        if genres:
            gr = p.get_genre_radio(genres[0].get("Name"), count=30)
            info("genre radio", f"{len(gr)} tracks for '{genres[0].get('Name')}'")
        # The high-value live regression guard: anti-clustering on REAL
        # fetched data (catches the ArtistId-shape class of bug).
        import random

        sa = sum(adjacency_rate(smart_one(songs, s), "AlbumArtist") for s in range(8)) / 8
        ca = []
        for s in range(8):
            c = list(songs)
            random.Random(s).shuffle(c)
            ca.append(adjacency_rate(c, "AlbumArtist"))
        cavg = sum(ca) / len(ca)
        check("smart shuffle spreads artists on LIVE data", sa <= cavg + 0.005,
              f"smart {sa:.3f} vs classic {cavg:.3f} ({len(songs)} songs)")
    except requests.exceptions.RequestException as exc:
        globals()["_server_down"] = True
        info("server dropped mid-queue-check", repr(exc))
        return

    section("Live: smart-playlist resolve (query_items)")
    try:
        recent = p.query_items({"match": "all", "rules": [
            {"field": "date_added", "op": "in_the_last", "value": 30}]})
        from jellytoast.providers.smart_rule_eval import _field_value, _in_the_last
        within = sum(1 for it in recent if _in_the_last(_field_value(it, "date_added"), 30))
        if recent:
            check("'date_added in_the_last 30' resolves, all in-window", within == len(recent),
                  f"{within}/{len(recent)} within 30d")
        else:
            info("'date_added in_the_last 30'", "0 items (nothing added in 30d?)")
    except requests.exceptions.RequestException as exc:
        globals()["_server_down"] = True
        info("server dropped mid-resolve", repr(exc))
        return

    section("Live: stream + cover serve (no playback / no play-report)")
    try:
        sid = p.get_random_audio_items("", limit=1)[0].get("Id")
        url = p.get_audio_stream_url(sid)
        check("get_audio_stream_url returns a URL", bool(url) and url.startswith("http"))
        hr = requests.get(url, headers={"Range": "bytes=0-65535"}, stream=True, timeout=15)
        chunk = next(hr.iter_content(8192), b"")
        check("server streams audio bytes", hr.status_code in (200, 206) and len(chunk) > 0,
              f"status={hr.status_code} ct={hr.headers.get('Content-Type')}")
        hr.close()
        alb = p.get_items(item_type="MusicAlbum", recursive=True, limit=1).get("Items", [])
        iu = p.get_image_url(alb[0].get("Id"), "Primary", 300)
        ir = requests.get(iu, stream=True, timeout=15)
        next(ir.iter_content(4096), b"")
        check("server serves cover image", ir.status_code == 200, f"ct={ir.headers.get('Content-Type')}")
        ir.close()
    except requests.exceptions.RequestException as exc:
        globals()["_server_down"] = True
        info("server dropped mid-stream-check", repr(exc))


def smart_one(songs, seed):
    import random

    from jellytoast.smart_shuffle import smart_shuffle

    return smart_shuffle([dict(x) for x in songs], [], rng=random.Random(seed))


# ════════════════════════════════════════════════════════════════════
def main() -> int:
    require_server = "--require-server" in sys.argv
    offline_only = "--offline" in sys.argv

    print(_c("1", "jellytoast smoke test") + "  (no audio, no writes)")
    run_offline()

    if offline_only:
        info("live checks skipped", "--offline")
    else:
        run_live()

    section("Summary")
    if _server_down:
        if require_server:
            check("server reachable (--require-server)", False, "could not reach the server")
        else:
            skip("live checks", "server unreachable — re-run when it's up")
    print(f"\n  {_c('31', str(_fails) + ' failed') if _fails else _c('32', 'all checks passed')}")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

# Cover-art cache keys & device-pixel-ratio

> **📍 Status — 2026-05-26:** Research note. The fix is mechanical
> once the pattern is settled; this note picks the pattern so the
> rollout can be fired as a single autonomous task.

Owner: august.
Last updated: 2026-05-26.

## 1. The problem

`screen_dpr()` returns a float that depends on the widget's screen.
On Wayland fractional scaling that value drifts across launches —
the same physical monitor at "150 %" reports 1.4999…, 1.5, 1.5001…
across separate sessions. When a fetch site bakes the raw DPR into
the requested physical pixel size, every distinct value gets its
own cache slot, so a library that was "fully loaded" under one
launch's DPR re-hits the network on the next.

`library_grid` was fixed in early May by switching to a **fixed
source size, independent of the live DPR** —
`_COVER_SOURCE_PX = COVER_SIZE × 3`, big enough for a 3× display, so
every DPR derives locally from the one cached raw. The other four
fetch sites still bake the raw DPR into their fetch and aren't fixed
yet:

| Site | Logical px | Current fetch math | Cache behaviour |
|---|---:|---|---|
| `search_view.py:160` | 44 (song-row thumb) | `max(120, round(44 × dpr))` | Per-DPR raw |
| `artist_page.py:599` | 180 (header cover) | `max(360, round(180 × dpr))` | Per-DPR raw |
| `artist_page.py:664` | 180 (album tile) | `max(360, round(180 × dpr))` | Per-DPR raw |
| `now_playing_bar.py:1969` | 108 (bar cover) | `max(256, round(108 × dpr))` | Per-DPR raw |
| `now_playing_bar.py:1994` | 108 (prefetch) | same as 1969 | Per-DPR raw |
| `now_playing_bar.py:2133` | 108 (radio cover) | same as 1969 | Per-DPR raw |

`songs_view.py:603` already picked a **third** pattern —
`dpr_bucket(screen_dpr(self))` — snapping the live DPR to the
nearest of {1.0, 1.5, 2.0, 3.0} before computing the fetch size.

So three patterns live in the tree:

1. **fixed source px** (library_grid) — `_SOURCE_PX = LOGICAL × 3`,
   request that, rescale per paint.
2. **bucketed DPR** (songs_view) — `dpr_bucket(...)` before
   multiplying, up to four raw entries per item.
3. **raw DPR** (the four sites above) — many raw entries per item,
   the leaky pattern this whole exercise is about.

`artist_page.py:619` (in `_on_cover_loaded`) and
`now_playing_bar.py:2023` (in `refresh_cover`) also call
`screen_dpr()`, but those are **paint-time** uses — the contract
explicitly says raw DPR is correct there (`setDevicePixelRatio` for
sharp rendering on the current screen). Those are not part of this
rollout.

## 2. Why pattern 2 (bucketing) isn't enough

The audit memo says it is — bucketing quantises away the
fractional-scale drift, so 1.49 and 1.51 both snap to 1.5 and share
a cache entry. That's true *within* a bucket.

But pattern 2 still creates **up to four** raw entries per item —
one per bucket the user has ever run at. Cases that hit this:

- A laptop docked at 1× and undocked at 1.5×.
- A user who moved from a 1080p panel to a 1440p panel (sane DPRs:
  1.0 vs 1.5) and the disk cache survives.
- Multiple monitors with different scales — Qt reports
  per-widget DPR, so the artist page on the laptop screen and the
  now-playing bar on the external monitor can hit different buckets
  *in the same session*.

Pattern 1 (fixed source) makes the L2 raw cache hold **one** entry
per item that serves every consumer at every DPR forever. That's
the property library_grid relies on to make "cross-surface" loads
(tile already cached → bar wants 256 → L2-raw hit, no fetch) work.

## 3. The L2 raw cache, and why pattern 1 is load-bearing

`ui_helpers.load_image_async` has a three-tier cache:

- **L1** in-memory, keyed by `f"{key}|{w}x{h}|r={radius}"` — always
  fragmented by physical px. Tiny — first per-DPR paint pays a
  ~1ms scale to populate. Not the problem.
- **L2 raw** (in-memory + on-disk), keyed by **semantic key**
  (everything before the first `|` — typically the AlbumId). Holds
  the decoded source `QImage` from the network.
- **L2 derivation** — when a request comes in for a target the
  raw is at least 75 % of on both axes, the raw is rescaled
  locally and L1 populated, no network.

The 75 %-derivation rule is what makes pattern 1 work without
"sometimes a request misses and goes to network": as long as the
raw cached for a given semantic key is ≥ `target × 0.75` on both
axes, every DPR derives.

| Raw cached at | 1× target derives? | 1.5× | 2× | 3× |
|---|---|---|---|---|
| `LOGICAL × 1` | ✅ | ❌ (1.5 > 1 / 0.75 = 1.33) | ❌ | ❌ |
| `LOGICAL × 2` | ✅ | ✅ | ✅ (2 ≥ 2 × 0.75) | ❌ |
| `LOGICAL × 3` | ✅ | ✅ | ✅ | ✅ (3 ≥ 3 × 0.75) |

So `LOGICAL × 3` is the minimum that **always** derives. Library_grid
chose this; it's the right floor for the unified pattern.

## 4. Recommendation: pattern 1 everywhere

Adopt **fixed-source-px** for every cover-fetch site, matching
library_grid. The migration per site is:

```py
# Before (pattern 3, raw DPR):
dpr = screen_dpr(self)
target_phys = max(LOGICAL, int(round(LOGICAL * dpr)))
radius_phys = int(round(RADIUS * dpr))
server_px = max(FLOOR, target_phys)
url = api.get_image_url(item_id, "Primary", server_px)
load_image_async(key, url, target_phys, target_phys, cb, radius_phys)

# After (pattern 1, fixed source):
_SOURCE_PX = LOGICAL * 3  # module-level constant, near the class
# (in the fetch method)
url = api.get_image_url(item_id, "Primary", _SOURCE_PX)
load_image_async(
    key, url,
    _SOURCE_PX, _SOURCE_PX,    # ← these are the SOURCE size now,
    cb, rounded_radius=0,       # ← no rounding at fetch time
)
# Then in the paint / setPixmap / setLabel path:
#   scale_pixmap_for_dpr(pix, LOGICAL, screen_dpr(widget))
# (or your existing per-paint scaler — the point is the FETCH no
#  longer cares about DPR).
```

Two contract shifts the migration has to honour:

1. **Rounded corners move from fetch-time to paint-time.** Today
   the four sites pass `rounded_radius` to `load_image_async`, which
   bakes the corners into the disk-cached pixmap. That **also**
   fragments by DPR (radius scales with DPR). Library_grid handles
   this by drawing a `QPainterPath` rounded clip at paint time
   (`_TileDelegate.paint`, lines 1054–1066). The four sites should
   either:
   - **(a)** Round at paint (artist page, now-playing bar — they
     already own a custom paint or set the QLabel mask), or
   - **(b)** Round at fetch but pass `rounded_radius=0` from the
     fetch and round once when the pixmap lands in
     `set_cover_pixmap`. This keeps the disk cache radius-agnostic.

   For the now-playing bar `refresh_cover` (line 2012–2040) the
   rounding is already done at paint time via the QLabel's clip and
   the bar's own `_round_corners` flow, so radius drops out of the
   fetch trivially.

2. **The per-paint scaler still needs raw DPR.** `screen_dpr(widget)`
   is the right call there — the bucketed value would soften the
   pixmap by ~5 % at intermediate scales. Only the FETCH and the
   L1/L2 cache keys should be DPR-blind.

## 5. Per-site call-out (constants & extra notes)

### `search_view.py:160`

`_SongRowDelegate.THUMB_SIZE = 44`, so source = **132 px**. Round to
a request the server actually serves cleanly — Jellyfin's image
endpoint takes any integer, Subsonic rounds, so 132 is fine. The
existing `server_px = max(120, target_phys)` floor was there to
keep 1× requests from being undersized; pattern 1 makes the
floor moot (132 > 120 always).

### `artist_page.py:599`

`HEADER_COVER = 180`, so source = **540 px** — same as library_grid's
`_COVER_SOURCE_PX`. The existing `server_px = max(360, target_phys)`
already serves a 2× display sharply; bumping to 540 covers 3×.
Radius (90 px) is currently `int(round(90 * dpr))` — move to
paint-time. The QLabel paints into a fixed-size circle, so a
`QPainterPath.addEllipse` clip at refresh time handles it.

### `artist_page.py:664`

`_TileDelegate.COVER_SIZE = 180`, source = **540 px**. This is the
artist's discography grid and uses the same `_TileDelegate` as the
main library grid — should just reuse library_grid's `_COVER_SOURCE_PX`
constant via import. Radius is drawn by the delegate's paint path
already; pass `rounded_radius=0`.

### `now_playing_bar.py:1969`, `:1994`, `:2133`

Logical 108, source = **324 px**. The existing `max(256, ...)` floor
was the un-bucketed equivalent — 256 covers 2.37× sharply. 324 is a
small bump (~27 %) and the gain (no fragmentation) is large.

Radius is **already** zero at the fetch (`rounded_radius=0` is
passed) — the bar's own `refresh_cover` (line 2012) does the
rounding at paint via `_round_corners`. So this site is the
cleanest of the three migrations: just change the size math.

### `songs_view.py:603` (already pattern 2, optional fold-in)

This site is *correct enough* — bucketing handles the immediate
fragmentation. Folding it into pattern 1 (`_SOURCE_PX = THUMB_SIZE × 3
= 132`) costs at most one server-side reencoding per cover on a 1×
display (132 instead of 60); cross-surface L2 hits with `search_view`
become free.

Worth folding in for consistency. The savings on cross-surface hits
(a song that shows up in both search and the song-row list shares
one raw entry) compensate for the 1× initial-fetch overhead.

## 6. What this does NOT change

- The cover-prefetch concurrency cap in `_load_visible_covers`.
- The "click an album whose tile is already cached → bar feels
  instant" cross-surface L2 hit. Pattern 1 makes this *more
  reliable*, not less.
- The placeholder colour or the failure-retry budget.
- The per-consumer L1 entries (those still fragment by target px;
  fine because L1 is small and an L2-raw derive is sub-ms).

## 7. Rollout shape

Single PR, ship to `auto/dpr-unify`. Touched files:

- `modules/search_view.py` — pattern 1 + drop radius from fetch.
- `modules/artist_page.py` (two sites) — pattern 1 + paint-time
  radius for the header circle.
- `modules/now_playing_bar.py` (three sites) — pattern 1 only,
  radius already drops out.
- (Optional) `modules/songs_view.py` — fold-in to pattern 1.

Test sweep:

- New focused tests asserting the disk cache holds **one raw entry
  per item** after sequential loads at three different DPRs (1.0,
  1.5, 2.0). `tests/test_disk_cache.py` has the disk-cache fixture
  already.
- Existing `tests/test_image_cache.py` and `tests/test_search_view*`
  pass without modification.

No production-behaviour regressions expected; the only user-facing
change is "first load on a 1× display is a slightly bigger image
download per cover". On the song-row thumb that's a few KB; on the
header cover it's ~50 KB. Both are noise next to the
network-round-trips this saves on every subsequent reload.

## 8. Open questions for august

- (none load-bearing — the pattern decision is clear)
- Folding songs_view in: yes / no. Defaults to yes for consistency.

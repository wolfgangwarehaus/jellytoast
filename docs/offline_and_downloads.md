# Offline & Downloads — Research & Design

> **📍 Status — 2026-05-20:** Shipped. Offline mode and explicit
> downloads landed across Phases 5–6 (2026-05-15 → 2026-05-18),
> including the progress UI and standalone Downloads page. Kept for
> rationale — see `docs/SPEC.md` §5 and `CHANGELOG.md`.

> **As-built (2026-06-08):** all phases below have shipped — the body
> from here on is the original design narrative, kept for rationale.
> Phase 1 scaffolded `modules/offline/`; Phase 2 wired a single-track
> download end to end; Phase 3 added album/playlist/artist cascade, the
> node-graph queue, completion roll-up, cascade deletion, the grid/track
> context-menu triggers, and the "Downloads" page; Phase 4 made
> `QueueManager` prefer a downloaded local blob over the server stream
> (gated by offline mode / reachability / `prefer_server_when_online`)
> and added `NowPlaying.is_local`. **Phases 5–6 also landed**
> (connectivity state machine, the offline chip, retry-backoff,
> index-repair walk, Wi-Fi-only gating); Phase 7 (transient
> cache-on-play) remains the only un-started follow-on. One spec change
> since: the **"Automatic offline mode" toggle was dropped in #55** —
> auto-degrade on a confirmed outage is now unconditional (see the §5.5
> / §7 / Phase 5 notes below).

> Scope note: this started as a "caching" doc. The actual goal is **explicit
> downloads** ("make this available offline") and **fully-local playback** —
> the app working with the server unreachable. Transient cache-on-play is a
> secondary, related concern and is kept as a §10 follow-on, not the headline.

## 1. Goal

Let the user deliberately take music with them. Pick an album / playlist /
artist / track → "Download" → it plays later with the server off entirely
(plane, dead Wi-Fi, server down for maintenance). Plus an **Offline mode** that
filters the UI to just what's downloaded so you can't accidentally stream.

## 2. Offline has three legs, not one

"Download a song" sounds like one problem (get the bytes). It is three:

1. **Audio bytes** — the actual file. jellytoast persists *nothing* today.
2. **Metadata** — track lists, album/artist/playlist info, durations, track
   numbers. The UI cannot render a library it can't describe. Today this is
   *partly* covered by accident (see §3).
3. **Album art** — covers. Today this *already works offline* — `image_cache.py`
   keeps decoded covers on disk and the loader checks disk before network.

Plus a cross-cutting fourth: **connectivity awareness** — knowing the server is
unreachable and degrading gracefully instead of throwing, and an explicit
offline-mode toggle.

## 3. Current state (codebase audit)

> **As-built note:** the table below is the *pre-build* audit (the
> greenfield baseline this design started from). Every "No" in it has
> since shipped — audio plays from a downloaded local blob
> (`queue_manager.py` `_audio_stream_url`), offline mode exists
> (`offline.set_offline_mode` / `is_offline_mode`), and artist/album/track
> detail pages fall back to `downloads.db` snapshots (`artist_page.py`).
> Kept verbatim for the rationale; read it as "where we were", not "where
> we are".

| Leg | Today | Works offline? |
|-----|-------|----------------|
| **Audio** | streamed live via mpv from `get_audio_stream_url()`; nothing on disk | **No** — playback is 100% network-dependent |
| **Browse metadata** | `disk_cache.py` persists view payloads (albums/songs/playlists grids) as JSON, keyed by view+scope+server-identity, **no TTL**; `library_grid.py` renders from it instantly then refreshes in background | **Partly** — a grid you've browsed renders offline; one you haven't is empty |
| **Item metadata** | `jellyfin_api.py` has a 512-entry **in-memory** LRU for `get_item`/`get_album_tracks` — lost on restart. **Artist detail pages have no cache at all** → "Couldn't load artist" offline | **No** for detail pages |
| **Album art** | `image_cache.py` — 3-tier (mem/disk-raw/disk-pixmap), disk survives restarts, loader checks disk before net | **Yes** |
| **Queue** | persisted to `queue.json` (v2 schema, full state + resume position) | State survives, but every item points to a network `stream_url` that 404s offline |
| **Connectivity** | `verify_session()` treats network errors as "creds still good" (won't bounce to login on a blip). No persistent-offline detection, no offline-mode concept, no UI indicator. View errors handled inconsistently — grids fail silent, artist pages show an error | **No offline concept exists** |

Takeaway: art is solved, browse-metadata is *accidentally* half-solved, and
**audio + authoritative metadata + offline-mode are greenfield.** The
`disk_cache.py` browse cache is a *convenience* cache — it is not an
authoritative record of "what I downloaded" and must not be conflated with one.

## 4. How peers do it

The relevant peer group is self-hosted multi-provider players.

### Finamp — learn from their rewrite

Finamp publishes a `DOWNLOADS_PLAN.md` — and it's largely a post-mortem of their
*original* downloads architecture. Their stated mistakes are our spec:

- **Don't spread state across many key-value stores.** Finamp tracks downloads
  across *five* Hive boxes that must be hand-kept in sync (`DownloadedItems`,
  `DownloadedParents`, `DownloadIds`, `DownloadedImages`, `DownloadedImageIds`).
  → **Use one relational store (SQLite) with real foreign keys.**
- **Don't store absolute paths.** Theirs break on iOS because the app's
  container directory changes on every update. → **Store paths relative to a
  base dir resolved at runtime.**
- **Don't hardcode parent types.** Their model has no clean way to add
  downloaded *artists* or *genres*. → **Generic node model**: a downloadable
  node + parent/child links, not an album-table and a playlist-table.
- They sync favourite status in the background (toggleable), and ship a
  **"repair"** action (rebuild the download index, re-link orphans) — both are
  features we'll want once the index can drift from disk.

### Symfonium — the settings surface to copy

Symfonium's Offline/Cache/Download settings, condensed (this is our menu):

- **Cache storage location** — selectable (SD card etc.)
- **Rolling cache size** — auto-evict oldest beyond N; coexists with permanent
- **Automatic offline mode** — auto-switch to offline filter when disconnected
- **Wi-Fi-only downloads** — block downloads on metered connections
- **Download quality** — transcode profile for downloaded media, separate from
  stream quality; favourites can pin at original quality
- **Prefer server version on Wi-Fi** — stream when connected, use the offline
  copy only when needed
- **Pre-cache count** — separate numbers for Wi-Fi vs mobile
- **Persistent image cache** — keep covers out of OS cleanup's reach

### Plexamp

- Cache-on-play "forever, like a download" (the §10 follow-on tier).
- Explicit album/playlist downloads as FLAC.
- Stream quality independent from stored quality.

## 5. Design

### 5.1 One SQLite store, generic node graph

A single `downloads.db` (SQLite). Core tables:

```
nodes        — one row per downloadable thing the user touched:
               id (provider_identity + item_id), kind (track|album|
               artist|playlist), metadata_json (snapshot, see 5.2),
               state (pending|downloading|complete|failed|stale),
               added_at, requested (bool — user explicitly asked vs.
               pulled in as a child)

edges        — parent_id -> child_id (album->track, playlist->track,
               artist->album). Makes cascade + orphan-cleanup a graph
               walk, not special-cased per type.

blobs        — node_id (track) -> relative_path, quality, codec,
               bytes, sha (optional), downloaded_at. Audio files only.
```

Why this shape: it's the direct fix for every Finamp mistake — one store, FKs,
relative paths in `blobs`, and `kind` is data so "downloaded artists/genres"
needs no schema change. A track downloaded as part of two playlists is **one**
`blobs` row with two `edges` — refcount = incoming edges, so deletion is
"delete node, delete now-orphaned children".

### 5.2 A download is a *snapshot*

When the user downloads an album, we persist, atomically:

1. the **audio blob** for each track (background HTTP GET — see 5.3),
2. a **metadata snapshot** — the item dicts (album + every track: title,
   artist, duration, track no., disc, IDs) frozen into `nodes.metadata_json`,
3. the **cover** — already handled by `image_cache.py`; downloads just *pin*
   the relevant `image_id`s so eviction can't reclaim them.

This metadata snapshot is the **authoritative offline record** — independent of
`disk_cache.py`'s browse cache, which can be cleared or scoped away at any time.
Offline-mode views read from `downloads.db`, not the browse cache.

### 5.3 Getting the bytes

**Independent background HTTP GET** of `get_audio_stream_url(item_id)` (at the
configured *download* quality) to a `.part` temp file, atomic-rename into the
blob store on completion. Decoupled from mpv playback entirely — pause/seek/skip
can't corrupt it; an interrupted download just leaves a `.part` to discard or
resume. Runs on the existing `async_io` bounded thread pool. (mpv's
`--stream-record` was considered and rejected — it corrupts the file on seek
and only captures from the demuxer position at the moment it's enabled.) A
download **manager** sits on top: a queue with progress, pause/resume, retry,
Wi-Fi-only gating, and per-node state written back to `nodes.state`.

### 5.4 Playback path

`QueueManager._build_now_playing()` today always calls
`api.get_audio_stream_url()`. New:

```
build_now_playing(item):
    blob = downloads.local_blob(provider_id, item_id)   # None if not downloaded
    if blob and (offline_mode or not server_reachable or not prefer_server_on_wifi):
        np.stream_url = blob.path.as_uri()
        np.is_local = True
    else:
        np.stream_url = api.get_audio_stream_url(item_id)
```

`prefer_server_on_wifi` (Symfonium's idea) decides whether a downloaded track
still streams when you *are* online. Default: use the local copy — it's faster
and free.

### 5.5 Offline mode & connectivity

- **Connectivity state** — a small `connectivity` helper: tracks
  reachable/unreachable from API call outcomes (and `QNetworkInformation` where
  reliable). Emits a bus signal on transition.
- **Offline mode** — explicit user toggle, *plus* an automatic degrade that
  flips it when the server goes unreachable. (As-built: the auto-degrade was
  originally gated behind an "Automatic offline mode" toggle, but that toggle
  was **dropped in #55** — OFF only produced a worse outage with no upside, so
  the auto-degrade is now unconditional.) When on: library/search/detail views
  read from `downloads.db` only; non-downloaded items are hidden or shown
  disabled; a persistent UI indicator shows the state.
- Detail pages (artist especially) must gain a `downloads.db` fallback so they
  stop showing "Couldn't load artist" offline.

### 5.6 Sync, staleness, repair

- **Snapshot drift** — server-side edits (renamed track, changed playlist)
  won't reflect in a snapshot. v1: snapshots are explicitly point-in-time; a
  manual "re-sync" per download re-fetches metadata. Background favourite-sync
  later (Finamp does this).
- **Index/disk drift** — a **"Repair downloads"** action: walk `blobs`, drop
  rows with no file, re-hash, re-link orphans, recompute sizes. Cheap insurance;
  Finamp added it after the fact — we design the index so it's a simple walk.
- **Staleness flag** — `nodes.state = stale` when a re-sync detects the source
  changed; surfaced in the downloads screen.

### 5.7 Deletion & orphan cleanup

Deleting a node walks `edges`: remove the node, then any child whose only
remaining parent was the deleted node (refcount → 0). A track in two playlists
survives deletion of one. Confirm dialog for parents (Finamp's lesson — and do
the walk off the GUI thread; deleting a big playlist froze their UI).

## 6. Storage location

Critical distinction the original doc got *half* right:

- **Downloads are user data, not a cache.** They must **not** live in
  `QStandardPaths.CacheLocation` — the OS (and "clean my disk" tools, and iOS
  under storage pressure) can purge that. Downloads go in **`AppDataLocation`**
  (`~/.local/share/jellytoast/downloads/` on Linux), or a **user-configurable**
  location (Symfonium's "cache storage location" — think external drive).
- The transient cache-on-play tier (§10) *does* belong in `CacheLocation`.
- **Relative paths only** in `blobs` — store `downloads/<sha>/<file>`, resolve
  against the base dir at runtime. This is the Finamp iOS lesson; it also makes
  "move my downloads folder" and "back up downloads" trivial.
- On iOS (future): set the do-not-backup flag so downloads don't bloat iCloud,
  and keep them in the app sandbox.

| | Linux | Windows | macOS | iOS (future) |
|--|-------|---------|-------|--------------|
| downloads | `~/.local/share/jellytoast/downloads/` | `%LOCALAPPDATA%\jellytoast\downloads\` | `~/Library/Application Support/jellytoast/downloads/` | sandbox `Application Support/`, no-backup flag |
| `downloads.db` | alongside, in `AppDataLocation` | ″ | ″ | ″ |

`QStandardPaths` resolves all of this with no per-OS branching. The *only* real
fork is the configurable-location default and the iOS no-backup flag — one
small `locations.py`, consistent with the backend-package pattern
(`autostart/`, `media_controls/`, `keep_above/`).

## 7. Settings surface

New **"Downloads & Offline"** section in `settings_dialog.py` (current sections:
General / Account / Appearance / Display / About). Modelled on Symfonium:

- **Download quality** — original / transcoded bitrate (reuses `audio_quality`
  machinery; independent of stream quality).
- **Download location** — path picker; default `AppDataLocation/downloads/`.
- **Wi-Fi-only downloads** — gate the download manager on metered connections.
  (Ship the toggle now; wire auto-detection behind a `platform_compat` probe
  later — `QNetworkInformation` metered status is flaky on Linux.)
- **Prefer server when online** — stream instead of using the local copy when
  the server is reachable (default off — use local).
- ~~**Automatic offline mode** — flip to offline filter when server
  unreachable.~~ Shipped as an *unconditional* auto-degrade instead — the toggle
  was dropped in #55 (no `auto_offline_mode` setting; auto-degrade always on).
- **Storage used** — live read-out (downloads total, broken out by kind).
- **Manage / Repair downloads** — opens the downloads screen; "Repair" action.

New `settings.py` properties: `download_quality`, `downloads_wifi_only`,
`prefer_server_when_online`. (As-built: no `auto_offline_mode` — that toggle
was dropped in #55; auto-degrade is unconditional. `download_location` is not
yet a setting either — the download root is fixed until it lands in a later
phase, so `locations.py` reads it defensively via `getattr`.)

## 8. Provider abstraction additions

`base.py` + both implementations need:

- **Metadata snapshot fetch** — there's already `get_item` / `get_album_tracks`
  / `get_artist_albums` / `get_playlist_items`. Downloads reuse these; no new
  method needed for metadata, just a caller that freezes the result into
  `nodes.metadata_json`.
- **Audio download** — *no new method needed*: reuse `get_audio_stream_url()`
  (with the download-quality setting) and GET it. Both backends already only
  expose stream URLs; the download worker owns the GET. Note Subsonic rotates
  salt/token per call, so resolve the URL at fetch time, never store it.
- One thing worth adding: a provider `server_identity` accessor (disk_cache.py
  already computes one) so `nodes.id` keys are isolated per server / survive a
  re-login cleanly.

## 9. Module scaffolding

New `modules/offline/` package — self-contained, provider-agnostic, no UI:

```
modules/offline/
  __init__.py        # public API: download(node), remove(node),
                     #   is_downloaded(), local_blob(), list_downloads(),
                     #   storage_usage(), repair(), set_offline_mode()
  db.py              # SQLite open/migrate; nodes / edges / blobs schema
  index.py           # node-graph ops: upsert node, link edges, cascade
                     #   delete, orphan-cleanup, refcount, repair walk
  store.py           # blob storage: atomic .part -> rename, relative-path
                     #   resolution, pin covers in image_cache, disk usage
  manager.py         # download queue: progress, pause/resume/retry,
                     #   wifi-only gating, writes nodes.state; async_io pool
  snapshot.py        # freeze provider metadata dicts into nodes.metadata_json;
                     #   re-sync / staleness detection
  connectivity.py    # reachable/unreachable state + bus signal;
                     #   QNetworkInformation where reliable
  locations.py       # path resolution; QStandardPaths + platform_compat for
                     #   configurable default + iOS no-backup. ONLY per-OS file.
  library_sync.py    # (as-built, post-design) bulk-download the whole
                     #   library album-by-album + an optional 6h periodic
                     #   re-sync timer; thin orchestrator over
                     #   offline.download(), idempotent.
```

Integration points (existing files, minimal change):
- `queue_manager.py` — `_build_now_playing()` prefers `local_blob()` (5.4).
- `player_backend.py` — pre-fetch hook reused later for the §10 tier.
- `settings.py` — new properties (§7).
- `settings_dialog.py` — new "Downloads & Offline" section + downloads screen.
- `artist_page.py` / detail views — `downloads.db` fallback when offline.
- Album / playlist / artist / track context menus — Download / Remove actions
  with a downloaded-state checkmark.
- Bus — `connectivity_changed`, `download_progress`, `offline_mode_changed`.

Mirrors the established backend-package convention and the
store/index/view separation already used by `image_cache.py` / `disk_cache.py`.

## 10. Phased rollout

1. **Scaffold** — create `modules/offline/` with the skeletons above, the
   SQLite schema + migrations, public API stubs. No behaviour change.
   **✅ Done 2026-05-14.** `db.py` (v1 schema: nodes/edges/blobs, FK
   cascade, WAL, migration runner) and `locations.py` (AppDataLocation
   base, relative-path resolution) are functional; `index`/`store`
   read queries work against the empty schema; `index`/`store`/
   `snapshot`/`manager` write paths and `connectivity` reachability
   are `NotImplementedError` skeletons tagged with their phase.
2. **Download a track** — `manager` + `store` + `snapshot` + `index` end to
   end for a single track; context-menu action; downloads land in `blobs`.
   **✅ Done 2026-05-14.** `install_song_context_menu` gains a "Download" /
   "Downloaded ✓" entry for single-track selections; `manager.enqueue`
   snapshots metadata, upserts the node as `requested`, and runs a
   chunked background HTTP GET (`async_io` pool) to a `.part` file →
   atomic `commit_blob`. Progress on the `download_progress` bus signal
   `(item_id, state, fraction)`. `offline.init()` runs in
   `_post_show_init`. Quality note: Phase 2 reuses `get_audio_stream_url`
   as-is (honours the *streaming* `audio_quality` setting); a separate
   `download_quality` setting is Phase 6.
3. **Cascade** — album / playlist / artist download via `edges`; the downloads
   screen (list, progress, storage used, remove with cascade + confirm).
   **Engine ✅ 2026-05-14.** `snapshot.freeze` expands album/playlist/
   artist children; `manager` plans the cascade on a worker, runs
   leaf-track downloads through a queue capped at 2 concurrent, and
   rolls completion upward (`index.recompute_state`); shared tracks are
   one job with N parents. `index.cascade_delete` + `store.delete_files`
   + `manager.remove` reap a subtree, orphans only. Smoke-tested.
   **UI ✅ 2026-05-14 (Phase 3b).** `_LibraryListView.contextMenuEvent`
   adds Download / Remove download to album/playlist/artist tiles
   (parent removal confirms); `install_song_context_menu`'s track
   entry is now a working Download / Remove toggle.
   `downloads_view.DownloadsView` — storage-used header + a live list
   of user-requested downloads with per-row progress and remove — is
   wired in as the settings dialog's "Downloads" page.
4. ~~**Play offline** — `_build_now_playing()` prefers `local_blob`; resume
   works with the server off.~~ **Landed 2026-05-14.** `_audio_stream_url()`
   does the gating; `NowPlaying.is_local` flags it; covered by
   `tests/test_queue_manager.py::TestAudioStreamUrl`. Runtime check (server
   off → still plays + resumes) still pending.
5. **Offline mode** — `connectivity` + the explicit toggle + unconditional
   auto-degrade; views filter to `downloads.db`; detail-page fallbacks; offline
   chip indicator. **✅ Shipped.** (The "Automatic offline mode" toggle from the
   original plan was dropped in #55 — auto-degrade is always on.)
6. **Robustness** — "Repair" (disk-reconciliation walk), staleness flag,
   re-sync, retry with exponential backoff, Wi-Fi-only gating. **✅ Shipped.**
7. **Follow-on: cache-on-play** — the transient rolling cache from the original
   research (every played track → LRU, size-capped, in `CacheLocation`). Shares
   `store`/`fetcher` plumbing; distinct from downloads (auto vs. deliberate,
   purgeable vs. user data).

## Sources

- [Finamp — DOWNLOADS_PLAN.md (their downloads architecture post-mortem)](https://github.com/jmshrv/finamp/blob/main/DOWNLOADS_PLAN.md)
- [Finamp — offline mode breaking with large libraries (#832)](https://github.com/UnicornsOnLSD/finamp/issues/832)
- [Finamp — remember storage location for downloads (#158)](https://github.com/jmshrv/finamp/issues/158)
- [Finamp — offline files deleted (#1290)](https://github.com/jmshrv/finamp/issues/1290)
- [Symfonium — Settings: Offline, Cache and Download](https://support.symfonium.app/t/settings-offline-cache-and-download/3563)
- [Symfonium — offline media cache, downloads, rolling/playback/automatic cache](https://support.symfonium.app/t/offline-media-cache-downloads-rolling-cache-playback-cache-automatic-cache-and-more/3565)
- [Plexamp — How exactly is cache handled?](https://forums.plex.tv/t/how-exactly-is-cache-handled/606593)
- [Plexamp | Plex](https://www.plex.tv/plexamp/)
- [just_audio — LockCachingAudioSource / local proxy (#47)](https://github.com/ryanheise/just_audio/issues/47)
- [mpv — stream-record / cache recording caveats (#7275)](https://github.com/mpv-player/mpv/issues/7275)

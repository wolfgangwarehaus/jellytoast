# Changelog

All notable user-facing and developer-facing changes for jellytoast.
Format roughly follows [Keep a Changelog](https://keepachangelog.com/);
versioning will become real once packaging lands (P0 in `docs/TODO.md`).

The **Unreleased** section gathers everything since the most recent
tagged version; snip it off when cutting a release.

---

## [Unreleased]

### Added — workflow & docs
- Three tracking docs: `docs/TODO.md` (P0-P4 prioritized backlog),
  `docs/manual_test_plan.md` (visual / at-keyboard tests),
  `docs/autonomous_tasks.md` (queueable unattended work).
- Competitive audit `docs/competitive_audit.md` against Supersonic,
  Feishin, Finamp, Sublime Music, Strawberry, Tauon, Symfonium.
- Research design docs for every P1/P2 parity feature:
  `docs/research/eq_dsp.md`, `smart_playlists.md`,
  `radio_and_seeded_queues.md`, `crossfade.md`, `visualizers.md`,
  `tag_editing.md`, `parity_small_items.md`.
- Architecture decision log at `docs/decisions.md`.
- This CHANGELOG.

### Added — features (shipped to working tree)
- **Offline Phase 5 UI surface**:
  - Accent-tinted "Offline" chip in the top bar with cycling
    "Connecting…" feedback on click.
  - Library, Songs, Search, and Artist page all swap to local-only
    rendering (downloads.db via `list_complete_items`) when offline
    mode is on.
  - Settings → Downloads: explicit "Offline mode" and "Automatic
    offline mode" toggles. Auto-flip from connectivity drops updates
    the checkbox via bus signal.
- **New offline helpers**: `offline.list_complete_items(kind)`,
  `offline.get_snapshot(item_id)`, `offline.child_snapshots(item_id, kind)`.
- **Artist-page offline fallback**: three-tier resolver — artist
  node → `AlbumArtists[].Id` match → `AlbumArtist` string-name match
  via id→name map built from downloaded tracks/albums. Synthesizes
  meta when no artist node exists.
- **Library / Songs / Search re-render on `offline_mode_changed`** —
  toggling the chip while a view is visible repaints from the new
  source.

### Added — tests
- 12 tests for Phase 5 connectivity state machine (threshold,
  auto-offline, reconnect lift, user-source persistence).
- 16 tests for scrobble eligibility math.
- 5 tests for QSettings rename migration.
- 13 tests for offline-search matching (Album / AlbumArtist /
  Artists, artist tile synthesis).
- 8 tests for the three-tier artist resolver.

### Fixed
- Offline search "air" missed the Air album + artist — now matches
  `Album` / `AlbumArtist` / `Artists` on songs, `AlbumArtist` /
  `AlbumArtists[].Name` on albums; synthesizes artist tiles from
  downloaded albums.
- Offline artist page returned "Couldn't load artist" when only an
  album was downloaded (no artist node) — three-tier fallback now
  handles the case.
- **Offline Albums / Songs / Search all returned empty when
  downloads.db had complete rows** — `_render_offline_items`,
  `_render_offline_songs`, and `_local_search` treated
  `list_complete_items` results as wrapper rows (`n.get("metadata")`)
  but the function returns bare metadata dicts, so the `Id` filter
  dropped every item. Three call sites fixed; the corresponding test
  stub in `test_search_offline.py` was returning the wrong shape and
  hiding the bug — also fixed.
- **Offline cover art missing on Songs / Albums / Search rows** —
  `load_image_async` short-circuited to placeholder before checking
  the in-memory raw cache or the on-disk raw cache. Offline gate now
  sits after every local cache tier, so a cover loaded at any size
  during a prior online session can derive to any other surface.
- **Offline Artists view always empty when only albums were
  downloaded** — `list_complete_items("artist")` only returns nodes
  with `kind = artist`, and downloading an album never creates one.
  Library grid now synthesizes artist entries from every downloaded
  album's `AlbumArtists`, same trick the offline search uses.

### Known issues (carry to next release)
- `set_offline_mode("yes")` doesn't coerce — in-memory flag can hold
  a non-bool. One-liner fix tracked at A6 in
  `docs/autonomous_tasks.md`.
- Phase 5 disconnect test pass deferred (in `manual_test_plan.md`
  §1).

---

## Historical highlights pre-Unreleased

Captured retrospectively for context. Pre-CHANGELOG, so commit log
is canonical.

- **2026-05-15** Lowercase rename `JellyToast → jellytoast`. QSettings
  + keyring + dirs all migrate via `_migrate_legacy_org_name` on
  first launch. Legacy `~/.config/JellyToast/` left as backup.
- **2026-05-15** Scrobble subsystem (`modules/scrobble/`): ListenBrainz
  client (functional), Last.fm client (built but blocked on API key),
  JSON-backed offline queue, Navidrome auto-detection. Reconnect-
  flush hooked into Phase 5 connectivity.
- **2026-05-15** Offline Phase 5 connectivity back-end (state machine,
  bus signals, provider hooks, scrobble flush trigger).
- **2026-05-14** Now-playing surfaces polish: cover lock, hover heart,
  per-member group cast volume.
- **2026-05-11** DPR-aware cover cache: `_COVER_SOURCE_PX` fixed-size
  fetches stopped cache fragmentation across launches.
- **2026-05-10** Main window switched to KDE server-side decorations.
- **2026-05-09** Mini-player keep-above via KWin window rule.
- **2026-05-08** Native PySide6 surfaces — QWebEngineView retired.
- **2026-05-08** Dual-store credentials (keyring + AES-GCM-encrypted
  QSettings blob).
- **2026-05-08** App-wide smooth scrolling via `SmoothScrollFilter`.
- **2026-05-04 → 05-08** Native browse pivot — every clicked surface
  is native; `qt6-webengine` dependency dropped.

---

## Conventions for this file

- Each new feature merged or significant fix shipped → bullet under
  `Unreleased`.
- Group bullets by section: **Added** / **Changed** / **Fixed** /
  **Removed** / **Deprecated** / **Security** / **Known issues**.
- When cutting a release: rename `Unreleased` → `[X.Y.Z] — YYYY-MM-DD`
  and start a fresh `Unreleased` above it.

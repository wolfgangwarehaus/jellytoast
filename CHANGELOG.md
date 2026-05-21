# Changelog

All notable user-facing and developer-facing changes for jellytoast.
Format roughly follows [Keep a Changelog](https://keepachangelog.com/);
versioning will become real once packaging lands (P0 in `docs/TODO.md`).

The **Unreleased** section gathers everything since the most recent
tagged version; snip it off when cutting a release.

---

## [Unreleased]

### 2026-05-21 — theming rework, window blur, cast-safe shutdown

**Theming — Phases 1-3 of the light/dark rework.** The `Theme`
dataclass went from 13 colour fields to 28 semantic tokens, named by
intent (`wash_hover`, `surface_input`, `idle_text`, …) — the layer that
will swap wholesale for a light theme. ~170 hardcoded
`rgba(255,255,255,a)` literals across ~18 widget files now route
through a new `ink_alpha()` helper: value-identical on the dark themes,
dark-tinted automatically on a future light theme. Theme-*mode*
switching is now **live** — no restart — re-stamping every surface's
QSS and repainting the painted window bodies. Settings-dialog pages
build lazily (the ~900 widgets are no longer constructed up front), so
a live theme switch is ~4-6x faster.

**Window blur.** The Frosted theme now asks KWin to blur behind the
main window, mini player, and settings dialog (`modules/blur/`, via
`KWindowEffects` reached through ctypes). Frosted's body opacity was
lifted so the blur reads as frosted glass. The mini player + settings
dialog are server-side-decorated with a `noborder` KWin rule on KDE
Wayland — KWin keeps blur alive through a window drag only for
*decorated* windows.

**Cast-safe shutdown.** Closing the launch terminal (SIGHUP), `kill`
(SIGTERM), or Ctrl+C (SIGINT) now shut the app down gracefully so the
`aboutToQuit` cleanup runs and an active Chromecast / AirPlay session
is stopped first — previously a terminal close orphaned the cast.

Also: mini-player toggle / open-window glyphs redrawn as registry
SVGs; a mini-player label-background fix the transparent theme exposed.

### 2026-05-20 — tag editing: the "Edit tags…" dialog

The metadata-write backend (Jellyfin GET-merge-POST with the
jellyfin#10724 LockedFields workaround) had shipped, but there was no
UI. Right-clicking a track in the songs view now offers **"Edit
tags…"** — a modal editor (`modules/tag_editor.py`) over the seven
server-editable fields (title, artists, album, album artist, genres,
track no., year). Save sends only the *changed* fields, so the
LockedFields set stays scoped to what was touched.

- **Admin gate** — Jellyfin only lets administrators edit metadata.
  `JellyfinAPI.is_admin` is captured from the `Policy` block of the
  authenticate / verify_session responses (no extra request), and the
  menu entry shows only when `can_edit_metadata` *and* the new
  `can_edit_metadata_on_account()` both pass. Subsonic shows nothing.
- Cover-art upload and bulk "apply to album" editing remain follow-ups.

### 2026-05-20 — editable hotkeys page

Settings → Hotkeys was read-only ("Customization coming soon"), even
though `modules/hotkeys.py` had supported per-action overrides all
along. The page is now registry-driven and editable: each in-app
shortcut gets a `QKeySequenceEdit` capture field and a per-row Reset,
plus a "Reset all to defaults". A new `hotkeys.find_conflict` helper
flags a chord already bound to another action — the edit snaps back
and an inline warning names the clash. Rebinds take effect immediately
(no restart): the page emits the new `PlayerBus.hotkeys_changed`
signal and the main window re-installs its QShortcuts. Media keys stay
in a read-only section (they route through MPRIS).

### 2026-05-20 — multi-server URLs: login UI + failover toast

The connectivity engine could already fail over between several server
addresses for one account, but there was no way to *enter* alternates
and no feedback when it switched.

- **Login screen** — an "+ Add alternate URL" affordance under the
  Server URL field opens a manager dialog (`_AlternateUrlsDialog`) for
  the `server_hostnames` failover list: add / edit / remove rows of
  url + optional label, saved in list order (= probe priority).
- **Failover feedback** — the main window now shows a transient toast
  on `host_switched` ("Switched to alternate server · <label>" /
  "Reconnected to the primary server").
- **New `modules/toast.py`** — a reusable in-app toast: a bottom-anchored
  pill that fades out on its own after a few seconds. Non-interactive,
  self-destroying, theme-token styled.

### 2026-05-20 — crossfade Settings controls

The crossfade engine (two-handle ping-pong fade) was built but only
reachable via the `JT_CROSSFADE=1` developer env var. Settings →
Playback now has a proper **Crossfade** section: an enable toggle, a
fade-duration slider (1–10s), and the "skip between same-album tracks"
escape hatch — all wired to the existing `crossfade_*` settings. The
section greys out while casting (crossfade is local-playback only).
The `JT_CROSSFADE` env gate is removed — the enable checkbox is now
the only opt-in; `_ensure_crossfader()` builds purely off
`crossfade_enabled`, so default behavior (off) is unchanged.

### 2026-05-20 — Last.fm scrobbling parked

Last.fm's account-signup firewall (Error 406) blocked registering the
in-app API key, repeatedly, from several networks and devices. Last.fm
is deferred until that cooperates — the client code stays dormant in
`modules/scrobble/lastfm.py`, and the Settings → Scrobbling page now
hides the Last.fm section entirely while `API_KEY` / `API_SECRET` are
empty (it previously showed a "coming in a future build" placeholder).
ListenBrainz is the supported scrobbling path and is unaffected.

### 2026-05-20 — smart-rule schema v2 + live-verification fixes

Merged the `auto/smart-rule-schema-v2` work (date-based smart-playlist
rule fields) and fixed everything live testing against real Jellyfin /
Subsonic servers turned up.

#### Added

- **Schema v2 — date smart-playlist rules.** New `date_added` /
  `last_played` rule fields with `in the last` / `before` / `after`
  operators. Jellyfin reads `DateCreated` / `UserData.LastPlayedDate`;
  Subsonic maps its `created` timestamp onto `date_added` (it has no
  per-track last-played data, so `last_played` never matches there).
  The "Recently added" preset now filters on a real date.

#### Changed

- **Smart-playlist editor — date-rule UI.** The new fields/operators
  get friendly combo labels (no more raw `date_added` / `in_the_last`
  tokens); `in the last` uses a day-count spinbox with a `days` suffix;
  `before` / `after` use a calendar date picker. Rule/match/sort combos
  size to their contents so labels never clip, and the dialog is wider.
- **Dropped the "Forgotten favorites" preset** — permanently empty on
  Subsonic and needs aged listening history on Jellyfin.

#### Fixed

- **Date smart rules timed out on Jellyfin.** A date rule has no
  server-side filter, so `query_items` fetched the entire library in
  one request to refine in Python — on a ~3700-track library Jellyfin
  exceeded the 15s HTTP timeout building the payload, the `except`
  swallowed it, and every date-rule playlist previewed empty. The
  refine fetch now pages via `StartIndex`, and "recent" rules
  (`in the last` / `after`) sort by the date server-side and stop
  paging at the cutoff — a 30-day window previews in ~4s.
- **Editor stored `in the last` values as strings**, which failed
  schema validation (`value must be an int`) — the `⚠` the preview
  showed. The day-count spinbox now emits a real int.

### 2026-05-20 — sleep-timer + smart-shuffle UI

Two engines that were fully built but unreachable now have UI. 1457
tests.

#### Added

- **Sleep-timer menu** — a moon button in the now-playing bar (between
  the mini-player and cast icons) opens a duration menu: 15 / 30 / 45
  min, 1 hour, 1 h 30 min, plus "Stop after current track". While a
  timer is armed the moon goes accent-tinted and the tooltip shows a
  live countdown; re-opening the menu offers "Cancel timer". Timed
  presets use the fade-to-stop wind-down. The bar talks to
  `PlayerBackend` through two new bus signals — `sleep_timer_requested`
  and `sleep_timer_cancel_requested` — so it needs no Player reference.
- **Smart-shuffle toggle** — Settings → Playback now has a "Smart
  shuffle" checkbox driving the existing `playback/smart_shuffle` key.
  The artist-spread picker (anti-clustering — keeps the same artist
  from landing back-to-back) was already wired into the queue; there
  was simply no way to turn it on.
- **`moon` icon** in the shared SVG registry.
- **Tests** — 3 headless tests covering the sleep-timer request-signal
  wiring, 4 covering `_VolumeSliderPopup` construction. Suite 1455 →
  1461.

#### Fixed

- **Now-playing bar volume slider didn't appear.** The 468c599
  "mini-player volume slot" refactor moved the popup's slider + layout
  creation inside `_apply_right_edge_qss`, which only runs in the mini
  player's right-edge mode. The now-playing bar's center-mode popup was
  built with no `slider`, so `VolumeButton._show_popup` raised
  AttributeError on `set_value()` and aborted before `show()` — the
  slider silently never appeared (the scroll wheel still worked, since
  it's a separate path). Slider construction moved into `__init__` for
  both modes; `_apply_right_edge_qss` is now a pure stylesheet refresh,
  which also fixes it orphaning a fresh slider on every reposition in
  the mini player.

### 2026-05-20 — context-menu wiring, dead-code removal, housekeeping

Smart-playlist and track-radio context-menu surfaces wired up; the
unused context-menu installer layer deleted. 1442 → 1455 tests.

#### Added

- **Context-menu wiring** — new `ui_helpers.open_create_smart_playlist()`
  helper. "Create smart playlist from this artist/album/genre" is now
  wired into the song, album, artist, and genre right-click menus, and
  "Start radio from this song" into the songs view (track radio was
  previously built but never reachable). SPEC.md §6 documents the menu
  surface.
- **Tests** — 6 headless tests pinning the context-menu action set per
  item kind. Suite 1442 → 1455.

#### Changed

- **Dead code removal** — deleted the unused context-menu installer
  layer from `ui_helpers.py` (`install_song/album/artist/genre_context_menu`
  + `_install_seed_radio_menu`, ~220 lines, zero call sites — the views
  inline their own context menus). `start_seed_radio` kept.
- **Repo housekeeping** — removed 18 stale agent worktrees and 25
  merged branches.

#### Branches awaiting review

- `auto/smart-rule-schema-v2` is built (adds `date_added` / `last_played`
  smart-playlist rule fields, +42 tests) but left unmerged pending
  live-server verification of the provider field names.

### 2026-05-20 — autonomous queue: cast refactors, radio parity, smart-playlist backend

Six-branch autonomous round merged to `main`. 1348 → 1442 tests.

#### Changed — refactors

- **`cast/dlna.py` split** — the 1188-LOC monolith became a
  `modules/cast/dlna/` 9-file subpackage (`_constants`, `_settings`,
  `_models`, `didl`, `codec`, `discovery`, `_loop`, `controller`).
  Pure refactor; `tests/test_cast_dlna.py`'s full-path imports
  preserved via `__init__.py` re-exports.
- **`cast_manager.py` split** — the 794-LOC monolith became a
  `modules/cast_manager/` package: `_ChromecastMixin` + `_AirplayMixin`
  + a thin `CastManager` orchestrator. The six `test_cast_gating.py`
  monkeypatch globals stay in the package `__init__` and are resolved
  at call time so the patches remain load-bearing.

#### Added — features

- **CastManager DLNA / Sonos / Snapcast discovery fan-out** — new
  `_OtherProtocolsMixin`. `discover_all` fans across all five
  protocols; each `discover_<type>` gates on `cast/<type>_enabled`
  + an optional-dep probe, runs blocking discovery off the GUI
  thread, adapts results into `CastDevice` rows, and pushes them
  through `_notify` so the cast dialog's per-protocol sections fill.
  `stop_cast` routes by `device_type`; `cleanup` tears down the
  DLNA loop thread.
- **Seeded radio entry-point parity** — album / artist / genre
  right-click "Start radio" wired into `LibraryGrid.contextMenuEvent`
  and `_GenresListView`, plus three reusable installers
  (`install_album/artist/genre_context_menu`) and a shared
  `start_seed_radio()` launcher in `ui_helpers.py`. RadioFeeder
  already honours every seed kind.
- **Smart-playlist backend hardening** — recipe factories
  (`from_artist` / `from_album` / `from_genre` / `from_year`) so a
  right-click "Create from this X" flow has rules to call; schema
  additions `is_favorite` (bool), `starts_with` / `ends_with`
  (string), and `sort: random`; `schema_version` field on persisted
  entries (v0 entries load cleanly as v1);
  `open_smart_playlist_editor(preset_rules=, suggested_name=)`.

#### Fixed

- Mechanical ruff cleanup — 11 findings (unused imports, a dead
  local, extraneous f-string prefixes) across 8 files.

### 2026-05-19 — visualizer chill arc, smart playlists, internet radio, EQ UI

Two large commits (`7e0bed0` AM, `468c599` PM) collapsed a wide
mix of features direct to `main` — too tightly coupled to live
verification for the `auto/*` queue. 1229 → 1348 tests.

#### Added — features

- **Visualizer paint widget** shipped end-to-end:
  - `modules/visualizer_widget.py` — initially 32 grounded log-bars,
    upgraded same-day to a Catmull-Rom Bezier wave (16 downsampled
    control points, x-warp 0.55, per-band amplitude weight 1.0→3.0,
    3-tap spatial smoothing, 65% height cap).
  - `settings.np_left_pane_mode` tri-state (`cover | lyrics |
    visualizer`); NP-page toggle cycles `lyrics ↔ visualizer`.
  - Pre-signal "Visualizer · waiting for audio signal" caption until
    first FFT payload.
- **Visualizer per-stream audio tap** — `MonitorAudioTap` prefers
  `pw-record --target=jellytoast` (per-stream isolation; reads only
  mpv's stream since mpv registers with `audio_client_name="jellytoast"`).
  Falls back to `parec --device=@DEFAULT_MONITOR@`. System audio
  from other apps no longer bleeds into the bars. The `JT_VISUALIZER=1`
  env gate is dropped — NP mode-pick is the consent gesture.
- **Smart playlists end-to-end:**
  - `settings.smart_playlists` — JSON list of
    `{name, rules, created_at}`; setter validates via
    `smart_rule_schema.validate_rules`.
  - `modules/smart_playlist_editor.py` — dialog with name + preset
    picker (4 starter recipes: Recently added / Forgotten favorites
    / Top played / Year), match mode all/any, rule chips
    (between op swaps to spinner pair), sort + descending + limit,
    **live preview** pane with 350 ms debounce calling
    `provider.query_items` async (first 25 matches).
  - `modules/smart_playlists_view.py` — library tab between Playlists
    and Songs; rows with Play / Edit / Delete. Play resolves rules →
    tracks (async) and installs a PLAYLIST queue so all existing NP
    chrome works.
- **Internet radio UI:**
  - New "Radio" library tab; `modules/radio_view.py` with
    `_StationRow`, `_StationFormDialog`, popular-stations picker.
  - `modules/radio_presets.py` — 10 curated stations (SomaFM ×4,
    KEXP, WFMU, NTS ×2, Radio Paradise ×2) with logos via
    apple-touch-icon convention.
  - `modules/radio_art.py` — MusicBrainz + Cover Art Archive lookup
    (1 req/sec rate-limited, LRU cached, ICY title parser).
  - `modules/radio_state.py` — single source of truth (`RadioState`
    + `radio_state_changed` signal); unifies bar / mini / NP page.
  - LIVE indicator playback-gated: ● LIVE · station while streaming,
    dim PAUSED · station on pause.
- **EQ shipped** — 10-band graphic EQ + master pre-amp
  (`settings_dialog.py:807-980`). Enabled checkbox, preset combo,
  save/delete, double-click snap-to-zero, cast-greying caption.
- **Track radio right-click entry point** —
  `install_song_context_menu` adds "Start radio from this song"
  for single-track selections.
- **Mini-player volume right-edge slot** — popup hugs the bar-height
  bottom slice (96 px), bottom-anchored. Compact: fills right strip;
  expanded: sits below album. Dynamic top-right corner radius.
  Reparented to `miniContainer` (Wayland z-fight fix).
- **Downloaded indicator + bytes-fraction progress** — hover-revealed
  BL download/check + BR heart corner buttons on album tiles; accent
  progress ring during download. NP-page cover gained a BL download
  CTA.
- **Settings dialog is non-modal** (commit `74d304d`).

#### Changed

- **NP-page toggle UX** — toggle always-visible-when-eligible (drop
  hover gate) so the row's height stops collapsing on cursor-leave
  and the visualizer doesn't shift 1-2 px on every hover crossing.
- **Queue manager radio path** — `_build_now_playing` honours embedded
  `streamUrl` so radio items skip offline-blob lookup;
  `_on_started` skips provider cover for radio items so station
  logos aren't clobbered.

#### Fixed

- **NameError in `_on_radio_state`** at `now_playing_bar.py:1809` —
  stray `run_async(lookup_art_url, ...)` line from the radio
  refactor; radio path no longer throws on every state emit.

#### Tests

- 1229 → 1348 tests across both sessions. New cases include:
  visualizer widget, visualizer engine pw-record/parec selection,
  smart-playlist settings round-trip + drop-malformed + name-trim
  + malformed-JSON recovery, radio_state, radio_art, radio_presets,
  queue radio path, offline bytes helper.

### 2026-05-18 (evening) — downloads progress arc + library walk

Capped the long 2026-05-18 day with an interactive Downloads arc on
top of the afternoon's 9-branch merge round. Net suite: 1178 → 1229
(+51 over the arc; 1057 → 1229 across the whole day).

#### Added — features

- **Aggregate "Downloading X of Y · Z%" block** on Settings →
  Downloads. Speed (`X MB/s`), longest-job ETA (`Y left` /
  "calculating…" / hidden past 12 h), 4 px accent progress bar.
  Live-applies accent. Hides when idle. Variants for paused +
  paused-mid-library-walk.
- **"Download entire library" button** + confirmation dialog. Two-
  phase walk: enumerate albums to pre-sum `ChildCount` for a stable
  total, then enqueue each album not already downloaded. Idempotent
  re-run.
- **"Keep library in sync" setting** with 6-hour periodic re-walk
  timer; auto-bootstraps on app start via `offline.init`.
- **Notify-on-completion checkbox** — desktop notification fires on
  drain via `modules/notifications/`; gated by
  `Settings.notify_on_download_complete` (default on).
- **Standalone Downloads main-content view** reached from top bar →
  tab dropdown → "Downloads". Per-album list (Re-sync + Remove,
  stale badge) moved out of Settings → Downloads into its own page;
  settings stays pure controls.
- **"Clear all downloads" button** with confirmation. Full reset:
  empties queue, lifts pause flag, zeroes in-memory session
  counters, clears persisted library-walk state, emits a final
  stats `(0, 0, 0.0, 0.0)` so the aggregate hides. Auto-hides when
  there's nothing to clear.
- **Resume on app restart**: `manager.resume_pending` walks the
  index for nodes in state `pending` / `downloading`, resets the
  latter back to `pending`, and re-queues their leaf tracks.
  `.part` fragments overwrite cleanly thanks to the atomic-rename
  architecture.
- **Persisted library-walk state** survives a close-reopen:
  `library_download_in_progress` keeps the "Pause library download"
  rebrand; `library_download_expected_total` keeps the stable "of Y"
  count.

#### Added — backend

- `PlayerBus.download_queue_stats` signal carrying
  `(active, total_session, speed_bps, eta_seconds)` at 1 Hz.
- Per-job byte-rate sampling over a 3-second window; longest-job
  ETA projection capped at 12 h.
- `manager.set_session_expected_total(n)` to clamp `total_session`
  from below so library walks read a stable right-hand number.
- `manager.reset_session_counters()` for the clear-all path.
- Package re-exports: `offline.clear_all`, `offline.resume_pending`,
  `offline.sync_library`, `offline.start_periodic_library_sync`,
  `offline.stop_periodic_library_sync`.

#### Fixed

- **Stats timer created on the wrong thread**: `_dispatch` runs on
  whichever thread invoked `enqueue` — often a `QThreadPool` worker
  via `sync_library`. A `QTimer` built there never fires.
  `_ensure_stats_timer` now hops to the GUI thread via
  `QTimer.singleShot(0, app, ...)`. Without this the aggregate
  block + pause button were invisible for the duration of every
  library walk.
- **Row popup spam during bulk enqueue**: rapid "pending" emits used
  to trigger a full `reload()` each, briefly flashing every row as
  a top-level window on Wayland. Now incremental — single row added
  per "pending"; `reload` hides + removes from layout before
  re-parenting.
- **Pause button stayed visible at idle.** Now hidden unless
  `paused == True` or `active > 0`.
- **Aggregate tail clipped to "49.1 M"** on tighter Wayland HiDPI
  fonts. Stacked counts on top of tail vertically.
- **Tray `AttributeError`s** on every playback event because the
  `QAction` block had drifted into `_reapply_menu_styling` — only
  built after the first `theme_changed`. Moved to a new
  `_build_menu_actions` called once from `_build_menu`.
- **"Resume downloads" ghost button** after a clear-all + restart.
  `clear_all` now lifts the pause flag and zeroes session state.

#### Changed

- **Settings → Downloads** is now slim — toggles, aggregate, storage,
  pause + Download entire library + Clear all downloads. The
  per-album list moved out to the standalone page.
- **Compact one-line check rows** on Settings → Downloads. Six
  multi-line wordwrapped notes replaced with single-line captions
  pushed to the right of each checkbox.
- **"On drain" → "on completion"** in the one user-facing string
  that used the queue-internal jargon.
- **Whole-page scroll** on Settings → Downloads. Single outer
  scroll area; the inner downloads-list scroll region is gone.
- Pause / resume button rebrands to "Pause library download" /
  "Resume library download" during a full-library walk. Reverts on
  drain.
- Library walk auto-resumes the queue if it was paused — implicit
  consent to drain.
- Library walk does a two-phase enumeration → enqueue so the "of Y"
  total reads stably from the start.

#### Research

- `docs/research/downloads_progress_ui.md` (`4bbf731`) — ~2300 words
  spec that drove the whole arc. Placement, format, edge cases,
  slice plan (A backend / B UI / C notification toggle).

### 2026-05-18 (afternoon) — autonomous-agent queue clearout (9 merges)

The morning's 15-agent autonomous queue landed onto `main` in a
single afternoon review round. 1057 → 1178 tests; ruff clean. All
nine `auto/*` branches in the suggested low-conflict order:

- **`auto/font-token-cleanup`** — `settings_colors_page.py` raw-px
  font-size sweep routed through `type_qss(TYPE_*)`.
- **`auto/qss-parse-fix`** — regression test only; no static
  offender found, audit harness left in place.
- **`auto/backend-package-tests`** — +39 tests for autostart /
  media_controls / keep_above dispatch shapes.
- **`auto/notifications-backend`** — new `modules/notifications/`
  package (notify-send on Linux, unsupported stub elsewhere). +9
  tests.
- **`auto/smart-playlist-presets`** — new `modules/smart_playlists/`
  package with four starter rule sets + a Year-X factory. +16
  tests.
- **`auto/offline-phase6-wifi-only`** — `downloads_wifi_only`
  setting + manager dispatch gate + bus signal + UI checkbox. +14
  tests.
- **`auto/offline-phase6-downloads-ui`** — Pause / Resume queue
  button, per-row Re-sync, stale badge. +8 tests.
- **`auto/radio-feeder`** — seeded-radio queue-side feeder + skip
  detection via the existing `bus.next_track` split. +14 tests.
- **`auto/crossfade-v1-backend`** — new `modules/playback/crossfade.py`,
  two-mpv-handle ping-pong behind `JT_CROSSFADE=1`. +20 tests.

### 2026-05-17 — autonomous-agent queue clearout (11 merges)

Three back-to-back agent rounds emptied the `auto/*` backlog. Net
contribution: +446 tests (533 → 979), all green. All merges follow
the [[feedback-provider-parity]] rule (features identical on both
backends) and ship cast / visualizer / heavy deps as optional extras
with lazy-import gates per the packaging precedent established below.

#### Added — features

- **`scrobble-cap-precision`** — `_MAX_TICK_DELTA_MS` cap inclusive at
  5000ms (was exclusive); `_MIN_TRACK_DURATION_MS` strictly `>` 30s
  per Last.fm / ListenBrainz spec. +6 tests.
- **`offline-index-repair`** — disk-reconciliation walk: drops orphan
  blob rows, recomputes wrong byte counts, surfaces orphan files, flips
  done-state nodes with no blob to failed. +14 tests.
- **`sleep-timer-fade`** — completes A11's `fade_stop` TODO. Linear
  volume ramp at 50ms ticks, configurable duration via new
  `playback/sleep_fade_duration_ms` (default 8000 ms, clamp 1000-60000).
  Cast-active path falls through to immediate pause. +13 tests.
- **`smart-shuffle`** — new `modules/smart_shuffle.py` greedy weighted
  picker behind `playback/smart_shuffle` setting (default off). Spread
  penalty (distance from same-artist picks) × recency penalty. Below
  16 items falls back to classic random.shuffle. +22 tests.
- **`jellyfin-local-radio-stations`** — Jellyfin radio CRUD on a
  QSettings-backed JSON list (`radio/stations`). Dict shape matches
  Subsonic exactly. +28 tests.
- **`offline-retry-backoff`** — additive schema migration `_migrate_v2`
  adds `retry_count` + `retry_after_ts` to `nodes`. Backoff schedule:
  30s, 60s, 120s, 240s, 480s, 960s, 1920s, capped. `retry_failed()`
  filters by `retry_after_ts > now`; new `force=True` kwarg. +27 tests.
- **`tag-editing-backend`** — `provider.can_edit_metadata` capability
  (True on Jellyfin only) + abstract `update_track_metadata`. Jellyfin
  impl appends touched-field lock-names to `LockedFields` per Jellyfin
  bug #10724 (otherwise scheduled refreshes silently revert edits).
  v1 fields: Name, Artists, Album, AlbumArtist, Genres, IndexNumber,
  ProductionYear. +13 tests.
- **`visualizer-fft-backend`** — `modules/visualizer.py` Hann window →
  rFFT → log-spaced mel bands → dB normalisation, MpvAudioTap stub,
  _FFTWorker on dedicated QThread, VisualizerEngine relaying to
  `PlayerBus.visualizer_bands_changed`. Dormant unless `JT_VISUALIZER=1`
  AND numpy importable. +20 tests.
- **`smart-playlist-evaluator`** — `modules/providers/smart_rule_eval.py`
  pure-Python AND/OR rule refinement (sort, limit). Jellyfin pushes
  genre equals, year equals/between, play_count `>`, rating `>` to the
  server; Python refines the rest. Subsonic AND fires the most
  selective server-mappable rule first; OR queries per rule and unions.
  +37 tests.
- **A25 `cast-toggle-discovery`** — per-protocol cast toggles
  (cast/chromecast_enabled, cast/airplay_enabled, cast/dlna_enabled,
  cast/sonos_enabled, cast/snapcast_enabled) + `cast/discovery_timing`
  (startup vs on_demand, default on_demand). Cast settings move to
  their own page. +15 tests.
- **A26 `cast-menu-collapsible`** — CastDialog refactor: one collapsible
  section per protocol type, per-section state persisted in QSettings,
  mutual exclusion across sections. Empty sections auto-collapse.
  +14 tests.
- **A22 `cast-dlna`** — `modules/cast/dlna.py` full DLNA / UPnP-AV
  backend. SSDP discovery + AVTransport push + 714/701 transcode-retry
  + DIDL-Lite builder with mandatory upnp:class + cover-URL cap.
  Private asyncio loop on daemon thread (documented exception to
  [[feedback-async-io-pattern]]). Cast-proxy mandatory. +106 tests.
- **A23 `cast-sonos`** — `modules/cast/sonos.py` SoCo-based zone
  discovery, group transport, event bridge fanning to existing
  `PlayerBus.cast_*` signals (no new signals). Untested against real
  hardware. +74 tests.
- **A24 `cast-snapcast`** — `modules/cast/snapcast.py` Snapcast control
  surface (Option B, not URL push). Groups + clients listing, stream
  switching, volume + mute, group rename. Three new PlayerBus signals
  (`snapcast_groups_changed`, `snapcast_clients_changed`,
  `snapcast_stream_changed`). +57 tests.

#### Added — packaging + tooling

- **Optional extras pattern locked in** for all heavyweight per-feature
  deps:
  ```
  [project.optional-dependencies]
  visualizer = ["numpy>=1.24"]
  dlna       = ["async-upnp-client>=0.47.0,<1.0"]
  sonos      = ["soco>=0.31,<1"]
  snapcast   = ["snapcast>=2.3.8"]
  ```
  Each backend soft-imports via an `_ensure_<dep>()` gate and stays
  dormant when the dep is missing. Keeps the base install (and the
  Flathub bundle) lean.
- **A19 `pre-commit-hooks`** — scaffold for ruff (`--fix` + format).
  Opt-in via `pip install pre-commit && pre-commit install`. Lint
  rules stay in `pyproject.toml`; hook doesn't widen them.
- **`[build-system]` table** added to pyproject.toml (setuptools, flat
  layout). `jellytoast.py` exposed as a `gui-scripts` entry point.
  Repo is now pip-installable (`pip install -e .`) — prereq for AUR /
  Flatpak packaging.
- **Dev helpers moved to `dev/`** — `install.sh`, `run.sh`,
  `create_desktop_entry.sh`. They're git-clone scaffolding, not part
  of the AUR/Flatpak install path.
- **`requirements.txt` removed** — `pyproject.toml [project]
  dependencies` is the single source of truth.
- **pyatv pin bumped** to `>=0.17` (code targets the modern API).
- **Ruff format pass** — 113 files reformatted (cosmetic; PEP-8 slice
  spacing, function-call rewrap, blank lines after lazy imports).
- **Ruff `--fix`** — 16 F401 unused imports + 1 F541 f-string-without-
  placeholders cleaned up. `ruff check .` now reports "All checks
  passed!"

#### Changed

- Cast settings moved out of the Playback page into their own page
  per [[feedback-cast-settings-own-tab]].
- DLNA / Sonos / Snapcast section state in CastDialog persists across
  sessions per [[feedback-cast-menu-unified-collapsible]].

#### Fixed

- Scrobble eligibility math edge cases (5000 ms cap inclusivity, 30 s
  minimum strictness) per scrobble spec.
- Sleep timer fade-to-stop didn't exist — A11 left a `fade_stop` TODO
  the cleanup branch resolved.

#### Known issues (carry to next release)

- CastManager UI wiring for DLNA / Sonos / Snapcast backends pending —
  the backends ship but discovery results don't surface in the cast
  dialog yet (only the section UI is in place).
- Visualizer rendering widget pending — FFT pipeline shipped but no
  paint surface yet (gated on subjective tuning).
- Internet-radio UI surfaces pending — CRUD shipped on both providers
  but no UI affordance to add / edit / play stations.

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

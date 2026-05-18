# jellytoast project TODO

Running development backlog. Last rewritten 2026-05-18 after a full-
project audit (see session_handoff for the rewrite context).

## How this list works

Each item carries a priority tag and an effort tag. Definition of
"done" rises with priority.

| Priority | Meaning | Done criteria |
|---|---|---|
| **P0** | Blocking momentum or "ship before anything else" | Tests pass + SPEC.md updated + memory entry if user-facing |
| **P1** | Next strategic push — moat-extending or highest-visibility parity gaps | Tests pass + SPEC.md updated |
| **P2** | Important parity / quality polish; pick up between P0/P1 work | Tests pass for any new logic |
| **P3** | Stretch / deferred. Real, but not yet load-bearing | Best effort |
| **P4** | Hardware-gated or long-horizon (Windows / macOS / iOS) | Best effort, often blocked on hardware |

Effort tags: **S** (a few hours), **M** (~1 day), **L** (multi-day),
**XL** (week+).

Pair with:

- `docs/manual_test_plan.md` — visual / at-the-keyboard tests.
- `docs/autonomous_tasks.md` — work Claude can run unattended.
- `docs/research/` — design docs for each P1/P2 feature.
- `docs/decisions.md` — ADR-style log of cross-cutting decisions.
- `docs/competitive_audit.md` — 2026-05-15 audit; source of strategy.

---

## 🛑 In-flight (review-ready)

Nothing review-ready as of 2026-05-18 evening. The 2026-05-18 merge
round emptied the nine `auto/*` branches (font-tokens, qss-parse-fix,
backend-package-tests, notifications-backend, smart-playlist-presets,
offline-phase6-wifi-only, offline-phase6-downloads-ui, radio-feeder,
crossfade-v1-backend) onto `main`; afterwards an interactive Downloads
arc shipped slices A/B/C of the downloads-progress feature plus the
library-walk subsystem and several UX polishes — all direct to `main`.
1057 → 1229 tests passing.

---

## P0 — Now

### 📦 AUR PKGBUILD — **S/M, the moat-gate**
Repo has been pip-installable since 2026-05-17 (`[build-system]` +
flat layout + `gui-scripts`). All code prereqs done. Authoring the
PKGBUILD + AUR submission is ~1-2 hours of mechanical work. This is
the standing P0 across multiple sessions.

### 📦 Flathub manifest + screenshots — **L**
Per `packaging/io.github.augustvontrips66.jellytoast.metainfo.xml`:
the AppData XML is built; `<screenshots>` block is present but
commented out. Need:
- Screenshot PNG captures (Library / Now playing / Cast dialog /
  Downloads / Settings).
- Uncomment + populate `<screenshots>` block.
- Flatpak `.yaml` manifest (separate from metainfo).
- Submit PR to flathub/flathub. Expect days of reviewer back-and-forth.

### 📥 Downloaded indicator on album/artist pages — **S/M, new**
Surfaced 2026-05-18: the standalone Downloads nav entry lists what's
downloaded, but album/artist views don't currently show whether a
given item is downloaded. Add a small badge or icon on each tile +
on the detail page. Needs to live behind a fast `offline.is_downloaded`
check (already O(1) indexed); subscribe to `download_progress` for
live updates.

### 📊 Bytes-fraction aggregate progress — **S, new**
The aggregate progress bar + percent currently track `(total_session -
active) / total_session` — coarse but truthful. A bytes-fraction
(`sum(got_bytes) / sum(total_bytes)` across `_jobs`) would give a
smoother ramp. Needs a new `manager.get_queue_bytes_progress()`
helper readable on the GUI thread; aggregate calls it from
`_on_stats`. Backend hooks already cache per-job bytes
(`manager.py` `_jobs[tid]["got_bytes"]` / `["total_bytes"]`).

---

## P1 — Next strategic push

### 🔌 CastManager device wiring (DLNA / Sonos / Snapcast) — **M**
Backend modules at `modules/cast/dlna.py`, `cast/sonos.py`,
`cast/snapcast.py` exist and are non-empty. CastDialog section
placeholders exist for all 5 protocols
(`cast_dialog_sections.py:32–38`). But `cast_manager.py:735–754`
only calls `discover_chromecasts()` + `discover_airplay()` — the
three new protocols are dormant.

Remaining:
- Discovery orchestration respecting A25 per-type toggles +
  on-demand vs startup mode.
- `discover_*()` per backend (these methods may already exist in the
  protocol modules — verify before duplicating).
- Result fanout into already-built CastDialog sections (A26).
- Push methods to start streams on each.

### 📻 Internet radio UI — **M**
Backend is fully shipped (was a surprise during 2026-05-18 audit):
- Subsonic CRUD: `providers/subsonic.py:1086-1170`
- Jellyfin CRUD: `providers/jellyfin.py:529-585`
- Local fallback: `settings.radio_stations`
- ICY title pipeline: `radio_title_changed` signal + mpv observer

UI still to build:
- "Internet Radio" tab in library nav (new `modules/radio_view.py`).
- Add / edit / delete station form.
- NP surface: replace scrubber with elapsed + LIVE pip when
  `QueueContext.kind == INTERNET_RADIO`.
- `cast_proxy` already handles redirects + Range — no new cast code.

### 🎯 Seeded radio — RadioFeeder + right-click — **M**
Provider methods shipped on BOTH backends:
- Jellyfin: `get_similar_songs`, `get_instant_mix`, `get_genre_radio`
  (`providers/jellyfin.py:349-406`)
- Subsonic: same three (`providers/subsonic.py:826-866`)
- `QueueKind.INSTANT_MIX` shipped, `QueueContext.seed_kind` +
  `radio_played_ids` shipped (`player_state.py:34,65-66`)
- NP page already shows "INSTANT MIX" label (`now_playing_page.py:2664`)

Missing — the actual *loop*:
- **RadioFeeder**: within-5-of-end detector in `queue_manager.py`,
  refill 25 at a time via provider methods, dedupe against
  `radio_played_ids`, cap at 200 (trim oldest *played* first). Push
  ids to `radio_played_ids` on `playback_started`. Per
  `docs/research/radio_and_seeded_queues.md` §5.2.
- Right-click "Start radio from here" affordance everywhere
  (track / album / artist / genre).
- Skip-heavy heuristic (§5.4 — ≥3-of-last-5 skipped → reseed with
  offset).

Pure backend; RadioFeeder is autonomous-eligible.

### 🎶 Smart playlists editor UI — **M**
Evaluator + rule schema shipped at `providers/smart_rule_schema.py`
+ `providers/smart_rule_eval.py`. Editor still to ship:
- Local `smart_playlists.json` store (rules, not server-side write).
- Chips + live preview pane in the Playlists area.
- 4 preset recipes: "Recently added", "Forgotten favorites", "Top
  played", "Year X" — these are JSON rule sets matching the existing
  schema; autonomous-eligible.
- v2: read-only surfacing of Navidrome `.nsp` server-native smart
  playlists via OpenSubsonic `readonly: true`.

### 🎬 Cast-proxy demo GIF — **S**
30-second README hero shot: Chromecast playing from Tailscale-only
Navidrome with laptop offline. Pairs with Flathub screenshots in P0.
Requires real recording session, not autonomous.

### 🎫 Last.fm API key registration (august task)
`modules/scrobble/lastfm.py:47-48` — `API_KEY`/`API_SECRET` still
empty. Register at `last.fm/api/account/create`, drop values in,
Settings → Scrobbling Last.fm half lights up automatically.

### 🎨 Visualizer rendering widget — **M, now autonomous-eligible**
FFT pipeline + signal infrastructure shipped (`modules/visualizer.py`
+ `PlayerBus.visualizer_bands_changed`). **Spec landed 2026-05-18** at
`docs/research/visualizer_rendering.md` — 32-bar grounded rectangles,
asymmetric exponential smoothing (`attack_α=0.35`, `release_α=0.12`),
ACCENT_DEEP→ACCENT linear gradient, decay-to-baseline idle. ~250-350
LOC single slice; no subjective tuning at implementation time.
- New `np_left_pane_mode = visualizer` on NP page (tri-state grows
  from current `_show_lyrics: bool`).
- New `modules/visualizer_widget.py` with the spec'd paint code.
- Real mpv lavfi-complex audio tap (currently returns zeros) — same
  slice or follow-up, agent's choice.
- Cast edge: static "Casting to <device>" placeholder per spec §8.

---

## P2 — Important parity / quality

### 🌐 Multi-server hostnames — login UI — **M**
Backend fully shipped:
- `settings.server_hostnames` JSON list (`settings.py:627`)
- Connectivity tracker tries alternates before declaring unreachable
  (`offline/connectivity.py:216,516`)
- `PlayerBus.host_switched(label)` signal + emit

Pending:
- Login UI: "+ Add alternate URL" affordance + drag-to-reorder.
- NP-bar toast subscriber on `host_switched`.

### 🔀 Crossfade — **M+**
Per `docs/research/crossfade.md`. Net-new feature; no code yet (audit
confirmed zero hits). Two alternating libmpv instances ping-pong A→B.
Smart-album-continuity escape hatch routes same-album adjacent tracks
back through gapless. v1 behind `JT_CROSSFADE=1` env flag before
exposing settings.
- `playback/crossfade_enabled`, `playback/crossfade_duration_ms`,
  `playback/crossfade_smart_album_continuity`.
- Greys out during cast.
- ⚠ Audio device contention on Windows WASAPI exclusive + raw ALSA.
- Backend plumbing is autonomous (subjective curve tuning isn't).

### ⌨️ Hotkey rebinding UI — **M**
Registry shipped at `modules/hotkeys.py`. Settings → Hotkeys page
currently read-only (`settings_dialog.py:1819-1856`). Need
`QKeySequenceEdit` per row + persistence + conflict detection.

### 🏷️ Tag editing UI — **M, Jellyfin-only**
Backend shipped 2026-05-17:
- `provider.can_edit_metadata` cap (`base.py:75`; Jellyfin returns
  True at `jellyfin.py:135`)
- `update_track_metadata` on both providers
- LockedFields workaround embedded in Jellyfin call

UI:
- Right-click "Edit tags…" in views/NP page.
- v1: single-track edit + cover-art upload.
- v2: bulk-album ("Apply to all in this album").

### 🎨 Theme modes — **L**
Per `docs/research/parity_small_items.md`. Two-phase:
1. **Live-apply theme MODE** — accent already live-applies, theme
   doesn't (`theme.py:1-17` notes restart required). Build
   `_reapply_theme()` per-surface, mirror accent contract.
2. **Light theme + rgba audit** — 15 files / 95 occurrences of
   `rgba(255,255,255,...)` need routing through tokens.

System-auto mode via `QStyleHints.colorSchemeChanged` (Qt 6.5+) is
the easy capstone once Phase 2 lands.

---

## P3 — Stretch / deferred

### 🐞 Stylesheet parse warning on a QPushButton — **S**
Surfaced 2026-05-17 during live offline testing:
`Could not parse stylesheet of object QPushButton(0x...)`. Harmless
but visible. Likely a malformed property in a toggle / chip / cast
button. Autonomous-eligible: grep `setStyleSheet` on QPushButton
subclasses, run with `JT_DEBUG_QSS` or similar to locate, fix typo.

### 📡 Registered Cast receiver app — **L, needs $ + hosting**
Screens show "Default Media Receiver" instead of "jellytoast". $5
Google dev account + hosted custom receiver web app. Deferred past
Phase 4.

### 🎵 AirPlay 2 sender refinements
Edge cases (LG webOS, shairport-sync 5.x) per
`reference_airplay2_pyatv_compat`.

### 🔌 `QNetworkInformation` integration
Supplementary connectivity signal. Linux flaky; revisit on
Windows/macOS.

### 📥 Server-side playlist import (m3u, etc.)
Out-of-scope for music-only / streaming-first unless requested.

---

## P4 — Hardware-gated / long-horizon

### 🪟 Windows native backends
- `media_controls/` → SMTC (Windows.Media.Control)
- `autostart/` → Run registry key
- `keep_above/` → Win32 `SetWindowPos(HWND_TOPMOST)`
- `notifications/` → Toast notifications (Windows.UI.Notifications)
- Verify PMv2 HiDPI path

### 🍏 macOS native backends
- `media_controls/` → NowPlayingInfoCenter via pyobjc
- `autostart/` → AppKit login items
- `keep_above/` → `NSWindowLevel`
- `notifications/` → `UNUserNotificationCenter`

### 🔔 Desktop notifications backend package — **S/M (new finding)**
`modules/notifications/` package **does not exist yet**. Other
platform-backends follow a clean pattern (`autostart/`,
`media_controls/`, `keep_above/` — see
`[[architecture-cross-platform]]`). Same shape needed for
notifications. Linux backend would use `Notify` via `dbus-next` or
`org.freedesktop.Notifications` directly. Currently any notification
code is ad-hoc / non-existent.

### 📱 iOS native
After Mac. Sandbox awareness for downloads (no-backup flag), CarPlay
handoff, lock-screen artwork.

### 🎛️ ASIO / exclusive output
Windows-only audiophile feature. Strawberry has it. Only if a
Windows user asks.

### 🎵 Visualizer per-OS audio taps
Once v1 paint widget ships (mpv-tap based), per-OS loopback backends
unlock visualization during cast:
- Linux: PipeWire / PulseAudio monitor sink
- Windows: WASAPI loopback
- macOS 14.4+: `CATapDescription` (native); pre-14.4 needs BlackHole
- iOS: AVAudioEngine tap on output

---

## ✅ Recently shipped (since prior TODO refresh)

For paper trail. Move to `CHANGELOG.md` on next release cut.

**2026-05-18 session (full day, 13 autonomous agents + downloads arc):**

*Code, merged to `main`:*
- All nine `auto/*` branches from the morning queue (Wi-Fi-only,
  downloads-ui, font-tokens, smart-playlist-presets, notifications-
  backend, radio-feeder, crossfade-v1-backend, backend-package-tests,
  qss-parse-fix). 1057 → 1178 tests.
- Downloads-progress feature, slices A/B/C (`9cd1f3b`, `5fd1031`,
  `f56340c`): backend stats (byte sampling, 1 Hz QTimer, drain-edge
  notification, session counters), aggregate "Downloading X of Y · Z%"
  block on Settings → Downloads, notify-on-complete toggle.
- Library walk subsystem (`5a62f19`, `b67b168`): "Download entire
  library" button + "Keep library in sync" + 6h periodic; two-phase
  walk pre-counts `ChildCount` for a stable progress total.
- Standalone Downloads main-content view (`a56d624`): per-album list
  moved off Settings → Downloads into a top-bar nav entry; settings
  page becomes pure controls.
- Clear all downloads (`a56d624`, `19c85c6`, `eb1682f`): full reset
  (queue, pause flag, persisted library-walk state, session
  counters); auto-hides when nothing to clear.
- Resume on app restart (`15f9705`): `manager.resume_pending` walks
  the index for mid-flight nodes and re-queues; `.part` files
  overwrite cleanly thanks to the existing atomic-rename architecture.
- Library-walk persistence across restart (`dbf5934`, `e4b10ee`):
  `library_download_in_progress` + `library_download_expected_total`
  in QSettings so "Resume library download" and the stable "of Y"
  total survive a close-reopen cycle.
- Compact one-line check rows on Settings → Downloads (`7cff722`).
- Stats-timer-on-wrong-thread fix (`525a222`): hop to GUI thread via
  `QTimer.singleShot(0, app, ...)` when invoked from a `QThreadPool`
  worker so the aggregate actually appears during a bulk walk.
- Row-popup spam fix (`525a222`): incremental row add on "pending"
  instead of full reload, hide+remove-from-layout before
  `setParent(None)` to avoid Wayland top-level flash.
- Pause button auto-hide at idle (`01e5358`).
- Auto-resume when starting a library walk if previously paused
  (`10c799b`).
- Tray-actions-built-on-construction fix (`ba436ec`): play_action /
  now_playing built in `_build_menu` not `_reapply_menu_styling`, so
  signal handlers don't `AttributeError` on every playback event.
- Aggregate truncation fix (`ee06a82`): "49.1 MB/s · X min left" no
  longer clips to "49.1 M" on tighter Wayland HiDPI fonts.

*Research, landed directly on `main`:*
- `docs/research/visualizer_rendering.md` (`bc2a437`) — 32-bar paint
  spec, unblocks autonomous visualizer paint widget.
- `docs/research/provider_abstraction_cleanup.md` (`0870e9a`) — split
  plans for `cast_manager.py` + `cast/dlna.py`.
- `docs/research/downloads_progress_ui.md` (`4bbf731`) — full spec
  that drove the downloads arc; aggregate placement, label format,
  edge cases, slice plan.

**Confirmed already-shipped during 2026-05-18 audit** (TODO was stale):
- ReplayGain mode combo (Settings → Playback, `_rg_combo`).
- Server-side scrobble badge UI (`_ScrobbleBadge` on NP bar).
- Offline Phase 6 backend: `pause()`, `resume()`, `is_paused()`,
  `mark_stale()`, `resync()`, `is_stale()`, `downloads_paused`,
  `download_queue_paused`/`resumed` signals.
- Internet radio backend CRUD (both providers) + ICY observer +
  `radio_title_changed` signal.
- Seeded-radio provider methods on Jellyfin AND Subsonic.
- `QueueContext.seed_kind` + `radio_played_ids`,
  `QueueKind.INSTANT_MIX` + `INTERNET_RADIO`.
- Multi-server hostnames backend + `host_switched` signal.
- Sleep timer signals (`sleep_timer_started/cancelled/fired`).
- Smart playlist evaluator + rule schema.
- Scrobble status changed signal.

---

## ❌ Explicitly NOT on the roadmap

Per `[[project-competitive-positioning]]`:

- **Local-file libraries** — Strawberry / Tauon heritage territory.
- **Podcasts** — out of music-only scope.
- **Mobile (Android / direct-iOS)** — Symfonium / Finamp own those.
- **Heavy audiophile DSP** (AutoEQ, 256-band PEQ) — Symfonium
  uncatchable.
- **CarPlay / Android Auto** — mobile-only.

## ⛔ No longer in this TODO (verified obsolete 2026-05-18)

- ~~`set_offline_mode("yes")` coercion bug~~ — fixed at boundary
  (`offline/connectivity.py:538`).
- ~~Settings duplicate property cleanup (`cast_<type>_enabled`
  vs `<type>_enabled`)~~ — no actual duplicate exists; per-protocol
  modules use internal `_settings_enabled()` helpers, not public
  properties.
- ~~ReplayGain mode UI toggle~~ — shipped at `settings_dialog.py:731`.
- ~~Server-side scrobble badge~~ — shipped at `now_playing_bar.py:1046`.

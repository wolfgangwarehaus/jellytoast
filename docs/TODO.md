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

**Nine `auto/*` branches** landed 2026-05-18, all local, unmerged.
~+121 tests projected across them (1057 → ~1178 if all merge clean).
Suggested merge order at the bottom.

| Branch | Commits | Tests | Slice |
|---|---|---|---|
| `auto/offline-phase6-wifi-only` | `3105b47`, `a254124` | +14 | Wi-Fi-only gating + UI checkbox |
| `auto/offline-phase6-downloads-ui` | `6a3318f`, `4735936` | +8 | DownloadsView pause/resume/stale/re-sync |
| `auto/font-token-cleanup` | `103a47d` | +0 (mech) | settings_colors_page raw-px sweep |
| `auto/smart-playlist-presets` | `8d3add6` | +16 | 4 starter rule sets + Year-X factory |
| `auto/notifications-backend` | `21bd63b` | +9 | New `modules/notifications/` package |
| `auto/radio-feeder` | `130ddba` | +14 | Seeded-radio continuous extension |
| `auto/crossfade-v1-backend` | `7810e3d` | +20 | Two-mpv-handle plumbing behind `JT_CROSSFADE` |
| `auto/backend-package-tests` | `2b44060` | +39 | Tests for autostart/media_controls/keep_above |
| `auto/qss-parse-fix` | `6a3d444` | +1 | Regression test only (no offender found statically) |

**Merge order (minimizes conflict resolution):**

1. `auto/font-token-cleanup` — touches only `settings_colors_page.py`, no overlap.
2. `auto/qss-parse-fix` — touches only `tests/test_qss_audit.py`, no overlap.
3. `auto/backend-package-tests` — three new test files, no overlap.
4. `auto/notifications-backend` — new package, no overlap.
5. `auto/smart-playlist-presets` — new package, no overlap.
6. `auto/offline-phase6-wifi-only` — wider footprint (settings/player_state/offline). Merge before downloads-ui.
7. `auto/offline-phase6-downloads-ui` — overlaps wifi-only in `offline/__init__.py` (`__all__` list union; trivial).
8. `auto/radio-feeder` — touches queue_manager + provider/* + player_state signals.
9. `auto/crossfade-v1-backend` — touches player_backend + settings + player_state signals. **Conflicts likely** with `auto/radio-feeder` in `player_state.py` (both add bus signals). Resolve by keeping both signal blocks.

Verification queue lives in `manual_test_plan.md`. After all nine
merge clean: next pickup is P0 packaging (AUR + Flathub).

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

### 🎨 Font-token audit pass — **S**
Per `[[feedback-typography-tokens]]` every widget should flow through
`type_qss(TYPE_*)`. Old violations cleaned up; new audit found:
- `modules/settings_colors_page.py` lines 226, 240, 255, 298, 397,
  415, 599 — raw `font-size: 11px/12px/13px` in stylesheet strings.
  Sweep through `type_qss()`.

Allowed raw-px exceptions: A–Z rail (9px in `library_grid.py`),
user-tunable lyric sizes. The `now_playing_bar.py:443` and
`airplay_pairing.py:203` violations from prior audits no longer
appear — closed.

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

**2026-05-18 session (full day, 13 autonomous agents):**

*Code, on `auto/*` branches awaiting review:*
- Offline Phase 6 Wi-Fi-only gating (`auto/offline-phase6-wifi-only`).
- Offline Phase 6 DownloadsView UI: pause/resume, stale badge, per-row
  re-sync (`auto/offline-phase6-downloads-ui`).
- Font-token cleanup in `settings_colors_page.py` (`auto/font-token-cleanup`).
- Smart-playlist preset recipes (`auto/smart-playlist-presets`).
- Notifications backend package scaffold (`auto/notifications-backend`).
- RadioFeeder continuous-extension for seeded radio (`auto/radio-feeder`).
- Crossfade v1 backend behind `JT_CROSSFADE=1` (`auto/crossfade-v1-backend`).
- Backend package tests for autostart/media_controls/keep_above (`auto/backend-package-tests`).
- QSS parse warning audit (`auto/qss-parse-fix`) — no static offender
  found; left audit harness + regression test for `OfflineChip`.

*Research, landed directly on `main`:*
- `docs/research/visualizer_rendering.md` (`bc2a437`) — 32-bar paint
  spec, unblocks autonomous visualizer paint widget.
- `docs/research/provider_abstraction_cleanup.md` (`0870e9a`) — split
  plans for `cast_manager.py` + `cast/dlna.py`.

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

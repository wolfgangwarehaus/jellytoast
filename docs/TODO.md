# jellytoast project TODO

Running development backlog. Last rewritten 2026-05-19 after a full-
project audit (followed two large 2026-05-19 commits — see CHANGELOG
2026-05-19 entries for what they collapsed).

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

**Smart-playlist editor — live preview unverified on a real library
(2026-05-19).** Editor dialog + library tab + 4 preset recipes shipped
(`modules/smart_playlist_editor.py`, `smart_playlists_view.py`, 8
regression tests). The live preview pane runs `query_items` async at
350 ms debounce against the current provider — but only round-tripped
against unit-test stubs. august should run each preset against the
real Subsonic / Jellyfin server and confirm the preview row count
matches expectations, plus that the saved playlist actually plays
through the queue.

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
  Downloads / Settings / Visualizer / Smart playlists / Radio).
- Uncomment + populate `<screenshots>` block.
- Flatpak `.yaml` manifest (separate from metainfo).
- Submit PR to flathub/flathub. Expect days of reviewer back-and-forth.

---

## P1 — Next strategic push

### 🎬 Cast-proxy demo GIF — **S**
30-second README hero shot: Chromecast playing from Tailscale-only
Navidrome with laptop offline. Pairs with Flathub screenshots in P0.
Requires real recording session, not autonomous.

### 🎫 Last.fm API key registration (august task)
`modules/scrobble/lastfm.py:47-48` — `API_KEY`/`API_SECRET` still
empty. Register at `last.fm/api/account/create`, drop values in,
Settings → Scrobbling Last.fm half lights up automatically.

---

## P2 — Important parity / quality

### 🌐 Multi-server hostnames — login UI — **M**
Backend fully shipped (`settings.server_hostnames`, alternate-URL
probe in `offline/connectivity.py:216,516`, `host_switched(label)`
signal). Pending:
- Login UI: "+ Add alternate URL" affordance + drag-to-reorder.
- NP-bar toast subscriber on `host_switched`.

### 🔀 Crossfade — Settings UI exposure — **S**
Backend shipped behind `JT_CROSSFADE=1`:
- `modules/playback/crossfade.py` state machine
  (IDLE → ARMING → CROSSFADING → SWAP → IDLE)
- Two-handle ping-pong via `player_backend._crossfader`
- `settings.crossfade_enabled` + duration + smart-album-continuity
  keys already exist
- Smart-album-continuity escape hatch routes same-album adjacents
  back through gapless

Pending Settings exposure:
- Settings → Playback: checkbox + duration slider + smart-album
  toggle. Auto-greys-out when cast active.
- Drop the `JT_CROSSFADE` env gate; the user-facing checkbox is
  the consent gesture.

### ⌨️ Hotkey rebinding UI — **M**
Registry shipped at `modules/hotkeys.py`. Settings → Hotkeys page
currently read-only (`settings_dialog.py:1853-1879`; line 1874 still
reads "Customization coming soon"). Need `QKeySequenceEdit` per row
+ persistence + conflict detection.

### 🏷️ Tag editing UI — **M, Jellyfin-only**
Backend shipped 2026-05-17 (`provider.can_edit_metadata`,
`update_track_metadata`, LockedFields workaround). UI:
- Right-click "Edit tags…" in views/NP page (add an inline action to
  `SongsView._on_context_menu` + the LibraryGrid / NP-page menus;
  gate on `provider.can_edit_metadata`).
- v1: single-track edit + cover-art upload dialog.
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

### 📱 iOS native
After Mac. Sandbox awareness for downloads (no-backup flag), CarPlay
handoff, lock-screen artwork.

### 🎛️ ASIO / exclusive output
Windows-only audiophile feature. Strawberry has it. Only if a
Windows user asks.

### 🎵 Visualizer per-OS audio taps
Linux taps (`pw-record --target=jellytoast` preferred, `parec
--device=@DEFAULT_MONITOR@` fallback) shipped 2026-05-19. Per-OS
loopback backends needed for cross-platform parity:
- Windows: WASAPI loopback
- macOS 14.4+: `CATapDescription` (native); pre-14.4 needs BlackHole
- iOS: AVAudioEngine tap on output

---

## ✅ Recently shipped (since prior TODO refresh)

For paper trail. Move to `CHANGELOG.md` on next release cut.

**2026-05-20 session (autonomous queue A1-A6 + cast wiring):**

- **cast/dlna.py split** — 1188-LOC monolith → `modules/cast/dlna/`
  9-file subpackage. Pure refactor; test-patch contract preserved.
- **cast_manager.py split** — 794-LOC monolith → `modules/cast_manager/`
  package (`_ChromecastMixin` + `_AirplayMixin` + thin orchestrator).
- **CastManager DLNA / Sonos / Snapcast discovery fan-out** —
  `discover_all` now fans across all five protocols via a new
  `_OtherProtocolsMixin`; each `discover_<type>` gates on
  `cast/<type>_enabled` + an optional-dep probe, runs blocking
  discovery off the GUI thread, adapts results into `CastDevice`
  rows, and pushes them through `_notify` so the cast dialog
  sections fill. `stop_cast` routes by `device_type`.
- **Seeded radio entry-point parity** — album / artist / genre
  right-click "Start radio" wired into `LibraryGrid.contextMenuEvent`
  + `_GenresListView`; three reusable installers in `ui_helpers.py`.
- **Smart-playlist backend hardening** — `from_artist/album/genre/year`
  recipe factories, `is_favorite` / `starts_with` / `ends_with` /
  `sort: random` schema additions, `schema_version` on persisted
  entries, `open_smart_playlist_editor(preset_rules=, suggested_name=)`.
- **Ruff cleanup** — 11 lint findings cleared.
- Tests: 1348 → 1442.
- Note: the right-click "Create smart playlist from this X" *visual*
  affordance is still unwired — backend recipes are ready, the QMenu
  entry is the remaining hookup.

**2026-05-19 PM session (visualizer chill arc + mini-player volume + smart playlists):**

- **Visualizer per-stream audio tap** — `MonitorAudioTap` rewritten to
  prefer `pw-record --target=jellytoast` (per-stream isolation since
  mpv registers with `audio_client_name="jellytoast"`); falls back to
  `parec --device=@DEFAULT_MONITOR@`. System audio from other apps no
  longer bleeds into the bars.
- **Visualizer paint upgrade** — bars → Catmull-Rom Bezier wave (16
  downsampled control points, x-warp 0.55 power, per-band amplitude
  weight 1.0→3.0, 3-tap spatial smoothing `[0.2, 0.6, 0.2]`, 65%
  height cap). Pre-signal caption "Visualizer · waiting for audio
  signal" until first FFT payload; silence decays to true zero.
- **`JT_VISUALIZER=1` env gate dropped** — mode-pick from the NP
  toggle is the consent gesture.
- **Mini-player volume right-edge slot** — popup hugs the bar-height
  bottom slice (96 px), bottom-anchored. Compact mode: fills the
  right strip; expanded: sits below the album. Dynamic top-right
  corner radius. Reparented to `miniContainer` so `raise_()` lifts
  above the cover (Wayland z-fight fix). `leaveEvent` force-starts
  the hide timer (Wayland popup-side leave events drop at window
  boundary).
- **Smart playlists end-to-end** —
  `modules/smart_playlist_editor.py`: dialog with name + preset
  picker, match mode all/any, rule chips (between op swaps to spinner
  pair), sort + descending + limit, **live preview** pane with 350 ms
  debounce calling `provider.query_items` async, first 25 matches
  rendered as `title · artist` rows.
  `modules/smart_playlists_view.py`: library tab between Playlists
  and Songs; Play / Edit / Delete rows. Play installs a `PLAYLIST`
  queue so all existing NP chrome works.
  `settings.smart_playlists` — JSON-persisted list of
  `{name, rules, created_at}`; setter validates via
  `smart_rule_schema.validate_rules`. 8 regression tests.
- **Track radio right-click entry point** —
  `install_song_context_menu` adds "Start radio from this song" for
  single-track selections (`ui_helpers.py:1583-1631`).
- **NP-page toggle UX** — cycle is `lyrics ↔ visualizer`; toggle
  always-visible-when-eligible to stop the toggle-row's height
  collapsing on hover-leave and shifting the visualizer 1-2 px.
- 1334 → 1348 tests.

**2026-05-19 AM session (downloads polish + internet radio + visualizer paint):**

- **Visualizer paint widget shipped** — `modules/visualizer_widget.py`
  (32 grounded log bars at the time; later upgraded to Bezier wave
  in the PM session).
- **`np_left_pane_mode` tri-state** — `cover | lyrics | visualizer`.
- **Internet radio UI** — new "Radio" library tab,
  `modules/radio_view.py` + `_StationRow` + `_StationFormDialog` +
  popular-stations picker. `modules/radio_presets.py` — 10-station
  curated list (SomaFM ×4, KEXP, WFMU, NTS ×2, Radio Paradise ×2)
  with logos. `modules/radio_art.py` — MusicBrainz + Cover Art
  Archive (1 req/sec rate-limited, LRU, ICY title parser).
  `modules/radio_state.py` — single source of truth via
  `radio_state_changed` bus signal.
- **LIVE indicator playback-gated** — ● LIVE · station while
  streaming, dim PAUSED · station on pause.
- **Queue manager radio path** — `_build_now_playing` honours
  embedded `streamUrl` so radio items skip offline-blob lookup;
  `_on_started` skips provider cover for radio items so station
  logos aren't clobbered.
- **Downloaded indicator + bytes-fraction progress** — hover-revealed
  BL download/check + BR heart corner buttons on album tiles;
  accent progress ring while downloading; click routing in
  `_LibraryListView`; NP page cover gained a BL download CTA.
- **Settings → non-modal dialog + EQ slider polish** (commit
  `74d304d`).
- **EQ shipped** — 10-band graphic EQ + master pre-amp,
  `settings_dialog.py:807-980`. Enabled checkbox, preset combo,
  save/delete, double-click snap-to-zero, cast-greying via "EQ
  applies to local playback only and is inactive now" caption.
- 1238 → 1334 tests.

---

## ❌ Explicitly NOT on the roadmap

Per `[[project-competitive-positioning]]`:

- **Local-file libraries** — Strawberry / Tauon heritage territory.
- **Podcasts** — out of music-only scope.
- **Mobile (Android / direct-iOS)** — Symfonium / Finamp own those.
- **Heavy audiophile DSP** (AutoEQ, 256-band PEQ) — Symfonium
  uncatchable.
- **CarPlay / Android Auto** — mobile-only.

## ⛔ No longer in this TODO (verified obsolete 2026-05-19)

- ~~Verify visualizer renders + cycle UX~~ — shipped 2026-05-19;
  august verified live during the chill-wave PM session.
- ~~Visualizer mpv `lavfi-complex` audio tap~~ — shipped via
  `MonitorAudioTap` per-stream `pw-record` (not lavfi; the IPC path
  was abandoned for direct PipeWire taps which are simpler).
- ~~Smart playlists editor UI~~ — shipped end-to-end.
- ~~Crossfade "no code yet"~~ — backend shipped behind
  `JT_CROSSFADE=1`. Only Settings UI exposure remains (now P2).
- ~~Notifications backend package "does not exist yet"~~ — shipped
  with Linux (`notify-send`) + unsupported stub for Win/macOS.
- ~~EQ~~ — full 10-band graphic + pre-amp shipped.
- ~~Stylesheet parse warning hunt~~ — `1e004d1` and friends silenced
  the parse warnings; regression test in place.

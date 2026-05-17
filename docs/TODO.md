# jellytoast project TODO

The running development backlog. Last rewritten 2026-05-17 after the
autonomous-agent queue cleared and the full cleanup sweep landed.

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
- `docs/competitive_audit.md` — 2026-05-15 audit; source of the strategic priorities.

---

## 🛑 In-flight (pickup here next session)

1. **Visual verify the SVG-driven app icon** — `make_app_icon()`
   rasterizes `packaging/icons/jellytoast.svg` via QSvgRenderer.
   Confirm the design shows in window decoration, system tray, and the
   Alt-Tab task switcher after a fresh launch.
2. **Manual test plan items §1.1–§1.3** — the real-world connectivity
   disconnect pass (pull Ethernet / toggle Wi-Fi off, watch terminal
   for `connectivity → unreachable` after 3 failed requests).

No `auto/*` branches pending — queue at zero as of 2026-05-17.

---

## P0 — Now

### 📦 AUR + Flathub packaging — **L, the moat-gate**
Until jellytoast is one `flatpak install` away, no differentiator
reaches users. Strawberry, Supersonic, Feishin all have Flathub.

Prereqs done (2026-05-17):
- `[build-system]` in pyproject.toml; repo is `pip install -e .`-able.
- `gui-scripts` entry point exposes `jellytoast`.
- Single source of truth for deps (requirements.txt dropped).
- Heavy per-feature deps moved to optional extras (visualizer / dlna /
  sonos / snapcast).

Remaining sub-tasks:
- AUR PKGBUILD (community/AUR — hours).
- Flathub manifest + screenshots (days).
- Capture screenshots and re-enable the `<screenshots>` block in
  `packaging/io.github.augustvontrips66.jellytoast.metainfo.xml`.

### 🎨 Audit font usage across the UI — **S/M**
Reported 2026-05-16: "different things going on in different parts
of the UI" font-wise. Per [[feedback-typography-tokens]] every widget
should flow through `type_qss(TYPE_*)` from `design_tokens`; raw px
is reserved only for the A–Z rail (9px) and user-tunable lyric sizes.
Sweep: grep for `font-size:` / `setPointSize` / `setPixelSize` outside
design_tokens, identify mismatches, route them through tokens.
Visual pass on Library tiles, Songs rows, NP page, mini player,
Settings, top bar, account view.

Known raw-px violations (caught in 2026-05-17 audit, not yet fixed):
- `now_playing_bar.py:443` — 13px chevron font
- `airplay_pairing.py:203` — 22px PIN input

---

## P1 — Next strategic push

### 🔌 CastManager UI wiring for new backends — **M**
DLNA / Sonos / Snapcast backends shipped 2026-05-17 (A22 / A23 / A24)
but are dormant — CastManager doesn't call into them and discovery
results don't surface in the cast dialog.
- Discovery orchestration that respects A25's per-type toggles +
  on-demand vs startup mode.
- CastManager methods to push per-protocol.
- Result fanout into the (already-built) collapsible sections in
  CastDialog (A26).
- Each backend module is dormant until then.

### 📦 Offline Phase 6 — finish the moat — **M-L**
Per `docs/research/` + `memory/architecture-offline-phase5`. Largest
competitive moat (no maintained desktop peer has real offline
downloads). Partially shipped 2026-05-17:
- ✅ `retry_failed()` with exponential backoff (A21 follow-up).
- ✅ "Repair downloads" walk (disk-reconciliation).
- Remaining:
  - `pause()` / `resume()` UI in `modules/offline/manager.py`.
  - Wi-Fi-only download gating (manual toggle now, auto-detect later).
  - Staleness flag (`nodes.state = "stale"`) + manual re-sync via
    `offline.snapshot.resync` (currently `NotImplementedError`).

### 🎚️ EQ / DSP — **M, ~1 day**
Per `docs/research/eq_dsp.md`. 10-band graphic EQ via mpv's
`anequalizer` filter (ISO octaves 31Hz → 16kHz, ±12dB).
- New `playback/eq_enabled`, `playback/eq_preset`, `playback/eq_bands`
  QSettings keys.
- 8 built-in presets + user-saved.
- New `PlayerBus.eq_changed` signal (throttled).
- Settings → Playback page section.
- Off by default + "no longer bit-perfect" disclosure.
- Greys out during cast sessions.
- ⚠ Use `anequalizer`, not the deprecated `equalizer` filter.

### 📻 Internet radio UI — **M**
Per `docs/research/radio_and_seeded_queues.md`. Provider CRUD shipped
(Subsonic native, Jellyfin local) — UI surface pending:
- "Internet Radio" tab in the library nav.
- Add / edit / delete station form.
- NP surface: replace scrubber with elapsed + stop (live stream).
- mpv ICY title observation via `metadata/by-key/icy-title` →
  new `PlayerBus.radio_title_changed`.
- `cast_proxy` already handles redirects + Range — radio streams ride
  the existing code path, no new cast code.

### 🎯 Artist / album / track seeded radio — **M**
Per `docs/research/radio_and_seeded_queues.md`. Ships AFTER internet
radio UI.
- `QueueKind.INSTANT_MIX` already exists in `player_state.py`
  (unused) — slots in cleanly.
- Three new provider methods: `get_similar_songs`, `get_instant_mix`,
  `get_genre_radio`. Subsonic aliases mix → `getSimilarSongs2`;
  Jellyfin implements both natively.
- Two additive `QueueContext` fields: `seed_kind`, `radio_played_ids`.
- Continuous extension: re-seed when queue runs low.
- Right-click → "Start radio from here" affordance everywhere.

### 🎶 Smart playlists editor UI — **M**
Per `docs/research/smart_playlists.md`. Evaluator + multi-rule logic
shipped 2026-05-17 (`smart_playlist-evaluator`). Editor UI pending:
- Local `smart_playlists.json` (rules), not server-side write.
- Chips + live preview pane in the Playlists area.
- Recipes to ship: "Recently added", "Forgotten favorites", "Top
  played", "Year X".
- v2: layer in read-only surfacing of Navidrome's `.nsp` server-
  native smart playlists via OpenSubsonic `readonly: true`.

### 🎬 Cast-proxy demo GIF — **S**
The 30-second README hero shot: Chromecast playing music from a
Tailscale-only Navidrome with the laptop offline. Unique-to-jellytoast
— no competitor can demo this. Pairs with Flathub screenshots in P0.

### 🎫 Last.fm API key registration (august task)
Register at `last.fm/api/account/create`, drop API_KEY / API_SECRET
into `modules/scrobble/lastfm.py:43-44`. Settings → Scrobbling Last.fm
half lights up automatically.

### 🎨 Visualizer rendering widget — **M**
FFT pipeline shipped 2026-05-17 — math, worker thread, bus signal,
optional numpy extra. No paint surface yet (gated on subjective
tuning per `docs/autonomous_tasks.md`).
- Third `np_left_pane_mode = visualizer` on NP page.
- QPainter render off `PlayerBus.visualizer_bands_changed`.
- Real mpv lavfi-complex audio tap (currently a stub returning silence).
- Cast edge: ship "Casting to <device>" placeholder, not a frozen frame.

---

## P2 — Important parity / quality

### 🔔 Server-side scrobble badge — **S, highest ROI**
Per `docs/research/parity_small_items.md`. Already-populated settings
(`server_scrobbles_lastfm`, etc.) just need a label on the NP bar
saying "Scrobbled by Navidrome." Genuinely free win.

### 🌐 Multi-server hostnames — **M**
Per `docs/research/parity_small_items.md`. Extend connectivity tracker
to try alternates on failure before declaring unreachable.
- `server/hostnames` JSON setting.
- Login UI: "+ Add alternate URL" affordance.
- New `PlayerBus.host_switched` signal.

### 🔀 Crossfade — **M+**
Per `docs/research/crossfade.md`. Two alternating libmpv instances
(ping-pong A→B). Smart-album-continuity check routes same-album
adjacent tracks back through gapless. v1 behind `JT_CROSSFADE=1` env
flag before exposing Settings toggle.
- `playback/crossfade_enabled`, `playback/crossfade_duration_ms`,
  `playback/crossfade_smart_album_continuity`.
- Greys out during cast (cast plays server's raw stream).
- ⚠ Audio device contention on Windows WASAPI exclusive + raw ALSA.

### ⌨️ Hotkey rebinding — **M**
Per `docs/research/parity_small_items.md`. `modules/hotkeys.py`
registry exists; need `QKeySequenceEdit` per row in Settings →
Hotkeys (currently read-only).

### 🏷️ Tag editing UI — **M, Jellyfin-only**
Backend shipped 2026-05-17 (`provider.can_edit_metadata` cap +
`update_track_metadata` + LockedFields workaround). UI pending:
- Right-click "Edit tags…" in views/NP page.
- v1: single-track edit + cover-art upload.
- v2: bulk-album edit ("Apply to all in this album").

### 🎨 Theme modes — **L (was M, reclassified)**
Per `docs/research/parity_small_items.md`. The `rgba(255,255,255,...)`
audit hits 15 files / 95 occurrences — bigger than expected. Two-phase:
1. Live-apply theme MODE (currently requires restart — Qt 6.5+
   `QStyleHints.colorSchemeChanged` enables auto-detect).
2. Light theme + the audit (the long pole).

### 🎼 ReplayGain mode UI toggle — **S**
Setting exists (`playback/replaygain`: no/track/album); verify the
Settings → Playback combo lets the user pick it.

### 🧹 Settings duplicate property cleanup — **S**
A25 added `cast_<type>_enabled` properties; per-protocol modules
(A22/A23/A24) added `<type>_enabled` properties. Both back the same
QSettings key. Pick one naming convention and delete the other.

---

## P3 — Stretch / deferred

### Bug: `set_offline_mode("yes")` doesn't coerce — **S**
A3 finding. `_set_offline_mode_internal` stores raw value; only the
settings setter wraps `bool(...)`. Public API should coerce at
boundary. No caller hits this today.

### 📡 Registered Cast receiver app — **L, needs $ + hosting**
Screens show "Default Media Receiver" instead of "jellytoast". $5
Google dev account + hosted custom receiver web app.

### 🎵 AirPlay 2 sender refinements
Edge cases (LG webOS, shairport-sync 5.x) behind
`reference_airplay2_pyatv_compat`.

### 🔌 `QNetworkInformation` integration
Supplementary connectivity signal. Linux flaky; revisit on
Windows/macOS.

### 📥 Server-side playlist import (m3u, etc.)
Several clients allow it. Out-of-scope for music-only / streaming-
first unless requested.

---

## P4 — Hardware-gated / long-horizon

### 🪟 Windows native backends
- `media_controls/` → SMTC (Windows.Media.Control)
- `autostart/` → Run registry key
- `keep_above/` → Win32 `SetWindowPos(HWND_TOPMOST)`
- Verify PMv2 HiDPI path

### 🍏 macOS native backends
- `media_controls/` → NowPlayingInfoCenter via pyobjc
- `autostart/` → AppKit login items
- `keep_above/` → `NSWindowLevel`
- `notifications/` → NSUserNotification

### 📱 iOS native
After Mac. Sandbox awareness for downloads (no-backup flag), CarPlay
handoff, lock-screen artwork.

### 🎛️ ASIO / exclusive output
Windows-only audiophile feature. Strawberry has it. Only if a
Windows user asks.

### 🎵 Visualizer per-OS audio taps
Once v1 rendering ships (mpv-tap based), per-OS loopback backends
unlock visualization during cast:
- Linux: PipeWire / PulseAudio monitor sink
- Windows: WASAPI loopback
- macOS 14.4+: `CATapDescription` (native); pre-14.4 needs BlackHole
- iOS: AVAudioEngine tap on output

---

## ❌ Explicitly NOT on the roadmap

Per `memory/project_competitive_positioning.md`:

- **Local-file libraries** — Strawberry / Tauon heritage territory.
- **Podcasts** — out of music-only scope.
- **Mobile (Android / direct-iOS)** — Symfonium / Finamp own those.
- **Heavy audiophile DSP** (AutoEQ, 256-band PEQ) — Symfonium
  uncatchable.
- **CarPlay / Android Auto** — mobile-only.

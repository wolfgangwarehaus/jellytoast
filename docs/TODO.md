# jellytoast project TODO

The running development backlog. Last rewritten 2026-05-15 after the
research pass landed `docs/research/*.md` for every P1/P2 feature.

## How this list works

Each item carries a priority tag and an effort tag. Definition of
"done" rises with priority.

| Priority | Meaning | Done criteria |
|---|---|---|
| **P0** | Blocking momentum, in-flight bugs, or "ship before anything else" | Tests pass + SPEC.md updated + memory entry if user-facing |
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
- `docs/competitive_audit.md` — 2026-05-15 audit; source of the strategic priorities.

---

## 🌿 Open `auto/*` branches awaiting review

These need august's eyes before merging to `main`. Worktrees live at
`/home/august/Projects/jellytoast/.claude/worktrees/agent-<id>/`.

| Branch | Commit | Purpose | Tests |
|---|---|---|---|
| `auto/search-air-fix` | `1910cf6` | Bug 1 — search matches Album/AlbumArtist/Artists + synthesizes artist tiles | 13 ✓ |
| `auto/artist-page-offline-fix` | `6ee4b61` | Bug 2 — artist page AlbumArtist string fallback | 8 ✓ |
| `auto/connectivity-tests` | — | Phase 5 state machine tests | 12 ✓ |
| `auto/scrobble-tests` | `adf128c` | Eligibility math tests | 16 ✓ |
| `auto/migration-tests` | — | QSettings rename migration tests | 5 ✓ |

⚠ **Merge order:** A1 + A2 branched from `main` and rebuilt offline
accessors that already exist in this session's working tree. Commit
the Phase 5 UI first, then 3-way merge each branch and resolve in
favor of the branch's improved versions.

---

## P0 — Now

### 🐛 Two open Phase 5 bugs (autonomous fixes ready)
Both shipped via the `auto/*` branches above. Just need merge + verify.
- `auto/search-air-fix` — search "air" finds the Air album/artist
- `auto/artist-page-offline-fix` — artist page renders offline with only-album downloaded

### 🧪 Phase 5 real-world disconnect test pass
Captured in `manual_test_plan.md` §1. Requires august pulling the
cable.

### 📦 AUR + Flathub packaging — **L, the moat-gate**
Until jellytoast is one `flatpak install` away, no differentiator
reaches users. Strawberry, Supersonic, Feishin all have Flathub.
Sub-tasks:
- AUR PKGBUILD (community/AUR — hours)
- Flathub manifest + screenshots (days)
- AppStream metainfo with screenshots from the cast-proxy demo GIF

### 📝 SPEC.md drift cleanup — **S**
Phase 5 UI shipped this session but `docs/SPEC.md` doesn't mention
the offline chip, library/songs/search filters, or artist-page
offline fallback. Update §5 and §6 accordingly.

---

## P1 — Next strategic push

### 📦 Offline Phase 6 — finish the moat — **L**
Per `docs/research/` analysis + `memory/architecture-offline-phase5`.
The single largest competitive moat (no maintained desktop peer has
real offline downloads).
- `pause()` / `resume()` / `retry_failed()` in `modules/offline/manager.py`
- Wi-Fi-only download gating (manual toggle now, auto-detect later)
- Staleness flag (`nodes.state = "stale"`) + manual re-sync via
  `offline.snapshot.resync` (currently `NotImplementedError`)
- "Repair downloads" action in Settings → Downloads

### 🎚️ EQ / DSP — **M, ~1 day**
Per `docs/research/eq_dsp.md`. 10-band graphic EQ via mpv's
`anequalizer` filter (ISO octaves 31Hz → 16kHz, ±12dB).
- New `playback/eq_enabled`, `playback/eq_preset`, `playback/eq_bands`
  QSettings keys
- 8 built-in presets + user-saved
- New `PlayerBus.eq_changed` signal (throttled)
- Settings → Playback page section
- Off by default + "no longer bit-perfect" disclosure
- Greys out during cast sessions
- ⚠ Use `anequalizer`, not the deprecated `equalizer` filter

### 📻 Internet radio — **M**
Per `docs/research/radio_and_seeded_queues.md`. Ship FIRST (proves
the no-scrubber NP treatment).
- Wire Subsonic's 4 `*InternetRadioStation` endpoints (already
  exposed by Navidrome)
- Jellyfin users get a local-only `radio/stations` JSON list
- New `QueueContext.INTERNET_RADIO`
- mpv ICY title observation via `metadata/by-key/icy-title` →
  new `PlayerBus.radio_title_changed`
- NP surface: replace scrubber with elapsed + stop (live stream)
- `cast_proxy` already handles redirects + Range — radio streams ride
  the existing code path, no new cast code

### 🎯 Artist / album / track seeded radio — **M**
Per `docs/research/radio_and_seeded_queues.md`. Ships AFTER internet
radio.
- `QueueKind.INSTANT_MIX` already exists in `player_state.py`
  (unused) — slots in cleanly
- Three new provider methods: `get_similar_songs`, `get_instant_mix`,
  `get_genre_radio`. Subsonic aliases mix → `getSimilarSongs2`;
  Jellyfin implements both natively.
- Two additive `QueueContext` fields: `seed_kind`,
  `radio_played_ids`
- Continuous extension: re-seed when queue runs low
- Right-click → "Start radio from here" affordance everywhere

### 🎶 Smart / dynamic playlists — **M**
Per `docs/research/smart_playlists.md`. Client-side rule storage +
provider-rendered evaluation.
- Local `smart_playlists.json` (rules), not server-side write
- New provider method: `query_items(rules) -> List[items]`
- Each provider translates as much of the rule set as it can into a
  native server query; refines the remainder in Python
- v2: layer in read-only surfacing of Navidrome's `.nsp` server-
  native smart playlists via OpenSubsonic `readonly: true`
- Editor: chips + live preview pane in the Playlists area
- Recipes to ship: "Recently added", "Forgotten favorites", "Top
  played", "Year X"

### 🎬 Cast-proxy demo GIF — **S**
The 30-second README hero shot: Chromecast playing music from a
Tailscale-only Navidrome with the laptop offline. Unique-to-
jellytoast — no competitor can demo this.

### 🎫 Last.fm API key registration (august task)
Register at `last.fm/api/account/create`, drop API_KEY / API_SECRET
into `modules/scrobble/lastfm.py:43-44`. Settings → Scrobbling
Last.fm half lights up automatically.

---

## P2 — Important parity / quality

### 🔔 Server-side scrobble badge — **S, highest ROI**
Per `docs/research/parity_small_items.md`. Already-populated settings
(`server_scrobbles_lastfm`, etc.) just need a label on the NP bar
saying "Scrobbled by Navidrome." Genuinely free win.

### 💤 Sleep timer — **S-M, highest ROI**
Per `docs/research/parity_small_items.md`. Ephemeral (session-
scoped, no QSettings). Options: 15/30/60/90 min + "end of current
track". Action: pause OR fade-and-stop (user picks). New
`PlayerBus.sleep_timer_started` signal. Small dropdown on NP bar.

### 🎲 Smart shuffle — **M**
Per `docs/research/parity_small_items.md`. Rolling history window +
weight candidates by recency penalty × artist-spread penalty. Setting
`playback/smart_shuffle` (default off — preserve `random.shuffle` as
the simple option).

### 🌐 Multi-server hostnames — **M**
Per `docs/research/parity_small_items.md`. Extend connectivity
tracker to try alternates on failure before declaring unreachable.
- `server/hostnames` JSON setting
- Login UI: "+ Add alternate URL" affordance
- New `PlayerBus.host_switched` signal

### 🔀 Crossfade — **M+**
Per `docs/research/crossfade.md`. Two alternating libmpv instances
(ping-pong A→B). Smart-album-continuity check routes same-album
adjacent tracks back through gapless. v1 behind `JT_CROSSFADE=1`
env flag before exposing Settings toggle.
- `playback/crossfade_enabled`, `playback/crossfade_duration_ms`,
  `playback/crossfade_smart_album_continuity`
- Greys out during cast (cast plays server's raw stream)
- ⚠ Audio device contention on Windows WASAPI exclusive + raw ALSA

### 🎸 Audio visualizers — **M, deferred per-OS L**
Per `docs/research/visualizers.md`. v1: mpv `--lavfi-complex`
`asplit` tap → PCM into Python → FFT on QThread (via
`modules.async_io`) → QPainter render → third
`np_left_pane_mode = visualizer` on NP page.
- Defer per-OS loopback (PipeWire/WASAPI/CATap) to v2+
- Defer ProjectM / OpenGL to v3+
- Cast edge: ship "Casting to <device>" placeholder, not a frozen frame

### ⌨️ Hotkey rebinding — **M**
Per `docs/research/parity_small_items.md`. Bulk is the refactor, not
the widget. Shortcuts are inlined in `jellytoast.py:549-570`; need
a `modules/hotkeys.py` registry first. Then `QKeySequenceEdit` per
row in Settings → Hotkeys (currently read-only).

### 🎯 Scrobble eligibility precision — **S**
Already wired but uses summed forward position deltas with a 5s tick
cap. Edge cases: cap is exclusive on both ends (delta == 5000ms is
dropped, not capped). Refinement listed as scrobble Phase 4 polish.

### 🏷️ Tag editing — **M, Jellyfin-admin only**
Per `docs/research/tag_editing.md`. Documented cross-provider parity
exception (Subsonic + Navidrome have no edit endpoints).
- New `provider.can_edit_metadata` boolean gates UI
- Jellyfin `POST /Items/{id}` — ⚠ bug #10724: send full BaseItemDto
  with `LockedFields` or scheduled refreshes silently revert edits
- v1: single-track edit + cover-art upload
- v2: bulk-album edit ("Apply to all in this album")
- Right-click "Edit tags…" in views/NP page

### 🎨 Theme modes — **L (was M, reclassified)**
Per `docs/research/parity_small_items.md`. The `rgba(255,255,255,...)`
audit hits 15 files / 95 occurrences — bigger than expected. Two-
phase:
1. Live-apply theme MODE (currently requires restart — Qt 6.5+
   `QStyleHints.colorSchemeChanged` enables auto-detect)
2. Light theme + the audit (the long pole)

### 🎼 ReplayGain mode UI toggle — **S**
Setting exists (`playback/replaygain`: no/track/album); verify the
Settings → Playback combo lets the user pick it.

### 📡 Cast protocol expansion — **M / M / M**
Research landed 2026-05-15 in `docs/research/casting_dlna.md`,
`casting_sonos.md`, `casting_snapcast.md`. Three new protocols
slot alongside the existing Chromecast + AirPlay 2 paths:
- **DLNA / UPnP** (`async-upnp-client>=0.47.0`) — biggest reach;
  smart TVs, AV receivers, NAS-attached players. Autonomous slice
  is A22 in `docs/autonomous_tasks.md`.
- **Sonos** (`soco>=0.31`) — primarily for older Sonos S1 hardware
  (no AirPlay 2 path). august has no Sonos hardware, so ships
  "should-work, untested." Autonomous slice is A23.
- **Snapcast** (`snapcast>=2.3.8`) — control surface (Option B)
  only in v1; audio routing (Option A) deferred to v1.5 Linux-
  experimental. Autonomous slice is A24.

Prerequisites: **A25** (per-type cast toggle + discovery-timing
settings) + **A26** (unified collapsible cast menu) land first so
the new protocols slot in cleanly.

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

### 🔀 `download-ux` branch
Unmerged. Decide whether superseded by Phase 5 work or fast-forward.

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
Once v1 ships (mpv-tap based), per-OS loopback backends unlock
visualization during cast:
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

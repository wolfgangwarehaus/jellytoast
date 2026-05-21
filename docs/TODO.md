# jellytoast — what's left to do

The running backlog, in plain language. Last refreshed **2026-05-21**
against the code on `main` (`94371c6`, 1533 tests passing).

Companion docs:

- `docs/manual_test_plan.md` — things to check by hand / by eye.
- `docs/autonomous_tasks.md` — work that can be handed to an unattended
  agent.
- `docs/SPEC.md` — what the app actually does today.
- `CHANGELOG.md` — what's already shipped, dated.
- `docs/research/` — the original design docs for each feature (each
  now carries a status banner saying whether it shipped).
- `docs/decisions.md` — why certain architectural choices were made.

## How this list is ordered

Items are grouped by urgency. The old priority tags (P0–P4) are kept
in parentheses so the other docs that reference them still line up:

- **Right now (P0)** — the thing blocking everything else: packaging.
- **Next up (P1)** — high value, mostly small, do these soon.
- **Worth doing soon (P2)** — real quality/parity gaps, no rush.
- **Later (P3)** — genuine ideas, not yet load-bearing.
- **Hardware-blocked (P4)** — needs a Windows machine or a Mac.

---

## Right now — the packaging gate (P0)

This is the standing top priority and has been for several sessions.
Nothing about the app is blocked on more features; it's blocked on
being installable.

### Write the AUR package (a couple of hours)

The app has been pip-installable since 2026-05-17 — there's a proper
build system, a flat layout, and a `gui-scripts` entry point. All the
code-side prerequisites are done. What's left is writing the actual
Arch `PKGBUILD` file and submitting it to the AUR. It's largely
mechanical, but it needs a maintainer's judgement on the optional
dependencies and any post-install hooks, so it's best done together
rather than handed to an agent.

### Get onto Flathub (multi-day, lots of waiting)

The AppStream metadata file, the `.desktop` file, and the icons are
all already in `packaging/`. Still missing:

- **Screenshots.** Capture clean PNGs of Library, Now Playing, the
  Cast dialog, Downloads, Settings, the Visualizer, Smart Playlists,
  and Radio.
- The `<screenshots>` block in the metainfo XML is written but
  commented out — uncomment and fill it once the PNGs exist.
- **A Flatpak build manifest** (`.yaml`) — separate from the metadata
  file, doesn't exist yet.
- Then open a pull request against the `flathub/flathub` repo and
  expect days of back-and-forth with their reviewers.

---

## Next up (P1)

These are high value and mostly small — good things to pick up first
once packaging is moving.

### Record the cast-proxy demo clip

A 30-second hero clip for the README: a Chromecast playing music from
a Tailscale-only server while the laptop is offline. It shows off the
single most distinctive thing the app does. Needs a real recording
session — it pairs naturally with capturing the Flathub screenshots.

---

## Worth doing soon (P2)

Real quality and parity gaps. In every case here the **engine is
already built and tested** — what's missing is the user-facing
control. None of them is urgent.

Shipped 2026-05-20: crossfade controls (Settings → Playback, `JT_CROSSFADE`
env gate gone); the multi-server login UI (an alternate-URL manager
dialog + a toast on failover, plus a reusable `modules/toast.py`); the
editable Settings → Hotkeys page (per-action `QKeySequenceEdit` with
conflict detection, live re-apply, per-row + reset-all); and
single-track tag editing (right-click "Edit tags…", Jellyfin admins).

### Tag editing — cover art + bulk edit

Single-track tag editing shipped 2026-05-20 (right-click "Edit tags…"
on Jellyfin, admin-gated). Still to do: **cover-art upload** (the
`upload_cover_art` provider method is a stub — needs multipart-form
handling) and **bulk "apply to whole album"** editing.

### Theme modes — the light theme (Phase 4)

Phases 1-3 of the theming rework shipped 2026-05-21: the semantic
token layer, ~170 hard-coded white literals routed through `ink_alpha`,
and live theme-mode switching (no restart). What's left is **Phase 4**
— authoring the actual light `Theme` (real visual QA, not a mechanical
invert), a light "Transparent" variant, and a Light/Dark/Follow-system
setting reading `QStyleHints.colorScheme()`. The setting is the easy
finish; the palette itself is the work, and it needs august's eye.

---

## Later (P3)

Real ideas, but not yet pulling weight.

- **A registered Cast receiver app.** Right now Chromecast screens
  show "Default Media Receiver" instead of "jellytoast". Fixing that
  needs a $5 Google developer account and a small hosted web app.
- **AirPlay 2 edge cases.** A few specific receivers (older LG webOS
  TVs, shairport-sync 5.x) misbehave with the AirPlay library.
- **A supplementary network-status signal** (`QNetworkInformation`) —
  flaky on Linux; worth revisiting when the Windows/macOS work starts.
- **Importing server-side playlist files (m3u, etc.)** — probably out
  of scope for a streaming-first music app unless someone asks.

---

## Hardware-blocked (P4)

These need a Windows machine or a Mac, neither of which is available
for testing yet, so writing the code now would be writing it blind.

- **Windows support** — the native bits for media-key integration,
  autostart, always-on-top, and notifications; plus checking the
  HiDPI path.
- **macOS support** — the same set of native bits via the Mac APIs.
- **iOS** — only after a Mac exists. Needs download-storage sandbox
  handling, CarPlay handoff, lock-screen artwork.
- **Exclusive audio output (ASIO)** — a Windows-only audiophile
  feature; only if a Windows user asks for it.
- **Per-OS visualizer audio taps** — the Linux audio tap works; the
  visualizer needs equivalent taps on Windows, macOS, and iOS for
  cross-platform parity.

---

## Tiny loose ends

- There's a stale code comment in `modules/cast/cast_dialog_sections.py`
  (around line 31) claiming the DLNA/Sonos/Snapcast sections "stay
  empty" — they don't anymore; the discovery wiring landed afterward.
  Worth correcting next time that file is open.

---

## Recently shipped

The full dated history lives in `CHANGELOG.md`. The short version of
the last few sessions: smart playlists end-to-end, the audio
visualizer, internet radio, the 10-band EQ, the whole downloads /
offline system, all five casting protocols wired up, the right-click
"Create smart playlist" and "Start radio" menu entries, the
sleep-timer menu (moon button in the now-playing bar) and the
smart-shuffle toggle in Settings → Playback, and — most recently —
smart-rule schema v2: date-based smart-playlist rules (`date_added` /
`last_played`), merged and verified against live Jellyfin / Subsonic
servers.

---

## Parked — deferred, not dropped

- **Last.fm scrobbling.** The client code is built and stays dormant
  in `modules/scrobble/lastfm.py`, but registering the in-app API key
  needs a Last.fm account — and their signup firewall (Error 406)
  blocked it repeatedly, from several networks and devices. The
  Settings → Scrobbling page hides the Last.fm section entirely while
  `API_KEY` / `API_SECRET` are empty; populate them to bring it back.
  **ListenBrainz** is the supported scrobbling path and works today.

---

## Explicitly not on the roadmap

Deliberately out of scope — each is a fight a competitor already wins:

- **Local-file libraries** — that's Strawberry / Tauon territory.
- **Podcasts** — outside the music-only focus.
- **A mobile app** — Symfonium and Finamp own that space.
- **Heavy audiophile DSP** (automatic headphone correction, very
  high-band parametric EQ) — Symfonium is uncatchable there.
- **CarPlay / Android Auto** — mobile-only concerns.

# jellytoast — what's left to do

The running backlog, in plain language. Last refreshed **2026-05-23**
against `main` (`3f039b7`, 1692 tests passing) following a full
code+doc audit pass.

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

**Phase plan (2026-05-23):** the feature list is complete enough. The
remaining gaps are small, well-scoped, and not blocking. We're now in
the **bug-squash phase** before packaging.

1. **Bug squash** — close the audit-surfaced correctness bugs and
   walk the manual test plan. This is the work that makes the project
   genuinely dialled in.
2. **Tiny feature finishers** — the three small UI pieces that close
   out the feature list (cover-picker dialog, bulk-edit dialog,
   crossfade easing curve).
3. **Packaging** — scaffolded, deferred until 1 + 2 are done.
4. **Later (P3)** — real ideas, not yet load-bearing.
5. **Hardware-blocked (P4)** — Windows / Mac / iOS.

---

## Bug squash — primary focus

### Audit-surfaced bugs (2026-05-23) — DRAINED

All nine items from the morning's full-codebase audit landed in
`dd21314` (HIGH/MEDIUM/LOW batch) and the round-2 follow-up.
Specifically: sign-out flush, FloatingMiniPlayer pinned to
`_refresh_provider_refs`, theme-change `Qt.UniqueConnection` for
CastDialog + VolumePopup, `_OpaqueComboBox` flag-set ordering,
`kde_titlebar` fall-through early-return, `offline.library_sync`
QTimer parent, the local re-import sweep. The scrobble `>= vs >`
boundary was reverted — the existing test contract explicitly
asserts "exactly 30s ≠ eligible," so the audit recommendation was
wrong.

### Deep-audit round-2 follow-ups — still open

These came out of the deeper code audit (perf + correctness agents).
The high-impact ones landed in round 2; what's listed here is what's
left.

**MEDIUM**

- **Light-theme gaps in newer surfaces.** `modules/radio_view.py`,
  `modules/smart_playlist_editor.py`, `modules/tag_editor.py` bake
  `ACCENT` / `TEXT_DIM` / `TEXT_FAINT` colours at construction
  without subscribing to `PlayerBus.theme_changed`. After a theme
  switch with these views already built, colours stay stale until
  the user reopens the view. Fix is mechanical but view-by-view
  (rebuild QSS strings on the signal) and worth visual checking, so
  it pairs with the manual-test walkthrough rather than landing
  blind.
- **`queue_manager.save_queue` synchronous on GUI thread.**
  `modules/settings.py:2138-2152` serializes the full queue payload
  to JSON + does an atomic rename, called from every `add_next` /
  `add_to_end` / `play_now` / play-head advance. A 200-track radio
  queue serializes the entire payload on every track change. Worth
  measuring on a real radio session — if the disk write is
  observable, debounce via 500 ms `QTimer` so a burst of mutations
  coalesces to one write.

**LOW**

- **Per-paint QFont / QFontMetrics allocation** in
  `library_grid._TileDelegate.paint`, `_SongRowDelegate.paint`, and
  `now_playing_page._TrackDelegate._paint_track`. Each paint allocs
  2–4 QFonts + QFontMetrics objects to elide the same titles against
  the same widths. Cache the `(QFont, QFontMetrics)` pair on the
  delegate, invalidate on `theme_changed`. Skipped today because
  measurement would help size the win before disturbing the paint
  path.
- **DPR cache-key fragmentation outside library_grid.** Cover-fetch
  sizes in `search_view.py:160`, `artist_page.py:599,619,664`,
  `now_playing_bar.py:1940,1965` still compute physical pixels from
  the raw `screen_dpr()` rather than `dpr_bucket()`. Wayland's
  fractional-DPR drift fragments the on-disk cache key so a "loaded"
  surface re-hits the network on reload. Library_grid was fixed
  differently (fixed-source-px); needs a unified pattern decision
  before rolling out to the rest.
- **Production `print(` sites (~30 across the codebase).** Migrate
  to the standard `logging` module so log output is level-filterable
  and respects a user log-level setting. `visualizer.py` already
  uses `logging.getLogger(__name__)` — that's the pattern. Sweep
  through `cast_proxy.py` (~18), `hotkeys.py`, `airplay2.py`,
  `radio_art.py`, `disk_cache.py`, `single_instance.py`,
  `airplay_pairing.py`, `queue_manager.py:585`, `offline/manager.py`.

### Manual test plan walk-through

`docs/manual_test_plan.md` carries the by-hand verifications that
have never been confirmed against a real server. The "Ready to verify
now" sections are:

1. Smart playlists editor + live preview (`§1`)
2. Start-radio right-click entries (`§2`)
3. Internet radio (`§3`)
4. Audio visualizer (`§4`)
5. Cast dialog — all 5 protocols (`§5`)
6. Downloads — Phase 6 behaviours (`§6`)
7. Smart-rule schema v2 — date-based rules (`§7`)
8. Sleep timer (`§8`)
9. Smart shuffle behaviour (`§9` — now always-on, verify the
   anti-clustering still holds)

Walk these end-to-end against a live Jellyfin **and** a live Subsonic
server. Anything that breaks goes back into this Bug-squash section.

### Provider live-server checks

These backends are unit-tested via mocked HTTP but have **never been
exercised against a live server**:

- `upload_cover_art` (Jellyfin `JellyfinAPI.upload_primary_image`).
- `update_album_track_metadata` (Jellyfin bulk-edit backend; Subsonic
  unsupported).

Confirm against a live Jellyfin instance before depending on either
in the UI.

---

## Tiny feature finishers

The three small UI pieces that close out the feature list. Backends
are built; what's left is the user-facing finish.

### Cover-picker control in the Edit-tags dialog

`upload_cover_art` is mocked-HTTP tested and ready to drive. The
dialog needs a file-picker button + a small preview pane wired to
the provider method. Visual — build with august.

### Bulk-edit dialog ("apply to whole album")

`update_album_track_metadata` shipped 2026-05-22 (per-track
fault-tolerant; returns `{album_id, succeeded, failed, total}`). The
dialog needs an "Apply to whole album" affordance + a confirm step.
Visual — build with august.

### Crossfade easing curve

The crossfade volume ramp is a deliberate linear v1 placeholder
(`modules/playback/crossfade.py:316`, marked `TODO(august)`).
Swapping in a tuned curve (equal-power, S-curve) is a subjective
polish call — august's to make. Design notes:
`docs/research/crossfade.md` §5.

---

## Packaging — scaffolded, deferred

Deferred by choice: bug-squash + tiny finishers come first. Nothing
is dropped — the scaffolding is done so it's a short hop when the
time comes.

### AUR package

The app has been pip-installable since 2026-05-17 — proper build
system, flat layout, `gui-scripts` entry point. What's left is
writing the Arch `PKGBUILD` and submitting it. Mechanical, but it
needs maintainer judgement on optional dependencies and post-install
hooks — do it with august.

### Flathub

The AppStream metadata file, the `.desktop` file, and the icons are
all in `packaging/`. Still missing:

- **Screenshots.** Clean PNGs of Library, Now Playing, the Cast
  dialog, Downloads, Settings, the Visualizer, Smart Playlists, Radio.
- The `<screenshots>` block in the metainfo XML is written but
  commented out — uncomment and fill it once the PNGs exist.
- **A Flatpak build manifest** (`.yaml`) — doesn't exist yet. Must
  grant `--filesystem=xdg-data/kwin` so `modules/drag_repaint/` can
  install its KWin scripted effect from inside the sandbox. Drafting
  this is queued as a candidate autonomous task (AT-5).
- Then a pull request against `flathub/flathub` and days of reviewer
  back-and-forth.

### Cast-proxy demo clip

A ~30-second hero clip for the README: a Chromecast playing music
from a Tailscale-only server while the laptop is offline — the single
most distinctive thing the app does. Needs a real recording session;
pairs naturally with capturing the Flathub screenshots.

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

## Recently shipped

The full dated history lives in `CHANGELOG.md`. The short version of
the last few sessions: dead-weight settings cleanup (gapless, smart
shuffle, MPRIS, streaming-info all promoted from opt-in toggles to
always-on), see-it/fix-it polish, titlebar double-click respecting
`kwinrc`, unified elevated-surface treatment for dark themes, the
audio routing fix (PipeWire 1.6.5 link-policy + WirePlumber persisted
mute), borderless main window, light themes end-to-end, smart
playlists end-to-end, the audio visualizer, internet radio, the
10-band EQ, the whole downloads / offline system, all five casting
protocols wired up, smart-rule schema v2, crossfade controls, the
multi-server login UI, the editable Hotkeys page, single-track + bulk
tag editing backends.

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

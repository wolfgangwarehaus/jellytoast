# jellytoast — what's left to do

The running backlog, in plain language. Last refreshed **2026-05-27**
against `main` (`169cea9`, 1730 tests passing) after merging the two
pending `auto/*` branches (AT-6 + AT-7) and rolling forward from the
2026-05-26 audit.

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
2. **Packaging** — scaffolded, deferred until 1 is done.
3. **Later (P3)** — real ideas, not yet load-bearing.
4. **Hardware-blocked (P4)** — Windows / Mac / iOS.

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

**HIGH**

- **Migrate Chromecast discovery from `get_chromecasts(blocking=True)`
  to explicit `CastBrowser`.** pychromecast 14.0.10's
  `get_chromecasts` blocking path internally calls the deprecated
  `discover_chromecasts`, which logs at INFO every discovery sweep
  ("discover_chromecasts is deprecated and will be removed in June
  2024, update to use CastBrowser instead.") The deadline has slipped
  three times but the library will eventually drop the function and
  our discovery breaks silently. Replace the call in
  `modules/cast_manager/_chromecast.py:47` with a `CastBrowser` +
  `SimpleCastListener` event-driven pattern (`start_discovery()` /
  `stop_discovery()` + callbacks). Shape change: the current
  one-shot blocking list becomes an add/remove flow — touches the
  caller in `_manager.py:44` and any test stubs in
  `tests/test_cast_gating.py`. Until then, see the
  `logging.getLogger("pychromecast.discovery").setLevel(logging.WARNING)`
  one-liner mute (queued to land alongside this work, NOT before, so
  the warning keeps poking at us as a daily reminder).

**LOW**

- **Per-paint QFont / QFontMetrics allocation** in
  `library_grid._TileDelegate.paint`, `_SongRowDelegate.paint`, and
  `now_playing_page._TrackDelegate._paint_track`. Each paint allocs
  2–4 QFonts + QFontMetrics objects to elide the same titles against
  the same widths. Cache the `(QFont, QFontMetrics)` pair on the
  delegate, invalidate on `theme_changed`. Skipped so far because
  measurement would help size the win before disturbing the paint
  path.

Drained this session:

- ~~Production `print(` sites → `logging` sweep~~ — drained
  2026-05-26 (`d63b55f`). All 119 production calls migrated; default
  INFO, override via `JT_LOG_LEVEL`.
- ~~DPR cache-key fragmentation outside library_grid~~ — drained
  2026-05-27 via the AT-7 merge (`169cea9`). `search_view`,
  `artist_page` header + tiles, `now_playing_bar` live + prefetch,
  and `songs_view` all switched to the unified fixed-source-px
  pattern (`LOGICAL × 3`). +6 tests verify each site's
  `get_image_url` size is DPR-invariant across 1.0 / 1.5 / 2.0.
  Radio cover (`now_playing_bar.py:2133`) intentionally left alone
  — its L2 raw key is the URL itself, no DPR fragmentation.

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
10. Crossfade equal-power curve (`§10` — new 2026-05-25; verify the
    perceived-loudness flatness across cross-album fades)

Walk these end-to-end against a live Jellyfin **and** a live Subsonic
server. Anything that breaks goes back into this Bug-squash section.

### Audiophile playback path

Roadmap from `docs/research/bit_perfect_playback.md`. Goal: match the
audiophile-tier bar (Audirvana / Roon / foobar2000 / HQPlayer) while the
EQ research in `docs/research/eq_dsp_v2.md` lifts the DSP side toward
Symfonium parity. The mpv config in `_make_mpv_handle` is already
audited-clean — corners are downstream.

- **T1 — landed 2026-05-27.** `docs/bit_perfect.md` user guide.
  Zero code in the audio path; documents the contract and the PipeWire
  recipe.
- **T2 — landed 2026-05-27.** "Bit-perfect mode" toggle at the top of
  Settings → Playback. When on: `set_volume` clamps to 100 at the
  source (`player_backend.py:1109`), Normalization / EQ / Crossfade
  controls disable + force to safe values, volume slider in the
  now-playing bar disables + tooltip, "Lossless · " prefix appears on
  the streaming-info pill when source is `Original` quality. Backed
  by `PlayerBus.bit_perfect_changed` for live UI updates. +4 tests
  (`test_bit_perfect_mode.py`).
- **T3 — landed 2026-05-27.** `audio_exclusive` sub-toggle nested
  under Bit-perfect mode in Settings → Playback. When enabled, mpv
  opens with `audio-exclusive=yes` — WASAPI Exclusive on Windows,
  CoreAudio HogMode on macOS, sink-cork on PipeWire. The shared-mode
  fallback in `_make_mpv_handle` catches mpv #11600/#11733-style
  construction failures and retries without the flag so the app still
  launches. Runtime apply via `PlayerBus.audio_exclusive_changed` →
  `MpvController.set_audio_exclusive` — change takes effect on the
  next track open. +5 tests. **Live-tested on Linux/PipeWire only;
  Windows + macOS exclusive paths exist but are hardware-blocked.**
- **T4 — landed 2026-05-27.** "Install PipeWire bit-perfect config"
  button under the BIT-PERFECT section of Settings → Playback. Drops
  `10-jellytoast-bitperfect.conf` into
  `~/.config/pipewire/pipewire.conf.d/` with `default.clock.allowed-
  rates` + `resample.quality = 14`. Idempotent + reversible — the file
  carries an ID-stamp header so the Remove path won't touch a
  user-authored file at the same path. Linux-only (the button is
  hidden on Windows / macOS — PipeWire isn't a thing there).
  `modules/pipewire_setup.py` is the helper. +11 tests.

### EQ upgrade — Symfonium-parity research

Research in `docs/research/eq_dsp_v2.md`. The current 10-band biquad
graphic EQ is correctly implemented (real DSP, ±12 dB, 0.7-oct Q) but
the original design specified `anequalizer` and silently fell back to
the deprecated `equalizer` because of a syntax bug (`c-1` vs explicit
`c0|c1` per-channel binding). Fixing the wart unlocks parametric.

- **EQ T1 — landed 2026-05-27.** Fixed the `anequalizer` wart.
  `modules/eq_presets.py` `format_eq_filter_string` now emits a single
  `anequalizer` filter with concrete per-channel indices (`c0|c1|…`)
  instead of the cascaded `equalizer` biquads the v1 ship used as a
  workaround. `apply_eq` in `player_backend.py` queries mpv's
  `audio-params/channel-count` and passes it to the formatter so
  mono / stereo / 5.1 sources all get the correct band cross-product.
  +4 tests (mono, surround, fallback on invalid count, explicit
  no-`c-1` check). One filter instance = cleaner composite phase
  than 10 cascaded biquads + unblocks T3 (per-band Q/freq).
- **EQ T2 — landed 2026-05-27.** Linear-phase FIR via `firequalizer`,
  opt-in. New `Settings.eq_linear_phase` (default False) and a
  "Linear phase" checkbox next to the EQ Enable toggle in
  Settings → Playback. `format_firequalizer_string` builds the
  `gain_entry='entry(f,g);...':zero_phase=on:delay=0.02` filter;
  `apply_eq` picks between `anequalizer` (IIR) and `firequalizer`
  (FIR) per the setting, with `linear_phase` baked into
  `_last_eq_state` so toggling forces a re-apply. Same bit-perfect /
  cast gating as the rest of the EQ section. +11 tests (7 formatter,
  4 apply_eq pick).
- **EQ T3 — landed in slices.**
  - **T3a — landed 2026-05-27.** AutoEQ ParametricEQ.txt import.
    `parse_autoeq_profile()` reads autoeq.app-format profiles (PK
    filters kept; LSC/HSC recorded as "skipped"). Parametric
    formatters `format_anequalizer_parametric` and
    `format_firequalizer_parametric` accept arbitrary centre
    frequencies + per-band Q (`w = f / Q`). New
    `Settings.eq_autoeq_profile_json` stores the active profile;
    `apply_eq` switches to the parametric path when it's populated
    and adds the profile's pre-amp to the user's master pre-amp.
    Settings UI: AutoEQ status row + Import dialog (with live
    parsing preview) + Clear button below the slider grid. Graphic
    EQ controls grey out while a profile is loaded. +28 tests.
  - **T3b — landed 2026-05-27.** Parametric curve editor in
    `modules/eq_curve_editor.py`. Log-frequency canvas (20 Hz → 22 kHz),
    dB y-axis (-15 → +15), grid + axis labels, accent-coloured
    cumulative response curve, draggable nodes per band. "Curve"
    toggle on the EQ row swaps the slider grid for the editor;
    persisted via `Settings.eq_view_advanced`. Drag y always works;
    x-drag unlocks when an AutoEQ profile is loaded (movable centres).
    `band_dragging` mirrors back to the slider widget live; release
    persists to `eq_bands` (graphic mode) or `eq_autoeq_profile_json`
    (AutoEQ mode). +23 tests for the coordinate-transform + response
    math (the widget itself unit-tests via its pure functions; the
    QPainter surface is visually verified in the dev workflow).
  - **T3c — landed 2026-05-27.** Full parametric ergonomics on the
    curve editor — mouse-wheel on a node adjusts Q (1.2× per notch,
    clamped to [0.1, 20]); double-click on empty canvas adds a band
    at the click freq/gain (capped at 16 = `MAX_BANDS`); right-click
    on a node removes it (refuses to drop the last band so the cache
    stays sane). Hover/drag floating tooltip surfaces (freq · gain ·
    Q) over the active node. All three gestures are PEQ-mode-only;
    graphic mode keeps its fixed 10-band ISO layout. Q stays put as
    the user drags a node's centre (recomputes `w` to preserve the
    chosen Q). +6 tests for `width_to_q` + `MAX_BANDS` invariant.
    This lands genuine Symfonium PEQ parity for the common case
    (movable centres + per-band Q + add/remove); GEQ-side
    5/10/15/31-band layout selector is the one remaining piece and
    is deferred under "Later (P3)" — the curve editor covers the
    audiophile use cases already.
- **EQ T4 — deferred.** Convolution / impulse-response AutoEQ headphone
  correction. Past Flathub launch.

### Provider live-server checks

These backends are unit-tested via mocked HTTP but have **never been
exercised against a live server**:

- `upload_cover_art` (Jellyfin `JellyfinAPI.upload_primary_image`).
- `update_album_track_metadata` (Jellyfin bulk-edit backend; Subsonic
  unsupported).

Confirm against a live Jellyfin instance before depending on either
in the UI.

---

## Tiny feature finishers — drained 2026-05-26

All three landed in `2efc487`:

- **Cover-picker control** — `tag_editor.py:196,374,422` (Replace
  cover button + preview pane wired to `upload_cover_art`).
- **Bulk "Apply to whole album"** — `tag_editor.py:152-157,374,412`
  ("Apply changes to all tracks on this album" checkbox calling
  `update_album_track_metadata`).
- **Crossfade easing curve** — `crossfade.py:322,365-383`
  (`_equal_power_gains` replaced the linear placeholder).

Live-server checks on Jellyfin still pair with the manual test plan.

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
the last two weeks:

- **2026-05-27** — AT-6 (+29 tests, single_instance / cast common /
  login alt-URLs) and AT-7 (+6 tests, DPR cache-key unification
  across search / artist / now-playing-bar / songs) merged. Suite
  1695 → 1730.
- **2026-05-26** — Logging migration (119 → stdlib `logging`),
  flatpak research note, tag-editor cover-upload reporting fix.
- **2026-05-25** — Settings dialog condense (Library page dropped,
  cache moved to Downloads); unified login + settings (inline URL
  edit, shared Selector, painted login card); cover-picker + bulk
  album edit + equal-power crossfade; live-accent staleness fix in
  radio / smart-playlist / tag-editor; queue-save debounce; A-Z
  snap-back fix.
- **2026-05-24** — Custom tooltip popup, sharp icons, uniform top
  bar, refined repeat glyph; `_Selector` replaces `QComboBox` in
  settings + frosted menus + centred dropdowns; lift-wash elevated
  surfaces + About dialog; frosted-popup pass + accent swatches +
  theme-swap perf.
- **2026-05-23** — Smart-playlist editor frosted chrome + dialog
  placement; radio stations cast cleanly; bug-squash batch + round 2
  (shutdown speed, sign-out flush, queue race, .part leak, range
  parse, signal leaks, lyrics perf, scrobble race, image cache
  eviction); dead-weight settings cleanup (gapless / smart shuffle /
  MPRIS / streaming-info all promoted from opt-in toggles to
  always-on); see-it/fix-it polish; titlebar double-click respecting
  `kwinrc`.

Older highlights still worth remembering: unified elevated-surface
treatment for dark themes, the audio routing fix (PipeWire 1.6.5
link-policy + WirePlumber persisted mute), borderless main window,
light themes end-to-end, smart playlists end-to-end, the audio
visualizer, internet radio, the 10-band EQ, the whole downloads /
offline system, all five casting protocols wired up, smart-rule
schema v2, the multi-server login UI, the editable Hotkeys page,
single-track + bulk tag editing backends.

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
- **CarPlay / Android Auto** — mobile-only concerns.

> **Note 2026-05-27.** "Heavy audiophile DSP" used to live in this
> list. Reconsidered after a benchmark against Symfonium found the
> gap is closeable in ~1 work-week (see EQ + audiophile-playback
> roadmaps above). Parametric EQ + bit-perfect mode are now active
> priorities; full convolution AutoEQ is still parked past Flathub.

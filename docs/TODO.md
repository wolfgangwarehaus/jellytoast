# jellytoast — what's left to do

The running backlog, in plain language. Last refreshed **2026-05-28**
against `main` (`ec544c8`, **1844 passed / 1 skipped**) after the AT-8
(CastBrowser) + AT-9 (delegate font cache) merges, then songs
pagination (`b80449b`) and the smart-playlist editor rework
(`ec544c8`), and a full multi-agent codebase audit (its findings are
the first section below).

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

### Full-codebase audit (2026-05-28) — fresh backlog

A multi-agent audit swept the codebase across 8 dimensions (structure,
performance, dead-code, the 15 architecture invariants, tests,
docs, robustness, deps); every finding was adversarially verified
against the real code. **Headline: the project is in genuinely good
shape** — zero invariant violations (14/15 clean; only the DPR
cache-key discipline deviates in 2 secondary surfaces), no bare
`except`, every network/subprocess call carries a timeout, no
stray prints / commented-out cruft, and the hot UI paths already cache
fonts + fetch covers at a DPR-stable size. Nothing crash-class was
found. The doc-drift the audit surfaced has already been applied this
session (SPEC §15, manual_test_plan, this header, CHANGELOG,
autonomous_tasks, two stale code comments, pyproject prose). What's
left is the real work, below.

**High — correctness / coverage on the moats:**

- ~~**DLNA cast path is half-wired**~~ — **DONE 2026-05-28** (`6085ca8`,
  `88d9a4f`). The audit found DLNA / Sonos / **Snapcast** all had
  discovery + dialog sections + stop-routing + unit-tested transport but
  no PLAY dispatch — every non-Chromecast pick fell through to the
  AirPlay-1 `POST /play` and silently failed (the dialog even labelled
  them "the yet-unmerged backends"). Fixed all three:
  - **DLNA + Sonos** — `CastManager.cast_to_dlna` / `cast_to_sonos`
    (`_others.py`) mirror `cast_to_chromecast/airplay`; both dispatch
    sites (`_cast_to_device` + `MpvController.play`) gained `dlna`/`sonos`
    branches (off the GUI thread — DLNA `play` blocks up to 30 s). Shared
    `modules/cast_payload.py` builds the DIDL metadata + 714 transcode
    fallback. +10 tests.
  - **Snapcast** — it's a control matrix, not a URL push, so a pick opens
    `modules/snapcast_control.py:SnapcastControlDialog` (connect →
    route groups to streams + per-room volume) instead of the
    active_cast play flow. `get_snapcast_controller()` singleton added.
    +11 tests.
  - **Still open (hardware-gated):** live-verify DLNA against a real
    renderer (TV / VLC "Renderer" mode — august can do this); Sonos +
    the Snapcast dialog's layout/UX need actual hardware to verify and
    a visual polish pass (no devices available). Tracked in
    `known_issues` + `manual_test_plan §5`.
- **Add real provider auth/streaming tests** → autonomous **AT-10**.
  Subsonic token+salt md5 (`subsonic.py:160`) and both providers'
  `get_audio_stream_url` / `report_playback_*` are exercised only via
  hand-rolled fakes, never the real implementation — a salt/md5 or
  response-shape regression passes CI and only breaks on a live server.
  _(medium)_
- **Test the Chromecast media-load / transport flow** → autonomous
  **AT-11**. `_chromecast.py` connect/cast/pause/seek/volume/stop
  (`:112,180,308,351-377`) + `chromecast_audio_mime_for` (`:146`) have
  zero coverage (only discovery/gating). _(medium)_

**Scrobble / shutdown lifecycle cluster** (surfaced by the
completeness-critic pass, all verified — no single dimension owned
this seam):

- **Currently-playing eligible track is lost on a non-tray quit.**
  Window-close / SIGTERM → `aboutToQuit` → `_cleanup`
  (`jellytoast.py:~3413`) calls `mpv_ctrl.shutdown()` directly with no
  `playback_stopped` emit and no scrobble flush — the tray Quit path
  gets it for free via `stop_requested`. Call
  `get_scrobble_manager().flush_current()` (or
  `_maybe_scrobble_current`) in `_cleanup` before mpv shutdown.
  _(medium)_
- **On-quit scrobble POST dies before its offline-queue fallback.** The
  final scrobble dispatches via `run_async` on the shared QThreadPool;
  `sys.exit(app.exec())` tears down with no `waitForDone()` anywhere,
  so the in-flight POST aborts and its `on_error → _enqueue_lb`
  fallback never runs → silently dropped. Write the on-quit scrobble
  straight to the queue synchronously, or add a bounded
  `waitForDone(2000)` in `_cleanup`. _(medium)_
- **`flush_pending()` ignores offline mode.** `_send_now_playing` /
  `_maybe_scrobble_current` gate on `settings.offline_mode`, but
  `flush_pending` / `_flush_listenbrainz_async` / `_flush_lastfm_async`
  (`scrobble/manager.py:386-454`) check only `*_enabled` + token — so
  at startup (called unconditionally, `jellytoast.py:3350`) and on every
  connectivity edge the app POSTs to ListenBrainz/Last.fm despite the
  user's explicit offline intent. Add
  `if self._settings.offline_mode: return` at the top of
  `flush_pending`. _(medium — privacy/correctness; trivial fix)_
- **Casting an already-eligible track double-scrobbles it.**
  `_cast_to_device` emits `stop_requested` (→ scrobble + `_current=None`)
  then re-emits `playback_started(_np)` (fresh `_TrackState`,
  `scrobbled=False`); the Chromecast status feed re-drives
  `position_updated`, re-crossing eligibility → second scrobble with a
  different `listened_at`. Suppress scrobble re-arming on a cast-handoff
  re-emit. _(medium)_
- **Queue-flush `remove()` mis-removes on a malformed entry.** Flush
  filters `pending[:MAX]` to `listens` then `remove(service,
  len(listens))`, but `queue.py:remove()` drops the **oldest N**
  regardless of which were sent — a malformed early entry → a never-sent
  entry discarded + a sent one re-sent (duplicate). Remove by
  index/identity, not "oldest N". _(low)_

**Medium — perf / structure:**

- **Cache the list-mode row cover** → autonomous **AT-13**.
  `library_grid._RowDelegate.paint` (`:1429-1436`) re-runs a 180→36px
  SmoothTransformation downscale + crop every paint, uncached; the
  sibling `_TileDelegate` already caches (`:1014`). Kills list-mode
  scroll jank on large libraries. _(small)_
- **Extract the EQ section out of `settings_dialog.py`.** ~1000 lines
  (`_build_eq_section` 1542 → `_emit_eq_changed` 2563, ~24% of the
  4335-line file) form one cohesive subsystem → `modules/settings_eq_panel.py:
  EqPanel(QWidget)` emitting `eq_changed`. Highest-value cut in the
  largest module. _(large — maintainability only, no correctness payoff;
  defer behind the above unless it becomes an editing bottleneck)_

**Smart-playlist editor follow-ups** (the editor was just reworked in
`ec544c8`):

- **Preview vs play disagree on empty-value rules** (the §1-walk bug,
  was item #18). `genre equals ""` → 25 alphabetical "matches" in the
  preview, 0 at play time. Validate-at-save (reject empty values) or
  reconcile the preview vs eval paths. _(medium)_
- **Preview has a stale-result race.** `_refresh_preview`
  (`smart_playlist_editor.py:~709`) fires the debounced
  `run_async(on_result=_on_preview_result)` with no generation token →
  whichever query *finishes* last wins, not which *started* last. Add an
  int generation counter captured in the closure. _(low)_

**Low — cleanup / robustness (batch-able):**

- **Dead-code purge (~17 verified-zero-caller symbols)** → autonomous
  **AT-12**. `downloads_view._refresh_download_all_visibility:933`;
  `library_grid._on_view_activated:2490` (also = Enter-to-browse on
  tiles silently does nothing — **wire** rather than delete if wanted);
  5 vestigial NP methods (`now_playing_page.py:480,483,499,663,2455`) +
  `_flush_pending_refresh:2733`; and 10 dead accessors across
  offline/crossfade/songs/eq/cast/presets/ui_helpers (see audit notes).
- **DPR fetch-size cleanup (invariant 4).** `mini_player.py:1243/1321/1380`
  and `downloads_view.py:225-231` couple the *server fetch size* to raw
  `screen_dpr()` instead of fixed `LOGICAL*3` (fragments raw/disk cache
  above ~2.5 DPR, floor-masked below). Also wrap `now_playing_bar.py:
  2135/2161/2300` target in `dpr_bucket()` for consistency. → folds into
  **AT-13**. _(small)_
- **Cache genres delegate fonts** → **AT-13** (`genres_view.py:156-160`).
- **Single-instance shared-memory key isn't per-user.**
  `single_instance.py:66` uses the static `QSharedMemory("jellytoast")`
  while the socket name (`:56`) is per-user — on a multi-user box, user
  B launches into a false-stale path and exits without a window. Make
  the segment key per-user too (`QSharedMemory(self._socket_name)`).
  _(low — multi-user only)_
- **Visualizer audio tap leak.** `visualizer.py:417-422` drops the dead
  subprocess on EOF without `stdout.close()`/`wait()` (FD + zombie) and
  the "restart next cycle" comment is false (nothing re-spawns →
  permanently flat after a mid-session sink loss). Close+reap and
  re-`start()` on `_proc is None`, or drop the comment. _(low; opt-in
  behind `JT_VISUALIZER=1`)_
- **Image-waiter fan-out guard.** `ui_helpers.py:1148-1165` invokes
  coalesced callbacks unguarded — a deleted-widget `RuntimeError` aborts
  the loop, skipping remaining subscribers. Wrap each `cb(pix)` in
  try/except. _(trivial)_
- **Harden Jellyfin auth-body parse.** `jellyfin_api.py:93-94` indexes
  `data["AccessToken"]` / `data["User"]["Id"]` directly — a 200 from a
  captive portal raises `KeyError` not a clean auth error. Use `.get()`
  + a domain error. _(trivial)_
- **Dependency-declaration hygiene** → autonomous **AT-14**: declare
  `python-xlib` (imported `jellytoast.py:2953`, undeclared); cap `pyatv`
  (`>=0.17` uncapped while `airplay2.py:81` drives the private
  `pyatv.support.rtsp` API); decide a PySide6 upper bound; reconcile the
  cap policy + lazy-vs-hard-dep modeling for pychromecast/zeroconf.
- **Shared-helper unification.** `_ElidingLabel` is reimplemented 3×
  (`library_grid.py:109`, `now_playing_page.py:179`, `songs_view.py:76`)
  → hoist to `ui_helpers`; two clashing `_round_corners` signatures
  (`now_playing_bar.py:38` vs `ui_helpers.py:1179`); the cast cover/MIME
  routing in `_cast_to_device` (`jellytoast.py:2814-2838`) is duplicated
  verbatim in `player_backend.py:821-838` → promote to
  `CastManager.prepare_cast_payload(np)`. _(small each)_
- **Convert the single skipped test** (`test_offline_connectivity.py:
  344-352`, a permanent empty placeholder) into a real cross-layer test
  or delete it. _(trivial)_

**Structural refactors (maintainability, no correctness payoff —
defer behind the above):** extract the Cast dialog UI
(`now_playing_bar.py:2688-3675` → `modules/cast_dialog.py`), the NP
track-list model/view/delegate (`now_playing_page.py:256-1956`), and
the custom tooltip subsystem (`jellytoast.py:314-601` → `modules/`).
Each is a move-and-reexport with the call sites already proving the
seam; do them only when a file becomes an active editing bottleneck.

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

### Deep-audit round-2 follow-ups — DRAINED

All HIGH + LOW items from the round-2 audit are now closed.

Drained 2026-05-28 via AT-8 (`auto/castbrowser-migration`,
`4fbcd87`) + AT-9 (`auto/delegate-font-cache`, `ece2951`):

- ~~Migrate Chromecast discovery from `get_chromecasts(blocking=True)`
  to explicit `CastBrowser`~~ — AT-8 replaced the deprecated blocking
  sweep with `CastBrowser` + `SimpleCastListener` +
  `get_chromecast_from_cast_info`. `DISCOVERY_WINDOW_S` (default 3 s,
  patched to 0 in tests) replaces the old `timeout=3` arg. The
  `pychromecast.discovery` → WARNING log mute bundled in.
- ~~Per-paint QFont / QFontMetrics allocation~~ — AT-9 added
  `_build_fonts()` to all four list delegates (`_TileDelegate`,
  `_RowDelegate`, `_SongRowDelegate`, `_TrackDelegate`); they pre-build
  `(QFont, QFontMetrics)` pairs and refresh on `PlayerBus.theme_changed`.
  `_TrackDelegate` caches both bold and regular variants of its
  bold-conditional fonts so per-row `is_current` is a ternary pick.

Drained earlier:

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
  source (`player_backend.py:1198`, gate at `:1213`), Normalization / EQ / Crossfade
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

# jellytoast — what's left to do

The running backlog, in plain language. Last refreshed **2026-05-30**.

**State of the tree (2026-05-30):** a full-app multi-agent code review
(108 verified findings) drove a big fix series — **now fully landed on
`main`.** Merged: **#10** (review Phases 0–5, all critical/high, +
crossfade-EOF), **#11** (test order-independence; **enables
pytest-randomly in CI**), **#12** (backend batch: scrobble dedup #437,
MPRIS, offline LIKE-escape #69, smart-rule UTC #153, provider teardown
#46), **#13** (root-caused + fixed the residual `-n auto` worker SIGSEGV
— a leaked SmartPlaylistEditor preview timer; conftest now flushes
deferred dispatches with `run_async` neutralised + `gc.collect`s), and
**#14** (robustness batch: image-waiter fan-out guard, Jellyfin auth-body
hardening, and the **invariant-4 DPR fetch-size migration completed**
across mini_player/downloads_view/horizontal_rail).

The 2026-05-28 audit backlog is drained; the 2026-05-29 self-test pass
fixed the Jellyfin smart-shuffle no-op. What's left below is a short tail
of trivial/​small cleanups plus the hardware-/ears-gated manual walk.

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
  - **DLNA LIVE-VERIFIED 2026-05-28** against a real LG TV
    (`192.168.50.144`): discovery + `cast_to_dlna` push via the cast
    proxy → renderer reported PLAYING w/ position advancing. This
    surfaced + fixed the **LG webOS `Stop`-before-`SetAVTransportURI`
    701/auto-play quirk** (`d5f2c51`). (VLC is *not* a DLNA renderer —
    use a TV / `gmediarender` / Kodi UPnP.)
  - **Still open (hardware-gated):** confirm DLNA end-to-end from the
    GUI cast dialog (verified so far at the controller level); Sonos +
    the Snapcast dialog's layout/UX need actual hardware + a visual
    polish pass (no devices available). Tracked in `known_issues` +
    `manual_test_plan §5`.
- ~~**Add real provider auth/streaming tests** → **AT-10**~~ — ✅ **DONE**
  (`7baf722`, +57 real-impl provider auth/streaming tests).
- ~~**Test the Chromecast media-load / transport flow** → **AT-11**~~ —
  ✅ **DONE** (`503559b`, +63 Chromecast media-load/transport tests).

**Scrobble / shutdown lifecycle cluster** — ✅ **DONE 2026-05-28**
(`27814b7`, +8 tests): all five below fixed — offline-mode gate on
`flush_pending`, synchronous `flush_current_on_quit()` (window-close +
tray paths), scanned-slice queue removal, and `note_cast_handoff()`
de-dup. _(Original findings kept below for the paper trail.)_

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
- ~~**Queue-flush `remove()` mis-removes on a malformed entry.**~~ — ✅
  **DONE 2026-05-30 (PR #12, open)**. `scrobble_queue.remove()` is now
  identity-based (`remove(service, records=…)`, matched as a `Counter`
  multiset over `json.dumps(sort_keys=True)`) instead of "oldest N", so a
  flush removes exactly the records it sent. Plus a per-service
  `_..._flush_in_flight` guard against double-submit + an `_extract_mbid`
  `_subsonic_raw.musicBrainzId` fallback.

**Medium — perf / structure:**

- ~~**Cache the list-mode row cover** → **AT-13**~~ — ✅ **DONE**
  (`ecf6472`, cached list-row cover scale + genres delegate fonts).
  **NB:** AT-13 did *not* include the mini_player/downloads_view DPR
  fetch-size cleanup — that's a separate still-open item in the Low
  section below (the old "folds into AT-13" note was wrong).
- **Extract the EQ section out of `settings_dialog.py`.** ~1000 lines
  (`_build_eq_section` 1542 → `_emit_eq_changed` 2563, ~24% of the
  4335-line file) form one cohesive subsystem → `modules/settings_eq_panel.py:
  EqPanel(QWidget)` emitting `eq_changed`. Highest-value cut in the
  largest module. _(large — maintainability only, no correctness payoff;
  defer behind the above unless it becomes an editing bottleneck)_

**Smart-playlist editor follow-ups** (the editor was just reworked in
`ec544c8`):

- ~~**Preview vs play disagree on empty-value rules**~~ — ✅ **DONE
  2026-05-28** (`a220f08`): `validate_rules` rejects empty/blank text
  values (str-fields only), gated at preview/save/save&play. +tests.
- ~~**Preview has a stale-result race**~~ — ✅ **DONE 2026-05-28**
  (`a220f08`): `_refresh_preview` carries a generation token;
  `_on_preview_result` drops a stale result. +tests.

**Low — cleanup / robustness (batch-able):**

- ~~**Dead-code purge (~17 verified-zero-caller symbols)** → **AT-12**~~ —
  ✅ **DONE** (`4ccaa1a`, purged 15 confirmed-dead symbols, −184 LOC).
  (The `library_grid._on_view_activated` Enter-to-browse-on-tiles gap
  was a *delete*, not a wire — if Enter-to-browse on tiles is wanted,
  that's a new small feature, not dead code.)
- ~~**DPR fetch-size cleanup (invariant 4)**~~ — ✅ **DONE 2026-05-30**
  (PR #14). `mini_player` (both `_prefetch_cover` + `_on_started`),
  `downloads_view._load_thumb`, and `horizontal_rail._load_covers` now
  fetch at a fixed `LOGICAL*3` source (`_MINI_SOURCE_PX=960`,
  `THUMB_SOURCE_PX=108`, rail `_COVER_SOURCE_PX=540`) instead of raw
  `screen_dpr()`. `horizontal_rail` was a sibling site an audit sweep
  surfaced (not in this list). **Invariant-4 migration is now complete
  across every cover-fetch site;** each pinned by a DPR-invariance test
  in `tests/test_dpr_unify_fetch.py`.
- ~~**Cache genres delegate fonts**~~ — ✅ **DONE** via **AT-13**
  (`ecf6472`, `genres_view` delegate now caches fonts).
- ~~**Single-instance shared-memory key isn't per-user**~~ — ✅ **DONE
  2026-05-28** (`5d47d2a`): per-user `<socket_name>-shm` key. +1 test.
- ~~**Production-module ruff backlog (11 F401/F841)**~~ — ✅ **DONE
  2026-05-28** (`4af4f5f`): each inspected (the `now_playing_bar` pair
  were dead module-level imports shadowed by live nested re-imports;
  `ui_helpers` `tooltip_bg` was a dead QSS-tooltip leftover). **`ruff
  check .` is now clean repo-wide.**
- **Visualizer audio tap leak — PARTIALLY addressed** (audit 2026-05-29).
  `stdout.close()` now runs in a `finally` and EOF sets `self._proc =
  None` (the "mark for restart" path). Still wants confirming: that the
  engine actually re-`start()`s the tap on `_proc is None` after a
  mid-session sink loss (vs staying flat), and an explicit `wait()` to
  reap the zombie. _(low; opt-in behind `JT_VISUALIZER=1`)_
- ~~**Image-waiter fan-out guard**~~ — ✅ **DONE 2026-05-30** (PR #14).
  `ui_helpers._on_image_reply_finished` now guards each subscriber in
  BOTH fan-out loops (success + failure/placeholder), so a deleted-widget
  `RuntimeError` from one coalesced subscriber no longer starves the
  rest. +3 tests.
- ~~**Harden Jellyfin auth-body parse**~~ — ✅ **DONE 2026-05-30**
  (PR #14). `JellyfinAPI.authenticate` reads `AccessToken` AND `User.Id`
  defensively (`.get()`, non-dict-body safe) and raises a clear
  `ValueError` on a malformed/captive-portal 200 instead of a cryptic
  `KeyError`; nothing persists on the failure path. +parametrized tests.
- ~~**Dependency-declaration hygiene** → **AT-14**~~ — ✅ **DONE**
  (`8eda2e9`, declared `python-xlib`, capped `pyatv<1.0` + `PySide6<7.0`).
  **Still wants:** a clean-room `pip install` smoke check of the new
  caps (hardware-/env-gated).
- **Shared-helper unification — NOT a safe mechanical hoist (needs
  at-computer visual verify).** `_ElidingLabel` is reimplemented 3×
  (`library_grid.py:109`, `now_playing_page.py:179`, `songs_view.py:76`)
  but the impls DIFFER: now_playing_page uses `_full_text` + a near-zero
  `minimumSizeHint` override (required for its `QScrollArea` context) the
  others lack — unifying changes grid/songs layout, so it needs eyes, not
  a blind hoist. Same caution for the two `_round_corners` signatures
  (`now_playing_bar.py:38` vs `ui_helpers.py:1179`, image pipeline) and
  the cast cover/MIME routing dup (`_cast_to_device` vs
  `player_backend.py:821-838` → `CastManager.prepare_cast_payload(np)`,
  hardware-gated). _(small each, but visual/hardware-gated — defer to an
  at-computer session)_
- ~~**Convert the single skipped test**~~ — ✅ **DONE 2026-05-30**
  (PR #17). The permanent `test_offline_connectivity` placeholder is gone;
  the 4xx-vs-network classification it described is now really tested at
  the provider call site (`test_jellyfin_api.TestGetConnectivityClassification`:
  a 4xx response records note_request_success/server-reachable, a
  RequestException records note_request_failure). Suite skip count → 0.

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

**2026-05-29 self-test progress** (logic + live, no audio — see
`manual_test_plan.md` for per-section notes): §1 empty-value/parity,
§7 date operators, §9 anti-clustering (bug found+fixed), and §2
instant-mix integration are now **logic/live-verified**; light theme
**render-verified** + stylesheet-clean. Still needing eyes/ears/​UI:
the editor UI walks (§1/§7), the radio queue auto-extend end-to-end
(§2), and the ears-only items (§3 radio audio, §4 visualizer, §10
crossfade). §5 Sonos/Snapcast remain hardware-gated; DLNA re-cast still
wants a live GUI confirm of resume + bar-advance.

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

- **2026-05-30** — Full-app multi-agent code review (108 verified
  findings) → fix series. **PR #10 merged to `main`:** Phases 0–5 (all
  critical/high) — test-foundation/xdist isolation, the snapcast SIGSEGV
  Bug 2 fix (`call_on_gui` GUI-thread marshalling), the 🔴 crossfade
  observer-reattach fix + a second crossfade-EOF bug caught during live
  verification, cast-backend fixes (AirPlay pairing flag, DLNA seek,
  Sonos label, Stop routing, proxy ranges), provider fixes (Subsonic LDAP
  auth, smart-rule parity), offline DB lock-leak/cancel/migration. **Two
  PRs OPEN, green, pending review:** **#11** order-independence +
  **pytest-randomly now ON in CI** (visualizer worker-stop SIGABRT via a
  `_stop_requested` latch; cast/async leak cluster — conftest pool-drain
  + cast loop-thread teardown, cast_gating `is_available` stubs, a
  cast_sonos `sys.modules`-swap footgun, a hotkeys `server/url` leak that
  made `SettingsDialog`'s auto-probe SIGSEGV, smart-playlist preview
  `run_async` stub; verified 12/12 single-proc + 24/24 random clean);
  **#12** backend batch (scrobble dedup #437, MPRIS, offline LIKE #69,
  smart-rule UTC #153, provider teardown #46). See
  `reference_test_isolation_bugs` + `session_handoff` memory.
- **2026-05-29** — Self-test pass (no audio; logic + live Jellyfin).
  Found + fixed a real bug: **smart-shuffle anti-clustering was a
  complete no-op on Jellyfin** (`4341ad5`) — it keyed on `ArtistId`,
  which is `None` on every adapted Jellyfin song item, so all tracks
  collapsed into one bucket and back-to-back-same-artist rate equalled
  plain random (0.022 vs 0.021; ~0.23 on an artist-heavy queue). Fixed
  via `artist_key()` AlbumArtist/Artists fallback + routed the recency
  window through it; post-fix 0.001 vs 0.015 / 0.054 vs 0.233 (4.3×).
  +3 regression tests on the real Jellyfin item shape. Also: stale
  `theme_mode` comment corrected (`f9521df`). Logic+live verified §1
  empty-value/parity, §7 date ops, §2 instant-mix integration; render-
  verified light theme + stylesheet-warning-clean. Suite 2015 → 2018.
- **2026-05-28** — Audit marathon: full multi-agent codebase audit +
  doc-sync; wired the cast PLAY dispatch for DLNA/Sonos/Snapcast
  (`6085ca8`/`88d9a4f`, DLNA live-verified vs an LG TV + the webOS
  Stop-before-Set fix `d5f2c51`); merged **AT-10/11/12/13/14**
  (provider+Chromecast tests, dead-code purge −184 LOC, delegate perf,
  dep caps); fixed the scrobble/shutdown cluster (#9 `27814b7`),
  single-instance per-user key (#10 `5d47d2a`), smart-playlist
  empty-value + preview race (#11 `a220f08`); cleared the ruff backlog.
  Suite 1844 → 2015.
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

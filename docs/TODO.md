# jellytoast — what's left to do

The running backlog, in plain language. Last refreshed **2026-06-08** after a
full-app multi-agent code review (18 subsystem reviewers, every finding
adversarially re-verified against real code) + a docs/repo-org audit.

**State of the tree (2026-06-08):** `main` @ `cfaac9e`, suite **2686** green,
ruff clean, 0 bare-excepts, 0 stray `print()`s. The review confirmed a healthy
core: **0 critical bugs**, an A-grade signal-bus / provider / queue / state
layer, disciplined error handling. The actionable output is **5 high + 8 medium
behaviour bugs**, a tail of 18 low bugs, ~42 tidy/cleanup items, and a sizeable
**docs-organization** backlog (this file was 1304 lines of mostly-shipped
history; CHANGELOG is ~53 commits stale).

This refresh **drains the doc itself**: the stale "P2 cast-proxy hardening is
the standout open item" headline is gone — it shipped in `de9e58c` (binds the
resolved LAN IP, verifies TLS by default, expires tokens on stop, closes the
TOCTOU). The multi-session "paper-trail" / "Recently shipped" blocks moved to
`CHANGELOG.md` (the designated dated-history home). The full prior version is in
git (`git show HEAD~1:docs/TODO.md`).

Companion docs:

- `docs/manual_test_plan.md` — things to check by hand / by eye.
- `docs/autonomous_tasks.md` — work that can be handed to an unattended agent.
- `docs/SPEC.md` — what the app actually does today.
- `CHANGELOG.md` — what's already shipped, dated.
- `docs/research/` — original design notes (most now shipped → archive pending).
- `docs/decisions.md` — why certain architectural choices were made.

## How this list is ordered

- **P0** — real behaviour bugs the review confirmed (user-visible). Fix first.
- **P1** — medium behaviour bugs (narrower trigger or smaller blast radius).
- **P2** — tidy / cleanup (dead code, dup consolidation, stale comments) +
  the docs/repo-organization backlog.
- **P3** — low-severity bugs + features not yet pulling weight.
- **P4** — hardware-gated / cross-platform.
- Then packaging (deferred), parked, and explicitly-not-on-roadmap.

---

## P0 — confirmed behaviour bugs

✅ **ALL FIXED 2026-06-08** on branch `fix/review-2026-06-08-bugs` (commit
`7b8481a`), each with a regression test; suite 2703 green. See `CHANGELOG.md`.
Detail retained below for the record until the branch merges.

No critical/crashing bugs were found; these were the highest-impact ones.

1. **Crossfade + user-skip leaves the next track near-silent.**
   `modules/player_backend.py:714-744` + `modules/playback/crossfade.py:276-287`.
   Pressing Next mid-crossfade calls `_abort_crossfade()` → `Crossfader.abort()`,
   which "leaves the active handle alone" — but that handle (`self._mpv`) was
   ramped *down* by the fade tick. `play()` then reloads it without restoring
   volume, and mpv's `volume` is a persistent (not per-file) property, so the new
   track — **and every subsequent local track** — plays at the faded-down level
   until the user drags the slider. The natural-EOF path already guards this via
   `complete_now()`; the user-Next path doesn't.
   *Fix:* on any explicit transition that aborts a live fade, restore
   `self._mpv["volume"]` to the user target (respecting the bit-perfect-100 pin),
   **or** route mid-fade Next through `Crossfader.request_skip()` (see P2 tidy —
   that method is the documented hard-cut path and is currently dead code; wiring
   it fixes this bug cleanly).

2. **A failed cast leaves local playback dead on "Nothing playing".**
   `modules/cast_dispatcher.py:219-249`. When a track is playing, the local mpv
   stream is stopped up front (`stop_requested.emit()`, line 220) *before* the
   cast is attempted. The shared `_on_cast_result` re-emits `playback_started`
   only on success; the failure `else` branch (244-249) only shows a warning and
   never restores. So a failed Chromecast/DLNA/Sonos/AirPlay cast halts mpv with
   no recovery and the bar reads "Nothing playing".
   *Fix:* in the `else` branch, when `_playing`, re-emit `playback_started(_np)`
   (mirroring the success + pairing-cancel paths) **and** actually resume mpv
   (the prior stop halted the player, so a UI-only re-render isn't enough).

3. **Context-menu "Remove from queue" deletes the wrong track on a shuffled
   album.** `modules/now_playing_page.py:1325-1359`. In source-order display the
   model's `play_index` is the *original_items* index, but the remove path emits
   it straight to `QueueManager.remove_at`, which treats it as a *play-order*
   index. `_on_shuffle_changed` permutes `play_order` without setting
   `is_modified`, so a shuffled album stays in source-order display → the two
   indices diverge → right-clicking "track 3 → Remove" deletes whatever sits at
   play-order position 2. The click path already maps source→play by Id; the
   remove path is the only one missing it.
   *Fix:* when `_displayed_items_kind == "source"`, map the item's Id to its
   play-order index (mirror `_on_row_clicked`) before emitting `queue_remove_at`.

4. **Colors "Reset" writes wrong colours — `color_tokens` defaults drifted from
   `theme.py`.** `modules/color_tokens.py:168-247`. The hardcoded token defaults
   no longer match what `FROSTED_DARK` actually produces (e.g. `BODY_COLOR`
   default `(18,18,18,232)` vs live `(18,18,18,172)`; the `WASH_*` defaults are a
   different colour *model* entirely — solid tinted rgba vs neutral white-wash).
   Reset / Reset-all write the stale value, so resetting a body token makes the
   frosted surface markedly more opaque and resetting a wash reads completely
   wrong. The module docstring claims "defaults match ui_helpers" — invariant
   broken and untested.
   *Fix:* derive each default from `DEFAULT_THEME` at load (a single hardcoded
   default can only ever be right for one mode), and add a test asserting
   `get_default(name) == getattr(<module>, name)` for the default theme.

5. **Primary climb-back probes on every API success, blocking, with no
   cooldown.** `modules/offline/connectivity.py:186-190, 564-582`. After a
   failover, `note_success()` (fired from *every* successful Jellyfin/Subsonic
   request on the 8-thread pool) calls `_try_climb_back_to_primary()`, which does
   a **synchronous** up-to-3s `requests.get` probe of the primary — with no
   rate-limit, despite the docstring claiming "gated by a short cooldown" (no such
   guard exists). While the primary stays down, each of a page's 8+ parallel
   requests pays a full blocking probe, throttling page loads for anyone with
   `server_hostnames` configured who has failed over.
   *Fix:* add a monotonic cooldown timestamp under `_state_lock` and early-return
   when `now - _last_climb_attempt < ~30s` (implement the advertised behaviour),
   or route the probe through `run_async` so it never blocks a worker.

---

## P1 — medium behaviour bugs

✅ **ALL FIXED 2026-06-08** on branch `fix/review-2026-06-08-bugs` (same commit
as P0). Detail retained below for the record until the branch merges.

- **Top-bar menus show stale colours after a live theme change.**
  `modules/top_bar.py:18, 425-426, 723-724, 877-878`. The three menu builders read
  `TEXT`/`POPUP_OPAQUE_FILL` from an *import-time* binding; `ui_helpers` rebinds
  these on every theme change. Sibling code in the same file already does it right
  via `ui_helpers as _u`. *Fix:* reference `_u.TEXT` / `_u.POPUP_OPAQUE_FILL`.
- **Mini player never handles `playback_restored`** → a resumed track shows
  "Nothing Playing" on launch. `modules/mini_player.py:1118-1163`.
- **Play icon flips to the pause glyph while paused** when `_on_started` is
  replayed (cache-clear / dpr-change). `modules/now_playing_bar.py:800, 1117-1124,
  673-676`.
- **`_compute_subtitle` crashes in delegate paint** when `AlbumArtist` is empty
  but `AlbumArtists` is populated. `modules/library_grid.py:1643` (+ sibling 462).
- **Genres background refresh blanks the grid** — overwrites a good cache with an
  empty list. `modules/genres_view.py:448-456`.
- **AutoEQ band-drag "Q preserve" is a no-op** — width is recomputed from the new
  freq, so octave bandwidth isn't preserved. `modules/settings_eq_page.py:747-758`.
- **MPRIS Shuffle/LoopStatus go stale** — app-side shuffle/repeat changes are
  never pushed to D-Bus. `modules/media_controls/_mpris.py:359-367, 195-201,
  176-184`.
- **AirPlay leaks on cancel/rescan** — legacy AirPlay-v1 discovery leaks a
  Zeroconf instance per rescan (`modules/cast_manager/_airplay.py:44-53`); the
  pairing dialog leaks the pyatv pairing event loop on cancel
  (`modules/airplay_pairing.py:220`).

---

## P2 — tidy / cleanup

High-value structural cleanups first (these remove real footguns or dead
weight), then the long trivial tail.

### Structural

- **Wire or remove `Crossfader.request_skip()`** (`modules/playback/crossfade.py:
  220-232`) — the documented mid-fade hard-cut path is dead code. Wiring it is the
  clean fix for **P0 #1**.
- **Delete the dead `LibraryTile` widget class + its 3 label helpers**
  (`modules/library_grid.py:109-587`) — no longer instantiated (the grid is a
  `QListView`/delegate now). ~480 lines.
- **Collapse the duplicate cast QSettings property pairs**
  (`modules/settings.py:536-550, 670-678, 743-753`): `cast_sonos_enabled` vs
  `sonos_enabled`, `cast_snapcast_enabled` vs `snapcast_enabled` back the same key.
- **Smart-playlists row-Play should reuse `play_entry`** instead of
  reimplementing resolve→queue→navigate (`modules/smart_playlists_view.py:315-375`).
- **De-dup copy-paste**: year-text extraction across six sites
  (`library_grid.py`), Catmull-Rom control-point math
  (`media-extras` visualizer `_wave_path`/`_paint_wave`), and the `_VolumeSlider`
  center-mode body QSS (inlined twice).

### Trivial tail (37 items — mostly typos, dead constants, redundant imports)

Low-risk; safe to sweep in a single "tidy" PR. Notable ones:

- Dead import `popup_paint_qcolor` in `_ToolTipPopup.show_under`
  (`jellytoast.py:394`); vestigial `QStackedLayout(StackAll)` for a removed boot
  overlay (`jellytoast.py:973-975`); `_MouseClearFocusFilter` walks
  `QApplication.allWidgets()` on every click (`jellytoast.py:318-326`).
- Redundant `except (KeyError, Exception)` in six snapcast write paths; redundant
  re-imports; stale module-path docstrings (`cast_dialog_sections`,
  dispatcher, `blur/_unsupported`).
- Dead assignments / always-true guards: `self._initial_accent`
  (`settings.py`), `_preview_meta is not None` (`now_playing_page.py`),
  `LibraryGrid.PAGE_SIZE`/`TILE_WIDTH` stale constants.
- `JellyfinAPI.search` default `IncludeItemTypes` pulls Movie/Series/Episode in a
  music-only client (`modules/providers/jellyfin.py`).
- File I/O without explicit UTF-8 (palette export/import, `settings_eq_page.py`);
  `disk_cache.clear_all` glob misses `.json.tmp` temps.
- Comment typos ("ringa"→"rings"), misleading comments, an exported-but-unwired
  `from_year` recipe factory.

*(Full per-item list with file:line is in the 2026-06-08 review output; ask to
expand any group into discrete tasks.)*

### Docs / repo organization

The docs are a write-only log that grows without pruning. Done in the
2026-06-08 sweep:

- [x] **Trim `docs/TODO.md`** 1304 → this version (history → CHANGELOG).
- [x] **Backfill `CHANGELOG.md`** — consolidated 2026-06-03…06-08 sections added
  (the 2026-06-02→present gap is closed).
- [x] **Archive `docs/code_audit_2026-06-01.md`** → `docs/archive/` with a dated
  SUPERSEDED banner; README doc-map updated to point at `docs/archive/`.
- [x] **Fix CONTRIBUTING extras note** — backends ship standard since #62 (no
  extras; the dead "Optional extras" reference is gone). README already says so.
- [x] **Rename the stale-org packaging icon** to `io.github.wolfgangwarehaus…png`
  — it was a BROKEN ref (the `.desktop` `Icon=` is new-org), not an orphan to
  delete, so rename was the right fix. Last `augustvontrips66` string is gone.

Still open (deferred — each needs per-file verification first; the 2026-06-08
docs audit got the Windows-blur claim wrong, so don't apply its doc edits blind):

- [ ] **`docs/research/` reorg** — most of the 21 notes describe shipped features.
  Add a status index and fix the genuinely-contradicting banners IN PLACE (verify
  each against code first; do NOT mass-`git mv` — `artist_page.py` et al. cite
  research paths in comments). NOTE: `portable_blur` §5 ("Mica, don't wire
  Acrylic") is likely **correct for main** — `modules/blur/_dwm.py` on main is
  still Mica; the Acrylic work is on the unmerged `feat/auto-follow-os-theme`.
- [ ] **`docs/SPEC.md`** — verify + fix the `auto_offline_mode` (toggle dropped
  in #55), `library_page_size`, and Windows-status claims against current code.
- [ ] **Re-tense `docs/cross_machine_packaging_plan.md`** to a durable reference.
- [ ] **offline_and_downloads.md / scrobbling.md** banner/body contradictions.
- [ ] **Shrink `autonomous_tasks.md`** (drop the literally-doubled paragraph).
- [ ] ~~README Windows-blur "Mica"~~ — NOT a bug: it matches main's `_dwm.py`
  (Mica). The audit's "pivoted to Acrylic" claim is unverified for main.
- [ ] **Branch cleanup** — see the dedicated branch-cleanup pass (squash-aware
  `git cherry`); enable GitHub "auto-delete head branches" to stop the recurrence.

---

## P3 — low-severity bugs + features not yet pulling weight

### Low-severity behaviour bugs (18) — narrow triggers, low blast radius

Tray show/hide-mini label desync; `RadioView.reload()` double-rows on rapid
re-nav; `cast_toggle_pause` flips `_cast_paused` even when the off-thread call
fails; `CastDialog` never deregisters its devices callback (closed dialog stays
alive); radio title/artist clobbered on `_on_started` replay; `_last_displayed_sec`
not reset on track change (elapsed label can skip the first second); A-Z highlight
stops tracking scroll in list-view mode; buffer-drain path skips the per-page
article resort; search song-cover loads can land a stale cover on the reused
model; curve-editor drag mirrors gain into the wrong slider with an AutoEQ profile
loaded; icon pixmaps baked at app-DPR but painted at widget-DPR (mixed-DPI
multi-monitor); `blur.apply()` can raise despite its "never raises" contract when
`dark is None`; snapshot diff treats numeric `0` track/index as missing; Subsonic
year-rule native leg can return tracks whose year ≠ album year; Subsonic
`authenticate` leaves creds dirty if plain-auth fallback also fails; queued
ListenBrainz scrobbles stranded forever once a server-side scrobbler is detected;
permanently-rejected (4xx) ListenBrainz listens re-queue and retry forever; tag
editor's async cover fetch can overwrite a freshly-picked replacement cover.

*(Each has a file:line + suggested fix in the review output.)*

### Features

- **OS media-integration toggle + Windows SMTC.** *(requested 2026-06-06)* MPRIS
  (Linux/KDE) is already wired. Two pieces: (1) a Settings → Playback toggle to
  enable/disable OS media integration on both platforms (gate `media_controls`
  start/stop on a new QSetting, default on); (2) a **Windows SMTC backend** to
  replace the no-op `media_controls/_unsupported.py` — `Windows.Media.
  SystemMediaTransportControls` via WinRT (metadata + thumbnail, play/pause/next/
  prev/seek, hardware keys). The dispatcher seam already exists; only the Windows
  branch needs the real backend. **No longer hardware-blocked** (Win 11 verified).
- **A registered Cast receiver app** — Chromecast screens show "Default Media
  Receiver" not "jellytoast". Needs a $5 Google dev account + a hosted receiver.
- **AirPlay 2 edge cases** — older LG webOS TVs / shairport-sync 5.x misbehave.
- **`QNetworkInformation` supplementary network-status signal** — flaky on Linux;
  revisit during the Windows/macOS push.
- **Importing server-side playlist files (m3u, …)** — probably out of scope unless
  asked.

---

## P4 — hardware-gated / cross-platform

- **Windows native stubs** *(Win 11 available + verified — no longer blind)*:
  autostart (launch-on-login), always-on-top for the mini player, toast
  notifications (`notifications/_unsupported.py`). HiDPI + Acrylic blur +
  borderless chrome already shipped (#71/#72).
- **Cross-thread cast write-race** — `active_cast`/`_cast_paused` written off the
  GUI thread inside `_CastTransportMixin`; needs a hardware cast session to verify
  the fix safely.
- **Sonos / Snapcast live cast verification** — code wired, not yet exercised on
  real hardware (Chromecast / AirPlay / DLNA already live-verified).
- **macOS support** — native bits via the Mac APIs (vibrancy, NowPlaying, …).
- **iOS** — only after a Mac exists.
- **Exclusive audio output (ASIO)** — Windows-only; only if a Windows user asks.
- **Per-OS visualizer audio taps** — Linux tap works; Windows/macOS/iOS need
  equivalents for parity.

---

## Packaging — scaffolded, deferred

Deferred by choice; nothing dropped — the scaffolding is done so it's a short hop.

- **AUR** — `packaging/aur/PKGBUILD` written + dry-run validated. Left: tag a real
  `v0.1.0`, then `updpkgsums` + `makepkg -si` + `namcap` + `.SRCINFO` + push to
  `aur@aur.archlinux.org` (steps in `packaging/aur/README.md`) — first submit with
  august.
- **Flathub** — AppStream metainfo, `.desktop`, icons all in `packaging/`. Left:
  clean screenshots (Library / Now Playing / Cast / Downloads / Settings /
  Visualizer / Smart Playlists / Radio), uncomment the `<screenshots>` block, and
  a Flatpak build manifest (`.yaml`) that grants `--filesystem=xdg-data/kwin` so
  `drag_repaint/` can install its KWin effect from the sandbox (queued as AT-5).
- **Cast-proxy demo clip** — a ~30s hero clip (Chromecast playing from a
  Tailscale-only server while the laptop is offline); pairs with the screenshots.

---

## Parked — deferred, not dropped

- **Last.fm scrobbling** — client code built + dormant in
  `modules/scrobble/lastfm.py`; needs an in-app API key (signup firewall Error 406
  blocked registration). The Settings → Scrobbling Last.fm section stays hidden
  while `API_KEY`/`API_SECRET` are empty. **ListenBrainz** is the supported path
  and works today.

---

## Explicitly not on the roadmap

Deliberately out of scope — each is a fight a competitor already wins:

- **Local-file libraries** — Strawberry / Tauon territory.
- **Podcasts** — outside the music-only focus.
- **A mobile app** — Symfonium / Finamp own that space.
- **CarPlay / Android Auto** — mobile-only.

> **Note 2026-05-27.** "Heavy audiophile DSP" left this list after a Symfonium
> benchmark found the gap closeable in ~1 work-week. Parametric EQ + bit-perfect
> mode are now shipped; full convolution AutoEQ stays parked past Flathub.

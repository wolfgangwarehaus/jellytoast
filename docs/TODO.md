# jellytoast — what's left to do

The running backlog, in plain language. Last refreshed **2026-06-09** at the
close of the frosted-glass consistency pass.

**State of the tree (2026-06-09):** `main` @ `2872772`, **pushed to origin**,
suite **2813** green, ruff clean, 0 bare-excepts, 0 stray `print()`s. The
2026-06-09 frosted-glass pass (custom tooltips, real-blur volume popups,
light-theme dropdowns — see `CHANGELOG.md`) and the earlier autonomous-audit
batch are both merged and pushed. No branches in flight on the frosted/theming
work. The last review confirmed a healthy core: **0 critical bugs**, an A-grade
signal-bus / provider / queue / state layer, disciplined error handling.

> **2026-06-09 autonomous-audit batch — ✅ MERGED + PUSHED:** a fresh
> multi-agent audit refilled the queue with **21 new test/build-verifiable
> findings**, all implemented + merged (**8 `auto/*` branches**, +40 tests) —
> per-branch detail in `CHANGELOG.md` (2026-06-09) and
> `docs/autonomous_tasks.md`. Only 2 P3 low-bugs stayed deferred
> (`cast_toggle_pause` off-thread flag, mixed-DPI icon bake — hardware/visual).
> These were NEW findings beyond the 2026-06-08 review, so the P0–P3 sections
> below are unchanged by the batch.

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

✅ **ALL FIXED & MERGED to `main`** (branch `fix/review-2026-06-08-bugs`, commit
`7b8481a`), each with a regression test. **Re-verified present in `main`
2026-06-09** by a per-bug audit (quoted code for each). Detail retained below
for the record only — these are closed.

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

✅ **ALL FIXED & MERGED to `main`** (branch `fix/review-2026-06-08-bugs`, same
commit as P0). **Re-verified present in `main` 2026-06-09** by a per-bug audit —
all 8 confirmed fixed with quoted code. Detail retained below for the record
only — these are closed.

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

✅ **MOSTLY DONE 2026-06-08.** Track A (merged, PR #74) removed the dead
`LibraryTile` stack + `Crossfader.request_skip` (~−530 lines). The tidy-tail
batch (branch `chore/review-2026-06-08-tidy-tail`) then drained ~30 more trivial
items + the cast-pause bug + the dup-key alias + the unused-`from_year` drop —
each re-verified against current code (one finding, `providers-auth-4`, was
already fixed and skipped). Full suite green, ruff clean.

### Tidy refactors — 3 of 4 done (branch `chore/review-2026-06-08-tidy-refactors`)

✅ **DONE:** the `_MouseClearFocusFilter` `allWidgets()` walk → `weakref.WeakSet`
registry (`modules/keyboard_focus.py`, +test); the ~6-site year-text dedup
(`_year_text`/`_year_int` helpers, behaviour-preserving — the click stays
ProductionYear-only); the center-mode volume-popup QSS dedup
(`_center_body_qss`). Smart-playlists `play_entry` reuse + the Catmull-Rom Bezier
dedup already landed in the tidy-tail batch.

⏸️ **DEFERRED — hardware-gated, do NOT land unverified:**
- **Mixed-DPI icon bake** (`modules/icons.py` + `icon_button.py`) — pixmaps baked
  at app-DPR but painted at widget-DPR (blurry only on a mixed-DPI multi-monitor
  setup; a no-op on single-DPI). The fix touches the core icon paint path, so it
  needs verification on actual differing-DPR monitors (`spectacle -f -b -n` on the
  secondary screen) before landing. Approach B (a `svg_pixmap(name,color,size,dpr)`
  helper + an opt-in `IconButton.set_glyph`) is the low-churn route.

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

- [~] **`docs/research/` banner contradictions — code-contradicting ones FIXED
  2026-06-09** (per-bug audit, quoted code). `portable_blur` Mica/Acrylic was
  already corrected in a prior pass; this pass fixed two stale sub-status lines
  sitting under newer "Shipped" banners (`provider_abstraction_cleanup.md`,
  `visualizer_rendering.md`). The broader "add a status index + reorg the 21
  notes" is still open (low value; do NOT mass-`git mv` — `artist_page.py` et al.
  cite research paths in comments).
- [x] **`docs/SPEC.md` — DONE 2026-06-09.** Audit confirmed the three target
  claims accurate (`auto_offline_mode` dropped #55, `library_page_size`
  removed/hardcoded, Windows Acrylic-default); fixed one genuinely-stale number —
  the connectivity tracker is now 2 failures + a ~4 s window (was documented as 3).
- [x] **`docs/cross_machine_packaging_plan.md` — DONE 2026-06-09.** Fixed the two
  stale "(opt deps)" claims (numpy/soco/snapcast/async-upnp-client are required
  deps now). The §2/§6 checkbox-vs-durable-tense polish is cosmetic and left as-is.
- [x] **offline_and_downloads.md / scrobbling.md — DONE 2026-06-09.** `scrobbling.md`
  verified accurate (Last.fm parked, ListenBrainz live); `offline_and_downloads.md`
  §7 property list corrected (`downloads_wifi_only`, not `wifi_only_downloads`;
  `download_location` is unshipped, dropped from the list).
- [x] **Shrink `autonomous_tasks.md` — already clean.** The "literally-doubled
  paragraph" was removed in `1530f73` (2026-06-08); no duplicate remains.
- [x] **README/docstring Windows-blur "Mica" → Acrylic** — FIXED. main's
  `_dwm.py` runs real Acrylic by default; the docstrings + README table said
  Mica (the audit's rm-1 was right). Corrected the README row + the `_dwm.py`
  module/`apply()` docstrings that caused the confusion.
- [ ] **Branch cleanup** — see the dedicated branch-cleanup pass (squash-aware
  `git cherry`); enable GitHub "auto-delete head branches" to stop the recurrence.

### Theming audit — off-theme dialogs + stale-on-theme-change chrome

A 30-finding theming audit (2026-06-08, re-verified) found two classes the user
hit by eye: **(a) bare `QDialog`s with no `GLOBAL_STYLE`/frosted body** (render
OS-palette, near-black on a light theme) and **(b) widgets that bake theme ink
into per-widget QSS and never re-stamp on `theme_changed`** (go wrong-contrast
on a live dark↔light flip). The high-impact, reachable members are ✅ **MERGED**
(PR #81): a new reusable `FrostedDialog` base; the AutoEQ import dialog + both
radio dialogs converted; radio/downloads native msgboxes →
`frosted_info`/`frosted_warning`; live re-stamp added to the settings ⓘ/✕, the
A-Z rail, the HorizontalRail header, the search ✕/status/Songs header, and the
whole CastDialog titlebar + banner + sections; `test_theme_restamp.py` extended.

Then (branch `fix/light-popup-frost`, ✅ **merged 2026-06-08** after an eyeball):
light-family popups were stark white (`POPUP_OPAQUE_FILL` alpha 0.80 vs dark's
0.65) — new `ui_helpers.popup_body_fill()` + a cap in `popup_paint_qcolor` frost
them to `_POPUP_FROST_ALPHA` (0.62) **only when blur is verified** (bare
QMenu/QComboBox stay opaque — no blur.apply()); the opaque volume popup (child
surface, can't blur) now matches its button-hover tone (224 light / 74 dark)
instead of reading stark white. Plus the view + library dropdowns centred under
their buttons (shared `_popup_menu_centered`) and the library picker's checkable
✓ column un-crowded (shared `_dropdown_menu_qss`).

Remaining tail (lower-impact / latent), each with file:line in the audit output:

✅ **Tier 3 + slider fills + frosted_confirm whole-class FIXED 2026-06-08** on
branch `fix/theming-restamp-sweep` (3 commits; suite 2745 green;
`test_theme_restamp.py` +24, `test_frosted_dialog.py` +5):

- [x] **Stale-on-switch (live flip while open), Tier 3:** now-playing lyrics
  toggle + "● Live" button, unsynced lyrics + status (`np_lyrics`
  `_restamp_lyrics_theme`), mini-player radio LIVE badge (was actively
  *clobbered* to TEXT_DIM on flip — re-stamps from `radio_state.current()`),
  group-volume popup chrome, `_AboutDialog` text, login `_AlternateUrlsDialog`,
  downloads queue-counts paused colour. All seven re-read live tokens on
  `theme_changed` (never the stale `from … import TEXT` binding).
- [x] **Hardcoded-white `:disabled` slider fills** — `settings_eq_page` +
  `settings_dialog` `:disabled` fills swapped `rgba(255,255,255,a)` →
  `ink_alpha(a)`.
- [x] **`frosted_confirm(...) -> bool` helper** added to `frosted_dialog.py`
  (+ `FrostedConfirmDialog`); swept the whole class — 18 native `QMessageBox`
  sites across 11 reachable surfaces → frosted (11 confirms, 5 warnings, 2
  infos). `settings_colors_page.py` deliberately skipped (unreachable + mixes
  `QInputDialog`/`QFileDialog` the helpers can't replace).
- [x] **Volume popup true-frost — DONE 2026-06-09.** August reversed the earlier
  "decline": `_VolumeSliderPopup` was promoted to a top-level `Qt.ToolTip` window
  (`_toplevel=True`, `WA_TranslucentBackground`) that rides REAL KWin blur, in
  both centre + right-edge (mini-player) modes — Source-painted to match the
  button-hover / tooltip glass. Positioning/dismiss stayed intact (ToolTip
  windows position on Wayland and don't grab the mouse). See `CHANGELOG.md`
  (2026-06-09 frosted-glass consistency).
- [ ] **Latent / unreachable (low):** Last.fm connect modal
  (`settings_dialog._lf_open_auth_modal`, gated behind an unshipped API key),
  the entire `settings_colors_page.py` palette CRUD (no UI entry point), and
  `SnapcastControlDialog` (`snapcast_control.py`, hardware-gated) — all bare
  `QDialog`/native-msgbox; bring to `FrostedDialog` parity when surfaced.

---

## P3 — low-severity bugs + features not yet pulling weight

### Low-severity behaviour bugs (18) — narrow triggers, low blast radius

✅ **16 of 18 FIXED 2026-06-08** on branch `fix/review-2026-06-08-low-bugs`
(9 regression tests; suite 2710 green): tray label desync, `RadioView.reload()`
double-rows, `CastDialog` callback leak, mini radio clobber, `_last_displayed_sec`
reset, A-Z highlight in list mode (both highlight + jump paths), paginator
article-resort on buffer drain, search stale cover, AutoEQ wrong-slider mirror,
`blur.apply()` never-raises, snapshot `0`-value, Subsonic year-rule per-track
filter, Subsonic auth creds cleanup, stranded ListenBrainz queue, permanent-4xx
ListenBrainz retry loop, tag-editor cover race.

**2 DEFERRED** (higher-risk / lowest-value — pick up later):
- `cast_toggle_pause` flips `_cast_paused` even when the off-thread SOAP
  pause/resume fails — needs the DLNA/Sonos pause methods to report success
  and a `_run_off_thread_result` + `call_on_gui` flag flip (cross-thread,
  hardware path).
- Icon pixmaps baked at app-DPR but painted at widget-DPR — only mixed-DPI
  multi-monitor setups; the common downscale stays acceptably sharp
  (`modules/icons.py`).

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

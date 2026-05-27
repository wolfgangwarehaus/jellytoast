# Changelog

All notable user-facing and developer-facing changes for jellytoast.
Format roughly follows [Keep a Changelog](https://keepachangelog.com/);
versioning will become real once packaging lands (P0 in `docs/TODO.md`).

The **Unreleased** section gathers everything since the most recent
tagged version; snip it off when cutting a release.

---

## [Unreleased]

### 2026-05-26 — AT-6 + AT-7 merged: test coverage sweep round 2 + DPR cache-key unification

Two autonomous-task branches landed off `auto/*`, taking the suite to
**1730 passing, 1 skipped**.

**AT-6 — coverage sweep round 2 (+29 tests).** Three new Qt-fixture
test files for modules the AT-4 sweep had deferred:

- `test_single_instance.py` (+6) — lock acquire, duplicate detection
  (`raise_requested` fires on the first instance), stale-segment
  recovery branch, attach-and-create race fallback, `_signal_existing`
  / `_on_new_connection` primitives.
- `test_cast_common.py` (+10) — `_AirPlayListener`
  add/update/remove, the `get_service_info`-missing and addressless-
  info branches, mDNS-suffix strip in display name; `_type_enabled`
  default-true, per-kind isolation, false when the QSettings flag is
  off.
- `test_login_view_alternate_urls.py` (+13) — `_UrlRow`
  construction + remove-button callback; `_AlternateUrlsDialog`
  pre-populate from settings, `_add_row` / `_remove_row` plumbing,
  blank-URL drop on accept, by-order priority assignment, whitespace
  strip, label preservation, reject path leaves settings untouched.

No production code touched.

**AT-7 — unify cover-fetch DPR pattern (+6 tests).** `library_grid`
switched to fixed-source-px (`LOGICAL × 3`) in May; the four
sibling fetch sites still baked raw `screen_dpr()` into the server
URL, so Wayland's fractional-scale drift fragmented the L2 raw
cache one entry per DPR per item. After this:

- `search_view.py:160` — `server_px = THUMB_SIZE × 3` (132).
- `artist_page.py:599` — `server_px = HEADER_COVER × 3` (540).
- `artist_page.py:664` — `server_px = _TileDelegate.COVER_SIZE × 3`
  (540).
- `now_playing_bar.py:1969,1994` — `_BAR_SOURCE_PX = 324` module
  constant.
- `songs_view.py:603` — folded from `dpr_bucket()` to fixed source
  × 3 so cross-surface L2 hits with `search_view` are free.

Per-paint scaling and the live `target_phys` / `load_image_async`
target args stay DPR-aware (L1 fragmentation is fine; L2 raw is
the rescue layer). Radio cover (`now_playing_bar.py:2133`) didn't
need the change — its URL identity is the size, no DPR in the
request. New focused tests verify each site requests the same
size from `get_image_url` across three DPRs (1.0 / 1.5 / 2.0).
Design rationale: `docs/research/dpr_cache_keys.md`.

### 2026-05-26 — fresh audit pass: cover-upload reporting, dead imports, flatpak research

A morning audit pass drained two correctness findings and parked
the AT-5 research:

**`tag_editor` cover-upload reporting.** A cover-upload failure
after a *successful* metadata write was misreported as "metadata
edit rejected". Wrapped `upload_cover_art` in its own try/except,
stash any error on the result dict, and surface it through
`_on_saved` separately from the metadata-rejection path. The
bulk-edit partial-failure summary ("Saved X of Y") composes
cleanly with a cover failure.

**Three other audit findings verified as non-issues.** Python
signal handlers run between bytecodes (no "logger-in-signal-
handler deadlock" risk), Qt auto-disconnects on QObject
destruction (no Selector signal leak), and Subsonic gates with
`can_edit_metadata=False` so the tag-editor dialog never opens
against it (no provider parity gap for `upload_cover_art`).

**Smart-playlist editor dead module-level import dropped.**
The `from modules.ui_helpers import TEXT, TEXT_DIM, …` block at
the top of `smart_playlist_editor.py` was unused — `d12fad1`'s
late-import pattern made every reference site re-import per-call.
Leftover module-level binding was exactly the footgun shape that
caused the live-accent staleness bug it was fixing. Removed.

**Test fixture cleanup.** Dropped the unused `capsys` fixture
parameter from `test_malformed_entry_dropped` — leftover from the
logging-migration cleanup on the two sibling tests.

**`docs/research/flatpak_packaging.md` written.** Research note
unblocking AT-5 (the Flatpak build manifest). Picks
`org.kde.Platform`/`org.kde.Sdk` 6.9 + the `io.qt.PySide.BaseApp`
6.9 BaseApp (PySide6 pre-built, drops ~200 MB from the build),
`flatpak-pip-generator` for the remaining Python wheels,
`--filesystem=xdg-data/kwin:create` +
`--filesystem=xdg-config/kwin:create` + `--talk-name=org.kde.KWin`
for `modules/drag_repaint/` (with `flatpak-spawn --host` wrap on
the `kwriteconfig6` / `qdbus` shell-outs in v1). libmpv is **not**
in the KDE runtime — listed as a build module. Five open
questions for august at §7.

### 2026-05-26 — logging migration: 119 print sites → stdlib logging

Production `print()` calls across 25 files converted to per-module
`logger = logging.getLogger(__name__)` with per-call level
(debug/info/warning/error). `logging.basicConfig` lives at the top
of `jellytoast.py`; default level is INFO, override via
`JT_LOG_LEVEL=DEBUG`. Two stdout-grepping radio-settings tests
moved to `caplog`. Suite stayed at 1695 passing.

### 2026-05-25 — settings dialog condensed: Library page dropped, cache moved to Downloads

The Library settings page was overflowing the 540-px viewport and
held knobs nobody was reaching for. Tightened the whole dialog and
pulled cache management closer to where users already think about
disk footprint (Downloads):

- **Dialog 820→720 wide, nav 170→128.** Numerous per-page
  tightenings: EQ band spacing 6→2, combo widths capped at 120 px
  on Playback, Scrobbling token/URL fields capped, Hotkeys rows
  right-padded.
- **Library page deleted.** `library_page_size` setting removed —
  grids now always load 100-per-page with auto-pagination. Shuffle
  queue size moved to a new SHUFFLE section on the Playback page.
- **Tiles section removed.** `library_cover_prefetch` /
  `library_tile_fade` settings deleted from `settings.py`; defaults
  baked in.
- **Cache subsection relocated.** Cover-art size readout + "Refresh
  album art" button moved to a new CACHE section in the Downloads
  view, alongside the downloads tally.
- **Downloads view rows tightened** — dropped the trailing
  right-edge captions from the toggle rows; the checkbox labels
  are self-explanatory.

### 2026-05-25 — unified login + settings: inline URL edit, shared Selector, painted login card

Bundle of consistency wins between the login screen and the settings
dialog, plus the inline server-URL edit that motivated the pass.

**Inline server-URL editing.** Server URL display is now a single
`QLineEdit`, readonly + frameless by default; clicking "Change
server URL…" drops readonly and paints in the dialog's input chrome
around the SAME text — no font, size, or baseline shift between
modes. Padding constant in both styles so the cursor x-position is
identical; the box pops *around* the URL, not above and over it.
Connection dot now lines up with the horizontal centre of the Sign
out button below it via a backfilled `QSpacerItem`.
`server_change_requested` upgraded to `Signal(str)` carrying the
committed URL; `jellytoast.py` drops the `QInputDialog` popup that
used to fire after the dialog closed.

**`Selector` → shared module.** `_Selector` extracted from
`settings_dialog` as the public `Selector` in `modules/selector.py`
so the login view can use the same dropdown. `selector_qss
(host_selector="")` returns the rule block — hosts merge it into
their own stylesheet. Chevron now drawn in `Selector.paintEvent` via
`QSvgRenderer` rather than `background-image` — Qt's QSS parser
only spec's the keyword form for `background-position`, so the CSS3
four-value offset silently fell back to `top left`, landing the
chevron on the dropdown's first letter of text. The paint path
gives a real 10-px right inset for free.

**Login card matches settings body.** New `_LoginCard(QFrame)`
paints its body the same way `SettingsDialog` paints its window:
rounded rect filled with `popup_paint_qcolor()` at `RADIUS_WINDOW`,
no border. Repaints on `PlayerBus.theme_changed`. Server-type
picker swaps `QComboBox` + `_AccentItemDelegate` + ~80 lines of QSS
for `Selector()` (12 lines). `_reapply_accent` now refreshes the
LoginView stylesheet (live accent for the Selector + transparency
sweep) plus the Sign in button.

### 2026-05-25 — P1 finishers: cover-picker + bulk album edit + equal-power crossfade

Three feature finishers closing out the P1 list:

- **Tag editor cover-picker.** Grows a preview pane + "Replace
  cover…" file picker (png/jpg/jpeg/webp, 25 MB ceiling) targeting
  AlbumId so the swap is visible in every surface that reads album
  cover.
- **Bulk "Apply to whole album".** Same dialog adds an "Apply
  changes to all tracks on this album" checkbox (gated on AlbumId
  presence) that routes Save through
  `provider.update_album_track_metadata`, with a `QMessageBox`
  confirm listing the fields being rewritten and partial-failure
  surfacing in the status line.
- **Equal-power crossfade ramp.** Linear placeholder replaced with
  `_equal_power_gains` (cos/sin at `progress·π/2`) so summed power
  stays flat across the fade — kills the ~3 dB mid-fade dropout on
  uncorrelated cross-album transitions. Endpoints pinned exactly to
  (1, 0) / (0, 1) so the post-swap volume clamp is a true no-op.
  New `TestEqualPowerCurve` pins the curve shape independently of
  the state machine; `test_midpoint_volumes` updated to expect ~57
  (was ~40 under linear).

`docs/manual_test_plan.md` gets a new §10 walking the audible
verification.

### 2026-05-25 — live-accent staleness fix + queue-save debounce + A-Z snap-back

**Live-accent staleness in three modal/non-modal surfaces.**
`radio_view`, `smart_playlist_editor`, and `tag_editor` all baked
theme constants (`TEXT_DIM`/`TEXT_FAINT`/`ACCENT`/…) at construction
via module-level `from modules.ui_helpers import N` imports.
`ui_helpers.refresh_theme()` rebinds the names in the `ui_helpers`
namespace (not in place), so any importer kept a STALE reference to
the load-time value. `radio_view` (non-modal, can be visible during
a Settings theme swap) got the full live-accent contract —
`_apply_styling` methods on `RadioView` and `_StationRow` re-import
the constants and re-stamp QSS, with `PlayerBus.theme_changed`
calling `_reapply_accent` on the view which iterates child rows.
The two modal editors got the lighter late-import-inside-method
fix.

**Queue save debounce.** Every queue mutation (`play_now`,
`add_next`, `advance`, …) called `QueueManager._save`, which wrote
the full payload to disk synchronously on the GUI thread. A
200-track radio queue with the refill feeder appending one track at
a time meant a fresh `queue.json` write per track transition.
Replaced with a 500 ms `QTimer` debounce; `flush_pending_save()`
runs any pending write synchronously and is wired into
`aboutToQuit` so the final queue state survives shutdown.

**A-Z snap-back fixed.** `SmoothScrollFilter` caches `(anim,
target)` per scrollbar so successive wheel notches coalesce into
one moving target. A programmatic jump (alphabet-rail click) calls
`sb.setValue()`, which the filter can't observe — the cached target
stays pointing at the pre-jump position, and the next wheel notch
animates back to where it was. New
`SmoothScrollFilter.invalidate(bar)` drops the cached entry; called
from `LibraryGrid._on_alphabet_jump` alongside the direct
`sb.setValue()`.

### 2026-05-24 — custom tooltip popup + sharp icons + uniform top bar + repeat glyph

- **Custom `_ToolTipPopup`.** Replaces Qt's `QTipLabel`. Qt's
  hardcoded `(2, 16)` offset inside `placeTip` + Wayland xdg_popup
  positioner ignoring post-show `move()` made it impossible to
  centre tooltips flush under their target widget. Owning the popup
  lets us `adjustSize()` before positioning and `move()` before
  show.
- **Sharp icons.** `icon()` in `modules/icons.py` now bakes pixmaps
  at every iconSize the app uses (12–32 px) so Qt picks an exact-
  size pre-rendered pixmap for any `setIconSize`. Eliminates the
  bilinear-scaling blur most visible on dense glyphs like grid +
  sort icons.
- **Refined repeat glyph.** V-shaped arrowheads instead of single-
  stroke hooks, stroke 2 → 1.75, bounding box pulled in. The
  `repeat_one` "1" digit shrunk + recentred to fit the tighter gap.
- **Top bar uniformity.** Every button in the top bar is now 34×34
  with radius-8 highlight pill. View button (`Albums`) got
  `setFixedHeight(34)` (was ~28 from natural sizing); search button
  shrunk 40×40 → 34×34 and radius 10 → 8.
- **A-Z highlight cell-math.** `_update_alphabet_highlight` now
  computes the top row from `sb.value() // cell_h * cols` instead
  of `indexAt()`. `indexAt()` in IconMode + Wrapping +
  UniformItemSizes under Wayland Qt 6 returns invalid even for
  points visually inside a tile, leaving the rail highlight stuck.
  Cell-math is the inverse of `_on_alphabet_jump`, so the two stay
  consistent.

### 2026-05-24 — `_Selector` replaces QComboBox + frosted menus + centred dropdowns

- **New `_Selector` (QPushButton + QMenu)** replaces
  `_OpaqueComboBox` / `_AccentDelegate` across `settings_dialog`,
  `downloads_view`, `settings_colors_page`. Sidesteps QComboBox's
  KDE Wayland popup misbehaviour (first-click drop, oversized first
  open, dismiss-on-resize).
- **Settings reorg.** Home page lifted out of Startup into its own
  section; cast routing combo → 3-radio group (Auto / Network cast
  / Route locally) with aligned faint hint suffixes; "Discover on
  demand (recommended)" → "Discover on cast".
- **Settings combos tightened.** Width 220 → 180, `_CTRL_H` 34 → 36
  with per-side border declarations + `outline:0` to fix the
  sub-pixel half-thinned bottom border.
- **`opaque_menu()` on frosted themes** stays translucent + installs
  compositor blur (deferred via `QTimer.singleShot(0)`,
  `corner_radius=4` shaped to the QSS pill); symmetric 14-px
  padding tightens menu width to its content.
- **Tooltips re-positioned** centred under their target widget
  instead of cursor-anchored; blur apply deferred to fix
  stale-geometry sizing on consecutive shows (Qt reuses one
  `QTipLabel`).
- **Top bar view-button dropdown** horizontally centred under the
  chevron.
- **`ensurePolished()` on current page** + lazy-built combos on
  first show, fixing the "Playback dropdowns don't work until you
  navigate away and back" symptom.

### 2026-05-24 — lift-wash elevated surfaces + frosted top-level popups + About dialog

Polish pass on the dark-frosted elevated-surface family and two
settings pages.

- **Elevated-surface polarity flip (dark themes).** `_DARK_ELEVATED`
  now lifts the body with a soft light wash
  (`rgba(255, 255, 255, 0.10)`) instead of darkening it
  (`rgba(0, 0, 0, 0.40)`), matching the Settings nav selected-row
  tone. Cascades to every elevated surface in the dark family:
  icon-button hover/press, volume popup, list-row hover, selected
  rows.
- **Frosted top-level popups.** `_DARK_ELEVATED_TOPLEVEL` /
  `_LIGHT_ELEVATED_TOPLEVEL` switch to the translucent body + wash
  composite (~`rgba(64, 67, 74, 0.65)` on dark). Tooltips paint
  via `QPainter` with `CompositionMode_Source` over an ARGB surface
  + compositor blur, instead of hardening to opaque — so they read
  as lifted glass at the same depth as the in-window elevated
  surfaces.
- **`_TooltipBackdropFilter` rewrite.** Split into frosted
  (translucent surface + blur + Paint-handler rounded rect) and
  solid (opaque-harden + opaque rect) paths. `QToolTip` QSS
  background goes transparent so Qt doesn't double-paint over the
  filter's rect.
- **About dialog as a borderless frosted pill.** New `_AboutDialog`
  with custom titlebar (drag via `startSystemMove`, icon close
  button matching the main window) and `popup_paint_qcolor` body
  fill. `ABOUT_DIALOG_WINDOW_TITLE` added to `keep_above` so the
  KWin `noborder` rule strips the server decoration on KDE Wayland.
- **Settings General page.** SERVER / STARTUP / WINDOW section
  headers. Home page combo folded under STARTUP since it picks the
  launch destination.
- **Settings Playback page.** AUDIO / CROSSFADE / EQUALIZER section
  headers; field labels share a fixed-width column so all four
  field starts vertically align. Save / Delete + Duration slider
  right edges land on the 16k EQ band's column. EQ section "draws
  in" when Enable is ticked — `QSlider:disabled` rules strip the
  accent + dim the groove, band labels restyle to `DISABLED_FG`
  when off so the whole grid greys up as one cluster. Crossfade
  reorganised: parent toggle + Skip-on-albums share one row,
  Duration sits below them as a top-level row aligned with the
  other labels.

### 2026-05-24 — frosted-popup pass, accent swatches, theme-swap perf, dialog hygiene

**Frosted-popup pass.** Diverged `popup_opaque_fill` per theme:
frosted themes get the translucent elevated wash, solid +
transparent stay opaque. Tooltip filter installs compositor blur on
Show for frosted themes with the rounded corner radius matching the
QSS border-radius, and resets `autoFillBackground` /
`WA_OpaquePaintEvent` left over from any prior solid-theme show on
the reused `QTipLabel` instance. `_OpaqueComboBox.showPopup`
installs blur on frosted themes instead of hardening opacity, so
dropdown popups now read as frosted glass matching the volume
slider + button hover wash. Volume popup body switched from
`POPUP_OPAQUE_FILL` to `WASH_HOVER` — child of the main window so
translucency works directly, and matching the hover wash makes the
popup feel continuous with the speaker button's hover state.

**Accent swatches.** Replaced QSS `border-radius` (which leaves
visible dots at 45° points on `QPushButton`) with a custom
`QPainter` paintEvent. Fixed inset so the selected swatch keeps the
same fill diameter as siblings (thin ring overlay instead of a
chunky ring that bit into the fill). Display settings page also
got: dropped "Mode:" label, "Switches live" caption, accent-section
caption; renamed "Accent" → "Accent color"; replaced "Open advanced
color editor →" link with a "Customize" button; hid the Colors row
in the left nav (still reachable via Customize).

**Theme-swap perf.** `_cascade_global_style`'s deferred indicator
repolish + `_reapply_dialog_accent_styling`'s checkbox/radio polish
walks both skip invisible widgets — biggest win on theme-mode
change where `_rebuild_pages_for_theme` is about to destroy every
checkbox in lazy-built non-current pages anyway. `accent_only=True`
fast path: accent picks now skip the dialog body repaint + the
compositor blur off→on toggle that only matter on a theme-mode
flip. `_cascade_global_style` now refreshes the full app palette
via `apply_app_palette()` instead of just Highlight /
HighlightedText — the partial stamp left `ToolTipBase` stale, which
is why tooltips styled before a theme swap held their old backdrop
colour.

**Dialog hygiene.** Main window `closeEvent` now closes any tracked
top-level dialogs (Settings, Cast) so they don't sit on the desktop
after the main window minimizes to tray.

### 2026-05-23 — smart-playlist editor frosted chrome + dialog placement

**Smart-playlist editor.** Frameless frosted dialog matching Cast +
Settings (custom titlebar, rounded translucent body, blur via the
new `keep_above` `noborder` rule scoped to "jellytoast Smart
Playlist Editor"). Save / Cancel moved beneath the preview column
so the preview list stretches the full right edge instead of being
trimmed by buttons floated next to it. Preview list: horizontal
scrollbar disabled (long titles elide), uniform item sizes for
smooth scroll. Replaced `QDialogButtonBox` with plain
`QPushButton` — drops the built-in floppy / no-entry icons and lets
the buttons inherit the global pill style. Save uses the accent
variant + `setDefault(True)` so Enter saves.

**Dialog launch placement.** New helpers `_center_dialog_on_main`
and `_position_dialog_above_now_playing` on the main window. Both
clamp to `screen.availableGeometry` so a partly-off-screen main
window doesn't push a dialog out of bounds. Settings + Cast
dialogs now pass `parent=self` so KWin establishes a Wayland
transient-for relationship — on KDE Wayland xdg-shell forbids
client-side `move()` so the parent is what gets the dialog onto
the right surface; KWin centers it on the parent. On X11 / Windows
/ macOS the helpers' `move()` additionally docks Cast above the
now-playing bar (right-edge aligned to the main window) and
Settings to the main window center.

### 2026-05-23 — radio stations cast cleanly (LIVE stream_type + transcode bypass + MPRIS trackid)

Three bugs were all triggered by trying to cast an internet-radio
station to a Chromecast.

- **Cast paths fell through to transcode URL for radio.** Both
  `player_backend._play_track` and `JellytoastWindow`'s device-
  selection handoff called `get_audio_transcode_url(np.item_id, …)`
  for radio because radio items have no `Container` field. That URL
  points the receiver at `/Audio/{station_id}/stream` — a 404,
  since `station_id` isn't a real audio item — wedging the receiver
  into IDLE/ERROR. Detect radio via `np.raw["streamUrl"]` and send
  the live URL through with `audio/mpeg`.
- **`stream_type="BUFFERED"` rejected by Default Media Receiver.**
  Plumb `is_live` through and switch to `"LIVE"` for radio; also
  zero out `current_time` since live streams aren't seekable.
- **MPRIS trackid invalid for user-added radio.**
  `MprisPlayer.update_metadata` templated `np.item_id` directly
  into a D-Bus object path, so user-added radio ids
  (`local-XXXXXXXX`) threw `SignatureBodyMismatchError` on every
  ICY-title refresh. Sanitize non-`[A-Za-z0-9_]` chars to
  underscore.

### 2026-05-23 — bug-squash round 2: lyrics perf, scrobble eligibility, image cache eviction

Follow-up to the morning's bug-squash. Round 2 drains the higher-
impact deep-audit findings that were safe to fix without visual
verification.

**Lyrics restyle perf.** `_restyle_lyrics_around` ran every active-
line tick (~1 Hz for synced lyrics) and re-stamped a full QSS string
on every lyric widget (~20+ per song) via `setStyleSheet`, which
re-parses + re-cascades every call even when the string is
identical. Three changes:

- Hoist the `lyrics_font_size` settings read out of the per-line
  loop — once per `_restyle_lyrics_around` call, not once per line.
- Memoize the produced CSS string by distance bucket — at most
  `len(_FALLOFF)` unique strings per call regardless of lyric count.
- Skip `setStyleSheet` entirely when the incoming string matches
  what's already on the widget.

**`_hex_to_rgb` lru_cache.** `theme._hex_to_rgb` was re-parsing the
same handful of theme-token hex strings thousands of times per
second from paint loops + QSS rebuilds + the lyrics restyle. Wrapped
with `@functools.lru_cache(maxsize=256)`; the cache is keyed by the
hex string so theme switches just see a cache miss for the new
binding — no manual invalidation needed.

**Scrobble eligibility late-duration race.** `_on_duration_set`
updated `state.duration_ms` but didn't re-evaluate eligibility. If
mpv emitted duration AFTER the position tick had already crossed
threshold (streamed content, late duration), a track ending in the
same tick the duration arrived would skip the scrobble. Now flips
`eligible=True` inside `_on_duration_set` if the elapsed math now
qualifies.

**Image cache eviction off the GUI thread.**
`image_cache._evict_if_over_cap` walks the cache dir + stats every
file synchronously; on a full cache (~2000 entries) that's 10–50 ms
on spinning disk, and it ran on the GUI thread every 50 puts (cover
load callbacks). Now scheduled via `async_io.run_async` so a
freshly-arrived cover never gates on the stat sweep.

**Cast cleanup logging.** `CastManager.cleanup()` silently swallowed
`stop_cast()` failures, hiding diagnostic info on a hung receiver
at app exit. Now prints a one-line breadcrumb with the exception.

**Local re-import sweep.** Removed five redundant local
`from modules.providers import get_provider` and
`from modules.player_state import PlayerBus` re-imports in
`library_grid.py` and `now_playing_page.py` where the name was
already at module top. Per `feedback_local_reimport_scoping.md` —
not bugs today, but the same pattern that flags a name as local-
scope for the whole function and `UnboundLocalError`s if a
module-level reference is added later.

### 2026-05-23 — bug-squash batch + shutdown tightening

A full code+doc audit pass surfaced a backlog of correctness, perf,
and shutdown-speed issues. Everything in this entry was caught by
that sweep and fixed in one go.

**Shutdown speed.** Closing the terminal that spawned the app used to
leave the main window + mini player visible for up to ~3.5 s while
the visualizer subprocess and FFT-worker thread tore down. Now:

- `_cleanup` hides the main window + mini player FIRST so the user
  sees them vanish the instant the shutdown signal arrives, before
  any blocking teardown runs.
- `VisualizerEngine.stop()` learnt a `fast=True` mode that skips the
  1.0 s + 0.5 s subprocess waits and shortens the QThread.wait from
  2 s to 100 ms — `_cleanup` calls it with `fast=True` so we don't
  pay those waits at shutdown. The OS reaps the orphan parec /
  pw-record subprocess when the process group dies anyway.
- Trims roughly ~3.5 s off shutdown when the visualizer is active.

**Sign-out flush.** `jellytoast.py` clears `access_token` / `user_id`
/ `username` on sign-out but never called `settings.flush()`. A tray
Quit immediately after sign-out could lose the credential clear
silently (per `known_issue_qsettings_flush.md`). Added the flush.

**Mini player provider staleness.** `FloatingMiniPlayer.api` was
cached at construction, but `mini` wasn't pinned to `win`, so
`_refresh_provider_refs` skipped it on sign-out / kind switch — the
mini player kept building stream + cover URLs against the discarded
provider singleton and silently 401'd. Pinned + added to the refresh
tuple.

**Queue radio-refill race.** A clear() between dispatch and callback
left the in-flight `_on_refill_result` to mutate the now-empty
queue, re-appending stale tracks and re-firing `radio_extended`.
Added a `_refill_gen` generation token bumped on clear; the callback
drops the batch silently if its captured gen no longer matches.

**Cast-proxy malformed Range header.** A reversed range like
`bytes=5-3` set `partial = False` and fell through with `start=5,
end=3`, sending `Content-Length: -1`. Now resets to whole-file on
any invalid range.

**Offline .part leak on disk-full.** A write failure mid-download
(ENOSPC, permission, network read error) left the partial `.part`
file on disk; repeated retries accumulated orphan fragments. The
write loop now wraps in try/except and calls `store.discard_part`
before re-raising.

**Theme signal-connection leaks.** `CastDialog`, `VolumePopup`, and
the per-speaker volume popup all connected to `PlayerBus.theme_changed`
at construction without a disconnect. Each session built up duplicate
slot subscribers (cast dialog is rebuilt every open; the volume popups
rebuild on speaker-list changes), and a theme flip then ran
`_reapply_accent` N+1 times. Switched all three to
`Qt.UniqueConnection`.

**Other audit fixes.**

- `_OpaqueComboBox._popup_opaque` flag was set *before* the
  conditional re-show; a failed re-show would leave the fast-path
  engaged with a still-translucent popup. Moved the flag-set after.
- `kde_titlebar.handle_titlebar_double_click` fell through to
  vertical-max for `Shade` / `Lower` / `OnAllDesktops` when the KWin
  shortcut invocation failed. Early-return now — honour the user's
  config by doing nothing rather than maximizing.
- `offline.library_sync._sync_timer` was a `QTimer()` without a
  parent; passes `QApplication.instance()` so a future non-GUI-thread
  caller doesn't lock the timer's affinity to a worker thread.
- Dead `NameError` block at `_OpaqueComboBox.showPopup` (referenced
  undefined `translucent_mode` and `apply_elevated_blur` — leftover
  from a refactor) deleted; unused `BORDER` import removed from
  `top_bar.py`. Ruff clean across the whole repo.

**Perf wins.**

- `library_grid` paint hot-path: tile + row paint were running
  `from modules.ui_helpers import TEXT/ACCENT` per call — IMPORT_NAME
  + IMPORT_FROM opcodes through `sys.modules` per tile. Switched to
  `from modules import ui_helpers as _u` once at module top, then
  `_u.TEXT` / `_u.ACCENT` reads in paint. Same live-theme semantics
  (attribute access reads the current binding, not a frozen value),
  much cheaper.
- `now_playing_bar._on_position` coalesces the `cur_time.setText`
  call to one per visible second. Position emits at mpv's observer
  cadence (~10 Hz); the label only changes every 1000 ms.

### 2026-05-23 — settings cleanup: dead-weight toggles dropped

Four Playback toggles never earned the row they took — every user who
turned them off was making a wrong call. They're now hard-coded on:

- **Gapless playback** — always on. Removed the checkbox, the
  `settings.gapless` property, and the `if gapless:` gates in
  `queue_manager._emit_prefetch` + `player_backend`. Prefetch + the
  mpv `gapless_audio` kwarg fire unconditionally.
- **Smart shuffle** — always on. Removed the checkbox, the
  `settings.smart_shuffle` property, and the dispatch gate in
  `queue_manager._shuffle_rest`. The queue always routes through the
  weighted anti-clustering picker; libraries under 16 tracks still
  fall back to classic shuffle.
- **OS media keys / MPRIS** — always on. Removed the checkbox + the
  `settings.media_controls_enabled` property; `MediaControlsService`
  starts unconditionally in `jellytoast.main()`.
- **Streaming-format readout** — always visible. Removed the
  checkbox, the `settings.show_streaming_info` property, the
  `PlayerBus.streaming_info_changed` signal, and the slot that
  toggled visibility.

Playback / Casting / Library settings pages also tightened —
explanatory captions dropped where the controls already spoke for
themselves, EQ density tuned so the page fits without scrolling.
Tests: 1692 → 1692 (smart-shuffle setting-gate test removed; the
weighted-picker behaviour is still covered).

### 2026-05-23 — see-it/fix-it polish + General settings redesign

- **Player bar gap** — sub-pixel seam between the body and the now-
  playing bar closed by routing the bar through the same elevated-
  surface path as the rest of the chrome.
- **LIVE pip centring** — the radio "LIVE" indicator now picks the
  exact baseline of the elapsed-time label so it reads as a single
  unit instead of a floating chip.
- **Volume popup opacity** — main-player popup re-stamps its own
  background QSS on every theme flip, so a dark↔light switch
  recolours the whole pill live.
- **General settings page** — redesigned to be a single readable
  page: theme + accent + font scale grouped together; "About this
  app" moved to a single right-aligned link row instead of a section.

### 2026-05-23 — titlebar double-click honors kwinrc

The borderless top bar now defers to KWin via D-Bus on titlebar
double-click and reads KDE's `TitlebarDoubleClickCommand` setting —
so a user who configured "Maximize", "Maximize Vertically", "Shade",
"Lower", "OnAllDesktops", "Restore", or "Nothing" gets the action
they expect on their other windows. New helper:
`modules/kde_titlebar.py` (`invoke_double_click_command()`,
`handle_titlebar_double_click()`); read at click time, cached for
the lifetime of the process. Falls back to the prior
"vertical-maximize" behaviour off-KDE / on D-Bus failure. Also moved
the native-window-border opt-in into Display → Interface (out of
Playback) where it belongs.

### 2026-05-22 — defer indicator repolish on theme switch

`PlayerBus.theme_changed` was repolishing every `_PageNavRow`
indicator synchronously inside the same Qt event the
`update_active_qss` cascade was already painting from — the chained
`style().polish()` calls re-issued the same painting path on the
same widget and cost ~80 ms on a cold theme flip. Repolish now
defers via `QTimer.singleShot(0, ...)`. Also moved tooltip body
colour to `QPalette.ToolTipBase` so KDE's tooltip QML reads the
right swatch without the global QSS shadowing.

### 2026-05-22 — audio routing fix (three-bug stack)

Playback was silent on every sink (Speakers, Sunshine virtual sinks,
Apollo streaming) after the PipeWire 1.6.4 → 1.6.5 update on
2026-05-15. Three independent bugs stacked:

1. **Visualizer tap stole mpv's sink routing.** PipeWire 1.6.5
   changed link policy — a capture stream targeting a playback
   stream node now suppresses that playback node's link to the
   sink. `pw-record --target=jellytoast` left mpv linked only to
   the visualizer tap, never to Speakers. **Fixed** by switching to
   `pw-record -P stream.capture.sink=true` (no `--target`), which
   captures the default sink's monitor and follows changes — mpv's
   routing is untouched. Trade-off accepted: visualizer reacts to
   all sink audio, not just jellytoast's.
2. **WirePlumber persisted a stuck mute.** `restore-stream` had
   `mute:true` pinned for `application.id:jellytoast` from a single
   accidental mute click and re-applied it every launch. **Fixed**
   in `modules/player_backend.py`: `toggle_mute()` rewritten as
   volume save/restore (never touches `mpv["mute"]`, so the PW
   node's mute flag stays false and WirePlumber has nothing to
   persist); `_init_mpv()` force-sets `mpv["mute"] = False` to
   clear any stale state already saved; `set_volume()` clears the
   mute state if the slider moves while muted.
3. **(System-side, no code change.)** Default sink had been the
   Sunshine virtual sink with no Apollo client receiving — surfaced
   only because the WirePlumber restart we did to clear bug 2
   reloaded a stale `default-nodes` pointer. Self-corrects in
   normal use.

### 2026-05-22 — unified elevated-surface treatment (dark themes)

One source of truth in `modules/theme.py` for every "lifted" surface:

- `_DARK_ELEVATED_ALPHA` / `_DARK_ELEVATED` drives translucent
  surfaces (button hover, list-row hover, selected row, volume
  popup body). Child widgets ride the body's compositor blur for
  free.
- `_DARK_POPUP_OPAQUE` drives top-level surfaces (combo popups,
  QMenus, tooltips). Tuned opaque colour matching what
  `_DARK_ELEVATED` looks like over the blurred body — going opaque
  here because Wayland surface translucency for top-level popups
  is too fragile across Qt's internal autofill paths.
- `_DARK_TOOLTIP_BG` aliases `_DARK_POPUP_OPAQUE` so tooltips
  match popups; kept as a separate alias so they can diverge.

Wired through `modules/ui_helpers.py` (`apply_elevated_blur`,
`_fill_is_translucent`, `_tooltip_fill_opaque`, opaque tooltip fill,
GLOBAL_STYLE QMenu + QComboBox QAbstractItemView defaults),
`modules/settings_dialog.py` (`_OpaqueComboBox` simplified to
always-opaque, `QFrame.Shape.NoFrame` on popup + view to kill the
residual top/bottom edge line), `modules/top_bar.py` (Albums + sort
menus switched `BG_PANEL` → `POPUP_OPAQUE_FILL`), and `jellytoast.py`
(`_TooltipBackdropFilter` — a `QApplication` event filter hardening
QTipLabel opacity on Show so top-row tooltips don't render as
floating text).

**Borders removed** from every dropdown and menu in the app.

### 2026-05-22 — live-theme misses + dialog blur refresh

Three surfaces the live theme-mode flip was missing:

- **`now_playing_page.py`** — `_reapply_theme` now re-stamps the
  right-pane kicker label ("ALBUM · 19", "PLAYLIST · …"). It was
  baking `ink_alpha(0.78)` at construction and never refreshing.
- **`settings_dialog.py`** — `_reapply_dialog_accent_styling` now
  re-stamps the "Settings" title label + cog icon (the titlebar is
  built once and never rebuilt, so the page-rebuild path missed it).
- **`settings_dialog.py`** — `_apply_blur` defers via
  `QTimer.singleShot(0)` after the body repaint and toggles blur
  off→on so KWin doesn't dedupe the re-issued `enableBlurBehind`
  call and silently no-op it on a live mode switch.

### 2026-05-22 — volume popup + crossfade slider + vertical-max

- **Main-player volume popup 25% shorter** — `POPUP_H: 135 → 101`
  in `_VolumeSliderPopup`. Mini-player popups pass their own height
  so they're unaffected.
- **Volume popup live-themes** — `_reapply_accent` now re-stamps
  the popup's own background QSS, not just the slider gauge inside,
  so a dark↔light flip recolours the whole pill live.
- **Crossfade duration slider** — was falling back to Qt Fusion's
  blue handle. New `_horiz_slider_qss` helper in
  `settings_dialog.py` applies the same accent-fill + white-handle
  treatment as every other horizontal slider, wired into
  `_reapply_dialog_accent_styling` so it follows accent live.
- **Vertical-max on titlebar double-click** — double-clicking the
  borderless titlebar now expands the window to full screen height
  (preserving x + width), with the pre-expand geometry stashed for
  toggle-back. Full maximize stays available via the maximize button.

### 2026-05-21 — borderless main window

The main window is now **borderless by default** on KDE Wayland. A
KWin `noborder` rule strips the server-side titlebar/frame, and
jellytoast's top bar doubles as the window's titlebar — drag any empty
area to move, double-click to maximize, with min / max / close on the
right. Edge + corner resize is supplied by an app-level event filter
(`startSystemResize`); the window paints its own rounded body, squared
while maximized so it sits flush.

Crucially the window stays *server-side-decorated* — the `noborder`
rule only hides the chrome, it doesn't make the window client-side —
so KWin keeps owning the geometry and **snapping / tiling stay fully
native and flush**, no gaps, no heuristics.

A **"Use native window border"** toggle (Settings, KDE Wayland
section) restores KDE's decoration; restart-required. Implementation:
a toggleable main-window `noborder` backend in `keep_above` (exact
title match), the `native_window_border` setting, `JtTopBar` titlebar
mode, `_ResizeEdgeFilter`, and three new `win_*` window-control icons.
Off KDE Wayland the main window is unchanged (KWin/native decoration).

### 2026-05-21 — OS-matched window corners + mini-player icon balance

The frameless surfaces (mini player, settings dialog) paint their own
corners — KWin draws no decoration for a `noborder` window — and were
over-rounded versus the desktop: mini player 12px, settings 14px
against KDE Breeze's ~8px (measured). New `RADIUS_WINDOW` design token
(8px, the host-OS window-corner radius) is the single source of truth;
both window bodies, the now-playing-bar cover's window-seating corner,
and the mini player's right-edge volume popup route through it. Album
covers were already at 8.

Also: the mini player's three bottom-right window-control icons
(`expand` / `open_window` / `volume`) were unbalanced — `open_window`
was drawn in only an ~8-unit area of the 24-unit icon grid while the
others filled ~14. Redrew `open_window` on the same ~14-unit grid so
the trio reads as one set.

### 2026-05-21 — drag-repaint fix for the blur "line artifact"

Dragging a translucent (blurred) jellytoast window on KDE Wayland left
a trail of stale blurred-rectangle "line artifacts" — KWin bug
455526/457727, the blur background-cache going stale on the optimized
partial-damage render path (pronounced on NVIDIA). Confirmed not
app-fixable from the Qt side: a Wayland client can't detect its own
drag, and client damage isn't compositor damage.

The fix is a tiny **bundled KWin scripted effect** (`modules/drag_repaint/`).
While one of jellytoast's windows is interactively moved, the effect
holds it under an *in-progress* zero-drift transform — KWin repaints an
actively-animating window every frame, which routes the drag through
the full-repaint path (`paintGenericScreen`) and never exercises the
buggy partial-damage path. It also sets `WindowForceBlurRole` so the
blur survives the transform (the trick the built-in `maximize` effect
uses). The transform is visually inert; it only flips the render path.

The effect ships as package data (a `metadata.json` + `main.js` pair)
and is installed into the user's KWin effects dir + loaded over D-Bus
at startup — idempotent, best-effort, a no-op off KDE Wayland. It's on
unconditionally (matching how the `keep_above` no-border rules already
work); `JT_NO_DRAG_REPAINT=1` removes it as a support escape hatch.

### 2026-05-21 — theming rework, window blur, cast-safe shutdown

**Theming — Phases 1-3 of the light/dark rework.** The `Theme`
dataclass went from 13 colour fields to 28 semantic tokens, named by
intent (`wash_hover`, `surface_input`, `idle_text`, …) — the layer that
will swap wholesale for a light theme. ~170 hardcoded
`rgba(255,255,255,a)` literals across ~18 widget files now route
through a new `ink_alpha()` helper: value-identical on the dark themes,
dark-tinted automatically on a future light theme. Theme-*mode*
switching is now **live** — no restart — re-stamping every surface's
QSS and repainting the painted window bodies. Settings-dialog pages
build lazily (the ~900 widgets are no longer constructed up front), so
a live theme switch is ~4-6x faster.

**Window blur.** The Frosted theme now asks KWin to blur behind the
main window, mini player, and settings dialog (`modules/blur/`, via
`KWindowEffects` reached through ctypes). Frosted's body opacity was
lifted so the blur reads as frosted glass. The mini player + settings
dialog are server-side-decorated with a `noborder` KWin rule on KDE
Wayland — KWin keeps blur alive through a window drag only for
*decorated* windows.

**Cast-safe shutdown.** Closing the launch terminal (SIGHUP), `kill`
(SIGTERM), or Ctrl+C (SIGINT) now shut the app down gracefully so the
`aboutToQuit` cleanup runs and an active Chromecast / AirPlay session
is stopped first — previously a terminal close orphaned the cast.

Also: mini-player toggle / open-window glyphs redrawn as registry
SVGs; a mini-player label-background fix the transparent theme exposed.

### 2026-05-21 — cover-art upload + theming/blur test coverage

Two autonomous-agent branches reviewed and merged onto `main`
(`e33f40e`); 1533 → 1597 tests.

- **`auto/cover-art-upload`** — `MediaProvider.upload_cover_art()` was
  a base-class stub. The Jellyfin provider now implements it:
  `JellyfinAPI.upload_primary_image()` POSTs to
  `/Items/{id}/Images/Primary` with the raw image **base64-encoded**
  as the body and the picture's own mime type as `Content-Type` (the
  endpoint takes neither multipart form data nor raw bytes). Subsonic
  has no clean cover-upload endpoint and keeps the raising stub.
  Mocked-HTTP tested; **no UI yet** and not exercised against a live
  server — both are follow-ups.
- **`auto/theming-blur-tests`** — `tests/test_theme.py` +
  `tests/test_blur.py`, +57 tests covering the 28-token `Theme`
  dataclass, `_DARK_TOKENS` sharing, `get_active_theme()` accent
  override + malformed-hex fallback, `ink_alpha()` value-identity, and
  the `modules/blur` backend dispatch / `_rounded_region` geometry.

### 2026-05-20 — tag editing: the "Edit tags…" dialog

The metadata-write backend (Jellyfin GET-merge-POST with the
jellyfin#10724 LockedFields workaround) had shipped, but there was no
UI. Right-clicking a track in the songs view now offers **"Edit
tags…"** — a modal editor (`modules/tag_editor.py`) over the seven
server-editable fields (title, artists, album, album artist, genres,
track no., year). Save sends only the *changed* fields, so the
LockedFields set stays scoped to what was touched.

- **Admin gate** — Jellyfin only lets administrators edit metadata.
  `JellyfinAPI.is_admin` is captured from the `Policy` block of the
  authenticate / verify_session responses (no extra request), and the
  menu entry shows only when `can_edit_metadata` *and* the new
  `can_edit_metadata_on_account()` both pass. Subsonic shows nothing.
- Cover-art upload and bulk "apply to album" editing remain follow-ups.

### 2026-05-20 — editable hotkeys page

Settings → Hotkeys was read-only ("Customization coming soon"), even
though `modules/hotkeys.py` had supported per-action overrides all
along. The page is now registry-driven and editable: each in-app
shortcut gets a `QKeySequenceEdit` capture field and a per-row Reset,
plus a "Reset all to defaults". A new `hotkeys.find_conflict` helper
flags a chord already bound to another action — the edit snaps back
and an inline warning names the clash. Rebinds take effect immediately
(no restart): the page emits the new `PlayerBus.hotkeys_changed`
signal and the main window re-installs its QShortcuts. Media keys stay
in a read-only section (they route through MPRIS).

### 2026-05-20 — multi-server URLs: login UI + failover toast

The connectivity engine could already fail over between several server
addresses for one account, but there was no way to *enter* alternates
and no feedback when it switched.

- **Login screen** — an "+ Add alternate URL" affordance under the
  Server URL field opens a manager dialog (`_AlternateUrlsDialog`) for
  the `server_hostnames` failover list: add / edit / remove rows of
  url + optional label, saved in list order (= probe priority).
- **Failover feedback** — the main window now shows a transient toast
  on `host_switched` ("Switched to alternate server · <label>" /
  "Reconnected to the primary server").
- **New `modules/toast.py`** — a reusable in-app toast: a bottom-anchored
  pill that fades out on its own after a few seconds. Non-interactive,
  self-destroying, theme-token styled.

### 2026-05-20 — crossfade Settings controls

The crossfade engine (two-handle ping-pong fade) was built but only
reachable via the `JT_CROSSFADE=1` developer env var. Settings →
Playback now has a proper **Crossfade** section: an enable toggle, a
fade-duration slider (1–10s), and the "skip between same-album tracks"
escape hatch — all wired to the existing `crossfade_*` settings. The
section greys out while casting (crossfade is local-playback only).
The `JT_CROSSFADE` env gate is removed — the enable checkbox is now
the only opt-in; `_ensure_crossfader()` builds purely off
`crossfade_enabled`, so default behavior (off) is unchanged.

### 2026-05-20 — Last.fm scrobbling parked

Last.fm's account-signup firewall (Error 406) blocked registering the
in-app API key, repeatedly, from several networks and devices. Last.fm
is deferred until that cooperates — the client code stays dormant in
`modules/scrobble/lastfm.py`, and the Settings → Scrobbling page now
hides the Last.fm section entirely while `API_KEY` / `API_SECRET` are
empty (it previously showed a "coming in a future build" placeholder).
ListenBrainz is the supported scrobbling path and is unaffected.

### 2026-05-20 — smart-rule schema v2 + live-verification fixes

Merged the `auto/smart-rule-schema-v2` work (date-based smart-playlist
rule fields) and fixed everything live testing against real Jellyfin /
Subsonic servers turned up.

#### Added

- **Schema v2 — date smart-playlist rules.** New `date_added` /
  `last_played` rule fields with `in the last` / `before` / `after`
  operators. Jellyfin reads `DateCreated` / `UserData.LastPlayedDate`;
  Subsonic maps its `created` timestamp onto `date_added` (it has no
  per-track last-played data, so `last_played` never matches there).
  The "Recently added" preset now filters on a real date.

#### Changed

- **Smart-playlist editor — date-rule UI.** The new fields/operators
  get friendly combo labels (no more raw `date_added` / `in_the_last`
  tokens); `in the last` uses a day-count spinbox with a `days` suffix;
  `before` / `after` use a calendar date picker. Rule/match/sort combos
  size to their contents so labels never clip, and the dialog is wider.
- **Dropped the "Forgotten favorites" preset** — permanently empty on
  Subsonic and needs aged listening history on Jellyfin.

#### Fixed

- **Date smart rules timed out on Jellyfin.** A date rule has no
  server-side filter, so `query_items` fetched the entire library in
  one request to refine in Python — on a ~3700-track library Jellyfin
  exceeded the 15s HTTP timeout building the payload, the `except`
  swallowed it, and every date-rule playlist previewed empty. The
  refine fetch now pages via `StartIndex`, and "recent" rules
  (`in the last` / `after`) sort by the date server-side and stop
  paging at the cutoff — a 30-day window previews in ~4s.
- **Editor stored `in the last` values as strings**, which failed
  schema validation (`value must be an int`) — the `⚠` the preview
  showed. The day-count spinbox now emits a real int.

### 2026-05-20 — sleep-timer + smart-shuffle UI

Two engines that were fully built but unreachable now have UI. 1457
tests.

#### Added

- **Sleep-timer menu** — a moon button in the now-playing bar (between
  the mini-player and cast icons) opens a duration menu: 15 / 30 / 45
  min, 1 hour, 1 h 30 min, plus "Stop after current track". While a
  timer is armed the moon goes accent-tinted and the tooltip shows a
  live countdown; re-opening the menu offers "Cancel timer". Timed
  presets use the fade-to-stop wind-down. The bar talks to
  `PlayerBackend` through two new bus signals — `sleep_timer_requested`
  and `sleep_timer_cancel_requested` — so it needs no Player reference.
- **Smart-shuffle toggle** — Settings → Playback now has a "Smart
  shuffle" checkbox driving the existing `playback/smart_shuffle` key.
  The artist-spread picker (anti-clustering — keeps the same artist
  from landing back-to-back) was already wired into the queue; there
  was simply no way to turn it on.
- **`moon` icon** in the shared SVG registry.
- **Tests** — 3 headless tests covering the sleep-timer request-signal
  wiring, 4 covering `_VolumeSliderPopup` construction. Suite 1455 →
  1461.

#### Fixed

- **Now-playing bar volume slider didn't appear.** The 468c599
  "mini-player volume slot" refactor moved the popup's slider + layout
  creation inside `_apply_right_edge_qss`, which only runs in the mini
  player's right-edge mode. The now-playing bar's center-mode popup was
  built with no `slider`, so `VolumeButton._show_popup` raised
  AttributeError on `set_value()` and aborted before `show()` — the
  slider silently never appeared (the scroll wheel still worked, since
  it's a separate path). Slider construction moved into `__init__` for
  both modes; `_apply_right_edge_qss` is now a pure stylesheet refresh,
  which also fixes it orphaning a fresh slider on every reposition in
  the mini player.

### 2026-05-20 — context-menu wiring, dead-code removal, housekeeping

Smart-playlist and track-radio context-menu surfaces wired up; the
unused context-menu installer layer deleted. 1442 → 1455 tests.

#### Added

- **Context-menu wiring** — new `ui_helpers.open_create_smart_playlist()`
  helper. "Create smart playlist from this artist/album/genre" is now
  wired into the song, album, artist, and genre right-click menus, and
  "Start radio from this song" into the songs view (track radio was
  previously built but never reachable). SPEC.md §6 documents the menu
  surface.
- **Tests** — 6 headless tests pinning the context-menu action set per
  item kind. Suite 1442 → 1455.

#### Changed

- **Dead code removal** — deleted the unused context-menu installer
  layer from `ui_helpers.py` (`install_song/album/artist/genre_context_menu`
  + `_install_seed_radio_menu`, ~220 lines, zero call sites — the views
  inline their own context menus). `start_seed_radio` kept.
- **Repo housekeeping** — removed 18 stale agent worktrees and 25
  merged branches.

#### Branches awaiting review

- `auto/smart-rule-schema-v2` is built (adds `date_added` / `last_played`
  smart-playlist rule fields, +42 tests) but left unmerged pending
  live-server verification of the provider field names.

### 2026-05-20 — autonomous queue: cast refactors, radio parity, smart-playlist backend

Six-branch autonomous round merged to `main`. 1348 → 1442 tests.

#### Changed — refactors

- **`cast/dlna.py` split** — the 1188-LOC monolith became a
  `modules/cast/dlna/` 9-file subpackage (`_constants`, `_settings`,
  `_models`, `didl`, `codec`, `discovery`, `_loop`, `controller`).
  Pure refactor; `tests/test_cast_dlna.py`'s full-path imports
  preserved via `__init__.py` re-exports.
- **`cast_manager.py` split** — the 794-LOC monolith became a
  `modules/cast_manager/` package: `_ChromecastMixin` + `_AirplayMixin`
  + a thin `CastManager` orchestrator. The six `test_cast_gating.py`
  monkeypatch globals stay in the package `__init__` and are resolved
  at call time so the patches remain load-bearing.

#### Added — features

- **CastManager DLNA / Sonos / Snapcast discovery fan-out** — new
  `_OtherProtocolsMixin`. `discover_all` fans across all five
  protocols; each `discover_<type>` gates on `cast/<type>_enabled`
  + an optional-dep probe, runs blocking discovery off the GUI
  thread, adapts results into `CastDevice` rows, and pushes them
  through `_notify` so the cast dialog's per-protocol sections fill.
  `stop_cast` routes by `device_type`; `cleanup` tears down the
  DLNA loop thread.
- **Seeded radio entry-point parity** — album / artist / genre
  right-click "Start radio" wired into `LibraryGrid.contextMenuEvent`
  and `_GenresListView`, plus three reusable installers
  (`install_album/artist/genre_context_menu`) and a shared
  `start_seed_radio()` launcher in `ui_helpers.py`. RadioFeeder
  already honours every seed kind.
- **Smart-playlist backend hardening** — recipe factories
  (`from_artist` / `from_album` / `from_genre` / `from_year`) so a
  right-click "Create from this X" flow has rules to call; schema
  additions `is_favorite` (bool), `starts_with` / `ends_with`
  (string), and `sort: random`; `schema_version` field on persisted
  entries (v0 entries load cleanly as v1);
  `open_smart_playlist_editor(preset_rules=, suggested_name=)`.

#### Fixed

- Mechanical ruff cleanup — 11 findings (unused imports, a dead
  local, extraneous f-string prefixes) across 8 files.

### 2026-05-19 — visualizer chill arc, smart playlists, internet radio, EQ UI

Two large commits (`7e0bed0` AM, `468c599` PM) collapsed a wide
mix of features direct to `main` — too tightly coupled to live
verification for the `auto/*` queue. 1229 → 1348 tests.

#### Added — features

- **Visualizer paint widget** shipped end-to-end:
  - `modules/visualizer_widget.py` — initially 32 grounded log-bars,
    upgraded same-day to a Catmull-Rom Bezier wave (16 downsampled
    control points, x-warp 0.55, per-band amplitude weight 1.0→3.0,
    3-tap spatial smoothing, 65% height cap).
  - `settings.np_left_pane_mode` tri-state (`cover | lyrics |
    visualizer`); NP-page toggle cycles `lyrics ↔ visualizer`.
  - Pre-signal "Visualizer · waiting for audio signal" caption until
    first FFT payload.
- **Visualizer per-stream audio tap** — `MonitorAudioTap` prefers
  `pw-record --target=jellytoast` (per-stream isolation; reads only
  mpv's stream since mpv registers with `audio_client_name="jellytoast"`).
  Falls back to `parec --device=@DEFAULT_MONITOR@`. System audio
  from other apps no longer bleeds into the bars. The `JT_VISUALIZER=1`
  env gate is dropped — NP mode-pick is the consent gesture.
- **Smart playlists end-to-end:**
  - `settings.smart_playlists` — JSON list of
    `{name, rules, created_at}`; setter validates via
    `smart_rule_schema.validate_rules`.
  - `modules/smart_playlist_editor.py` — dialog with name + preset
    picker (4 starter recipes: Recently added / Forgotten favorites
    / Top played / Year), match mode all/any, rule chips
    (between op swaps to spinner pair), sort + descending + limit,
    **live preview** pane with 350 ms debounce calling
    `provider.query_items` async (first 25 matches).
  - `modules/smart_playlists_view.py` — library tab between Playlists
    and Songs; rows with Play / Edit / Delete. Play resolves rules →
    tracks (async) and installs a PLAYLIST queue so all existing NP
    chrome works.
- **Internet radio UI:**
  - New "Radio" library tab; `modules/radio_view.py` with
    `_StationRow`, `_StationFormDialog`, popular-stations picker.
  - `modules/radio_presets.py` — 10 curated stations (SomaFM ×4,
    KEXP, WFMU, NTS ×2, Radio Paradise ×2) with logos via
    apple-touch-icon convention.
  - `modules/radio_art.py` — MusicBrainz + Cover Art Archive lookup
    (1 req/sec rate-limited, LRU cached, ICY title parser).
  - `modules/radio_state.py` — single source of truth (`RadioState`
    + `radio_state_changed` signal); unifies bar / mini / NP page.
  - LIVE indicator playback-gated: ● LIVE · station while streaming,
    dim PAUSED · station on pause.
- **EQ shipped** — 10-band graphic EQ + master pre-amp
  (`settings_dialog.py:807-980`). Enabled checkbox, preset combo,
  save/delete, double-click snap-to-zero, cast-greying caption.
- **Track radio right-click entry point** —
  `install_song_context_menu` adds "Start radio from this song"
  for single-track selections.
- **Mini-player volume right-edge slot** — popup hugs the bar-height
  bottom slice (96 px), bottom-anchored. Compact: fills right strip;
  expanded: sits below album. Dynamic top-right corner radius.
  Reparented to `miniContainer` (Wayland z-fight fix).
- **Downloaded indicator + bytes-fraction progress** — hover-revealed
  BL download/check + BR heart corner buttons on album tiles; accent
  progress ring during download. NP-page cover gained a BL download
  CTA.
- **Settings dialog is non-modal** (commit `74d304d`).

#### Changed

- **NP-page toggle UX** — toggle always-visible-when-eligible (drop
  hover gate) so the row's height stops collapsing on cursor-leave
  and the visualizer doesn't shift 1-2 px on every hover crossing.
- **Queue manager radio path** — `_build_now_playing` honours embedded
  `streamUrl` so radio items skip offline-blob lookup;
  `_on_started` skips provider cover for radio items so station
  logos aren't clobbered.

#### Fixed

- **NameError in `_on_radio_state`** at `now_playing_bar.py:1809` —
  stray `run_async(lookup_art_url, ...)` line from the radio
  refactor; radio path no longer throws on every state emit.

#### Tests

- 1229 → 1348 tests across both sessions. New cases include:
  visualizer widget, visualizer engine pw-record/parec selection,
  smart-playlist settings round-trip + drop-malformed + name-trim
  + malformed-JSON recovery, radio_state, radio_art, radio_presets,
  queue radio path, offline bytes helper.

### 2026-05-18 (evening) — downloads progress arc + library walk

Capped the long 2026-05-18 day with an interactive Downloads arc on
top of the afternoon's 9-branch merge round. Net suite: 1178 → 1229
(+51 over the arc; 1057 → 1229 across the whole day).

#### Added — features

- **Aggregate "Downloading X of Y · Z%" block** on Settings →
  Downloads. Speed (`X MB/s`), longest-job ETA (`Y left` /
  "calculating…" / hidden past 12 h), 4 px accent progress bar.
  Live-applies accent. Hides when idle. Variants for paused +
  paused-mid-library-walk.
- **"Download entire library" button** + confirmation dialog. Two-
  phase walk: enumerate albums to pre-sum `ChildCount` for a stable
  total, then enqueue each album not already downloaded. Idempotent
  re-run.
- **"Keep library in sync" setting** with 6-hour periodic re-walk
  timer; auto-bootstraps on app start via `offline.init`.
- **Notify-on-completion checkbox** — desktop notification fires on
  drain via `modules/notifications/`; gated by
  `Settings.notify_on_download_complete` (default on).
- **Standalone Downloads main-content view** reached from top bar →
  tab dropdown → "Downloads". Per-album list (Re-sync + Remove,
  stale badge) moved out of Settings → Downloads into its own page;
  settings stays pure controls.
- **"Clear all downloads" button** with confirmation. Full reset:
  empties queue, lifts pause flag, zeroes in-memory session
  counters, clears persisted library-walk state, emits a final
  stats `(0, 0, 0.0, 0.0)` so the aggregate hides. Auto-hides when
  there's nothing to clear.
- **Resume on app restart**: `manager.resume_pending` walks the
  index for nodes in state `pending` / `downloading`, resets the
  latter back to `pending`, and re-queues their leaf tracks.
  `.part` fragments overwrite cleanly thanks to the atomic-rename
  architecture.
- **Persisted library-walk state** survives a close-reopen:
  `library_download_in_progress` keeps the "Pause library download"
  rebrand; `library_download_expected_total` keeps the stable "of Y"
  count.

#### Added — backend

- `PlayerBus.download_queue_stats` signal carrying
  `(active, total_session, speed_bps, eta_seconds)` at 1 Hz.
- Per-job byte-rate sampling over a 3-second window; longest-job
  ETA projection capped at 12 h.
- `manager.set_session_expected_total(n)` to clamp `total_session`
  from below so library walks read a stable right-hand number.
- `manager.reset_session_counters()` for the clear-all path.
- Package re-exports: `offline.clear_all`, `offline.resume_pending`,
  `offline.sync_library`, `offline.start_periodic_library_sync`,
  `offline.stop_periodic_library_sync`.

#### Fixed

- **Stats timer created on the wrong thread**: `_dispatch` runs on
  whichever thread invoked `enqueue` — often a `QThreadPool` worker
  via `sync_library`. A `QTimer` built there never fires.
  `_ensure_stats_timer` now hops to the GUI thread via
  `QTimer.singleShot(0, app, ...)`. Without this the aggregate
  block + pause button were invisible for the duration of every
  library walk.
- **Row popup spam during bulk enqueue**: rapid "pending" emits used
  to trigger a full `reload()` each, briefly flashing every row as
  a top-level window on Wayland. Now incremental — single row added
  per "pending"; `reload` hides + removes from layout before
  re-parenting.
- **Pause button stayed visible at idle.** Now hidden unless
  `paused == True` or `active > 0`.
- **Aggregate tail clipped to "49.1 M"** on tighter Wayland HiDPI
  fonts. Stacked counts on top of tail vertically.
- **Tray `AttributeError`s** on every playback event because the
  `QAction` block had drifted into `_reapply_menu_styling` — only
  built after the first `theme_changed`. Moved to a new
  `_build_menu_actions` called once from `_build_menu`.
- **"Resume downloads" ghost button** after a clear-all + restart.
  `clear_all` now lifts the pause flag and zeroes session state.

#### Changed

- **Settings → Downloads** is now slim — toggles, aggregate, storage,
  pause + Download entire library + Clear all downloads. The
  per-album list moved out to the standalone page.
- **Compact one-line check rows** on Settings → Downloads. Six
  multi-line wordwrapped notes replaced with single-line captions
  pushed to the right of each checkbox.
- **"On drain" → "on completion"** in the one user-facing string
  that used the queue-internal jargon.
- **Whole-page scroll** on Settings → Downloads. Single outer
  scroll area; the inner downloads-list scroll region is gone.
- Pause / resume button rebrands to "Pause library download" /
  "Resume library download" during a full-library walk. Reverts on
  drain.
- Library walk auto-resumes the queue if it was paused — implicit
  consent to drain.
- Library walk does a two-phase enumeration → enqueue so the "of Y"
  total reads stably from the start.

#### Research

- `docs/research/downloads_progress_ui.md` (`4bbf731`) — ~2300 words
  spec that drove the whole arc. Placement, format, edge cases,
  slice plan (A backend / B UI / C notification toggle).

### 2026-05-18 (afternoon) — autonomous-agent queue clearout (9 merges)

The morning's 15-agent autonomous queue landed onto `main` in a
single afternoon review round. 1057 → 1178 tests; ruff clean. All
nine `auto/*` branches in the suggested low-conflict order:

- **`auto/font-token-cleanup`** — `settings_colors_page.py` raw-px
  font-size sweep routed through `type_qss(TYPE_*)`.
- **`auto/qss-parse-fix`** — regression test only; no static
  offender found, audit harness left in place.
- **`auto/backend-package-tests`** — +39 tests for autostart /
  media_controls / keep_above dispatch shapes.
- **`auto/notifications-backend`** — new `modules/notifications/`
  package (notify-send on Linux, unsupported stub elsewhere). +9
  tests.
- **`auto/smart-playlist-presets`** — new `modules/smart_playlists/`
  package with four starter rule sets + a Year-X factory. +16
  tests.
- **`auto/offline-phase6-wifi-only`** — `downloads_wifi_only`
  setting + manager dispatch gate + bus signal + UI checkbox. +14
  tests.
- **`auto/offline-phase6-downloads-ui`** — Pause / Resume queue
  button, per-row Re-sync, stale badge. +8 tests.
- **`auto/radio-feeder`** — seeded-radio queue-side feeder + skip
  detection via the existing `bus.next_track` split. +14 tests.
- **`auto/crossfade-v1-backend`** — new `modules/playback/crossfade.py`,
  two-mpv-handle ping-pong behind `JT_CROSSFADE=1`. +20 tests.

### 2026-05-17 — autonomous-agent queue clearout (11 merges)

Three back-to-back agent rounds emptied the `auto/*` backlog. Net
contribution: +446 tests (533 → 979), all green. All merges follow
the [[feedback-provider-parity]] rule (features identical on both
backends) and ship cast / visualizer / heavy deps as optional extras
with lazy-import gates per the packaging precedent established below.

#### Added — features

- **`scrobble-cap-precision`** — `_MAX_TICK_DELTA_MS` cap inclusive at
  5000ms (was exclusive); `_MIN_TRACK_DURATION_MS` strictly `>` 30s
  per Last.fm / ListenBrainz spec. +6 tests.
- **`offline-index-repair`** — disk-reconciliation walk: drops orphan
  blob rows, recomputes wrong byte counts, surfaces orphan files, flips
  done-state nodes with no blob to failed. +14 tests.
- **`sleep-timer-fade`** — completes A11's `fade_stop` TODO. Linear
  volume ramp at 50ms ticks, configurable duration via new
  `playback/sleep_fade_duration_ms` (default 8000 ms, clamp 1000-60000).
  Cast-active path falls through to immediate pause. +13 tests.
- **`smart-shuffle`** — new `modules/smart_shuffle.py` greedy weighted
  picker behind `playback/smart_shuffle` setting (default off). Spread
  penalty (distance from same-artist picks) × recency penalty. Below
  16 items falls back to classic random.shuffle. +22 tests.
- **`jellyfin-local-radio-stations`** — Jellyfin radio CRUD on a
  QSettings-backed JSON list (`radio/stations`). Dict shape matches
  Subsonic exactly. +28 tests.
- **`offline-retry-backoff`** — additive schema migration `_migrate_v2`
  adds `retry_count` + `retry_after_ts` to `nodes`. Backoff schedule:
  30s, 60s, 120s, 240s, 480s, 960s, 1920s, capped. `retry_failed()`
  filters by `retry_after_ts > now`; new `force=True` kwarg. +27 tests.
- **`tag-editing-backend`** — `provider.can_edit_metadata` capability
  (True on Jellyfin only) + abstract `update_track_metadata`. Jellyfin
  impl appends touched-field lock-names to `LockedFields` per Jellyfin
  bug #10724 (otherwise scheduled refreshes silently revert edits).
  v1 fields: Name, Artists, Album, AlbumArtist, Genres, IndexNumber,
  ProductionYear. +13 tests.
- **`visualizer-fft-backend`** — `modules/visualizer.py` Hann window →
  rFFT → log-spaced mel bands → dB normalisation, MpvAudioTap stub,
  _FFTWorker on dedicated QThread, VisualizerEngine relaying to
  `PlayerBus.visualizer_bands_changed`. Dormant unless `JT_VISUALIZER=1`
  AND numpy importable. +20 tests.
- **`smart-playlist-evaluator`** — `modules/providers/smart_rule_eval.py`
  pure-Python AND/OR rule refinement (sort, limit). Jellyfin pushes
  genre equals, year equals/between, play_count `>`, rating `>` to the
  server; Python refines the rest. Subsonic AND fires the most
  selective server-mappable rule first; OR queries per rule and unions.
  +37 tests.
- **A25 `cast-toggle-discovery`** — per-protocol cast toggles
  (cast/chromecast_enabled, cast/airplay_enabled, cast/dlna_enabled,
  cast/sonos_enabled, cast/snapcast_enabled) + `cast/discovery_timing`
  (startup vs on_demand, default on_demand). Cast settings move to
  their own page. +15 tests.
- **A26 `cast-menu-collapsible`** — CastDialog refactor: one collapsible
  section per protocol type, per-section state persisted in QSettings,
  mutual exclusion across sections. Empty sections auto-collapse.
  +14 tests.
- **A22 `cast-dlna`** — `modules/cast/dlna.py` full DLNA / UPnP-AV
  backend. SSDP discovery + AVTransport push + 714/701 transcode-retry
  + DIDL-Lite builder with mandatory upnp:class + cover-URL cap.
  Private asyncio loop on daemon thread (documented exception to
  [[feedback-async-io-pattern]]). Cast-proxy mandatory. +106 tests.
- **A23 `cast-sonos`** — `modules/cast/sonos.py` SoCo-based zone
  discovery, group transport, event bridge fanning to existing
  `PlayerBus.cast_*` signals (no new signals). Untested against real
  hardware. +74 tests.
- **A24 `cast-snapcast`** — `modules/cast/snapcast.py` Snapcast control
  surface (Option B, not URL push). Groups + clients listing, stream
  switching, volume + mute, group rename. Three new PlayerBus signals
  (`snapcast_groups_changed`, `snapcast_clients_changed`,
  `snapcast_stream_changed`). +57 tests.

#### Added — packaging + tooling

- **Optional extras pattern locked in** for all heavyweight per-feature
  deps:
  ```
  [project.optional-dependencies]
  visualizer = ["numpy>=1.24"]
  dlna       = ["async-upnp-client>=0.47.0,<1.0"]
  sonos      = ["soco>=0.31,<1"]
  snapcast   = ["snapcast>=2.3.8"]
  ```
  Each backend soft-imports via an `_ensure_<dep>()` gate and stays
  dormant when the dep is missing. Keeps the base install (and the
  Flathub bundle) lean.
- **A19 `pre-commit-hooks`** — scaffold for ruff (`--fix` + format).
  Opt-in via `pip install pre-commit && pre-commit install`. Lint
  rules stay in `pyproject.toml`; hook doesn't widen them.
- **`[build-system]` table** added to pyproject.toml (setuptools, flat
  layout). `jellytoast.py` exposed as a `gui-scripts` entry point.
  Repo is now pip-installable (`pip install -e .`) — prereq for AUR /
  Flatpak packaging.
- **Dev helpers moved to `dev/`** — `install.sh`, `run.sh`,
  `create_desktop_entry.sh`. They're git-clone scaffolding, not part
  of the AUR/Flatpak install path.
- **`requirements.txt` removed** — `pyproject.toml [project]
  dependencies` is the single source of truth.
- **pyatv pin bumped** to `>=0.17` (code targets the modern API).
- **Ruff format pass** — 113 files reformatted (cosmetic; PEP-8 slice
  spacing, function-call rewrap, blank lines after lazy imports).
- **Ruff `--fix`** — 16 F401 unused imports + 1 F541 f-string-without-
  placeholders cleaned up. `ruff check .` now reports "All checks
  passed!"

#### Changed

- Cast settings moved out of the Playback page into their own page
  per [[feedback-cast-settings-own-tab]].
- DLNA / Sonos / Snapcast section state in CastDialog persists across
  sessions per [[feedback-cast-menu-unified-collapsible]].

#### Fixed

- Scrobble eligibility math edge cases (5000 ms cap inclusivity, 30 s
  minimum strictness) per scrobble spec.
- Sleep timer fade-to-stop didn't exist — A11 left a `fade_stop` TODO
  the cleanup branch resolved.

#### Known issues (carry to next release)

- CastManager UI wiring for DLNA / Sonos / Snapcast backends pending —
  the backends ship but discovery results don't surface in the cast
  dialog yet (only the section UI is in place).
- Visualizer rendering widget pending — FFT pipeline shipped but no
  paint surface yet (gated on subjective tuning).
- Internet-radio UI surfaces pending — CRUD shipped on both providers
  but no UI affordance to add / edit / play stations.

### Added — workflow & docs
- Three tracking docs: `docs/TODO.md` (P0-P4 prioritized backlog),
  `docs/manual_test_plan.md` (visual / at-keyboard tests),
  `docs/autonomous_tasks.md` (queueable unattended work).
- Competitive audit `docs/competitive_audit.md` against Supersonic,
  Feishin, Finamp, Sublime Music, Strawberry, Tauon, Symfonium.
- Research design docs for every P1/P2 parity feature:
  `docs/research/eq_dsp.md`, `smart_playlists.md`,
  `radio_and_seeded_queues.md`, `crossfade.md`, `visualizers.md`,
  `tag_editing.md`, `parity_small_items.md`.
- Architecture decision log at `docs/decisions.md`.
- This CHANGELOG.

### Added — features (shipped to working tree)
- **Offline Phase 5 UI surface**:
  - Accent-tinted "Offline" chip in the top bar with cycling
    "Connecting…" feedback on click.
  - Library, Songs, Search, and Artist page all swap to local-only
    rendering (downloads.db via `list_complete_items`) when offline
    mode is on.
  - Settings → Downloads: explicit "Offline mode" and "Automatic
    offline mode" toggles. Auto-flip from connectivity drops updates
    the checkbox via bus signal.
- **New offline helpers**: `offline.list_complete_items(kind)`,
  `offline.get_snapshot(item_id)`, `offline.child_snapshots(item_id, kind)`.
- **Artist-page offline fallback**: three-tier resolver — artist
  node → `AlbumArtists[].Id` match → `AlbumArtist` string-name match
  via id→name map built from downloaded tracks/albums. Synthesizes
  meta when no artist node exists.
- **Library / Songs / Search re-render on `offline_mode_changed`** —
  toggling the chip while a view is visible repaints from the new
  source.

### Added — tests
- 12 tests for Phase 5 connectivity state machine (threshold,
  auto-offline, reconnect lift, user-source persistence).
- 16 tests for scrobble eligibility math.
- 5 tests for QSettings rename migration.
- 13 tests for offline-search matching (Album / AlbumArtist /
  Artists, artist tile synthesis).
- 8 tests for the three-tier artist resolver.

### Fixed
- Offline search "air" missed the Air album + artist — now matches
  `Album` / `AlbumArtist` / `Artists` on songs, `AlbumArtist` /
  `AlbumArtists[].Name` on albums; synthesizes artist tiles from
  downloaded albums.
- Offline artist page returned "Couldn't load artist" when only an
  album was downloaded (no artist node) — three-tier fallback now
  handles the case.
- **Offline Albums / Songs / Search all returned empty when
  downloads.db had complete rows** — `_render_offline_items`,
  `_render_offline_songs`, and `_local_search` treated
  `list_complete_items` results as wrapper rows (`n.get("metadata")`)
  but the function returns bare metadata dicts, so the `Id` filter
  dropped every item. Three call sites fixed; the corresponding test
  stub in `test_search_offline.py` was returning the wrong shape and
  hiding the bug — also fixed.
- **Offline cover art missing on Songs / Albums / Search rows** —
  `load_image_async` short-circuited to placeholder before checking
  the in-memory raw cache or the on-disk raw cache. Offline gate now
  sits after every local cache tier, so a cover loaded at any size
  during a prior online session can derive to any other surface.
- **Offline Artists view always empty when only albums were
  downloaded** — `list_complete_items("artist")` only returns nodes
  with `kind = artist`, and downloading an album never creates one.
  Library grid now synthesizes artist entries from every downloaded
  album's `AlbumArtists`, same trick the offline search uses.

### Known issues (carry to next release)
- `set_offline_mode("yes")` doesn't coerce — in-memory flag can hold
  a non-bool. One-liner fix tracked at A6 in
  `docs/autonomous_tasks.md`.
- Phase 5 disconnect test pass deferred (in `manual_test_plan.md`
  §1).

---

## Historical highlights pre-Unreleased

Captured retrospectively for context. Pre-CHANGELOG, so commit log
is canonical.

- **2026-05-15** Lowercase rename `JellyToast → jellytoast`. QSettings
  + keyring + dirs all migrate via `_migrate_legacy_org_name` on
  first launch. Legacy `~/.config/JellyToast/` left as backup.
- **2026-05-15** Scrobble subsystem (`modules/scrobble/`): ListenBrainz
  client (functional), Last.fm client (built but blocked on API key),
  JSON-backed offline queue, Navidrome auto-detection. Reconnect-
  flush hooked into Phase 5 connectivity.
- **2026-05-15** Offline Phase 5 connectivity back-end (state machine,
  bus signals, provider hooks, scrobble flush trigger).
- **2026-05-14** Now-playing surfaces polish: cover lock, hover heart,
  per-member group cast volume.
- **2026-05-11** DPR-aware cover cache: `_COVER_SOURCE_PX` fixed-size
  fetches stopped cache fragmentation across launches.
- **2026-05-10** Main window switched to KDE server-side decorations.
- **2026-05-09** Mini-player keep-above via KWin window rule.
- **2026-05-08** Native PySide6 surfaces — QWebEngineView retired.
- **2026-05-08** Dual-store credentials (keyring + AES-GCM-encrypted
  QSettings blob).
- **2026-05-08** App-wide smooth scrolling via `SmoothScrollFilter`.
- **2026-05-04 → 05-08** Native browse pivot — every clicked surface
  is native; `qt6-webengine` dependency dropped.

---

## Conventions for this file

- Each new feature merged or significant fix shipped → bullet under
  `Unreleased`.
- Group bullets by section: **Added** / **Changed** / **Fixed** /
  **Removed** / **Deprecated** / **Security** / **Known issues**.
- When cutting a release: rename `Unreleased` → `[X.Y.Z] — YYYY-MM-DD`
  and start a fresh `Unreleased` above it.

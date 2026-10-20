# Plasma Mobile — plan & checklist

Living plan for making jellytoast a good Plasma Mobile app (audit 2026-10-05,
Plasma Mobile 6.7.5 on august's desktop in mobile mode: 601×1218 logical,
scale 2). Tick boxes as work lands; keep findings here, not in memory.

## ▶ Start here (next session)

State as of 2026-10-15: phases 1–5 are **committed** on `main` (phase 5,
the phone sweep, as one commit, with CHANGELOG lines for phases 4–5). Now
sweeping the remaining screens at phone scale — see "Phase 5" → "Next".

Still open: the touch + on-screen-keyboard check on real hardware, and the
metainfo "phone-friendly" flag, which is **on hold** (august: the app needs
more work before we advertise it). `packaging/pyinstaller/jellytoast.spec` is
august's own unrelated edit; leave it out of commits.

0. **0.2.2 is published** (draft went live 2026-10-08). The mobile work
   (phases 1–5) goes in the next release.
1. **Real-device pass on august's Plasma Mobile session** (the one thing the
   headless runs can't cover): run from the checkout, then check — window fits
   601 px; compact bar; Now Playing Playing | Tracks switch; Settings dropdown;
   flick-scroll the library; tap opens / drag doesn't; long-press an album →
   menu; on-screen keyboard on login + search; docked-mode toggle (window
   controls come back, layout reflows); swipe away while playing → keeps
   playing, media widget reopens it; PowerDevil shows "jellytoast — Playing
   music" (`busctl --user call org.kde.Solid.PowerManagement
   /org/kde/Solid/PowerManagement/PolicyAgent
   org.kde.Solid.PowerManagement.PolicyAgent ListInhibitions`).
   **Done 2026-10-08 (partly):** ran on the real session (600×1150, maximized,
   compact class). Screenshots confirmed: library, compact top bar, Now
   Playing Playing | Tracks + synced lyrics, Settings maximized with the
   section dropdown. Fixed on the spot:
   - **See-through window.** The no-blur body was ~78% opaque, and a light
     app behind read straight through. On a mobile shell the body is now
     opaque (`theme.body_color_for`, `app._faux_frost_base`).
   - **Top bar overflow at 600 px** with the multi-library picker and a wide
     monospace font. Compaction is now overflow-driven
     (`_position_center_cluster` records `_compact_need`), not just < 520 px.
   - **Window controls showing** when the shell maximized the window before
     the state-change hook ran. Now re-checked on every resize too.
   - Tests no longer inherit the developer's mobile env (`tests/conftest.py`
     clears `PLASMA_PLATFORM` / `QT_QUICK_CONTROLS_MOBILE`).

   - **Docked mode changed nothing.** The shell's `convergentwindows` script
     gives windows decorations back but keeps them MAXIMIZED, so "hide the
     trio while maximized" never let ours return. Added
     `platform_compat.is_docked()` (plasmamobilerc `[General]
     convergenceModeEnabled`) plus a `QFileSystemWatcher` in `app.main`;
     verified live: docked on → min / max / close appear.

   august confirmed: background play after swipe-away ✓, drawer media
   controls ✓. **Still open:** on-screen keyboard on login and search; touch
   (his phone-streaming setup can't do reliable touch, so needs a real
   touchscreen); PowerDevil ListInhibitions while playing.
2. ~~Fix what that turns up, then commit~~ Done 2026-10-08, in the four
   commits above.
3. Phase 2/3 follow-ups below (stepped-out controls on compact Now Playing;
   rail / artist-page long-press menus), then phase 4.
4. Release: these are user-facing (`CHANGELOG.md` [Unreleased] already has
   the entries) → the next release after 0.2.2.

Changed files: `app.py`, `artist_page.py`, `horizontal_rail.py`,
`library_grid.py`, `login_view.py`, `media_controls/_mpris.py`,
`now_playing_bar.py`, `now_playing_page.py`, `platform_compat.py`,
`player_state.py`, `power/{__init__,_linux}.py`, `settings_dialog.py`,
`top_bar.py`; new `responsive.py`, `touch.py`; tests `test_core_layering`,
`test_mobile_shell`, `test_responsive_layout`, `test_touch` (+ edits to
`test_power`, `test_context_menu_wiring`); `CHANGELOG.md`, `dev/README.md`,
new `dev/headless/sitecustomize.py` (the tray shim for headless runs).

### Headless verification recipe (what worked this session)

Run the real app isolated, offscreen, driven over the test bridge:

```
H=<scratch>/apphome; mkdir -p /tmp/claude-1000/jtb
env PYTHONPATH=dev/headless HOME=$H XDG_CONFIG_HOME=$H/.config \
  XDG_DATA_HOME=$H/.local/share XDG_CACHE_HOME=$H/.cache \
  XDG_RUNTIME_DIR=/tmp/claude-1000/jtb \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent-jt-bus \
  PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring QT_QPA_PLATFORM=offscreen \
  JT_TEST_BRIDGE=1 JT_INSTANCE_KEY=jt-verify TMPDIR=/tmp/claude-1000/jtb \
  .venv/bin/python -m jellytoast
# client: TMPDIR=/tmp/claude-1000/jtb .venv/bin/python dev/jt_ctl.py eval|exec "…"
# screenshot: eval "win.grab().save('…png')"  (blur-blind, fine for layout)
```

Gotchas, each cost time once:
- **Isolate everything** — `authenticate()` writes the real config + keyring
  (see memory `live-scripts-isolate-settings`); the dead D-Bus address keeps a
  test instance off august's session bus (MPRIS, KWin).
- **Short TMPDIR** — Unix socket paths cap at 108 bytes; the scratchpad path
  is too long ("QLocalSocket … Invalid name"). The sandbox also blocks bare
  `/tmp`; `/tmp/claude-1000/<short>` works.
- **Tray shim** — offscreen has no tray, so boot blocks on the modal "No
  system tray" warning; `PYTHONPATH=dev/headless` loads
  `dev/headless/sitecustomize.py`, which reports a tray.
- **Modal popups hang the bridge** (a QMenu / Selector `exec()` blocks the
  GUI thread). Launch with `PYTHONFAULTHANDLER=1`; `kill -ABRT <pid>` dumps
  the stack into the app log.
- **Synthesized touch in the bridge is unreliable** on nested widgets (points
  landed on a Selector). Use unit-level `QTest.touchEvent` tests instead.
- The shell inherits august's mobile env (`PLASMA_PLATFORM=phone:handset`),
  so `is_mobile_shell()` is True in local runs and tests unless patched.
- Fake "maximized" for the mobile window-controls path:
  `win.isMaximized = lambda: True; win.top_bar.update_window_controls()`.

## What Plasma Mobile does to an app

- Windows forced maximized + borderless + unmovable
  (`~/.config/plasma-mobile/kwinrc`: `Placement=Maximizing`,
  `BorderlessMaximizedWindows`, `InteractiveWindowMoveEnabled=false`).
  **Docked mode** (quick setting) turns that off at runtime — so layout must
  follow window *width*, not a launch-time "am I mobile" check.
- Blur disabled (`blurEnabled=false`) → our near-opaque fallback (fine).
- `plasma-keyboard` on-screen keyboard; `TabletMode=auto`.
- MPRIS drives the action-drawer + lockscreen media widget (title / artist /
  `artUrl` / prev-play-next). Tapping it runs
  `launchOrActivateApp(DesktopEntry + ".desktop")`.
- A tray *host* is registered, but the mobile status bar draws no tray items
  (only battery / signal / wifi / bt / volume indicators).
- Detection: `startplasmamobile` exports `PLASMA_PLATFORM=phone:handset` and
  `QT_QUICK_CONTROLS_MOBILE=true`.
- **KDE bug: blur goes missing on desktop logins.** `plasma-mobile-envmanager`
  runs in the regular desktop session too. Its restore path
  (`Settings::reloadKWinConfig`) reads `blurEnabled` with a default of `false`,
  so when the key is absent it `unloadEffect("blur")`s the live KWin. Symptom:
  jellytoast shows its fallback frost on the desktop. Fixed on august's box by
  pinning `kwriteconfig6 --file kwinrc --group Plugins --key blurEnabled true`
  (2026-10-05). A bug report was drafted for KDE; august files it. Unfixed on
  plasma-mobile master as of 2026-10-05.

## Phase 1 — quick fixes (also help desktop)

- [x] **MPRIS DesktopEntry matches the installed .desktop.** Was hard-coded
  `"jellytoast"`; every packaged channel installs
  `io.github.wolfgangwarehaus.jellytoast.desktop` (only `dev/create_desktop_entry.sh`
  makes `jellytoast.desktop`) → the mobile media widget couldn't reopen the app.
- [x] **Keep-awake blocks sleep, not the screen.** `org.freedesktop.ScreenSaver.Inhibit`
  on KDE is a `ChangeScreenSettings` inhibition (no dim / DPMS / lock) and does
  NOT block suspend — the opposite of the inhibitor's intent. Use
  `org.freedesktop.PowerManagement.Inhibit` (KDE → `InterruptSession`), falling
  back to the portal `Inhibit` with the suspend flag (GNOME, flatpak).
  Done in `power/_linux.py`; verified live (PowerDevil lists "jellytoast —
  Playing music", cleared on release). Gotchas found: PowerDevil activates
  an inhibition only after ~5 s; PySide6 QtDBus can't marshal uint32, so the
  old `UnInhibit(cookie)` never released anything — holds now live on a
  private connection and are released by disconnecting it.
- [x] **No invisible "hidden to tray" on mobile.** Close on a mobile shell: keep
  playing in the background if music is playing (reachable from the media
  widget / launcher), otherwise quit. Hide the tray checkbox there.

## Phase 2 — foundation: fit the screen

- [x] `is_mobile_shell()` (behaviour) + `jellytoast/responsive.py` width classes
  (layout): `compact` below 720 px of WINDOW width, broadcast live as
  `PlayerBus.width_class_changed` — so docked mode / rotation just reflow.
- [x] Window minimum 360 × 420 (was 720 × 560, font-scaled as before).
- [x] Transport bar: tiers at 660 (mini-player button out), 560 (compact:
  76 px bar + cover, sleep timer + streaming readout out), 430 (shuffle /
  repeat out). Fixed the breakpoint **ratchet**: fixed-width clusters made
  the bar's minimum ≥ its own width (window stuck at 740), and the bar now
  reports its phone floor as its minimum so jumps (rotation, docked→phone)
  aren't refused.
- [x] Now Playing: one pane at a time behind a Playing | Tracks switch at
  compact width.
- [x] Top bar below 520 px: forward / home / section title / grid-list toggle
  step out; on a mobile shell the window controls hide while maximized.
- [x] Login card fluid 280–420 px (its fixed 420 held the whole window at 420
  even as a hidden stack page).
- [x] Settings at compact width: section dropdown instead of the sidebar;
  resizable (a mobile shell can maximize it).
- [x] Follow-up: the controls that step out of the compact bar (shuffle,
  repeat, sleep timer) now have a row on the compact Now Playing page. The
  bar emits `stepped_out_changed`; the page mirrors exactly the hidden
  ones and drives the bar's own buttons (`attach_transport_bar`).
- [ ] Follow-up: verify on the real Plasma Mobile session (OSK, docked mode
  toggle, rotation) — so far verified headless at 360 / 601 / 1100.

## Phase 3 — touch

All in `jellytoast/touch.py` (one QApplication event filter, installed in
`app.main`; it only reacts to TouchScreen input — mouse is untouched).

- [x] Kinetic flick scrolling: every `QAbstractScrollArea` viewport grabs
  `QScroller`'s *touch* gesture on first show (no overshoot).
- [x] **Tap on release** inside scrolling views: Qt delivers a touch's press
  the instant the finger lands, and the grids act on press — so touching a
  tile to scroll opened it. Presses are held back and replayed at release
  only for a real tap (finger moved < 18 px, didn't stop a flick).
- [x] Long-press (500 ms) → the same `QContextMenuEvent` a right-click sends,
  so all existing menus work; counts only if a menu accepted it (else it's a
  tap); a pressed button gets a cancelling release, never sticks down.
- [x] Hover-only affordances: tile corner buttons / play disc aren't hit-live
  or painted after touch input; the tile menu gained **Play** + **Favorite**
  (desktop right-click gets them too).
- [x] Rails + artist page get the same tile menu as the grid. It's now
  `library_grid.show_tile_context_menu` (with `toggle_tile_favorite`),
  keeping the `_LibraryListView` translation context.
- [ ] Real-hardware pass. Verified with QTest touch events (unit tests drive
  the real grid view) + a headless app run (drag scrolled 1389 px without
  opening anything; a tap opened the album). Caveat for next time: in the
  bridge harness, synthesized touch points on deeply nested widgets landed
  on the wrong widget (a Selector popup then blocks the GUI thread) — prefer
  unit-level touch tests or real hardware.

## Phase 4 — polish

- [x] Hide the mini player on a mobile shell: bar button, `show_mini_player`
  signal, show-on-start, and the Settings WINDOW rows (no tray on mobile, so no
  tray entry to hide). Window controls: done.
- [x] MPRIS `artUrl` → local cached `file://` (no credentials on the bus; works
  offline on the lockscreen). 512px PNGs in `<cache>/mpris-art/`, newest 24
  kept; verified live via `busctl` (`tests/test_mpris_art.py`).
- [~] On-screen keyboard: no auto-focus on mobile at launch (done); the
  window/OSK interplay still needs a live check on a touch device.
- [ ] **On hold (August: not phone-friendly yet).** Metainfo: `<supports><control>touch</control>` + display_length ≥ 360
  once phases 2–3 land (currently *requires* ≥ 768 → mobile stores hide it).
- [ ] Consider aligning the Wayland app_id with the reverse-DNS desktop id
  (`app.setDesktopFileName("jellytoast")` today; KDE matches via
  `StartupWMClass`, but Flathub/GNOME prefer app_id == desktop id).

## Phase 5 — phone sweep (2026-10-08)

august, on his Plasma Mobile session: "the overall scale is still much too
small … much bigger fonts and scale and buttons", and "usable and touchable".

Done (verified live on his session via the test bridge + `win.grab()`; tests
for each):
- [x] **Whole-UI phone scale.** `app._apply_phone_scale()` sets
  `QT_SCALE_FACTOR=1.5` before QApplication on a mobile shell → his 600 px
  screen is ~400 logical px, so every compact layout engages. Override with
  `JT_PHONE_SCALE=<n>` (1 = off); an exported `QT_SCALE_FACTOR` wins.
- [x] **Touch targets.** `IconButton` lifts itself to 48 px (glyph ≥ 24) on a
  mobile shell (`icon_button.TOUCH_TARGET`); hover-only cover overlays opt
  out (`touch_target = False`). Menu rows are finger-tall
  (`ui_helpers.menu_item_vpad`).
- [x] **Transport bar touch tiers** (`now_playing_bar._TOUCH_*`): 48 px
  buttons, 56 px play, thick seek strip; sleep steps out < 720, shuffle /
  repeat < 540, cast < 420 → cover · prev/play/next · volume fits 360 px.
  Unequal clusters allowed when two equal ones can't fit (play a few px
  off-centre rather than overflow / ratchet).
- [x] **Top bar on a phone:** back · view dropdown · search · ⋮. The ⋮ menu
  (`_show_more_menu`) holds Libraries…, Shuffle all, Sort…, grid/list, Home,
  Settings. Dropdown re-centres after the flip (deferred `_fit_library_title`).
- [x] **Now Playing:** cover ~85 % of width (max 360; 240 while lyrics /
  visualizer share the pane), tabs 48 px, stepped row also carries cast,
  flat icon styling. Browsing an album: tab reads "Album"/"Playlist", opens on
  it, no queue-control row.
- [x] **Artist page** two album columns; **Songs** rows stack title over
  "artist · album" below 560 px; **Search** drops the redundant ✕;
  **Settings** wraps the long notification option, "Check now" on its own
  line, no focus box on ⓘ.
- [x] **Bug:** covers on screen could stay placeholders after the app started
  or sat in the background (nothing painted → LRU evicted them, nothing
  re-armed). `LibraryGrid` now re-runs the visible-cover pass on
  `applicationStateChanged → Active`.

Next (the rest of the sweep):
- [x] Commit phase 5 (single commit) + CHANGELOG lines for phases 4–5.
- [x] Phone scale default now **1.0** (2026-10-20): august set the shell to 3×
  (402×816 logical); our 1.5× stacked on it left the app 268 px — one
  column, mini player overlapping itself. The shell's scale is the size
  choice; `JT_PHONE_SCALE` still opts in. Was:
  Ask august if 1.5× feels right on the device (try `JT_PHONE_SCALE=1.75`);
  maybe a Settings "Interface size" option instead of env only.
- [x] **Sweep 2 (2026-10-15)**, headless at 400 px logical against the
  Navidrome demo (login via "Try a demo"; `JT_AUDIO_PCM_FILE=/dev/null` so
  nothing plays aloud — it runs faster than real time). Swept: login,
  library, Now Playing + Tracks, radio, downloads, Smart Mixes, playlists,
  genres, songs, every Settings page. Fixed:
  - Settings pages clipped on the right (no sideways scroll): EQ bands share
    the width on a phone, Save / Delete get their own row, no 46 / 140 px
    trailing pads, "(needs restart)" under Language, theme combo 220 px,
    narrower shortcut field. Every page ≤ 360 px (test pins it).
  - Downloads cache readouts wrap beside their buttons.
  - Radio / downloads empty states wrap (were clipped both sides); wording
    no longer assumes a mouse ("Use …", "Right-click (or long-press)").
    Spanish catalog updated for those 5 strings.
  - Bar cover hides on compact live Now Playing, back on leaving it
    (`_sync_np_bar_cover`). Desktop untouched — note desktop is itself
    inconsistent: the bar cover hides after a preview → live switch but
    not on a first live open (`clear_preview` emits nothing).
- [x] **Sweep 3 (2026-10-15):** the dialogs. Smart Mix editor needed
  ~710 px (form | preview side by side, 860 px minimum): on a phone the
  form stacks over a capped preview, Match / Sort split rows, rule chips
  wrap to two lines with a finger-sized ×. Tag editor: 520 px minimum
  dropped, cover above the form. Cast dialog: no longer fixed 440×480.
  Tests pin all three ≤ 360 px.
- [~] **Flick feel** (august: "not very smooth and kind of goes on too
  long"). Qt's QScroller defaults (deceleration 0.125, max 0.5 m/s) glided
  ~4 s, mostly a slow 1-px crawl, and capped every flick at one distance.
  Now `touch.FLICK_DECELERATION = 0.6`, `FLICK_MAX_VELOCITY = 1.1`: gentle
  ≈0.7 s / ½ screen, medium ≈1.5 s / 2 screens, hard ≈1.9 s / 3+ screens
  (simulated with `QScroller.handleInput`). Headless grid paint is ~2.5 ms
  a frame with steady 16 ms pacing, so jank wasn't paint there. Needs
  august's feel check on the device; nudge the two constants if needed.
- [x] **A–Z rail by touch** (august: "doesn't seem to be working great").
  It was 26 tiny buttons: a tap only counted with press + release on one
  ~20 px letter, no slide-to-scrub, and a jump during a flick got dragged
  away by the still-gliding scroller. Now the strip takes touch itself
  (`WA_AcceptTouchEvents`; letters don't, so touch lands on the strip):
  touch / slide jumps live to the letter under the finger (clamped to A/Z
  past the ends), a 64 px accent bubble shows it beside the finger, the
  strip is 30 px wide on a phone, and a jump stops any running flick.
  Mouse unchanged. Tests drive touch through the window handle (the
  widget overload of `QTest.touchEvent` didn't reach the strip).
- [x] **Touch polish pass (2026-10-15).** Audit + fixes, all in tests and
  checked end to end headless with real data:
  - Stuck hover after a tap (Qt parks its emulated pointer where the finger
    lifted): `touch.clear_hover` runs after every touch release; tooltips
    never fire for touch (`ToolTipFilter` skips while
    `last_input_was_touch()`); `_last_input_touch` now flips on TouchBegin,
    before the emulated enter reaches widgets.
  - Button focus rings with no keyboard: a tapped button gives its focus
    back after release; focus Qt hands to a button when a page hides
    (Other / ActiveWindow reason) is dropped while input is touch. Not
    mid-press — losing focus cancels the click.
  - Volume: hover opened the slider and a click muted, so a tap muted and
    the slider was unreachable. On touch a tap toggles the slider; it closes
    on the next touch outside it (TouchBegin filter); the hover-out timer
    ignores touch (Wayland's pointer position isn't the finger's — it would
    have closed the slider ~180 ms after opening). Mouse unchanged (click
    mutes). No touch mute now — the phone's own volume keys cover it.
  - Swipes: `touch.on_swipe(widget, handler, directions, when=)`. A drag
    that could scroll the view under the finger, or move a slider, stays a
    scroll. Bar: swipe up → Now Playing. Compact Now Playing: left / right
    switch Playing ↔ Tracks, down on Playing closes it.
  - Touch filter fix: a press a label ignores bubbles up its parents and the
    filter recorded each, ending on the top-level window — swipe handlers
    and long-press menus now resolve from the innermost widget.
  - Checked and fine: a vertical drag starting on a horizontal rail scrolls
    the page; the seek bar jumps / drags under the finger.
- [x] **Phone mini player** (august picked option A of three mockups). On a
  mobile shell below the touch-compact width the bar is a mini player:
  3 px progress line, 52 px cover · title / artist · play · next, 68 px
  tall. Tap the cover / title (on release — a swipe must not also tap) or
  swipe up for Now Playing. While the LIVE page is up the bar shows the
  full transport (seek, previous, volume) instead; a preview keeps the
  mini player (it shows what's playing). Nav drives it via
  `np_bar.set_now_playing_open` from `_sync_np_bar_cover` (+ on
  preview_changed, and off when leaving the page). Both views live in a
  plain box with the inactive one hidden — a QStackedLayout sizes to its
  largest page, and a hidden layout keeps stale sizes; either one held the
  phone window at 460–516 px (the ratchet). Desktop and wide phones keep
  the full bar.
- [x] **Balance pass (2026-10-20, from august's phone screenshot):**
  - View dropdown now truly centred: the side clusters shrink without the
    bar resizing (window controls hide on maximize, buttons step into ⋮),
    which left it at a stale x. `JtTopBar` re-centres on their Resize/Move.
  - Two album columns centred on the screen: on a mobile shell the grid puts
    the A–Z rail's width into its LEFT margin too and hides its own scroll
    bar while the rail shows (`_LibraryListView.set_side_rail`).
  - Quiet A–Z rail: at rest a slim track + accent dot at the current letter,
    faint A / Z at the ends; letters come back while touched (or hovered,
    docked) and linger 0.7 s. Same 30 px touch strip, scrubbing unchanged.
  - Mini player 1.5× taller (68 → 102 px): 64 px play / next, 72 px cover,
    title / artist one type tier up; still fits 360 px.
  - august to feel-check all four on the device.
- [x] **Phone Now Playing redesign (2026-10-20, Arpeggi as reference, not a
  copy):** `jellytoast/np_phone.py` (`_PhonePlayerMixin`). Mobile shell +
  compact + live only (previews / desktop untouched). One surface in the
  middle — big cover (title / artist / heart · ⋮ under it), or lyrics /
  visualizer / queue under a slim header (thumb · title · heart · ⋮).
  Fixed below: full-width seek + elapsed / format / remaining, shuffle ·
  prev · play · next · repeat, then cast · lyrics · visualizer · queue ·
  sleep (active one lit). Playing | Tracks tabs gone on the live player.
  The page's cover / lyrics / visualizer / right pane are MOVED in and back
  (`_left_cover_row`, `_lyrics_home_idx`, `_left_live_row`, `_outer`). Nav
  hides the bar while it's up (`phone_player_changed`). New icons: lyrics,
  visualizer. Rail: letters ↔ line cross-fade, scroll pill.
  Not yet seen: real synced lyrics in it (demo has none).
- [ ] Ideas not done: tap feedback on tiles / rows (a deferred tap shows no
  pressed state while the finger is down); long-press volume → mute if
  august wants touch mute back; favorite from the mini player (the heart
  lives on the full bar's cover overlay / Now Playing).
- [ ] Still not swept: context menus (popups block the bridge — render them
  via unit tests).
- [x] Nav: Radio / Downloads / Smart Mixes never pushed history (Now Playing
  → Radio → Back skipped Now Playing). They do now, and the View dropdown
  follows back / forward and in-app links (`_tab_for_surface`, also before
  Now Playing was ever opened).
- [x] Tracks tab gutter 58 → 38 px when compact (`_TrackDelegate.set_compact`).
  Push buttons (~30 px) left alone: at 1.5× that's ~45 dp on the phone,
  close to the 48 dp guideline; a global min-height risks the compact bars.
- [x] Translation drift fixed: the shared tile menu spells out
  `QCoreApplication.translate("_LibraryListView", …)` so lupdate keeps its
  context, and all 29 new / unfinished Spanish strings (phases 2–5 +
  Quick Connect) are translated. Catalog 774/774.
- [ ] Songs view showed "No songs yet" once on the demo (slow server
  timeout); Refresh fixed it. Fine, but the empty copy blames the library.
- [x] Smart Mixes empty state says "Use", not "Click". Settings button text
  sizes vary ("Change server URL…" vs "Check now") — still open.
- [x] Top bar view dropdown follows the surface (see Nav above). Search
  and the login screen keep whatever it read before (no tab of their own).
- [ ] The ⋮ menu was only verified in tests (popups don't show in
  `win.grab()`); august to eyeball it.
- [ ] Cover LRU is 64 MiB ≈ 55 covers at the phone scale (DPR 3); fine with
  the activation re-arm, but consider a DPR-aware budget.
- [ ] Order-dependent flaky tests (test_live_font, test_tile_corner_hover,
  one full-suite hang seen once) — flagged as a separate task. Tip: run
  single test files with `QT_QPA_PLATFORM=offscreen` (the sandbox has no
  display; the full suite sets it via some module).

## Toward iOS / Android (scaffolding, not apps yet)

Reality check on the stack: **PySide6 has no iOS target** (and Python apps are
an App Store uphill), and **Android** support (`pyside6-android-deploy`) is
real but young — Widgets + libmpv on Android would be a research spike. So a
phone app is most likely either a native/QML front end over a shared core, or
an Android build of this codebase. Either way, these keep that door open
cheaply, and most of them pay off for Plasma Mobile right now:

- [x] **UI-free core, pinned.** providers / Jellyfin API / scrobble / smart
  rules load only QtCore + QtNetwork — `tests/test_core_layering.py` keeps it
  that way. That core is the reference implementation (or the literal code,
  on Android) for any other front end.
- [x] **Platform facades, not `sys.platform` checks.** `media_controls/`,
  `power/`, `notifications`, `autostart`, `credentials` already pick a backend
  per platform; iOS/Android become `_ios.py` / `_android.py` (MPNowPlayingInfo /
  MediaSession, background audio, Keychain / Keystore). Keep new platform work
  in that shape.
- [x] **Capability helpers over OS checks** — `is_mobile_shell()` and the
  `responsive` width class; touch-primary lands with phase 3.
- [x] **Width classes + touch** (phases 2–3).
- [ ] **Design tokens as data** (export `design_tokens` / theme to JSON) so a
  non-Python front end can share the look.
- [ ] **Golden server fixtures**: recorded Jellyfin / Navidrome JSON the
  provider tests run against, reusable as a contract by any port.
- [ ] **Pipeline**: the App Store Connect automation (`dev/mas_submit.py`, JWT
  API) is the same API iOS uses; keep store steps as per-channel jobs in
  `release.yml` so mobile channels slot in. Bundle id family is already
  `io.github.wolfgangwarehaus.jellytoast`.
- [ ] Decide the mobile approach with a spike (PySide6-on-Android build of the
  current app vs. QML front end over the core) before investing further.

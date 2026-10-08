# Plasma Mobile — plan & checklist

Living plan for making jellytoast a good Plasma Mobile app (audit 2026-10-05,
Plasma Mobile 6.7.5 on august's desktop in mobile mode: 601×1218 logical,
scale 2). Tick boxes as work lands; keep findings here, not in memory.

## ▶ Start here (next session)

State as of 2026-10-08: phases 1–3 are **committed** on `main`:
`72b1452` (quick fixes), `ac82add` (phone-width layouts, including the
real-device fixes), `db16714` (touch) and the docs/changelog commit after
them. The real-device pass is mostly done (see step 1).
(`packaging/pyinstaller/jellytoast.spec` is august's own unrelated edit;
leave it out of commits.)

0. **First, finish the 0.2.2 release.** The `v0.2.2` tag is on `2706d5b`;
   release run 37394602410 built everything but Apple refused to notarize
   ("required agreement is missing or has expired"). august accepts the
   updated Apple developer agreement, then runs `gh run rerun 37394602410
   --failed`, then publish (see memory `release-status`). The mobile
   commits are after `2706d5b`, so they're not in 0.2.2.
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

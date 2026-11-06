# Qt Quick phone front end — plan

**Why:** the Widgets UI on Plasma Mobile works but doesn't feel native. Qt
Widgets paints on the CPU and its kinetic scrolling (QScroller) works in
physical millimetres derived from the screen's DPI (wrong when the "phone" is
a desktop monitor streamed to a handset). Qt Quick renders on the GPU with a
threaded render loop, and Flickable / ListView / GridView physics are in
pixels — the toolkit Plasma Mobile and Android/iOS-style apps are built on.

**Decision (august, 2026-10-20):** plain Qt Quick + Controls with our own
components and theme — not Kirigami. Works in every install type (pip/venv,
AUR, flatpak, AppImage) and is the same code a future Android/iOS front end
could run.

## Architecture

- **One app, two front ends.** Desktop keeps the Widgets UI. A phone shell
  (`touch.phone_ui()`) gets the Qt Quick UI — the default on Plasma Mobile since
  M4 (`JT_UI=quick|widgets` forces either). Same process model,
  settings, sign-in and cache as the Widgets app.
- **Shared core, unchanged:** providers / Jellyfin API, `QueueManager`,
  `MpvController`, `PlayerBus`, settings, offline, cast, media controls,
  scrobbling. `tests/test_core_layering.py` keeps them free of QtWidgets.
- **`jellytoast/quick/`** — the Python bridge QML talks to: list models
  (`QAbstractListModel`), a player controller (now-playing properties +
  transport slots on `PlayerBus`), navigation state, theme tokens as
  properties, and cover image URLs.
- **`jellytoast/quick/qml/`** — the screens. The current phone Widgets UI is the
  reference design (Now Playing layout, mini player, A–Z rail, menus).

## Milestones

- [x] **M0 — proof** (first session): `JT_UI=quick` opens a Qt Quick window
  with the real Albums grid (covers, titles, artists, paged loading) and a
  mini player (cover, title / artist, play / pause / next) driving the real
  player. Tap an album to play it. **Exit criterion: august feels the
  scrolling on the phone and judges it worth continuing.** Also prove the
  QML modules package (PyInstaller spec, flatpak, AUR deps).
  *Built 2026-10-20; august: "definitely could be an improvement in native
  feel and snappiness" → continue.* Run from a
  checkout: `JT_UI=quick python -m jellytoast` (sign in with the normal
  app first). Packaging: wheel ships `quick/qml/*.qml`; AUR `pyside6`
  already depends on `qt6-declarative`; flatpak's KDE runtime has QtQuick.
  **Packaging (2026-10-21):** the Linux PyInstaller bundle (AppImage /
  deb) now keeps QtQuick + only the QML modules the UI imports (QtQml,
  QtQuick, Controls Basic + impl, Templates, Layouts, Effects, Window) —
  the full QML tree would have added ~290 MB; pruned, the bundle measured
  289 MB locally. Windows / macOS still prune it. release.yml boots the
  frozen bundle with JT_UI=quick and fails on a QML load error.
- [x] **M1 — shell:** StackView navigation + back, top bar (section dropdown,
  search, ⋮), mini player ↔ Now Playing (cover, seek, transport, surface
  toggles), album page (header + tracks + play).
  *Done 2026-10-20:* StackView + back (button / Esc / Back key), album page
  (cover, Play / Shuffle, tracks, now-playing row), mini player (tap or
  swipe up) → Now Playing sheet (big cover, heart, seek, shuffle / prev /
  play / next / repeat, ⋮ → Go to album, drag down to close), app icon set
  via `image://icon/<name>/<rrggbb>`, rounded covers (square on the
  software renderer). *Moved to M2* with the sections they need: section
  dropdown, search, queue / lyrics toggles, format line.
  Offscreen screenshots need the GPU path:
  `QSG_RHI_BACKEND=opengl QT_QUICK_BACKEND=rhi` at scale 1 (2× garbles
  the readback).
- [x] **M2 — library:** artists, playlists, songs, genres, search, queue,
  lyrics, A–Z scrubber, swipe gestures, long-press menus (incl. Add to
  playlist).
  *Done 2026-10-21:* section dropdown (Albums / Artists / Playlists / Songs /
  Genres) on one `LibraryModel`; artist page (their albums, Play / Shuffle);
  playlist + album track pages; songs list; genre → albums; search as you
  type (artists / albums / songs); Now Playing lyrics (synced follow, tap a
  line to seek) and queue (tap to jump, swipe to remove) in place of the
  cover; A–Z rail (pages in more of the library to reach a letter);
  swipe a track left → queue; long-press → Play / Shuffle / Play next / Add
  to queue; edge-swipe back; toasts. *Left for M2.5:* Add to playlist
  (held with #238), Go to artist, favourites list, smart mixes / radio,
  remembering the last section.
- [x] **M3 — the rest:** sign-in, phone Settings, downloads / offline, cast
  picker, radio, smart mixes, theming parity.
  *Started 2026-10-21:* sign in (Jellyfin / Navidrome, demo server,
  readable errors) and sign out; Settings page (account, streaming /
  download quality, normalization, media controls, version); window-free
  services (offline / connectivity, keep-awake, now-playing notifications,
  scrobbling) + quit flushes; QML strings in the translation catalogs; last
  section remembered; Favorites section + Liked songs, Start radio,
  favourite toggles; cast picker (Play on…, the Widgets hand-off code) +
  per-protocol casting switches; Downloads section (progress, pause,
  remove, storage), Download in menus / on album pages, offline mode +
  Wi-Fi-only switches; sleep timer. **Opt-in without env vars:** desktop
  Settings → Display → "New phone interface (beta)" (shown on a phone
  shell; read at launch, undocked only), and "Use the classic interface"
  in the Quick Settings to go back. `JT_UI=quick|widgets` still forces.
  Offline browsing: in offline mode (chosen, or automatic when the server
  is unreachable) every section, album / playlist page and search reads
  the downloads, with an "Offline — showing your downloads" banner.
  Covers go through the app's image cache (`image://cover/<id>/<px>`), so
  they persist across launches and show offline.
  Theme picker (family + Dark / Light / Follow the system, live).
  Home section (Shuffle my library + Recently added / played, Most
  played, Favorites, random rails); Jellyfin Quick Connect sign-in.
  Covers: `Covers.request` + a synchronous pixmap provider — never Python
  on the image-reader thread (it deadlocked against the GIL).
- [x] **M4 — switch:** default on Plasma Mobile (undocked) —
  `settings.phone_ui_quick` defaults on; off via the Quick Settings "Use
  the classic interface" or the desktop Settings → Display "Phone
  interface". Metainfo: `<supports><control>touch</control>` and
  `display_length ≥ 360` (was ≥ 768, which hid it from phones). Parity
  gaps closed first: add to playlist + playlist editing, queue drag
  reorder, internet radio, EQ / crossfade / scrobbling / lyrics size.
- [ ] Remove the Widgets phone layouts — later, once the Quick UI has had
  a release or two in the wild.
- [ ] **Phosh** (prep for the OnePlus 6T; verify on a real device):
  - [x] `platform_compat.is_mobile_shell()`: also true on Phosh
    (`is_phosh()`: its session's `DesktopNames=Phosh;GNOME;` →
    `XDG_CURRENT_DESKTOP=Phosh:GNOME`), so it gets the Quick UI by default.
  - [x] `is_docked()` for Phosh: its docked toggle sets phoc's
    `sm.puri.phoc auto-maximize` to false (and restores the window buttons /
    turns the OSK off). Read with `gsettings get`, re-run only when
    `~/.config/dconf/user` changes; the app's docked watcher follows that
    file on Phosh. No phoc schema / flatpak sandbox → undocked (same as
    Plasma's plasmamobilerc in the flatpak).
  - [ ] Squeekboard pops up for the Quick UI's text fields — search,
    sign-in, new playlist name. Qt ≥ 6.7 speaks text-input-v3 (we need
    6.11), so it should; check `QT_IM_MODULE` isn't forcing something else.
  - [ ] Media controls on the Phosh lock screen (MPRIS — needs the
    DesktopEntry / icon to resolve), notifications.
  - [x] Close-to-background in the Quick UI (any phone shell): closing the
    window while music plays hides it and keeps playing; paused / stopped
    still quits. Launching again or the media controls' "open" (MPRIS Raise)
    bring it back — the Quick UI had wired neither. Only spontaneous closes
    are intercepted, so MPRIS Quit / session end still quit
    (`quick._CloseToBackground`, 2026-10-25).
  - [ ] Packaging: postmarketOS / Nura OS (aarch64) — PySide6 with
    QtQuick from the distro, or the Flatpak (KDE runtime, aarch64 already
    built) on Phosh.

## Rig (dev + tests)

- **Live:** `dev/quick_rig.sh start` (offscreen GPU, silent, copied
  profile on the null keyring — sign in / out there only touches the copy; `--window` for a real window, `--isolated` for signed-out,
  `--audible` / `--report` to opt into sound / server play reports), then
  `dev/qctl.py`:
  `state` (page / sheet / player / models), `tree` (on-screen items with
  names, text, geometry), `tap albumGrid|text:Play|x,y`, `long`, `drag T DX DY`,
  `swipe up [T]`, `edge`, `key esc`, `type`, `wait T`, `idle`, `shot [path]`,
  `fake-np` (fill Now Playing without playing), `js EXPR` (QML scope:
  ids, context properties), `py EXPR` (rig, player, albums, bus, qm, mpv…).
- **Tests:** the `quick` fixture (tests/quick_support.py) loads the real
  QML over a fake 150-album library; drive it with the same `Rig` API.
  Give interactive items an `objectName` so tests and qctl can find them.
- Rig waits out StackView / popup transitions and one rendered frame
  before input (StackView drops input while busy; layouts place new items
  in the pre-frame polish). Use `wait_still(T)` before computing raw
  coordinates on something that animates in. Never bind a Repeater model
  to an expression with other dependencies — it rebuilds every row. Hold one window wrapper (PySide invalidates the
  contentItem wrapper when its window wrapper is collected). Fixture
  teardown really deletes the window (a dead window can keep the mouse
  grab).

## Performance (phone)

Measured 2026-11-06 on the 6T (OnePlus 6T, Plasma Mobile, Python 3.14)
with `dev/phone.sh restart --timing` / the test bridge, and the rig on the
desktop. The phone runs pure-Python imports ~10× slower than this desktop.

- **Start: 5.3 s → 1.8 s to the first frame** (launch → `quick: first
  frame rendered`, wall clock from the unit's start; 1.74 s warm). Before,
  importing `jellytoast.app` alone took 2.1–2.9 s on the phone, ~1.3 s of it
  code the phone never shows (the desktop window, mpv, D-Bus, cast
  discovery). Now `jellytoast.launch` sends a phone shell to `quick.main()`,
  which never imports `app`; both entries share the pre-Qt setup in
  `boot_env.py`. The core (mpv / MPRIS / cast) starts after the first frame
  (`_FirstFrameRelay`); the library's first page is requested before it.
  Phone split now: ~0.45 s to Python + Qt, 0.27 s bridge objects, 0.57–0.9 s
  QML load (the QML disk cache in `~/.cache/jellytoast/qmlcache` is what
  makes the warm number), 0.25 s to the frame, then 0.35 s core.
- **Memory after a 12-fling browse: 794 MB → 518 MB resident.** 338 MB of
  the old figure was memory the app had freed but glibc kept (`mallinfo2`
  `fordblks`; `malloc_trim` gave 244 MB straight back): image buffers are
  ~1 MB, past glibc's dynamic mmap threshold, so they lived on the main
  heap. `boot_env.tune_malloc` fixes the mmap threshold at 256 KB and trims
  promptly. The rest was the shared pixmap cache holding 256 phone-sized
  tiles: both cover caches are byte-bounded now (quick 96 MB, ui_helpers
  128 MB — tight enough caps re-decode covers when scrolling back).
- **Idle.** Nothing renders while the app sits still (0 frames / 5 s on the
  grid, with a sheet open, on Now Playing). The 200 ms "wake Python for
  signals" timer is gone (`boot_env.install_signal_handlers`: a wake-up
  fd + QSocketNotifier), both UIs.
- **Playing.** Position ticks come ~11/s; each repainted the window because
  the progress bars moved a fraction of a pixel. The mini player's bar and
  the seek bar round to whole pixels: a tick repaints only when something
  visibly moves (a 4-minute song: every ~0.6 s).
- **Scrolling.** Covers decode on the pool; a six-fling scroll over 500
  albums showed no Python stall over 25 ms on the GUI thread. The GPU is
  not the limit (clock ~260 of 710 MHz while flinging, frost on or off).
  **Frosted glass doubles the app's CPU while scrolling** (7.6 s vs 3.7 s
  per 16 s of flinging): each `FrostedPane` is a live `ShaderEffectSource`
  of the page, so the top bar and the mini player each re-render the page
  every frame. Not changed yet — two candidates, both need an A/B on the
  device with the window exposed and the covers already cached (otherwise
  the decode pool dominates the reading): (a) one shared capture of the
  page per frame that every pane crops from; (b) a smaller blur
  (`blurMax` 48 → 24, multiplier 1.0) if the passes rather than the capture
  are the cost. Refreshing the capture on alternate frames was tried and
  reverted: `scheduleUpdate()` itself requests a frame, which looped
  (55k frames / 5 s on Now Playing in the rig).
- Measuring: the window must be *exposed* (another app in front → 0 frames
  and nothing to measure); keep the screen on with a `systemd-inhibit`
  user unit + `kscreen-doctor --dpms on`; count frames via
  `win.afterRendering`; `/proc/<pid>/stat` utime+stime for the app's CPU;
  `/sys/class/devfreq/5000000.gpu/cur_freq` for the GPU clock.

## Notes / risks

- The stream (phone → desktop session → video back) adds latency no UI can
  remove; M0's job is to tell whether GPU rendering + Quick physics is a
  felt improvement through it.
- Packaging: QML runtime modules (QtQuick, Controls, Layouts, Shapes…) must be
  bundled — the PyInstaller spec prunes Qt payload today.
- Tests: models / controller are plain QObjects (pytest); QML loads under
  `QT_QPA_PLATFORM=offscreen` for smoke tests.

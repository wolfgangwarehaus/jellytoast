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
  (`touch.phone_ui()`) gets the Qt Quick UI — opt-in with `JT_UI=quick` while
  it's being built, default on Plasma Mobile at M4. Same process model,
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
  **Gap:** the PyInstaller spec (AppImage / deb / Windows / macOS) prunes
  QtQuick + QtQml (~17 MB) — un-prune on Linux only, at M4.
- [ ] **M1 — shell:** StackView navigation + back, top bar (section dropdown,
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
- [ ] **M2 — library:** artists, playlists, songs, genres, search, queue,
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
- [ ] **M3 — the rest:** sign-in, phone Settings, downloads / offline, cast
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
  *Still to do:* smart mixes, theme picker, Quick Connect sign-in, offline
  browsing (library from downloads when the server is unreachable).
- [ ] **M4 — switch:** default on Plasma Mobile; remove the Widgets phone
  layouts only once parity holds; then the metainfo "phone friendly" flag.

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
- Rig waits out StackView transitions before input (StackView drops
  input while busy). Hold one window wrapper (PySide invalidates the
  contentItem wrapper when its window wrapper is collected). Fixture
  teardown really deletes the window (a dead window can keep the mouse
  grab).

## Notes / risks

- The stream (phone → desktop session → video back) adds latency no UI can
  remove; M0's job is to tell whether GPU rendering + Quick physics is a
  felt improvement through it.
- Packaging: QML runtime modules (QtQuick, Controls, Layouts, Shapes…) must be
  bundled — the PyInstaller spec prunes Qt payload today.
- Tests: models / controller are plain QObjects (pytest); QML loads under
  `QT_QPA_PLATFORM=offscreen` for smoke tests.

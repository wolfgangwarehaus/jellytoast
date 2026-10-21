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

- [ ] **M0 — proof** (first session): `JT_UI=quick` opens a Qt Quick window
  with the real Albums grid (covers, titles, artists, paged loading) and a
  mini player (cover, title / artist, play / pause / next) driving the real
  player. Tap an album to play it. **Exit criterion: august feels the
  scrolling on the phone and judges it worth continuing.** Also prove the
  QML modules package (PyInstaller spec, flatpak, AUR deps).
  *Built 2026-10-20 — awaiting august's on-phone verdict.* Run from a
  checkout: `JT_UI=quick python -m jellytoast` (sign in with the normal
  app first). Packaging: wheel ships `quick/qml/*.qml`; AUR `pyside6`
  already depends on `qt6-declarative`; flatpak's KDE runtime has QtQuick.
  **Gap:** the PyInstaller spec (AppImage / deb / Windows / macOS) prunes
  QtQuick + QtQml (~17 MB) — un-prune on Linux only, at M4.
- [ ] **M1 — shell:** StackView navigation + back, top bar (section dropdown,
  search, ⋮), mini player ↔ Now Playing (cover, seek, transport, surface
  toggles), album page (header + tracks + play).
- [ ] **M2 — library:** artists, playlists, songs, genres, search, queue,
  lyrics, A–Z scrubber, swipe gestures, long-press menus (incl. Add to
  playlist).
- [ ] **M3 — the rest:** sign-in, phone Settings, downloads / offline, cast
  picker, radio, smart mixes, theming parity.
- [ ] **M4 — switch:** default on Plasma Mobile; remove the Widgets phone
  layouts only once parity holds; then the metainfo "phone friendly" flag.

## Notes / risks

- The stream (phone → desktop session → video back) adds latency no UI can
  remove; M0's job is to tell whether GPU rendering + Quick physics is a
  felt improvement through it.
- Packaging: QML runtime modules (QtQuick, Controls, Layouts, Shapes…) must be
  bundled — the PyInstaller spec prunes Qt payload today.
- Tests: models / controller are plain QObjects (pytest); QML loads under
  `QT_QPA_PLATFORM=offscreen` for smoke tests.

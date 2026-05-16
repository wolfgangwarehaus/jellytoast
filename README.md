# jellytoast

A native PySide6 desktop client for **[Jellyfin](https://jellyfin.org/)** and **[Subsonic-API](http://www.subsonic.org/pages/api.jsp) servers** (Navidrome, Airsonic, etc), with bit-perfect audio via **[mpv](https://mpv.io/)**.

jellytoast is music-focused. It targets Arch Linux / CachyOS with KDE Plasma 6 + Wayland, but should work on any modern Linux desktop with Qt 6.

```
┌─ jellytoast (frameless QMainWindow) ─────────────────────────────┐
├─ JtTopBar (back / fwd / home / library tab / sort / search) ────┤
│                                                                  │
│         Native browse surfaces (LibraryGrid, ArtistPage,         │
│         NowPlayingPage, SongsView, GenresView, SearchView,       │
│         SuggestionsView). All covers loaded via QNAM,            │
│         disk-cached per server, paginated on scroll.             │
│                                                                  │
├─ NowPlayingBar (transport · scrubber · cast · volume) ───────────┤
└──────────────────────────────────────────────────────────────────┘
```

## Features

- **Multi-backend** — Jellyfin or any Subsonic-API server (tested against Navidrome). Switch backends in Settings → Account.
- **Bit-perfect audio** via mpv — FLAC / ALAC / OPUS / DSD play untouched.
- **Gapless album playback** + **ReplayGain** (track / album / off).
- **Resume on launch** — last position restores into the now-playing bar; press play to pick up where you stopped.
- **Native browse views** — albums grid (with grid / list toggle), artist page, songs view, genres, search, suggestions. Click an artist name on a tile to jump to their page; click a year to filter the grid.
- **Pagination** — large libraries page in on scroll, configurable in Settings → Library (100 / 200 / 500 / 1000 per page or "load all at once").
- **Floating mini player** — frameless, draggable, KWin keep-above on Wayland.
- **MPRIS2** — media keys, KDE Plasma media widget, `playerctl`, waybar.
- **System tray** with full media controls + minimize-to-tray.
- **Chromecast** + **AirPlay v1** casting (mDNS discovery).
- **Encrypted credential storage** — OS keyring (KWallet / GNOME Keyring) primary, AES-GCM-encrypted file fallback so boot doesn't depend on a responsive wallet. The fallback key is derived from `/etc/machine-id` + `$USER` and the config file is `chmod 600`.

## Install on Arch Linux

```bash
bash install.sh
bash create_desktop_entry.sh   # optional: add to app launcher
```

The installer installs system packages via pacman (`pyside6`, `mpv`, `python-keyring`, `python-cryptography`, …) plus a few PyPI packages (`python-mpv`, `pychromecast`, `dbus-next`).

## Run

```bash
python3 jellytoast.py
```

Or via the launcher (sets `LC_NUMERIC=C` for libmpv and tunes the Wayland startup path):

```bash
bash run.sh
```

First launch shows the LoginView. Pick **Jellyfin** or **Subsonic**, enter server URL + username + password. Credentials persist via the dual-store described above; subsequent launches sign you in automatically.

## Repository layout

```
jellytoast.py                    Entry point: window, boot auth check, nav,
                                  routing between native surfaces.
modules/
  providers/
    base.py                      MediaProvider abstract class.
    jellyfin.py                  JellyfinProvider (delegates to JellyfinAPI).
    subsonic.py                  SubsonicProvider (token+salt+md5 auth,
                                  getAlbumList2 / getArtists / getSong / etc).
  jellyfin_api.py                Jellyfin REST client + per-instance LRU
                                  meta-cache (albums / artists / items).
  player_state.py                PlayerBus signal hub + NowPlaying dataclass.
  player_backend.py              MpvController — load / play / pause / seek,
                                  prefetch + auto-advance handoff for gapless.
  queue_manager.py               Queue: navigation, mutations, shuffle,
                                  repeat, persistence.
  settings.py                    QSettings wrapper + dual-store auth (keyring
                                  + AES-GCM-encrypted file).
  library_grid.py                LibraryGrid + LibraryTile + model/view
                                  delegates — paginated grid/list of albums /
                                  artists / playlists with article-aware sort,
                                  alphabet jump, and lazy cover loads.
  artist_page.py                 ArtistPage (artist photo + chronological
                                  album grid).
  now_playing_page.py            NowPlayingPage (track list, lyrics rail,
                                  preview mode for tile-click → details).
  now_playing_bar.py             Bottom transport bar (cover, scrubber,
                                  cast dialog, volume).
  mini_player.py                 FloatingMiniPlayer (compact + expanded).
  songs_view.py / genres_view.py / search_view.py / suggestions_view.py
                                  Other native browse surfaces.
  top_bar.py                     JtTopBar — back / fwd / home / library tab
                                  dropdown / sort menu / view toggle / search.
  sidebar.py                     Drawer (libraries, account).
  login_view.py                  Sign-in form for both providers.
  settings_dialog.py             General / Account / Playback / Library /
                                  Lyrics / Appearance / Display / About.
  tray.py                        System tray icon + menu.
  mpris.py                       D-Bus MPRIS2 service (dbus-next on asyncio).
  cast_manager.py                Chromecast (pychromecast) + AirPlay v1.
  async_io.py                    `run_async` (worker pool + GUI-thread signal
                                  dispatch) + `get_qnam` (QNAM singleton).
  disk_cache.py                  JSON cache for browse payloads, scoped by
                                  server_url + provider_kind.
  image_cache.py                 PNG-on-disk cover cache, 200MB cap, mtime-LRU.
  ui_helpers.py                  Theme constants, autofade scrollbars, KDE
                                  blur, scrubbable slider, image loader.
  design_tokens.py               TYPE_* / SPACE_* / button registry.
  icons.py                       SVG icon registry, dpr-aware.
  theme.py                       Dark / light theme palette resolution.
  kwin_rules.py                  Mini-player keep-above via KWin window rule
                                  (xdg-shell forbids Qt-side StaysOnTop).
  single_instance.py             QSharedMemory + QLocalServer single-instance
                                  guard (raises existing window on second run).
  autostart.py                   Login-time .desktop file management.
  sort_utils.py                  Article-stripping + diacritic-fold sort key.
tests/                           pytest suite — Queue, MpvController, JellyfinAPI
                                  cache, Settings, image_cache, design_tokens,
                                  access_token (encrypted dual-store).
```

Everything talks through `PlayerBus` (Qt signals). UI emits intents (e.g. `queue_play_now`); backend listens, acts, emits state (`playback_started`). Adding a new component is wiring to the bus, not directly to mpv or the queue.

## Keyboard shortcuts

System-wide media keys (Play/Pause/Next/Prev) work via MPRIS2.

| Key            | Action                       |
| -------------- | ---------------------------- |
| `Space`        | Play / Pause                 |
| `Ctrl+,`       | Open Settings                |
| `Ctrl+L`       | Focus Music library          |
| `Ctrl+F`       | Focus Search                 |
| `Ctrl+Shift+M` | Toggle mini player           |
| `Ctrl+Q`       | Quit                         |

## Mini player

- **Compact** — narrow bar: cover, title marquee, transport, click-to-seek progress.
- **Expanded** — large square cover above the same bar.

Frameless, draggable, KDE blur on Plasma. On KDE Wayland, "always on top" is delivered via a KWin window rule (xdg-shell forbids apps setting their own stacking).

## Tray

- **Left-click** → toggle mini player
- **Middle-click** → play / pause
- **Double-click** → open main window
- **Right-click** → menu

Closing the window minimizes to tray. **Quit** from the tray menu or `Ctrl+Q` to exit.

## Casting

- **Chromecast** — Default Media Receiver, direct-play for browser-supported codecs (MP3 / AAC / FLAC), HTTP audio fallback for everything else.
- **AirPlay v1** — Apple TV 3rd gen and AirPlay-compatible speakers / older smart TVs. AirPlay 2 isn't supported (requires RAOP2 / DACP — significant effort).

When casting starts, local mpv stops; on disconnect, the local stream resumes at the cast position.

## Settings

Settings (gear icon, top right):

- **General** — autostart at login, home destination, minimize-to-tray, mini player on startup, mini-player keep-above (KDE Wayland).
- **Account** — server URL, username, sign in / sign out, switch backend.
- **Playback** — ReplayGain mode.
- **Library** — page size, cover prefetch, tile fade animation.
- **Lyrics** — font size.
- **Appearance** — theme (dark / light).
- **Display** — accent color, blur opacity.

Window geometry, sort order, view mode (grid / list), shuffle / repeat all persist automatically.

## Troubleshooting

| Issue | Fix |
| --- | --- |
| `mpv` import fails | `sudo pacman -S mpv` (libmpv must be installed) |
| `pyside6` import fails | `sudo pacman -S pyside6` |
| No tray icon | Install a tray helper for your DE (eg. `plasma-systray`) |
| Chromecast not found | Open UDP 5353 (mDNS); same VLAN as the Chromecast |
| AirPlay receiver not found | Newer Apple TVs require AirPlay 2 (not supported) |
| Wayland: video shows in wrong spot | Set `QT_QPA_PLATFORM=xcb` (`run.sh` does this) |
| `[boot-auth] token_len=0 is_auth=False` on every launch | Keyring (kwalletd6) is unresponsive at boot — the encrypted file fallback should kick in. If you keep landing on the login screen, sign in once; the next launch will be auto-signed-in via the file. |

## Why mpv?

Browser-based playback stacks have to transcode FLAC / ALAC to AAC server-side. mpv plays them untouched, supports gapless and ReplayGain, has hardware video decoding, and uses much less RAM than a browser playback pipeline.

## Why MPRIS?

MPRIS2 is the integration point on Linux:

- Keyboard media keys work.
- KDE Plasma's media widget shows the current track + controls.
- GNOME Shell's media controls show jellytoast.
- `playerctl play-pause` works from a script or keybinding.
- waybar / polybar / etc. can render the current song.
- Browsers pause their media when jellytoast starts.

## Roadmap

- Native Search view (currently uses the legacy Jellyfin-Web embed).
- Native Suggestions view (or accept the JF-Web embed as canonical).
- AUR PKGBUILD + Flatpak manifest.
- Last.fm / ListenBrainz scrobbling beyond the per-track Jellyfin / Navidrome reporting we already do.

## License

GPL-2.0-or-later. See `LICENSE`.

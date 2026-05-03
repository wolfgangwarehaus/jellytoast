# JellyToast

A native Linux desktop client for [Jellyfin](https://jellyfin.org/) that wraps **Jellyfin Web** in a Qt shell and replaces the in-browser player with **bit-perfect mpv**. You get Jellyfin Web's polish for browsing, plus the goodies a native app can offer: gapless audio, MPRIS2, KDE Plasma integration, system tray, floating mini player, and Chromecast/AirPlay casting.

Targets Arch Linux / CachyOS with KDE Plasma 6, but should work on any modern Linux desktop with Qt6.

## How it works

JellyToast loads your Jellyfin server's web UI inside a `QWebEngineView`, then intercepts every `/Audio|Videos/{id}/...` request before the browser plays it. The intercepted request is blocked, the item is fetched via the Jellyfin REST API, and playback is handed off to mpv natively. Jellyfin Web never knows it didn't play the file itself.

The native chrome — top nav bar, bottom transport, mini player, tray, MPRIS, casting — wraps that web view. Jellyfin Web's own header and now-playing bar are hidden via injected CSS.

```
┌─ JellyToast titlebar (frameless QMainWindow) ─┐
├─ JtTopBar (back/fwd/home/drawer/view/cast/…) ─┤
│                                               │
│         QWebEngineView → Jellyfin Web         │
│        (.skinHeader & .nowPlayingBar          │
│         hidden via injected CSS)              │
│                                               │
├─ NowPlayingBar (native transport)             ┤
└───────────────────────────────────────────────┘
```

## Features

- **Bit-perfect audio** via mpv — FLAC, ALAC, OPUS, DSD play untouched (no AAC transcode)
- **Gapless album playback** + **ReplayGain** (track or album)
- **Hardware-accelerated video** via mpv (VAAPI/VDPAU/NVDEC)
- **Native top nav bar** with library tab dropdown, search, cast, account
- **Native bottom transport** with title/artist, artwork, scrubber, shuffle/repeat
- **Floating mini player** — frameless, draggable, always-on-top, KDE-blurred
- **MPRIS2** — media keys, KDE Plasma media widget, `playerctl`, waybar
- **System tray** with full media controls
- **Chromecast** + **AirPlay v1** casting (mDNS discovery)
- **Album auto-queue** — clicking a track in Jellyfin Web queues the whole album for Next/Prev

## Install on Arch Linux

```bash
bash install.sh
bash create_desktop_entry.sh   # optional: add to app launcher
```

Or manually:

```bash
sudo pacman -S python pyside6 mpv libnotify ffmpeg
pip install --break-system-packages -r requirements.txt
```

## Run

```bash
python3 jellytoast.py
```

Or via the launcher script (sets `LC_NUMERIC=C` and `QT_QPA_PLATFORM=xcb` for Wayland):

```bash
bash run.sh
```

First launch prompts for your Jellyfin server URL, username, and password. Credentials are saved to `~/.config/JellyToast/JellyToast.conf`.

## Repository layout

```
jellytoast.py                    — Entry point: locale re-exec, window, WebEngine
modules/
  player_state.py                — PlayerBus signal hub + NowPlaying dataclass
  player_backend.py              — mpv controller
  queue_manager.py               — Queue mutations, shuffle, repeat, history
  jellyfin_api.py                — REST client (auth, library, streams, lyrics)
  settings.py                    — QSettings + queue persistence
  top_bar.py                     — JtTopBar (native top nav + library tab dropdown)
  now_playing_bar.py             — Bottom transport bar
  mini_player.py                 — Floating mini player (compact + expanded)
  tray.py                        — System tray icon + menu
  mpris.py                       — D-Bus MPRIS2 service (dbus-next on asyncio)
  cast_manager.py                — Chromecast (pychromecast) + AirPlay v1
  icons.py                       — Shared SVG icon registry
  ui_helpers.py                  — Theme, KDE helpers, app-icon painter
```

Everything talks through `PlayerBus` (Qt signals). UI emits intents; backend listens, acts, emits state. Adding a new component means wiring it to the bus, not to mpv or the queue directly.

## Keyboard shortcuts

System-wide media keys (Play/Pause/Next/Prev) work via MPRIS2.

| Key      | Action       |
| -------- | ------------ |
| `Space`  | Play / Pause |
| `Ctrl+Q` | Quit         |

## Mini player

Two modes via the toggle button:

- **Compact** — thin bar with artwork, title, transport, progress
- **Expanded** — large square artwork with full transport

Both are frameless, draggable, always-on-top, and use KDE blur on Plasma.

## Tray behavior

- **Left-click** → toggle mini player
- **Middle-click** → play/pause
- **Double-click** → open main window
- **Right-click** → menu (play/pause, next/prev, stop, mini player, open, quit)

Closing the main window hides it to the tray. The app keeps running for the tray and mini player. Use **Quit** in the tray menu or `Ctrl+Q` to exit.

## Casting

Click the cast button in the top bar while something is playing.

- **Chromecast** — uses the Default Media Receiver (MP3/AAC/FLAC, MP4/HLS)
- **AirPlay v1** — Apple TV 3rd gen and AirPlay-compatible speakers/older smart TVs

When casting starts, local mpv playback stops automatically.

## Why mpv?

The previous iteration used WebEngine for playback — that forced FLAC/ALAC to transcode to AAC at the server. mpv plays everything natively, supports gapless and ReplayGain, has hardware video decoding, and uses far less RAM than a browser playback stack. JellyToast still uses WebEngine, but only for browsing UI; the audio/video stream itself is intercepted and handed to mpv.

## Why MPRIS?

MPRIS2 is the integration point on Linux. With it:

- Keyboard media keys work
- KDE Plasma's media widget shows the current track
- GNOME Shell's media controls show JellyToast
- waybar / polybar can display the song
- `playerctl play-pause` works
- Browsers pause their media when JellyToast plays

## Troubleshooting

| Issue | Fix |
| --- | --- |
| `mpv` import fails | `sudo pacman -S mpv` (libmpv must be installed) |
| `QtWebEngineWidgets` import fails | `sudo pacman -S pyside6` (Qt6 WebEngine ships with it) |
| No tray icon | Install a tray helper for your DE |
| Chromecast not found | Check firewall (UDP 5353 mDNS), same VLAN |
| AirPlay receiver not found | Many newer Apple TVs require AirPlay 2; this client is v1 only |
| Video stutters | Verify hwdec: `mpv --hwdec=auto-safe yourfile.mp4` |
| Wayland: video shows in wrong spot | Set `QT_QPA_PLATFORM=xcb` (`run.sh` does this) |

## Roadmap

- Playlist context expansion (currently only albums auto-queue)
- TV episode → season auto-queue
- SecretService for auth tokens (currently plaintext in QSettings)
- Window state persistence (size, position, mini player position)
- AirPlay 2 (requires RAOP2/DACP — significant effort)
- Smart shuffle, sleep timer, Last.fm scrobbling
- src/ layout reorganize → Flatpak manifest → AUR PKGBUILD

## License

GPL-2.0-or-later. See `LICENSE`.

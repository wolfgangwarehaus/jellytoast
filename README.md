# 🎬 JellyPlayer

Audio-first Jellyfin desktop client for Arch Linux, with full video support.

## What it does

- **Bit-perfect audio** via mpv — FLAC, ALAC, OPUS, DSD all play untouched
- **Gapless playback** for albums (no audio drop between tracks)
- **ReplayGain** support (track or album mode)
- **Hardware-accelerated video** through mpv's VAAPI/VDPAU/NVDEC
- **MPRIS2 D-Bus integration** — media keys, KDE Plasma media widget, GNOME Shell
  controls, waybar/playerctl all work out of the box
- **Music browsing** — Artists, Albums, Songs with proper detail views and lyrics
- **Floating mini player** with **compact** and **expanded** modes
- **System tray** with full media controls
- **Persistent queue** with shuffle and repeat (off / all / one)
- **Chromecast** and **AirPlay v1** casting
- **Desktop notifications** on track change with album art
- **Persistent login** — no retyping your password
- **Continue listening / watching** on the home page

## Architecture

```
main.py                          — Entry point + lifecycle
modules/
  settings.py                    — QSettings-backed config + queue persistence
  jellyfin_api.py                — REST client (auth, library, music, lyrics, streams)
  player_state.py                — NowPlaying dataclass + central PlayerBus signals
  queue_manager.py               — Queue mutations, shuffle, repeat, history
  player_backend.py              — mpv controller + Qt video widget
  cast_manager.py                — Chromecast + AirPlay discovery & control
  mpris.py                       — D-Bus MPRIS2 service (media keys, desktop integ)
  notifications.py               — libnotify-based track notifications
  tray.py                        — System tray icon and menu
  mini_player.py                 — Floating mini player (compact / expanded)
  ui_helpers.py                  — Theme, image loader, shared widgets
  library_views.py               — Home, Music tabs, typed grids, search
  detail_views.py                — Album/Artist detail, Queue panel, Now Playing
  now_playing_bar.py             — Bottom transport bar + Cast dialog
  login_dialog.py                — First-run authentication
  main_window.py                 — Top-level window, navigation, view switching
```

The whole app communicates through a single `PlayerBus` (Qt signals). UI components
emit intents (`pause_toggled`, `next_track`, `queue_play_now`, etc.); the backend
listens, acts, then emits state updates (`playback_started`, `position_updated`).
This keeps every module decoupled — you can swap mpv for gstreamer, or
add a CLI controller, without touching the UI.

## Install on Arch Linux

```bash
bash install.sh
bash create_desktop_entry.sh   # optional: add to app launcher
```

Or manually:

```bash
sudo pacman -S python python-pyqt6 mpv libnotify ffmpeg
pip install --user -r requirements.txt
```

## Run

```bash
python3 main.py
```

First launch prompts for your Jellyfin server URL, username, and password.
These are saved to `~/.config/JellyPlayer/JellyPlayer.conf`. Subsequent launches
auto-connect.

## Keyboard shortcuts

| Key            | Action                |
| -------------- | --------------------- |
| `Space`        | Play / Pause          |
| `Ctrl+→ / ←`   | Next / Previous track |
| `→ / ←`        | Seek ±10s             |
| `Ctrl+F`       | Focus search          |
| `Ctrl+Q`       | Quit                  |

System-wide media keys (Play/Pause/Next/Prev) work via MPRIS2.

## Mini player

Two modes via the toggle button:

- **Compact** (380×120) — a thin bar with artwork, title, transport, progress.
  Perfect for keeping music controls visible while you work.
- **Expanded** (320×480) — large square artwork, full transport with
  shuffle/repeat. More like a "nano music app."

Both are frameless, draggable, always-on-top, and click-anywhere-to-drag.

## Casting

Click **📡 Cast** in the sidebar (or the cast button in the now-playing bar)
while something is playing. Click **Rescan** if your devices don't appear —
discovery uses mDNS so they should appear automatically once they advertise.

- **Chromecast** — uses the Default Media Receiver, supports MP3/AAC/FLAC and MP4/HLS video
- **AirPlay v1** — works on Apple TV (3rd gen and later) and AirPlay-compatible speakers/TVs

When casting starts, local mpv playback stops automatically.

## Tray behavior

- **Left-click** the tray icon → toggle mini player
- **Middle-click** → play/pause
- **Double-click** → open main window
- **Right-click** → menu (play/pause, next/prev, stop, mini player, open, quit)

Closing the main window hides it to the tray (configurable in `Settings.minimize_to_tray`).
The app keeps running for the tray and mini player. Use **Quit** in the tray menu
or `Ctrl+Q` to actually exit.

## Music quality

By default, audio plays at original quality (direct stream, no transcoding).
You can change this in `~/.config/JellyPlayer/JellyPlayer.conf`:

```ini
[playback]
audio_quality=original     # or 320, 192, 128 for transcoded MP3
gapless=true
replaygain=track           # or album, no
```

## Why mpv?

The previous iteration used WebEngine for playback — that forced FLAC/ALAC to
transcode to AAC at the server, killing audio quality. mpv plays everything
natively, supports gapless, supports ReplayGain, has hardware video decoding,
and uses far less RAM. It's also what every serious Linux music player uses
under the hood.

## Why MPRIS?

On Linux, MPRIS2 is the integration point. With it:

- Your keyboard's media keys work
- KDE Plasma's media widget shows the current track and lets you control it
- GNOME Shell's media controls in the top bar show JellyPlayer
- waybar / polybar can display the current song
- `playerctl play-pause` works
- Browsers know to pause their media when JellyPlayer plays (and vice versa)

Without it, you're a second-class Linux app. We implement it properly via
dbus-next on a background asyncio thread.

## Troubleshooting

| Issue                              | Fix                                                                    |
| ---------------------------------- | ---------------------------------------------------------------------- |
| `mpv` import fails                 | `sudo pacman -S mpv` (libmpv must be installed)                       |
| No tray icon                       | Install a tray helper for your DE (`gnome-shell-extension-appindicator` for GNOME) |
| No notifications                   | `sudo pacman -S libnotify`                                             |
| Chromecast not found               | Check firewall (UDP 5353 mDNS), ensure same VLAN                      |
| AirPlay receiver not found         | Many newer Apple TVs require AirPlay 2; this client is v1 only         |
| Video stutters                     | Check `hwdec` works: `mpv --hwdec=auto-safe yourfile.mp4`             |
| Wayland: video shows in wrong spot | Set `QT_QPA_PLATFORM=xcb` (Wayland embedding for mpv is finicky)      |

## Roadmap

Reasonable next steps if you want to extend:

- Series/episode browser (currently auto-plays first episode)
- Playlists (Jellyfin server-side playlists)
- Equalizer (mpv supports af=equalizer=...)
- Crossfade (mpv `audio-pitch-correction` + custom mix)
- AirPlay 2 (requires RAOP2/DACP — significant effort)
- Smart shuffle (avoid repeating recent artists)
- Sleep timer
- Last.fm scrobbling
- Lyric sync editing
- Wayland-native window embedding

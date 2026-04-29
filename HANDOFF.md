# JellyPlayer — Handoff for Claude Code

## What this project is

A native Linux desktop client for [Jellyfin](https://jellyfin.org/) media servers, built primarily for **audio playback** with full video support. Targets Arch Linux / CachyOS with KDE Plasma, but should work on any modern Linux desktop.

The user (`august`) is on **CachyOS with KDE Plasma 6**, using **fish shell**, running Wayland. Project lives at `~/Projects/jellyplayer/`.

### Key features the user cares about

- **Audio-first**: bit-perfect FLAC/ALAC/etc playback via libmpv, gapless album playback, ReplayGain
- **Floating mini player** in two modes (compact bar + expanded square panel), draggable, always-on-top
- **System tray integration** with media controls
- **MPRIS2 D-Bus** for media keys + KDE Plasma media widget integration
- **Chromecast** and **AirPlay v1** casting
- **Persistent queue** with shuffle/repeat
- Library browsing for music (Artists/Albums/Songs) and video (Movies/TV)

## Current state

The app **runs and authenticates against Jellyfin successfully**. The user reached the login screen, signed in, and was using it briefly before encountering a crash. After the crash, the process didn't fully die and prevented restart — we added single-instance handling and crash logging to address this. Status as of the last conversation: unknown whether it still crashes; the user has the logging + single-instance code but hadn't reported back.

### What we know works
- Locale fix (libmpv requires `LC_NUMERIC=C`, Qt was overriding it — solved by `os.execve` re-exec at top of `main.py`)
- Wayland workaround (forcing `QT_QPA_PLATFORM=xcb` in `run.sh` so mpv's `wid` embedding works under XWayland)
- Authentication against the user's Jellyfin server at `http://192.168.50.100:8096`
- Window/UI rendering, login dialog
- Module imports across all 17 module files

### What's unverified
- Actual playback (we never confirmed audio plays through mpv on the user's system)
- Mini player behavior in real use
- Casting (Chromecast/AirPlay)
- MPRIS2 (whether KDE Plasma media widget shows tracks)
- Queue persistence across launches
- Whether the post-crash recovery actually works
- Whether *any* video playback works under XWayland (mpv `wid` should work, but untested here)

## Architecture overview

```
main.py                          — Entry point: locale re-exec, logging, lifecycle
run.sh                           — Launcher (sets LC_NUMERIC=C, QT_QPA_PLATFORM=xcb)
modules/
  settings.py                    — QSettings + queue persistence (JSON in ~/.config/JellyPlayer)
  jellyfin_api.py                — REST client (auth, library, music, lyrics, streams, reporting)
  player_state.py                — NowPlaying dataclass + central PlayerBus signal hub
  queue_manager.py               — Queue mutations, shuffle, repeat, history
  player_backend.py              — mpv controller + Qt video widget
  cast_manager.py                — Chromecast (pychromecast) + AirPlay v1 (HTTP)
  mpris.py                       — D-Bus MPRIS2 service via dbus-next on asyncio thread
  notifications.py               — libnotify-based track notifications
  tray.py                        — System tray icon + menu (TrayController is a QObject)
  mini_player.py                 — Floating mini player (compact + expanded modes)
  ui_helpers.py                  — Theme, async image loader, formatting, common widgets
  library_views.py               — Home, Music tabs, typed grids, MediaCard, Section, GridView
  detail_views.py                — Album/Artist detail, Queue panel, Now Playing detail w/ lyrics
  now_playing_bar.py             — Bottom transport bar + Cast dialog
  login_dialog.py                — First-run authentication w/ background _LoginWorker thread
  main_window.py                 — Top-level window, sidebar nav, page stack, view switching
```

### The signal bus pattern (important)

Every component talks through `PlayerBus` (singleton, `PlayerBus.get()`). UI components emit *intents*:

```python
bus.pause_toggled.emit()
bus.next_track.emit()
bus.queue_play_now.emit(items, start_index)
bus.seek_requested.emit(ms)
```

Backend components listen, act, and emit *state updates*:

```python
bus.playback_started.emit(now_playing)
bus.position_updated.emit(ms)
bus.queue_changed.emit(queue, current_index)
```

This decouples everything. To add a new UI component, you don't wire it directly to mpv or the queue — you emit/listen on the bus. Full signal list is in `modules/player_state.py`.

### Class inheritance gotcha

PyQt6 6.5+ requires that any class connecting to signals via `@pyqtSlot`-decorated methods inherit from `QObject` (or a subclass like `QWidget`). We hit this with `TrayController` which was originally a plain Python class — connections silently failed with `Cannot connect ... to (nullptr)`. **If you add a new manager-style class with signal handlers, make it inherit from `QObject` and call `super().__init__(parent)`.**

Audited classes that currently look right: `QueueManager(QObject)`, `MpvController(QObject)`, `MprisService(QObject)`, `TrayController(QObject)`, `NotificationService(QObject)`. All Qt widgets are fine since they're already QObjects.

## Critical environment requirements

These caused real bugs and must stay in place:

### 1. `LC_NUMERIC=C` (libmpv requirement)

libmpv parses numbers as `1.5` not `1,5` and refuses to start otherwise. Qt's `QApplication.__init__` calls `setlocale(LC_ALL, "")` which undoes Python-side `setlocale()` calls. The only reliable fix is what's at the top of `main.py`:

```python
if os.environ.get("_JELLY_LOCALE_FIXED") != "1":
    if os.environ.get("LC_ALL") is not None or os.environ.get("LC_NUMERIC", "C") != "C":
        new_env = dict(os.environ)
        new_env.pop("LC_ALL", None)
        new_env["LC_NUMERIC"] = "C"
        new_env.setdefault("LANG", "C.UTF-8")
        new_env["_JELLY_LOCALE_FIXED"] = "1"
        os.execve(sys.executable, [sys.executable] + sys.argv, new_env)
```

This re-execs Python with the right env on first launch. The sentinel prevents an infinite loop. **Do not remove this.** `run.sh` also sets these vars belt-and-suspenders.

### 2. `QT_QPA_PLATFORM=xcb` on Wayland (mpv embedding requirement)

mpv's `wid`-based video embedding doesn't work on native Wayland — it segfaults. `run.sh` detects Wayland via `WAYLAND_DISPLAY` and forces XWayland:

```bash
if [ -n "$WAYLAND_DISPLAY" ] && [ -z "$QT_QPA_PLATFORM" ]; then
    export QT_QPA_PLATFORM=xcb
fi
```

Long-term proper fix would be switching `MpvVideoWidget` from `wid` to mpv's `render-api` (OpenGL context sharing). That's a few hundred lines of OpenGL setup and not yet done. If the user wants native Wayland video, this is the project to take on.

### 3. `MpvVideoWidget` lazy attachment

The video widget attaches to mpv only when shown (`showEvent`), not at construction time:

```python
def showEvent(self, event):
    super().showEvent(event)
    if not self._attached:
        QTimer.singleShot(0, self._try_attach)
```

This avoids attaching to an unrealized window which used to segfault. **Don't change this back to attaching in `__init__`.**

## How to run

The user runs:
```fish
cd ~/Projects/jellyplayer
bash run.sh
```

`run.sh` handles locale + Qt platform setup, then `exec`s `python3 main.py`. There's also a `.desktop` entry at `~/.local/share/applications/jellyplayer.desktop` that points to `run.sh`.

For a clean test run after killing zombies:
```fish
pkill -f "python3.*main.py"
bash run.sh
```

## Diagnostics

**Logs:** `~/.config/JellyPlayer/jellyplayer.log` — rotated, max 512KB × 3 files. Captures uncaught exceptions, boot environment, shutdown sequence. First lines of every run show Python version, working dir, LC_NUMERIC, display server (X11/Wayland), Qt platform — makes "won't start" issues solvable in seconds.

**Settings:** `~/.config/JellyPlayer/JellyPlayer.conf` (QSettings ini-style). Server URL, encrypted-ish auth token, user_id, device_id, volume, repeat/shuffle preferences, audio quality.

**Saved queue:** `~/.config/JellyPlayer/queue.json` (restored on next launch).

## Known issues / things to watch for

1. **The auth token in settings is stored in plaintext.** Should ideally use `kwallet` or `gnome-keyring` via SecretService API. Comment in `settings.py` notes this.

2. **AirPlay v1 only.** Newer Apple TVs (4K, 4th gen+) require AirPlay v2 which uses RAOP2/DACP — significant rewrite. Currently works on Apple TV 3rd gen and AirPlay-compatible speakers/older smart TVs.

3. **Series detail view is incomplete.** Clicking a TV series auto-plays the first episode of the first season. Needs proper season/episode browser. Stubbed in `main_window._on_item_clicked`.

4. **Music genre browsing is missing.** API has `get_genres()` but no UI surface.

5. **No playlist support.** Jellyfin server-side playlists aren't browseable.

6. **No search debouncing.** Search hits the server on Enter, not as-you-type.

7. **MPRIS `Position` property** isn't kept current — it only updates when `position_updated` signal fires (~once per second from mpv). Some clients want sub-second precision. Probably fine for KDE Plasma's media widget.

8. **The `next` track queue logic doesn't yet load follow-on tracks for video.** `bus.playback_ended` triggers `queue.next()` which works for music queues but for movies, the queue typically only has one item.

9. **Shuffle implementation is naive Python `random.shuffle`.** No "smart shuffle" that avoids repeating recent artists.

10. **Window state isn't persisted.** Size, position, mini player position all reset on launch.

## Quick reference: where to find things

| Want to change… | File |
|---|---|
| API endpoint behavior, auth | `modules/jellyfin_api.py` |
| Add a playback-related signal | `modules/player_state.py` (PlayerBus) |
| mpv settings (gapless, replaygain, hwdec) | `modules/player_backend.py:_init_mpv` |
| Tray menu items | `modules/tray.py` |
| Mini player layout/styling | `modules/mini_player.py` |
| Theme colors, button styles | `modules/ui_helpers.py:GLOBAL_STYLE` |
| Queue behavior (shuffle, repeat) | `modules/queue_manager.py` |
| Login dialog | `modules/login_dialog.py` |
| Album/artist detail views | `modules/detail_views.py` |
| Now-playing bar (transport at bottom) | `modules/now_playing_bar.py` |
| Sidebar navigation, page routing | `modules/main_window.py` |
| Cast device discovery/control | `modules/cast_manager.py` |
| MPRIS2 D-Bus interface | `modules/mpris.py` |
| Track-change notifications | `modules/notifications.py` |
| Persistent settings | `modules/settings.py` |
| Library browsing pages | `modules/library_views.py` |

## Stuff to potentially work on

In rough priority based on what would move the needle for an audio-focused user:

1. **Verify it actually plays audio end-to-end on the user's machine.** Step through a session: launch, navigate to an album, click a track, confirm sound. If broken, debug with `~/.config/JellyPlayer/jellyplayer.log`.

2. **Handle network errors gracefully.** Currently a server timeout in `_load_home` etc. just prints to console. Should show a non-modal error banner with retry.

3. **Search-as-you-type** with 300ms debounce.

4. **Series detail view** — proper season/episode picker.

5. **Persist window state** — size, position, mini player visibility & position, sidebar collapsed state.

6. **Crossfade between tracks** — mpv supports it; just needs UI toggle and a queue manager change to overlap last track's end with next track's start.

7. **Sleep timer.**

8. **Last.fm scrobbling** via the `pylast` library — fits naturally into the existing `playback_started`/`playback_ended` signals.

9. **Native Wayland video** via mpv's render-api (replaces `wid` embedding). Big project.

10. **SecretService for auth token** instead of plaintext config.

## Tooling notes

- **Python 3.10+** required (uses `tuple[str, ...]` annotations, `match` not used)
- **PyQt6 6.5+** required (strict QObject requirement on signal connections)
- **libmpv** must be installed system-wide (`pacman -S mpv`)
- **fish shell** — user is in fish, so heredocs (`<< EOF`) don't work in their terminal. Use `echo '...' > file` or have them drop into bash explicitly.
- **No virtualenv** in use — everything installed with `pip install --break-system-packages` per Arch convention
- **No tests yet.** A test suite is conspicuously absent. Adding pytest + a fake Jellyfin server fixture would be valuable.

## Communication style preferences (observed)

The user is technical, concise, and patient. They appreciate:
- Honest assessments (e.g., "this caused real bugs and must stay")
- Explicit "what to do, in order" instructions
- Knowing *why* something failed, not just the fix
- Code that's actually been tested before being shipped to them
- Bundled `.tar.gz` deliveries when many files change
- Clear callouts when something is unverified

Avoid: walls of marketing-style feature bullets, hedging, or asking permission for obvious next steps.

## If you're starting fresh

A reasonable first move: have the user run `bash run.sh`, then `tail -50 ~/.config/JellyPlayer/jellyplayer.log` to confirm current state. Then pick from the priority list above based on what the user wants to do, or ask what's broken right now if they're reporting an issue.

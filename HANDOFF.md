# JellyToast — Developer Handoff

A pointer doc for anyone (human or AI) jumping into JellyToast cold. For the user-facing pitch, see `README.md`.

## What this is

JellyToast embeds **Jellyfin Web** inside a `QWebEngineView` and intercepts media requests before they're played in-browser, handing playback to **mpv** natively. The native chrome around the web view (top bar, bottom transport, mini player, tray, MPRIS, casting) is owned by JellyToast.

This is a **pivot** from an earlier architecture (≤ 2026-04-29) that built every browse/library/detail view natively in PyQt. Those modules — `main.py`, `main_window.py`, `library_views.py`, `detail_views.py`, `login_dialog.py`, `notifications.py` — were deleted. The only entry point now is `jellytoast.py`.

## Repository layout

```
jellytoast.py                    — Entry point: locale re-exec, window, WebEngine, interceptor
run.sh                           — Launcher (sets LC_NUMERIC=C, QT_QPA_PLATFORM=xcb)
install.sh                       — Arch installer
create_desktop_entry.sh          — Generates ~/.local/share/applications/jellytoast.desktop
pyproject.toml                   — Project metadata (no [build-system] yet)
modules/
  player_state.py                — PlayerBus singleton + NowPlaying dataclass
  player_backend.py              — MpvController (mpv via python-mpv)
  queue_manager.py               — Queue mutations, shuffle, repeat, history
  jellyfin_api.py                — REST client (auth, items, streams, lyrics)
  settings.py                    — QSettings + ~/.config/JellyToast/queue.json
  top_bar.py                     — JtTopBar: native nav, library tab dropdown
  now_playing_bar.py             — Bottom transport
  mini_player.py                 — Floating mini player (compact + expanded)
  tray.py                        — System tray icon + menu
  mpris.py                       — D-Bus MPRIS2 (dbus-next on asyncio thread)
  cast_manager.py                — Chromecast + AirPlay v1
  icons.py                       — Shared SVG icon registry
  ui_helpers.py                  — Theme, KDE blur/skip-taskbar via xprop, app-icon painter
```

## How playback interception works

1. User clicks play in the embedded Jellyfin Web view.
2. `_PlaybackInterceptor` (a `QWebEngineUrlRequestInterceptor`) matches `r"/(?:Audio|Videos)/([a-f0-9]{32})/(?:universal|stream|master\.m3u8)"`, blocks the request, and emits `intent_detected`.
3. `_on_intent` fetches metadata via REST, expands audio tracks to the full album in `_expand_context` (injecting `AlbumId` from the originating item so cover art resolves), and emits `bus.queue_play_now`.
4. `QueueManager` + `MpvController` play it natively.

A 1.5s dedup window prevents double-fires when Jellyfin Web retries a blocked request.

## The signal bus pattern

Everything goes through `PlayerBus.get()`. UI components emit *intents*:

```python
bus.pause_toggled.emit()
bus.next_track.emit()
bus.queue_play_now.emit(items, start_index)
bus.seek_requested.emit(ms)
```

Backend components listen, act, and emit *state*:

```python
bus.playback_started.emit(now_playing)
bus.position_updated.emit(ms)
bus.queue_changed.emit(queue, current_index)
```

Full signal list: `modules/player_state.py`. To add a new UI component, emit/listen on the bus rather than wiring directly to mpv or the queue.

## QObject inheritance gotcha

PyQt6 6.5+ requires that any class connecting signals via `@pyqtSlot` inherit from `QObject` (or a subclass like `QWidget`) and call `super().__init__(parent)`. Connection failures are silent — symptom is `Cannot connect ... to (nullptr)`. Audited managers that already conform: `QueueManager`, `MpvController`, `MprisService`, `TrayController`, `CastManager`.

## PyQt6 QAction parent rule

When building a `QMenu`, every `QAction` must take a parent in its constructor (`QAction("text", menu)`) or be stored on `self.something`. Actions held only in local variables get garbage-collected after the function returns and silently disappear from the menu. Hit this in the tray menu — see commit history for `modules/tray.py`.

## Critical environment invariants — do not remove

### `LC_NUMERIC=C` re-exec at top of `jellytoast.py`

libmpv parses `1.5`, not `1,5`, and refuses to start otherwise. `QApplication.__init__` calls `setlocale(LC_ALL, "")` which undoes Python-side `setlocale()` calls. The only reliable fix is `os.execve` with the right env on first launch:

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

The sentinel prevents an infinite loop. `run.sh` also sets these belt-and-suspenders.

### `QT_QPA_PLATFORM=xcb` on Wayland

mpv's `wid` video embedding segfaults on native Wayland. `jellytoast.py` and `run.sh` both force XWayland when `WAYLAND_DISPLAY` is set. Long-term proper fix is mpv's `render-api` (OpenGL context sharing) — not done.

### `MpvVideoWidget` lazy attach

The video widget attaches to mpv only in `showEvent`, not `__init__`. Attaching to an unrealized window segfaults.

## WebEngine shim (`SHIM_JS` in `jellytoast.py`)

Injected at `DocumentReady` in `MainWorld`. Responsibilities:

- Hide Jellyfin Web's `.skinHeader` and `.nowPlayingBar` so JellyToast's native chrome is the only chrome.
- Kill page `padding-top` via `style.setProperty('padding-top', '0', 'important')` in a 750ms interval — Jellyfin Web sets `padding-top: 7em !important` on `.pageWithAbsoluteTabs` and plain inline assignments lose against external `!important`. Only inline-with-important wins.
- Suppress "Playback failed" dialogs via a JS `MutationObserver` (only acts on real `.dialog/.toast/[role=alertdialog]` ancestors — never walks up to the app shell).
- Expose JS bridges for the native top bar: `__jellytoast_toggle_drawer()`, `__jellytoast_switch_tab(label)`, `__jellytoast_collection_type()`.

## Color palette (Jellyfin Web aligned)

In `modules/ui_helpers.py`:

- `ACCENT = "#00a4dc"`, `ACCENT_DEEP = "#0085bd"` (Jellyfin blue)
- `BG = "#101010"`, `BG_PANEL = "#202020"`
- `TEXT = "#ffffff"`, `TEXT_DIM = "rgba(255,255,255,0.7)"`, `TEXT_FAINT = "rgba(255,255,255,0.4)"`

## Mini player translucency invariant

Qt's QSS `background: rgba(...)` does **not** reliably honor alpha on a child `QFrame`. The mini player body is painted manually in `FloatingMiniPlayer.paintEvent` as a rounded rect with `QColor(28,28,28,184)`. Inner widgets are explicitly transparent via stylesheet rules on the container. Reverting to QSS-only breaks translucency.

## KDE Plasma helpers (`modules/ui_helpers.py`)

- `enable_kde_blur(widget)` — sets `_KDE_NET_WM_BLUR_BEHIND_REGION` to `0,0,W,H` via `xprop`. KWin requires the cardinal count be a multiple of 4; passing a single `0` silently fails on modern KWin. Re-call on resize.
- `skip_taskbar_x11(widget)` — sets `_NET_WM_STATE_SKIP_TASKBAR + _SKIP_PAGER + _ABOVE` via `xprop`. Cleaner than `WA_X11NetWmWindowTypeUtility`, which some KDE themes decorate with a ghost strip above the window.

Both are X11/XWayland only; native Wayland silently no-ops.

## Audio stream URL

Use `/Audio/{id}/stream?static=true` for original-quality playback. **Never** use `/universal` — it requires capability negotiation and returns an empty body otherwise. Set in `modules/jellyfin_api.get_audio_stream_url`.

## Run

```bash
python3 jellytoast.py
# or
bash run.sh
```

Server URL, auth token, and user_id are stored in `~/.config/JellyToast/JellyToast.conf` (QSettings ini). Saved queue is `~/.config/JellyToast/queue.json`. Auth token is plaintext — should move to SecretService eventually.

## Tooling notes

- **Python 3.10+**, **PyQt6 6.5+**, **PyQt6-WebEngine 6.5+**, **libmpv** system-wide
- No virtualenv — packages installed with `pip install --break-system-packages` per Arch convention
- **fish shell** — heredocs (`<< EOF`) don't work in the user's terminal; use `echo '...' > file` or have them drop into bash explicitly
- No test suite yet

## Known open items

See `~/.claude/projects/-home-august-Projects-jellytoast/memory/known_issues.md` for the live list. Highlights:

- Auth token plaintext (should use kwallet/gnome-keyring via SecretService)
- AirPlay v1 only
- Playlist context expansion (only albums auto-queue currently)
- TV episode context (clicking an episode plays only that episode; should queue the season)
- Window state not persisted across launches

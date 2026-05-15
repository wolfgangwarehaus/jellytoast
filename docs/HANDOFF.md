# JellyToast — Developer Notes

A focused doc for anyone (human or AI) jumping into the codebase cold. For the user-facing pitch, see `README.md`.

The README's "Repository layout" section already describes every module. This doc is the lessons-and-gotchas layer — the things that bit us once and should bite no one twice.

## Architecture, in one paragraph

`jellytoast.py` is a `QMainWindow` with a `QStackedWidget` body that swaps between native browse surfaces (`LibraryGrid`, `ArtistPage`, `NowPlayingPage`, `SongsView`, `GenresView`, `SearchView`, `SuggestionsView`) plus the `LoginView`. Every user-clicked path is native PySide6; the legacy Jellyfin-Web embed has been retired. Backend is plug-replaceable via `MediaProvider` (`JellyfinProvider` or `SubsonicProvider` — selected by `Settings.provider_kind`). Everything talks through `PlayerBus` (Qt signals) — UI emits intents, backend emits state.

## The signal bus

```python
from modules.player_state import PlayerBus
bus = PlayerBus.get()

# Intents (UI → backend)
bus.queue_play_now.emit(items, start_index, ctx)
bus.pause_toggled.emit()
bus.next_track.emit()
bus.seek_requested.emit(ms)

# State (backend → UI)
bus.playback_started.emit(now_playing)
bus.position_updated.emit(ms)
bus.queue_changed.emit(queue, current_index)
```

Full signal list lives in `modules/player_state.py`. To add a new component, wire it to the bus instead of mpv / queue / api directly.

## QObject inheritance gotcha

Any class connecting `@Slot`s must inherit from `QObject` (or a subclass) and call `super().__init__(parent)`. **Connection failures are silent** — the symptom is `Cannot connect ... to (nullptr)` printed to stderr while the slot just never fires. Audited managers that already conform: `QueueManager`, `MpvController`, `MprisService`, `TrayController`, `CastManager`, every Library / Artist / NowPlaying view.

## QAction parent rule

When building a `QMenu`, every `QAction` must take a parent in its constructor (`QAction("text", menu)`) **or** be stored on `self.something`. Actions held only in local variables get garbage-collected after the function returns and silently disappear from the menu. Hit this in the tray menu and the sort menu both — see commit `e3...` for the tray fix and `top_bar.py:_show_sort_menu` for the canonical pattern.

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

## Library grid lessons

`LibraryGrid` is the single piece of furniture that does the most heavy lifting (`modules/library_grid.py`). A few things it learned the hard way:

- **Pagination overlap.** Tile placement uses `pos = len(self._tiles)` (absolute slot in the layout), not the per-batch loop index. Using the loop index meant page 2's tiles drew at `(0,0)`, `(0,1)`, … on top of page 1.
- **`QGraphicsOpacityEffect` + scroll repaint.** Leaving the effect attached after the fade-in animation completes makes every partial scroll repaint go through the effect chain, which on Wayland + QScrollArea consistently leaves tiles half-painted until the scroll stops. `LibraryTile.reveal()` detaches the effect on animation end so subsequent scrolls hit Qt's fast path.
- **Tile teardown vs in-flight callbacks.** Cover-load callbacks are bound to specific tile widgets; if the grid clears (e.g. mode switch / sort change) before a callback lands, calling `tile.set_cover()` raises `RuntimeError: C++ object already deleted`. `_clear_tiles` sets `tile._dead = True` before `deleteLater`; `set_cover` and `reveal` early-return on it.
- **Wayland boot-flash.** A widget that's parented but not yet placed in a layout briefly maps as a top-level platform surface on Wayland. `LibraryTile`s are placed in their final `(row, col)` cell at construction time so the floating-child gap never opens. The same constraint kept us from `setParent(None)` on teardown — kept the parent tied, just `hide()` first.
- **Diacritic fold in sort.** Lowercased Python sort placed `"ásgeir"` (U+00E1) after `"z"` (U+007A). `sort_utils._fold_diacritics` NFKD-normalizes and drops combining marks before lowercase so `"Ásgeir"` clusters with the A's.

## Auth: dual-store + AES-GCM

Token storage lives in `modules/settings.py`. Read order:
1. **Keyring** (`_keyring_get_token` — 5 × 100 ms retry).
2. **QSettings** under `server/token` — AES-GCM ciphertext with a `v1:` version prefix.

Encryption key is derived per-call via PBKDF2-SHA256 from `/etc/machine-id` + `$USER`. Never stored. The QSettings file is `chmod 600` on every `Settings.__init__`. See `_machine_key`, `_encrypt_token`, `_decrypt_token`. Pre-v1 plaintext tokens are detected by the missing prefix and re-encrypted forward on the first read.

The dual-store eliminates the boot-time hang where kwalletd6 was unresponsive for 8-15 seconds — the encrypted file is the resilience floor.

## Subsonic: salt + token, never plaintext over the wire

`SubsonicProvider._auth_params` builds `{u, t, s}` per request: `t = MD5(password || salt)` with a fresh 16-hex-char `s` each call. `_request` adds `f=json` for REST endpoints; `_build_url` for stream / cover-art omits it (those return raw bytes — `f=json` flips them to a JSON error envelope).

Subsonic's `getAlbumList2?type=byYear` requires both `fromYear` and `toYear`, otherwise it returns zero items. We default to `0..9999` when sort is by year and no specific year filter is set.

## Async work pattern

`modules/async_io.py`:

```python
from modules.async_io import run_async, get_qnam

run_async(
    api.get_album_tracks, album_id,
    on_result=lambda tracks: bus.tracks_loaded.emit(tracks),
    on_error=lambda e: print(f"failed: {e}"),
)
```

`run_async` runs `fn(*args, **kwargs)` on a shared `QThreadPool`, dispatches the result back to the GUI thread via a per-call `QObject` signaler that's pinned in a module-level set so PySide doesn't GC it mid-flight. Don't spawn raw `threading.Thread` — they bypass the pool's bound (4 workers) and the result-dispatch machinery.

For HTTP, use `get_qnam()` (a singleton `QNetworkAccessManager`). Image loading goes through `ui_helpers.load_image_async` which is QNAM-driven end-to-end. Don't import `requests` for new code paths.

## Settings dialog conventions (`modules/settings_dialog.py`)

Six pages: **General** (server section + checkbox stack + home destination), **Playback** (streaming quality, ReplayGain, gapless toggle, OS media-keys toggle), **Library** (page size, prefetch toggle, tile fade, cache size + clear button), **Display** (theme + lyrics size + scaling sliders — formerly three pages), **Hotkeys** (read-only keybinding list), **Scrobbling** (placeholder for Last.fm/ListenBrainz).

About is a small modal launched from the titlebar info button — not a settings page. Account was folded into the top of General as a Server section.

QSS for QComboBox: the dialog sets a dialog-wide stylesheet so every combo gets an opaque popup (`QComboBox QAbstractItemView { background: rgb(20,22,26); … }`) and a chevron-down arrow image. Without an explicit popup `background:`, Qt inherits the dialog's `WA_TranslucentBackground` and the popup ghosts over fields below. The chevron comes from `icons.icon_svg_path("chevron_down", color)` — materializes the in-memory SVG to a cache file with the requested color baked in, returns the path. Use the same helper anywhere QSS needs `image: url(...)`. Login view does the same trick.

`Settings` keys wired to UI: `audio_quality` (Playback streaming quality), `gapless` (Playback gapless toggle, gates `queue_manager._emit_prefetch`), `media_controls_enabled` (Playback OS media-keys toggle, gated at `MediaControlsService.start()` in boot — restart required), plus the existing replaygain, library_page_size, library_cover_prefetch, library_tile_fade, theme_mode, lyrics_font_size, home_destination, autostart, minimize_to_tray, show_mini_on_start, mini_player_keep_above.

## KDE Plasma helpers (`modules/ui_helpers.py`)

- `enable_kde_blur(widget)` — sets `_KDE_NET_WM_BLUR_BEHIND_REGION` to `0,0,W,H` via `xprop`. KWin requires the cardinal count be a multiple of 4; passing a single `0` silently fails on modern KWin. Re-call on resize.
- `skip_taskbar_x11(widget)` — sets `_NET_WM_STATE_SKIP_TASKBAR + _SKIP_PAGER + _ABOVE` via `xprop`. Cleaner than `WA_X11NetWmWindowTypeUtility`, which some KDE themes decorate with a ghost strip above the window.

Both are X11/XWayland only; native Wayland silently no-ops.

For "always on top" on KDE Wayland, see `modules/kwin_rules.py` — installs a `kwinrulesrc` rule scoped to the mini-player's window class. xdg-shell forbids apps setting their own stacking, so this is the only path that works.

## Audio stream URL (Jellyfin)

Use `/Audio/{id}/stream?static=true` for original-quality playback. **Never** use `/universal` — it requires capability negotiation and returns an empty body otherwise. Set in `JellyfinAPI.get_audio_stream_url`. When `Settings.audio_quality` is `"original"` we hit that endpoint; any kbps value forces `/Audio/{id}/stream.mp3?MaxStreamingBitrate=N` for server-side transcode.

Subsonic mirrors the same setting: `format=raw` for `"original"`, `format=mp3 + maxBitRate=N` otherwise. `format=raw` with a non-zero `maxBitRate` forces a transcode on older Navidrome builds, so we only set `maxBitRate` in transcode mode.

## Provider keep-alive endpoint

Each `MediaProvider` exposes `keep_alive_url() -> str` returning a cheap GET URL. Subsonic returns `/rest/ping?{auth}`; Jellyfin returns `/System/Info/Public`. Used by the 25-second QNAM heartbeat to prevent idle TCP/TLS connection drop. Returns empty string when not authenticated — the heartbeat no-ops in that state.

## Disk caches

`disk_cache.load(name, scope)` / `disk_cache.save(name, scope, payload)` — JSON, with the scope dict hashed into the file's identity so changes invalidate. `_server_scope()` automatically merges `_server_url` and `_provider_kind` into every scope so a fresh sign-in to a different server can't accidentally hit cache entries from the prior identity.

`image_cache` is the cover-art cache — PNG-on-disk, 200MB cap, mtime-LRU eviction every 50 puts. **Two parallel namespaces:** `get/put` are per-consumer rounded pixmaps keyed by full cache_key (`{semId}|{tag}|{w}x{h}|r={radius}`); `get_raw/put_raw` are pre-scale source `QImage`s keyed by **semantic id alone** (`raw_<sha1>.png`). The raw side is the L2-disk tier of the cover pipeline below.

## Cover-art pipeline (five-tier)

`ui_helpers.load_image_async` is the only path. Lookup order on miss: **L1** (per-target QPixmap LRU, 256 entries) → **L2-mem** (per-AlbumId raw QImage LRU, 32 entries) → **L2-disk** (raw source via `image_cache.get_raw`, persists across launches) → **L3** (per-target pixmap on disk via `image_cache.get`) → **network** (QNAM, with HTTP/2 priority hints).

Key contract: callers pass `key="{semantic_id}|{consumer_tag}"` (e.g. `"albumX|npbar"`, `"albumX|albumtile"`). The L2 layers use the part before `|` so any consumer asking for any size of the same image derives locally from another consumer's source. **Always pass `priority=`** for cover-art loads — `"high"` for user-action (bar / mini / np-page `_on_started`, hover prewarm), `"normal"` (default — visible tiles, queue prefetch), `"low"` (off-screen prefetch). Low-priority is gated to 4 in flight so a viewport burst can't choke a click.

**Right-size the URL per surface.** Each now-playing surface builds its own `provider.get_image_url(image_id, "Primary", target)` URL at its display target. Don't reuse `np.thumb_url` (sized 600 for cast/MPRIS). Navidrome resizes on EVERY request and caches the original, NOT the variant — asking for size=600 when the bar is 96px makes Navidrome do ~5× the encode work. Current sizes: bar=256, mini=800, nppage=512.

**Connection keepalive.** `JellyToastWindow._heartbeat()` pings `provider.keep_alive_url()` every 25s. Servers close idle keep-alive after 30-60s; without this, returning to the app after a coffee break cost a fresh TCP+TLS handshake on every cover fetch.

**Signal timing.** `queue_manager._play_current()` emits `playback_started` BEFORE `play_requested.emit(np)` (i.e. before mpv loadfile) so cover loads start as early as possible. `player_backend` re-emits after mpv.play returns; the duplicate is intentional — absorbed by the L1 hit and idempotent label sets.

**`NowPlaying.image_id`** is the AlbumId for audio (and the item id otherwise). Bar / mini / np-page key by `np.image_id or np.item_id` so every track on an album shares one cache slot. Without this, sequential tracks of the same album re-fetch identical art.

## Mini-player translucency invariant

Qt's QSS `background: rgba(...)` does **not** reliably honor alpha on a child `QFrame`. The mini player body is painted manually in `FloatingMiniPlayer.paintEvent` as a rounded rect with `QColor(28,28,28,184)`. Inner widgets are explicitly transparent via stylesheet rules on the container. Reverting to QSS-only breaks translucency.

## Tests

`tests/` has 130 tests covering Queue invariants, Settings persistence (including the encrypted dual-store), JellyfinAPI cache semantics, MpvController prefetch + auto-advance, image_cache LRU, design tokens, and access_token migration paths. Run via:

```bash
python3 -m pytest tests/ -q
```

`conftest.py` enables `QStandardPaths.setTestModeEnabled(True)` and exposes an `isolated_settings` fixture (monkeypatches `_config_dir` to `tmp_path`). The session-scoped `qapp` fixture builds a QGuiApplication for tests that construct QPixmaps.

## Tooling notes

- **Python 3.10+**, **PySide6 6.6+**, **libmpv** system-wide.
- No virtualenv — packages installed with `pip install --break-system-packages` per Arch convention.
- **fish shell** in the user's terminal — heredocs (`<< EOF`) don't work; use `echo '...' > file` or drop into bash explicitly.
- `ruff check .` should be clean (config in `pyproject.toml`).

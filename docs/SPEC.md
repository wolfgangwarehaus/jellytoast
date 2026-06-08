# jellytoast — Capability Spec

Native PySide6 desktop client for **Jellyfin** and **Subsonic / OpenSubsonic / Navidrome**. Music-only. Bit-perfect mpv playback, MPRIS2, system tray, floating mini player, casting (Chromecast / AirPlay 2 / DLNA / Sonos / Snapcast), explicit downloads with offline playback, 10-band EQ, audio visualizer, smart playlists, internet radio, and scrobbling.

---

## 1. Supported providers

| Feature | Jellyfin | Subsonic / Navidrome |
|---|---|---|
| Sign in | Username + password against a Jellyfin server | Username + password (token+salt; LDAP plain-pass fallback on error 41) |
| Browse libraries / albums / artists / songs / playlists / genres | Yes | Yes (mapped to `getMusicFolders` / `getAlbumList2` / `getArtists` / `getRandomSongs` / `getPlaylists` / `getGenres`) |
| Multi-bucket search (songs + albums + artists) | Yes (per-type round-trips, with artist-discography expansion for parity) | Yes (`search3`, single round-trip) |
| Resume / "Continue listening" rail | Yes (`/Items/Resume`) | Not supported by Subsonic — rail self-hides |
| Latest / newest media | Yes | Yes (`getAlbumList2?type=newest`) |
| Random / shuffle pool | Yes | Yes (`getRandomSongs`) |
| Favorites toggle | Yes | Yes (`star` / `unstar`) |
| Play-count / mark-played | Yes (`/PlayedItems`) | Auto via `scrobble submission=true`; no explicit mark-played |
| Playback reporting (Start / Progress / Stop with `PlaySessionId`) | Yes — `DirectStream` or `Transcode` | Start = `scrobble submission=false`; Stop = `scrobble submission=true`; no progress endpoint |
| Lyrics | Yes (`/Audio/{id}/Lyrics`, plain or synced) | Yes via OpenSubsonic `getLyricsBySongId` (synced & plain), normalized to Jellyfin shape |
| Cover art | Yes | Yes (`getCoverArt`) |
| Server-revoke logout | Yes | No-op (Subsonic is stateless per request) |

Credentials are dual-stored: OS keyring (KDE Wallet / GNOME Keyring / SecretService) primary, plus an AES-GCM-encrypted blob in QSettings (key derived from `/etc/machine-id` + username) as a resilience floor. Config file is chmod 600.

---

## 2. Audio playback

- **Engine:** mpv (single instance, headless audio). Hardware decode `auto-safe`.
- **Bit-perfect direct stream** for any container mpv decodes — FLAC, ALAC, OPUS, DSD, MP3, AAC, OGG, WAV, M4A, etc. (mpv's full set).
- **Quality tiers** (setting `playback/audio_quality`): `Original (no transcode)`, `320 / 256 / 192 / 128 / 96 kbps`. "Original" maps to Jellyfin DirectStream / Subsonic `format=raw`. Any kbps value forces server-side transcode to MP3.
- **Gapless playback**: always on. Implemented via mpv `gapless_audio=weak` + `prefetch_playlist=yes` and a queue-driven prefetch into mpv's internal playlist.
- **ReplayGain**: `Off`, `Track`, or `Album` (default Track). Applied via mpv's `replaygain` property; live-applied without restart. `replaygain_clip=no`.
- **Resume position**: persisted per item id (ms + item id pair); restored on launch with the queue and surfaced as a paused track at the saved position.
- **Streaming-info readout**: "Streaming `<codec>` · `<kbps>` kbps" above the play button, populated from mpv's `audio-bitrate` / `audio-codec-name`. Always visible while audio is loaded.
- **Volume**: 0–100, persisted (cast volume changes do *not* persist).

---

## 3. Queue

- **Model:** `Queue` with immutable `original_items` + a permuted `play_order` + `current_index` + `QueueContext` (kind: ALBUM / PLAYLIST / SHUFFLE / MANUAL / etc.).
- **Shuffle modes:** Off / On. Toggling preserves the currently-playing track at the head of the new permutation.
- **Smart shuffle:** always on. The shuffle permutation is built by the weighted picker in `modules/smart_shuffle.py` instead of a flat random draw. It is artist anti-clustering — a candidate's sampling weight is docked when its artist appeared recently in the in-progress order (spread penalty) or in the recent-artist history window, so the same artist doesn't land back-to-back. No play-count weighting. Only affects playback while Shuffle itself is on; libraries under 16 tracks fall back to classic shuffle.
- **Repeat modes:** Off / All / One.
- **Operations:** Play now (replaces), Add next (insert after current), Add to end, Move (drag-reorder), Remove at index, Clear, Jump to index, Next, Previous (restarts current track if >3s elapsed, otherwise goes back).
- **Persistence:** Full queue (context + original items + play_order + current_index) saved to `queue.json` atomically; v1 legacy schema (flat list + index) read transparently.
- **Shuffle library:** "Shuffle library" pulls a random pool from the current library, queue size capped by `ui/shuffle_queue_size` (default 100, clamped 10–1000). Pre-warmed on launch for an instant first click.

---

## 4. Casting

- **Five protocols, all wired into discovery and the cast dialog:** Chromecast, AirPlay 2, DLNA, Sonos, Snapcast.
- **Chromecast** (pychromecast): video, audio, and group receivers all discovered. Direct play for `mp3 / flac / ogg / opus / wav / m4a / mp4 / aac / webm`; anything else server-transcoded to 320 kbps MP3.
- **AirPlay 2** via pyatv when installed (preferred); falls back to **AirPlay 1** RTSP-POST against `_airplay._tcp.local.` for legacy receivers. Pairing dialog handles HomeKit-style PIN exchange.
- **DLNA / Sonos / Snapcast** receivers are discovered alongside the above and appear in the unified cast dialog.
- **Routing modes** (`playback/cast_stream_routing`):
  - `auto` (default) — direct URL when the server is a private LAN IPv4; relay through this machine's local HTTP proxy otherwise (handles Tailscale / public hostnames / self-signed certs).
  - `proxy` — always relay through the local proxy (port 8943, fixed; falls back to ephemeral if taken).
  - `direct` — never relay.
- **Cast proxy** also serves `file://` downloaded blobs (with HTTP Range support) so downloaded tracks cast even when the server is offline. Confined to the downloads directory for safety.
- **Transport while casting:** play / pause / seek (absolute + relative) / volume / stop. Position interpolates between Chromecast status pushes via a 500 ms poll. Track auto-advance and queue navigation route to the cast device. Disconnecting hands the current track back to local mpv at the cast's last position, paused.
- **Group volume:** Chromecast groups expose per-member volume sliders via MultizoneController (each member's physical Chromecast queried directly).
- **Favorites:** Hearted devices in the picker pin to the top and surface in the cast button's right-click menu.
- **Initial cast volume:** 30% (uniform safe baseline).
- **AirPlay 1 transport:** play + stop only (no progress channel — bar stays inert during AirPlay 1).

---

## 5. Offline / downloads

- **What's downloadable:** track, album, artist, playlist. Cascade expansion via `snapshot.freeze` (artist → albums → tracks).
- **Storage:** SQLite node graph (`nodes`, `edges`, `blobs`) under XDG data dir; blobs stored alongside with atomic `.part` → rename. Downloads run on the shared async pool, max 2 concurrent so an album download can't starve quick ops.
- **Quality independence:** `playback/download_quality` is separate from streaming quality (default `original` for bit-perfect copies; can be set to a kbps tier for smaller files).
- **Cascade delete:** removing a parent drops orphaned children only — a track still in another playlist survives.
- **Playback selection:** when a track has a local blob, the queue prefers it. The setting `playback/prefer_server_when_online` (default off) can flip this — but offline mode and an unreachable server always force the local copy.
- **Connectivity tracker:** `is_server_reachable` is driven by API-call outcomes — only network-class failures (`RequestException`, timeout) count; HTTP 4xx/5xx leave reachability alone since the server *is* reachable. Three consecutive failures flip the state to unreachable; a single success clears the counter and flips it back. State transitions emit `connectivity_changed(bool)` on `PlayerBus`.
- **Offline mode:** explicit user toggle, persisted across launches (`offline/offline_mode`). A confirmed outage (unreachable server) *also* flips offline mode on unconditionally — the old user-facing "Automatic offline mode" gate was dropped in #55 (turning it off only produced a worse outage with no upside); the auto-degrade now always applies. A reconnect lifts an auto-set offline mode but leaves a user-set one alone (the connectivity tracker remembers which source set it). Every transition emits `offline_mode_changed(bool)` on `PlayerBus`. In offline mode views read `downloads.db` only.
- **Offline chip:** small accent-tinted pill in the top bar's right column. Three states (hidden when idle): offline + reachable shows "Offline" and is clickable to go online (700 ms "Connecting…" animation, then offline mode lifts); offline + unreachable shows "Offline" as a passive indicator; online + unreachable shows "No connection".
- **Settings → Downloads toggle:** a single "Offline mode" checkbox at the top of the page (the "Automatic offline mode" checkbox was removed in #55 — auto-degrade is now unconditional). The checkbox subscribes to `offline_mode_changed` so an auto-flip from a network drop updates the UI.
- **Scrobble reconnect-flush:** `ScrobbleManager` subscribes to `connectivity_changed` and drains the queued-scrobbles JSON on the rising edge — replaces opportunistic per-call flushing.
- **Queue management (shipped):** downloads run through a pause/resume queue; an in-progress or queued download can be paused and resumed. Failed downloads retry with exponential backoff. Each Downloads-view row offers a per-row re-sync against current server metadata. "Download entire library" walks the whole library and enqueues every track.
- **Wi-Fi-only gating (shipped):** a setting restricts downloads to unmetered (Wi-Fi) connections; metered-connection detection holds the queue until an unmetered network is available.
- **Downloads view:** standalone view; lists user-requested roots only (cascade children excluded). Per-row size + storage usage breakdown by kind, per-row progress UI, and per-row re-sync. Hosts the single "Offline mode" toggle at the top.

---

## 6. Library / browse

- **Top-level views:** Albums, Artists, Songs, Playlists, Genres, Suggestions. (Top-bar dropdown; `home_destination` setting picks the home landing.)
- **View modes:** `grid` (multi-column tiles) or `list` (single-column rows). Toggle persists.
- **Sort** (top-bar dropdown): SortName / AlbumArtist / PremiereDate / ProductionYear / DateCreated / DatePlayed / Random, ascending or descending. Persisted (`ui/library_sort_by`, `ui/library_sort_order`). For Subsonic, mapped to the closest `getAlbumList2` type.
- **Pagination:** hardcoded 100 per page with auto-pagination on scroll. (The `ui/library_page_size` setting was removed in the 2026-05-25 settings condense.)
- **Cover prefetch:** background-fetches every tile's cover after first render (off-switchable for metered connections).
- **A–Z rail:** vertical letter strip on the right edge; current letter brightens, click jumps to first matching tile.
- **Right-click menus:** album/artist/genre tiles and song rows offer *Start radio* (seeds an INSTANT_MIX queue) and *Create smart playlist* (pre-fills the smart-playlist editor from a `from_artist`/`from_album`/`from_genre` recipe; on save the playlist lands on the Smart Playlists tab). Tiles also offer Download / Remove download.
- **Search:** native `SearchView`. Bucketed Songs / Albums / Artists with relevance reordering. Reachable by `Ctrl+F` or `/`.
- **Offline rendering:** Library grid, Songs view, Search, and Artist page short-circuit to `downloads.db` when offline mode is on, via `offline.list_complete_items(kind)` so cascaded children (tracks under a downloaded album, albums under a downloaded artist) surface alongside user-requested roots. Each surface subscribes to `offline_mode_changed` and re-renders on toggle.
- **Offline search:** matches against `Album` / `AlbumArtist` / `Artists` on songs and `AlbumArtist` / `AlbumArtists[].Name` on albums; synthesizes artist tiles from `AlbumArtists` entries on downloaded albums when no artist node exists.
- **Offline artist page:** three-tier resolver — artist node by id → albums whose `AlbumArtists[].Id` matches → albums whose `AlbumArtist` string-name matches via an id→name map built from downloaded tracks/albums. When no artist node exists (only an album was downloaded), the header is synthesized from the first matching `AlbumArtists` entry instead of falling through to "Couldn't load artist".

---

## 7. Now playing

Three coordinated surfaces, all sharing `PlayerBus`:

- **Bottom transport bar** — cover, title/artist, scrubber, transport (prev/play/next), shuffle, repeat, volume, cast button, favorite toggle, sleep-timer (moon) button, optional streaming-format readout.
- **Floating mini player** — frameless top-level window in two modes:
  - **Compact** — 96 px square cover + three-line metadata + transport.
  - **Expanded** — 320 px cover above the same bar (no shuffle/repeat). Width persisted.
  - Optional KWin-rule "keep above" on Wayland (writes `~/.config/kwinrulesrc` — opt-in).
- **Full now-playing page** — 50/50 split: left pane on the left, queue / album-source pane on the right. The left pane has three modes — **cover** (album art), **lyrics**, and **visualizer** (see §8). Lyrics support both synced (auto-scroll, line-position highlight) and plain. User-tunable lyrics font size and line padding. Auto-scroll suspends on user scroll. Cover art keyed by `image_id` (AlbumId for tracks) so consecutive tracks from one album reuse the same fetch.

**Sleep timer.** The transport bar's moon button opens a duration menu (15 / 30 / 45 min, 1 h, 1 h 30 min, or "stop after current track"). Timed presets arm a session-scoped countdown that fades playback out and pauses; "stop after current track" pauses cleanly at the next track boundary. The fade duration is the `playback/sleep_fade_duration_ms` setting. While armed the moon is accent-tinted and its tooltip shows the live countdown. The bar requests start/cancel via the `sleep_timer_requested` / `sleep_timer_cancel_requested` bus signals; `PlayerBackend` owns the timer. Not persisted — a fresh launch starts with no timer.

---

## 8. Audio visualizer

- **Pipeline:** an FFT pipeline taps the playback audio per-stream from PipeWire and produces a frequency spectrum in real time.
- **Paint widget:** a Bezier-wave paint widget renders the spectrum, accent-tinted.
- **Where it appears:** as one of the three left-pane modes on the full now-playing page (`cover | lyrics | visualizer`). Switching the left pane to visualizer shows the live wave for the playing track.

---

## 9. Equalizer

- **10-band graphic EQ** plus a master pre-amp slider. Lives in **Settings → Playback**.
- **Presets:** selectable EQ presets in addition to manual per-band adjustment.
- **Casting:** EQ controls are greyed out while casting (the cast device, not local mpv, owns the audio path).

---

## 10. Smart playlists

- **Rule-based playlists:** a smart playlist is a set of rules; matching tracks are evaluated rather than hand-picked.
- **Editor dialog:** a smart-playlist editor with **live preview** — the matching track set updates as rules are edited.
- **Library tab:** smart playlists surface on their own **Smart Playlists** tab in the library.
- **Right-click creation:** album / artist / genre tiles and song rows offer *Create smart playlist*, pre-filling the editor from a `from_artist` / `from_album` / `from_genre` recipe.
- **Evaluation:** client-side evaluation plus server-push so the playlist materializes on the backend where supported.
- **Date rules (schema v2):** `date_added` / `last_played` rule fields with `in the last` / `before` / `after` operators. Jellyfin reads `DateCreated` / `UserData.LastPlayedDate`; Subsonic maps its `created` timestamp onto `date_added` (it has no per-track last-played data, so `last_played` never matches there). A date rule has no server-side filter, so the refine fetch pages the library and — for `in the last` / `after` — sorts by the date and stops at the cutoff.

---

## 11. Internet radio

- **Radio tab:** a dedicated Radio tab in the library.
- **Presets:** a curated set of preset stations ships out of the box.
- **Station CRUD:** users can add / edit / remove their own stations, stored per provider.
- **ICY metadata:** now-playing track / title info is read from the stream's ICY metadata.
- **Instant mix:** seeded "instant mix" radio — the right-click *Start radio* on an album / artist / genre / track seeds an `INSTANT_MIX` queue (see §6).

---

## 12. Scrobbling

- **Subsystem:** `modules/scrobble/` — eligibility math (play-fraction / minimum-duration thresholds), a JSON-backed offline queue, and a reconnect flush that drains the queue on the `connectivity_changed` rising edge (see §5).
- **ListenBrainz — usable:** a Settings UI exposes a token field plus a *Validate* button; scrobbles submit to ListenBrainz.
- **Last.fm — deferred:** the Last.fm client is built but parked (2026-05-20). Registering the in-app API credentials needs a Last.fm account, and their signup firewall kept blocking it; the `API_KEY` / `API_SECRET` constants stay empty, so its Settings section is hidden and it does not run. ListenBrainz is the supported scrobbling path. See `docs/TODO.md` → Parked.

---

## 13. Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+F` or `/` | Focus search input (opens SearchView if not current; selects existing query for retype) |
| `Ctrl+Shift+L` | All music — native album grid scoped to the music library |
| `Ctrl+Shift+A` | Open currently-playing track's album (only when env `JT_NATIVE_ALBUM=1` is set) |
| `Ctrl+Q` | Quit (routes through `closeEvent` — minimize-to-tray vs hard quit per setting) |
| `Space` | Play / pause toggle (application-wide; suppressed when a text input has focus) |
| `Tab` / `Shift+Tab` | Rotate focus between the three structural sections (top bar → content → bottom transport) |
| `Down` from chrome | Dive into current content surface's first item |
| `Down` in lyrics view | Section navigation (NowPlayingPage) |
| Media Play / Next / Previous keys | Play-pause / next / previous via MPRIS |

`Ctrl+F`, `/`, `Ctrl+Shift+L`, and `Ctrl+Q` are also surfaced read-only in Settings → Hotkeys. Rebinding is not implemented.

---

## 14. Persisted settings

All under `jellytoast/jellytoast.conf` via `QSettings`.

| Key | Description |
|---|---|
| `server/provider_kind` | Backend identifier — `jellyfin` (default) or `subsonic` |
| `server/url`, `server/username`, `server/user_id`, `server/device_id` | Server identity (UUID-stamped device id minted on first run) |
| `server/token` | AES-GCM-encrypted access token (mirror of keyring entry) |
| `ui/window_geometry`, `ui/mini_player_geometry`, `ui/mini_player_mode`, `ui/mini_player_expanded_width` | Window / mini-player geometry |
| `ui/mini_on_start` | Launch directly into mini player |
| `ui/minimize_to_tray` | Close button minimizes instead of quitting (default on) |
| `ui/show_tooltips` | Global hover-tooltip toggle (live-applied via QApplication event filter) |
| `ui/autostart` | Mirror of XDG `~/.config/autostart/jellytoast.desktop` |
| `ui/home_destination` | Top-bar Home destination |
| `ui/mini_player_keep_above` | KWin-rule "always on top" for the mini player (Wayland-only, opt-in) |
| `ui/theme_mode` | Theme — dark and light families both ship (`_LIGHT_TOKENS`). Theme mode and accent color both **live-apply** via `PlayerBus.theme_changed`; only `font_scale` needs a restart |
| `ui/accent_color` | Hex accent override (`#967de1` default) — live-applied via `PlayerBus.theme_changed` |
| `ui/shuffle_queue_size` | Tracks pulled by "Shuffle library" (default 100, clamped 10–1000) |
| `ui/library_cover_prefetch` | Background-fetch covers after render (default on) |
| `ui/library_view_mode` | `grid` or `list` |
| `ui/library_tile_fade` | 180 ms cover fade-in (default on) |
| `ui/library_sort_by`, `ui/library_sort_order` | Library sort key + direction |
| `ui/lyrics_font_size`, `ui/font_scale` | `small / default / large / largest` (font_scale needs restart) |
| `playback/volume`, `playback/repeat`, `playback/shuffle` | Transport state |
| `playback/audio_quality` | `original` or kbps string |
| `playback/download_quality` | Quality for downloaded copies (independent) |
| `playback/cast_stream_routing` | `auto / proxy / direct` |
| `playback/favorite_cast_devices` | JSON list of pinned cast devices (uuid + name + type) |
| `playback/prefer_server_when_online` | Stream from server even when local copy exists (default off) |
| `playback/replaygain` | `no / track / album` |
| `playback/position_ms`, `playback/position_item_id` | Resume position pair |
| `offline/offline_mode` | Explicit user offline-mode toggle (persisted across launches). A confirmed outage also auto-sets it (unconditional since #55 dropped the `auto_offline_mode` gate); a reconnect lifts an auto-set value but not a user-set one |

Queue is persisted separately as `queue.json` (v2 schema with v1 legacy read).

---

## 15. Platform support

**Working today (Linux):**
- Linux (CachyOS / KDE Plasma / Wayland is the primary dev target; X11 also supported).
- MPRIS2 (`org.mpris.MediaPlayer2.jellytoast`) — picked up by KDE Plasma media widget, GNOME Shell, playerctl, waybar.
- System tray (Now-playing label + play / prev / next / stop / show mini / open / quit).
- XDG autostart `.desktop` entry.
- KDE Wallet / GNOME Keyring / SecretService for credentials.
- Wayland-specific keep-above for the mini player via a KWin window rule (`~/.config/kwinrulesrc`); KDE server-side decorations on the main window.

**Working today (Windows):** smoke-verified end-to-end on a clean Windows 11 25H2 box (pipx install, login, libmpv→WASAPI playback, Chromecast, persistence; 2026-06-05/06).
- Frameless borderless main window with real **Acrylic** frosted-glass blur as the default (`modules/blur/_dwm.py` calls `apply_acrylic` unless `JT_NO_WIN_BLUR`; legacy `SetWindowCompositionAttribute` / `ACCENT_ENABLE_ACRYLICBLURBEHIND`), rounded dialog corners, and a centered cast menu.
- Auto (follow-OS) theme that live-swaps light/dark with the Windows colour scheme, plus crisp HiDPI icon-buttons at fractional display scale.
- Credentials via the OS keyring; libmpv shipped as `libmpv-2.dll` (placement: pipx venv `Lib\site-packages` or on PATH).

**Scaffolded but not implemented:**
- Windows-native OS-integration backends — `media_controls` (SMTC), `autostart`, `keep_above` all still fall back to `_unsupported.py` on Windows.
- macOS backends for the same packages (NowPlaying via pyobjc).
- Custom Cast receiver app (would surface "jellytoast" instead of "Default Media Receiver") — deferred.
- AUR PKGBUILD / Flatpak build manifest — not started (packaging is scaffolded but deferred; see `docs/TODO.md`).

> **Shipped since this list was last accurate (corrected 2026-05-28 audit):**
> the items that used to sit under an "engine built, no UI" caveat all now
> have working UI and are user-facing capabilities — do **not** treat them
> as un-built:
> - **Crossfade** — full Settings → Playback section (enable checkbox,
>   smart-album-continuity toggle, duration slider); gated on the
>   `crossfade_enabled` setting (the old `JT_CROSSFADE=1` env gate is gone).
> - **Multi-server hostnames** — the login screen has an "Add alternate URL"
>   affordance (`login_view._AlternateUrlsDialog`) for entering failover hosts.
> - **Tag editing** — "Edit tags…" dialog (`modules/tag_editor.py`) with
>   single-track edit, cover-art replace, and bulk "apply to whole album"
>   (Jellyfin only; Subsonic has no metadata-write API).
> - **Hotkey rebinding** — Settings → Hotkeys is a live `QKeySequenceEdit`
>   per row with per-row reset + conflict warning (no longer read-only).
> - **Light theme** — a full `_LIGHT_TOKENS` family ships alongside the dark
>   themes; theme mode and accent both **live-apply** (only `font_scale`
>   still needs a restart).

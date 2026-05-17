# jellytoast

A native PySide6 desktop music client for **[Jellyfin](https://jellyfin.org/)** and **[Subsonic-API](http://www.subsonic.org/pages/api.jsp) servers** (Navidrome, Airsonic, OpenSubsonic), with bit-perfect audio via **[mpv](https://mpv.io/)**.

Audio-first, music-only. Targets Arch Linux / CachyOS with KDE Plasma 6 + Wayland but works on any modern Linux desktop with Qt 6.

```
┌─ jellytoast (KDE-decorated QMainWindow) ─────────────────────────┐
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

- **Multi-backend** — Jellyfin or any Subsonic-API server (tested against Navidrome). Switch in Settings → Account.
- **Bit-perfect audio** via mpv — FLAC / ALAC / OPUS / DSD play untouched. Gapless playback + ReplayGain (track / album / off).
- **Resume on launch** — last position restores into the now-playing bar; press play to pick up.
- **Native browse views** — library grid (grid/list toggle), artist page, songs, genres, search, suggestions, internet radio. Pagination for large libraries.
- **Floating mini player** — frameless, draggable, KWin keep-above on Wayland.
- **MPRIS2 + system tray** — media keys, KDE Plasma media widget, `playerctl`, waybar; minimize-to-tray.
- **Casting** — Chromecast, AirPlay 2, plus optional DLNA / Sonos / Snapcast backends. Cast-stream proxy for remote / Tailscale / self-signed servers.
- **Offline downloads** — explicit downloads + cascade (album / artist / playlist); full offline playback with offline-aware library filtering.
- **Scrobbling** — ListenBrainz and Last.fm, with offline queue + reconnect flush.
- **Smart shuffle** — recency- and artist-spread-aware over classic random.
- **Smart playlists** — rule-based, both providers (server-push where possible, Python refine for everything else).
- **Sleep timer** with fade-to-stop, **tag editing** (Jellyfin), **synced lyrics**, **internet radio CRUD**.
- **Encrypted credential storage** — OS keyring (KWallet / GNOME Keyring) primary, AES-GCM file fallback so boot doesn't depend on a responsive wallet.

## Install

### AUR (Arch Linux) — *planned*

```bash
yay -S jellytoast
```

### Flathub — *planned*

```bash
flatpak install flathub io.github.augustvontrips66.jellytoast
```

Both packaging targets are in progress. Until they land, install from source:

### From source

Requires Python 3.10+, Qt 6, mpv, libmpv, libnotify, ffmpeg. On Arch:

```bash
git clone https://github.com/augustvontrips66/jellytoast.git
cd jellytoast
bash dev/install.sh       # installs runtime deps via pacman + pip
python3 jellytoast.py
```

On other distros: install the system deps via your package manager, then:

```bash
pip install -e .
jellytoast
```

### Optional extras

Heavyweight per-feature deps ship as optional extras to keep the default install lean:

```bash
pip install -e ".[visualizer]"   # FFT audio visualizer (numpy)
pip install -e ".[dlna]"         # DLNA / UPnP-AV cast backend
pip install -e ".[sonos]"        # native Sonos cast backend
pip install -e ".[snapcast]"     # Snapcast control surface
```

Each is dormant unless the matching backend is enabled in Settings (and, for the visualizer, the `JT_VISUALIZER=1` env flag).

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

Per-protocol toggles + on-demand discovery in Settings → Casting:

- **Chromecast** — Default Media Receiver, direct-play for browser-supported codecs (MP3 / AAC / FLAC), HTTP audio fallback for everything else. Hard dep (`pychromecast`).
- **AirPlay 2** — pyatv-based, supports modern Apple TVs and AirPlay 2 speakers. Hard dep on Linux/macOS (broken on Windows).
- **DLNA / UPnP-AV** — `[dlna]` extra. SSDP discovery + AVTransport push, 714/701 transcode-retry decision tree, mandatory upnp:class for spec-finicky renderers.
- **Sonos** — `[sonos]` extra. Native SoCo-based zone discovery + group transport.
- **Snapcast** — `[snapcast]` extra. Option B control surface (groups, clients, stream switching, volume) — not a "push URL" cast model.

The **cast proxy** (modules/cast_proxy.py) relays streams to receivers that can't reach the server directly — Tailscale, remote, self-signed certs — and serves downloaded blobs off disk so cast works fully offline. Toggle via `cast_stream_routing` in Settings.

When casting starts, local mpv stops; on disconnect, the local stream resumes at the cast position.

## Settings

- **General** — autostart at login, home destination, minimize-to-tray, mini-player keep-above.
- **Account** — server URL, username, sign in / sign out, switch backend.
- **Playback** — ReplayGain mode, sleep-timer fade duration, smart-shuffle toggle.
- **Library** — page size, cover prefetch, tile fade animation.
- **Lyrics** — font size.
- **Casting** — per-protocol toggles, discovery timing (startup vs on-demand), cast-stream routing.
- **Downloads** — download root, offline-mode toggle.
- **Appearance** — theme (dark / light), accent color, blur opacity.
- **Scrobbling** — ListenBrainz / Last.fm account hookup.

Window geometry, sort order, view mode (grid / list), shuffle / repeat all persist automatically.

## Repository layout

```
jellytoast.py            Entry script: window, boot auth, nav, app shell
modules/                 Application code (see jellytoast.py imports)
  providers/             Jellyfin + Subsonic provider abstraction
  cast/                  DLNA / Sonos / Snapcast backends
  offline/               Downloads index + manager + on-disk store
  scrobble/              ListenBrainz / Last.fm + offline queue
  autostart/             Cross-platform autostart backends
  keep_above/            Wayland keep-above (KWin rule)
  media_controls/        MPRIS2 (Linux), system-tray
tests/                   pytest suite (~980 tests)
docs/                    Design docs, decisions, manual test plan, research
packaging/               Flatpak metainfo + .desktop + AppStream + icons
dev/                     Developer helpers (install.sh, run.sh, desktop entry)
```

Everything talks through `PlayerBus` (Qt signals). UI emits intents (e.g. `queue_play_now`); backend listens, acts, emits state (`playback_started`). Adding a new component is wiring to the bus, not directly to mpv or the queue.

## Troubleshooting

| Issue | Fix |
| --- | --- |
| `mpv` import fails | Install libmpv via your package manager (`sudo pacman -S mpv` on Arch) |
| `pyside6` import fails | `sudo pacman -S pyside6` or `pip install PySide6` |
| No tray icon | Install a tray helper for your DE (e.g. `plasma-systray`) |
| Chromecast not found | Open UDP 5353 (mDNS); same VLAN as the Chromecast |
| AirPlay receiver not found | Check Settings → Casting has AirPlay enabled; some older LG webOS / shairport-sync 5.x receivers are broken in pyatv (see docs/research/airplay2.md) |
| DLNA / Sonos / Snapcast not listed in cast menu | Install the matching `[extra]` (see above) AND enable the protocol in Settings → Casting |
| Wayland: video shows in wrong spot | Set `QT_QPA_PLATFORM=xcb` (or use `dev/run.sh` which sets it) |
| Login devolves to LoginView across launches | Keyring (kwalletd6) is unresponsive at boot — the encrypted file fallback should kick in. Sign in once; subsequent launches auto-sign-in via the file. |

## Developer setup

```bash
git clone https://github.com/augustvontrips66/jellytoast.git
cd jellytoast
pip install -e ".[dev]"        # ruff + pytest
pre-commit install             # auto-format + lint on commit
pytest -q                      # ~980 tests, ~13s
bash dev/run.sh                # launch with libmpv env vars set
```

The pre-commit config (`.pre-commit-config.yaml`) wires `ruff` (lint + `--fix`) and `ruff-format`. Lint rules are declared in `pyproject.toml [tool.ruff.lint]`; the hook doesn't duplicate them.

## Why mpv?

Browser-based playback stacks transcode FLAC / ALAC to AAC server-side. mpv plays them untouched, supports gapless and ReplayGain, has hardware video decoding, and uses much less RAM than a browser playback pipeline.

## Why MPRIS?

MPRIS2 is the integration point on Linux:

- Keyboard media keys work.
- KDE Plasma's media widget shows the current track + controls.
- GNOME Shell's media controls show jellytoast.
- `playerctl play-pause` works from a script or keybinding.
- waybar / polybar / etc. can render the current song.
- Browsers pause their media when jellytoast starts.

## Roadmap

- AUR PKGBUILD + Flathub manifest (in progress).
- CastManager UI wiring for DLNA / Sonos / Snapcast (backends shipped; discovery + push wiring pending).
- Visualizer rendering widget (FFT pipeline shipped; UI tuning pending).
- Internet-radio UI surfaces (CRUD shipped on both providers; UI pending).

## License

GPL-2.0-or-later. See `LICENSE`.

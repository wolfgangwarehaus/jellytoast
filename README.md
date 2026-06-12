<h1 align="center">jellytoast</h1>

<p align="center">
  A fast, native desktop music player for <a href="https://jellyfin.org/">Jellyfin</a> and
  <a href="https://www.subsonic.org/pages/api.jsp">Subsonic-API</a> servers
  (Navidrome, Airsonic, OpenSubsonic) — bit-perfect audio via <a href="https://mpv.io/">mpv</a>.
</p>

<p align="center">
  <a href="https://github.com/wolfgangwarehaus/jellytoast/actions/workflows/ci.yml"><img src="https://github.com/wolfgangwarehaus/jellytoast/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/wolfgangwarehaus/jellytoast/releases"><img src="https://img.shields.io/github/v/release/wolfgangwarehaus/jellytoast?include_prereleases" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--2.0--or--later-blue" alt="License"></a>
</p>

<!-- hero screenshot: docs/screenshots/hero.png (Library view, Frosted dark) -->
<!-- <p align="center"><img src="docs/screenshots/hero.png" width="800" alt="jellytoast — library view"></p> -->

Audio-first and music-only. Built with Qt (PySide6) — no Electron, no browser
engine. Linux (KDE Plasma is the reference desktop) and Windows 11.

## Features

- **Bit-perfect audio** via mpv — FLAC / ALAC / OPUS / DSD untouched, gapless, ReplayGain, ALSA-direct output, device picker.
- **Multi-backend** — Jellyfin or any Subsonic-API server (tested against Navidrome).
- **Casting** — Chromecast, AirPlay 2, DLNA, Sonos, Snapcast — plus a cast proxy that reaches receivers your server can't (Tailscale, remote, self-signed certs) and casts downloaded music fully offline.
- **Offline downloads** — explicit downloads with album / artist / playlist cascade and offline-aware browsing.
- **Native browse** — library grid, artist pages, songs, genres, search, suggestions, internet radio; paginated for big libraries.
- **Floating mini player** — frameless, draggable, keep-above, with a compact and an expanded layout.
- **System integration** — MPRIS2 media keys, tray, notifications, autostart.
- **Frosted-glass UI** — real compositor blur (KWin / Windows Acrylic) with honest fallbacks, dark + light, accent colors.
- **And the rest** — synced lyrics, FFT visualizer, smart playlists, smart shuffle, sleep timer, ListenBrainz scrobbling, tag editing (Jellyfin), encrypted credential storage.

## Install

| Platform | How |
| --- | --- |
| **Arch Linux** | `yay -S jellytoast` *(AUR — lands with v0.1.0)* |
| **Ubuntu / Debian** | Download the `.deb` from [Releases](https://github.com/wolfgangwarehaus/jellytoast/releases), then `sudo apt install ./jellytoast_*_amd64.deb` |
| **Flathub** | `flatpak install flathub io.github.wolfgangwarehaus.jellytoast` *(submission in progress)* |
| **Windows** | Installer or portable zip from [Releases](https://github.com/wolfgangwarehaus/jellytoast/releases) · `winget install wolfgangwarehaus.jellytoast` *(after v0.1.0)* |
| **Any distro (pip)** | Install `mpv`/`libmpv` + Qt 6 from your package manager, then `pipx install jellytoast` *(PyPI — lands with v0.1.0)* |

From source (any platform with Python 3.11+, Qt 6, libmpv):

```bash
git clone https://github.com/wolfgangwarehaus/jellytoast.git
cd jellytoast
pip install -e .
jellytoast            # or: python3 -m jellytoast
```

All cast backends and the visualizer ship in the standard install — no extras
to remember. Each stays dormant unless enabled in Settings.

## Documentation

| Doc | What it is |
| --- | --- |
| [`docs/user_guide.md`](docs/user_guide.md) | Shortcuts, mini player, tray, casting, settings, themes & blur, troubleshooting |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup + the conventions this codebase follows |
| [`docs/SPEC.md`](docs/SPEC.md) | What the app actually does today |
| [`docs/decisions.md`](docs/decisions.md) | Architecture decision log (why, not just what) |
| [`docs/TODO.md`](docs/TODO.md) | The backlog (P0–P4) |
| [`docs/manual_test_plan.md`](docs/manual_test_plan.md) | By-hand / by-eye verification checklist |
| [`docs/research/`](docs/research/) | Per-feature design docs (each carries a shipped/not-shipped banner) |
| [`LICENSING.md`](LICENSING.md) | License + the load-bearing PySide6 "or-later" note |
| [`SECURITY.md`](SECURITY.md) | How to report a vulnerability |
| [`CHANGELOG.md`](CHANGELOG.md) | Dated history of what shipped |

## Developer setup

```bash
pip install -e ".[dev]"        # ruff + pytest + pytest-xdist + pre-commit
pre-commit install             # ruff lint + import-sort on commit
pytest -n auto -q              # ~2900 tests, parallel
bash dev/run.sh                # launch from the repo (locale + Qt logging env)
```

Architecture in one line: everything talks through `PlayerBus` (Qt signals) —
UI emits intents (`queue_play_now`), the backend listens, acts, and emits state
(`playback_started`). Packaging lives in [`packaging/`](packaging/)
(AUR, Flatpak, deb, Windows installer, winget).

## License

GPL-2.0-or-later. See [`LICENSE`](LICENSE).

<h1 align="center">jellytoast</h1>

<p align="center">
  A desktop music player for <a href="https://jellyfin.org/">Jellyfin</a> and
  <a href="https://www.navidrome.org/">Navidrome</a> servers — bit-perfect
  <a href="https://mpv.io/">mpv</a> audio, casting, mini player, and offline downloads.
</p>

<p align="center">
  <a href="https://github.com/wolfgangwarehaus/jellytoast/actions/workflows/ci.yml"><img src="https://github.com/wolfgangwarehaus/jellytoast/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--2.0--or--later-blue" alt="License"></a>
</p>

<!-- hero screenshot: docs/screenshots/hero.png (Library view, Frosted dark) -->
<!-- <p align="center"><img src="docs/screenshots/hero.png" width="800" alt="jellytoast — library view"></p> -->

## Features

- **Bit-perfect audio** — plays your files exactly as they are (FLAC, ALAC, DSD, Opus…) through [mpv](https://mpv.io/): gapless, volume-matched, sent straight to your hardware, with your pick of output device.
- **Works with your server** — connects to Jellyfin and Navidrome, plus other Subsonic-compatible servers.
- **Cast anywhere** — send music to Chromecast, AirPlay 2, Sonos, DLNA, or Snapcast. A built-in local relay can forward the stream for trickier setups, like Tailscale connections or fully offline playback.
- **Real offline mode** — download a track, album, artist, or whole playlist and keep listening offline; your library still browses cleanly with no connection.
- **Browse your whole library** — albums, artists, songs, genres, search, suggestions, and internet radio, staying fast even on huge libraries.
- **Floating mini player** — a small, draggable window that stays on top, in a compact or an expanded layout.
- **Fits your desktop** — media keys, a tray icon, notifications, and start-on-login.
- **Frosted-glass look** — real background blur on KDE and Windows, light and dark themes, and your own accent color.
- **And more** — synced lyrics, an audio visualizer, smart playlists, smart shuffle, a sleep timer, ListenBrainz scrobbling, tag editing (Jellyfin), and encrypted login storage.

## Install

| Platform | How |
| --- | --- |
| **Ubuntu / Debian** (22.04+ / 12+) | Download the `.deb` from [Releases](https://github.com/wolfgangwarehaus/jellytoast/releases), then `sudo apt install ./jellytoast_*_amd64.deb` |
| **Windows 10/11** (x64) | Installer or portable zip from [Releases](https://github.com/wolfgangwarehaus/jellytoast/releases). The build is unsigned, so SmartScreen warns on first run — click **More info → Run anyway** (verify the download's SHA256 against `SHA256SUMS`). |
| **From source** (any OS) | Python 3.11+, Qt 6, libmpv — see below |

> **Coming soon:** AUR, Flathub, winget, and PyPI (`pipx install jellytoast`) are packaged and staged but not yet published — they land shortly after v0.1.0. Until then use the `.deb`, the Windows build, or from source. (Pip users can `pipx install` the wheel attached to the [release](https://github.com/wolfgangwarehaus/jellytoast/releases) today.)

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
| [`CONTRIBUTING.md`](.github/CONTRIBUTING.md) | Dev setup + the conventions this codebase follows |
| [`docs/SPEC.md`](docs/SPEC.md) | What the app actually does today |
| [`docs/decisions.md`](docs/decisions.md) | Architecture decision log (why, not just what) |
| [`docs/TODO.md`](docs/TODO.md) | The backlog (P0–P4) |
| [`LICENSING.md`](docs/LICENSING.md) | License + the load-bearing PySide6 "or-later" note |
| [`SECURITY.md`](.github/SECURITY.md) | How to report a vulnerability |
| [`CHANGELOG.md`](docs/CHANGELOG.md) | Dated history of what shipped |

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

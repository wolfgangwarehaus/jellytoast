<h1 align="center">jellytoast</h1>

<p align="center">
  A desktop music player for <a href="https://jellyfin.org/">Jellyfin</a> and
  <a href="https://www.navidrome.org/">Navidrome</a> servers —<br>
  bit-perfect playback, casting, mini-player, and offline downloads.
</p>

<p align="center">
  <a href="https://github.com/wolfgangwarehaus/jellytoast/actions/workflows/ci.yml"><img src="https://github.com/wolfgangwarehaus/jellytoast/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--2.0--or--later-blue" alt="License"></a>
</p>

<!-- hero screenshot: docs/screenshots/hero.png (Library view, Frosted dark) -->
<!-- <p align="center"><img src="docs/screenshots/hero.png" width="800" alt="jellytoast — library view"></p> -->

## Features

- **Desktop app for your self-hosted music** — supports Jellyfin and Navidrome, with multiple libraries.
- **Bit-perfect audio** — FLAC / ALAC / OPUS / DSD playback via [mpv](https://mpv.io/).
- **Cast anywhere** — send music to Chromecast, AirPlay 2, Sonos, DLNA, or Snapcast. A built-in local relay can forward the stream for trickier setups, like Tailscale connections or fully offline playback.
- **Offline mode** — cache albums, playlists, or your whole library for offline playback.
- **Floating mini player** — compact and album-art views.
- **Desktop features** — media keys, a tray icon, optional notifications, and a start-at-login option.
- **Frosted-glass look** — real background blur on KDE and Windows, light and dark themes, and your own accent color.
- **And more** — synced lyrics, an audio visualizer, smart playlists, smart shuffle, a sleep timer, ListenBrainz scrobbling, tag editing (Jellyfin), and encrypted login storage.

## Install

| Platform | How |
| --- | --- |
| **Ubuntu / Debian** (22.04+ / 12+) | Download the `.deb` from [Releases](https://github.com/wolfgangwarehaus/jellytoast/releases/latest), then `sudo apt install ./jellytoast_*_amd64.deb` |
| **Windows 10/11** (x64) | Installer or portable zip from [Releases](https://github.com/wolfgangwarehaus/jellytoast/releases). The build is unsigned, so SmartScreen warns on first run — click **More info → Run anyway** (verify the download's SHA256 against `SHA256SUMS`). |
| **PyPI** (any OS) | `pipx install jellytoast` |
| **From source** (any OS) | Python 3.11+, Qt 6, libmpv — see below |

> **Other channels:** **winget** — submitted, pending review. **AUR** — in progress, paused until the AUR reopens new-account registration.

From source (any platform with Python 3.11+, Qt 6, libmpv):

```bash
git clone https://github.com/wolfgangwarehaus/jellytoast.git
cd jellytoast
pip install -e .
jellytoast            # or: python3 -m jellytoast
```

Want to contribute? [`CONTRIBUTING.md`](.github/CONTRIBUTING.md) has the
dev setup, the architecture, and the conventions this codebase follows.

## Documentation

| Doc | What it is |
| --- | --- |
| [`docs/user_guide.md`](docs/user_guide.md) | Shortcuts, mini player, tray, casting, settings, themes & blur, troubleshooting |
| [`CONTRIBUTING.md`](.github/CONTRIBUTING.md) | Dev setup, architecture & the conventions this codebase follows |
| [`docs/SPEC.md`](docs/SPEC.md) | What the app actually does today |
| [`docs/decisions.md`](docs/decisions.md) | Architecture decision log (why, not just what) |
| [`docs/TODO.md`](docs/TODO.md) | The backlog (P0–P4) |
| [`SECURITY.md`](.github/SECURITY.md) | How to report a vulnerability |
| [`CHANGELOG.md`](docs/CHANGELOG.md) | Dated history of what shipped |

## License

GPL-2.0-or-later. See [`LICENSE`](LICENSE).

## Support

Want to leave a tip? [Ko-fi ☕](https://ko-fi.com/wolfgangwarehaus)

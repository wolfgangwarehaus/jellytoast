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
- **Cast anywhere** — send music to Chromecast, AirPlay 2, Sonos, or DLNA. A built-in local relay can forward the stream for trickier setups, like Tailscale connections or fully offline playback.
- **Offline mode** — cache albums, playlists, or your whole library for offline playback.
- **Floating mini player** — compact and album-art views.
- **Desktop features** — media keys, a tray icon, optional notifications, and a start-at-login option.
- **Frosted-glass look** — real background blur on KDE and Windows, light and dark themes, and your own accent color.
- **And more** — synced lyrics, an audio visualizer, smart playlists, smart shuffle, a sleep timer, ListenBrainz scrobbling, tag editing (Jellyfin), and encrypted login storage.

## Install

Latest builds are on [**Releases**][rel].

- **Linux** — [`.deb`][rel] · [AppImage][rel] · AUR *(soon)*
- **macOS** — [Apple Silicon][rel] · [Intel][rel] · App Store *(soon)*
- **Windows** — [**Microsoft Store**][store] · [installer][rel] · `winget install wolfgangwarehaus.jellytoast`
- **Any OS** — `pipx install jellytoast`

**From source** (Python 3.11+, Qt 6, libmpv):

```bash
git clone https://github.com/wolfgangwarehaus/jellytoast.git
cd jellytoast && pip install -e .
jellytoast
```

Want to contribute? [`CONTRIBUTING.md`](.github/CONTRIBUTING.md) has the dev setup, the architecture, and the conventions this codebase follows.

[rel]: https://github.com/wolfgangwarehaus/jellytoast/releases/latest
[store]: https://apps.microsoft.com/detail/9PNLTPXGHN79

## Documentation

- [`docs/user_guide.md`](docs/user_guide.md) — shortcuts, mini player, tray, casting, settings, themes & blur, troubleshooting
- [`CONTRIBUTING.md`](.github/CONTRIBUTING.md) — dev setup, architecture & conventions
- [`docs/SPEC.md`](docs/SPEC.md) — what the app actually does today
- [`SECURITY.md`](.github/SECURITY.md) — how to report a vulnerability
- [`CHANGELOG.md`](CHANGELOG.md) — what changed in each release

## License

GPL-2.0-or-later. See [`LICENSE`](LICENSE).

## Support

Want to leave a tip? [Ko-fi ☕](https://ko-fi.com/wolfgangwarehaus)

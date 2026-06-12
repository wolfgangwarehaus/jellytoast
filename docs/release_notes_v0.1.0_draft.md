# v0.1.0 release notes — DRAFT

> Draft for the first real release. Paste into the GitHub release body when
> cutting `v0.1.0` (the release.yml draft release auto-generates a commit
> list; replace it with this). Delete this file after the release ships.

---

## jellytoast 0.1.0 — first release

jellytoast is a fast, native desktop music player for **Jellyfin** and
**Subsonic-API** servers (Navidrome, Airsonic, OpenSubsonic). Qt all the way
down — no Electron — with bit-perfect audio via mpv.

### Install

| Platform | How |
| --- | --- |
| Arch Linux | `yay -S jellytoast` (AUR) |
| Ubuntu 22.04+ / Debian 12+ / Mint 21+ | download the `.deb` below, `sudo apt install ./jellytoast_0.1.0_amd64.deb` |
| Windows 10/11 (x64) | `jellytoast-0.1.0-windows-x64-setup.exe` below (or the portable zip — no install needed) |
| Any Linux distro | `pipx install jellytoast` (install `mpv`/`libmpv` + Qt 6 from your package manager first) |

Flathub and winget are in the pipeline and will be announced when live.

### Highlights

- **Bit-perfect playback** — FLAC / ALAC / OPUS / DSD untouched via mpv;
  gapless, ReplayGain, audio-device picker, ALSA-direct output with honest
  exclusivity semantics, PipeWire rate-following config installer.
- **Casting, five ways** — Chromecast, AirPlay 2, DLNA, Sonos, Snapcast. The
  built-in cast proxy reaches receivers your server can't (Tailscale,
  remote, self-signed certs) and casts downloaded music fully offline.
- **Offline downloads** — explicit downloads with album/artist/playlist
  cascade, offline-aware browsing, automatic connectivity detection.
- **Floating mini player** — frameless, draggable, keep-above, compact and
  expanded layouts.
- **Frosted-glass UI** — real compositor blur (KWin on KDE, Acrylic on
  Windows 11) with honest fallbacks everywhere else; dark + light themes,
  accent colors, fractional-HiDPI crisp.
- **System integration** — MPRIS2 media keys, tray, notifications, autostart
  (Linux); SMTC planned for Windows.
- **The rest** — synced lyrics, FFT visualizer, smart playlists, smart
  shuffle, sleep timer, internet radio, ListenBrainz scrobbling (with
  offline queue), tag editing (Jellyfin), encrypted credential storage
  (keyring + AES-GCM file fallback).

### Numbers

- ~2,900 tests, Python 3.11–3.13, CI on every push
- Two providers (Jellyfin, Subsonic) behind one abstraction — every feature
  works on both unless the server API makes it impossible (documented where so)

### Known limitations

- macOS: not yet supported (planned — needs hardware).
- Windows: AirPlay casting unavailable (upstream pyatv limitation); media
  keys (SMTC) not wired yet.
- Sonos and Snapcast casting are implemented but have had less real-hardware
  testing than Chromecast/AirPlay/DLNA.

### Checksums

(filled by the release workflow / `sha256sum dist/*`)

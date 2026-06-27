# Changelog

What changed in each release, in plain language — newest first. The detailed,
developer-facing history lives in [`docs/CHANGELOG.md`](docs/CHANGELOG.md).

## [Unreleased]

<!-- Voice: short, plain, a little casual — what's new for a *user*, not a press
     release. Specs right (versions/platforms); drop the gloss + the internals.
     This block becomes the GitHub release notes; cut_release.sh stamps it into a
     dated version on release. One line per change where you can. -->

- **Intel Mac support.** A native Intel (x86_64) `.dmg` now ships alongside the
  Apple Silicon one — grab the build for your chip. Both are signed + notarized.
- **Snapcast removed.** It only ever *controlled* an existing Snapcast server — it
  couldn't play your library to one — so it's gone. Chromecast, AirPlay 2, DLNA,
  and Sonos are unchanged.
- **Casting is opt-in.** Nothing scans your network until you turn a protocol on in
  Settings → Casting; the cast button takes you there if nothing's enabled yet.

## [0.1.4] — 2026-06-26

- **macOS support** — a signed, notarized `.dmg` with the native niceties: media
  keys & Now Playing, real window blur (honoring Reduce Transparency), a native
  menu bar + Dock menu, integrated titlebar, notifications, and launch-at-login.
- An interrupted download no longer wedges the queue; on macOS the mini-player
  lands bottom-right and the app shows "jellytoast", not "Python".

## [0.1.3] — 2026-06-21

- **Universal-Linux AppImage** — one self-contained file that runs on any modern
  distro with no install and no root (it bundles its own mpv).
- **"Try a demo"** on the sign-in screen — explore jellytoast against a public,
  read-only server with one click, no server of your own needed.

## [0.1.2] — 2026-06-20

- **The Linux `.deb` now launches on X11 / XWayland** (it was missing part of the
  Qt xcb library closure — this also affected 0.1.0 and 0.1.1).
- Fixed a Jellyfin crash on tracks with an unknown duration; downloads no longer
  follow you across a sign-out / server switch; internet radio casts reliably to
  DLNA & Sonos.
- **Security:** the credential file is owner-only from the very first launch (Linux).

## [0.1.1] — 2026-06-17

- **The Linux `.deb` launches on modern Ubuntu (24.04 / 26.04)** — the v0.1.0
  package failed to start.
- Cast / AirPlay discovery no longer deadlocks on Python 3.14.
- Frosted glass + frameless chrome on GNOME and other non-KDE Wayland desktops.

## [0.1.0] — 2026-06-16

First release — a native, frosted-glass music player for Jellyfin and Subsonic /
Navidrome (Linux `.deb` + Windows).

- Two backends at full parity (Jellyfin + Subsonic / Navidrome).
- Bit-perfect, gapless playback via libmpv.
- Cast anywhere — Chromecast, AirPlay 2, DLNA, Sonos (plus a Snapcast control
  surface), with a built-in proxy for receivers the app can't reach directly.
- Real offline mode, a floating mini player, frosted-glass UI, media keys, tray,
  ListenBrainz scrobbling, smart playlists, an FFT visualizer, and Jellyfin tag editing.

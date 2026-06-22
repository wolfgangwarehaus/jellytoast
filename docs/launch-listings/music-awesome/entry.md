# Awesome-SelfHosted-Music entry → PR to Tal0na/Awesome-SelfHosted-Music-Awesome

The old slug `Tal0na/SelfHosted-Music-Awesome` redirects to
`Tal0na/Awesome-SelfHosted-Music-Awesome` (branch `main`) — target the new name.
jellytoast is a desktop Subsonic/Jellyfin **client**, so it goes under the
per-platform client files in `Servers-Clients/`: **`linux.md`** and **`windows.md`**.
(Skip `mac.md` — no verified Mac build yet.) No `CONTRIBUTING.md`; the README just
says "open a PR, follow the existing structure." Feishin / Supersonic / Aonsoku are
the closest existing entries — mirror their **full** multi-field block. End with `---`.

Schema verified 2026-06-22 against the live `Servers-Clients/linux.md`.

---

## For `Servers-Clients/linux.md`

```markdown
### 🎧 jellytoast
- **API Support:** ✅ Jellyfin / ✅ Navidrome / ✅ OpenSubsonic
- **License:** GPL-2.0-or-later
- **Open Source:** ✅ Yes
- **Price:** 🆓 Free
- **Status:** 🟢 Actively maintained

#### 🚀 Highlights
- Native PySide6/Qt6 desktop UI (not Electron), frosted-glass light/dark themes
- Bit-perfect gapless mpv playback, ReplayGain, audio visualizer
- Explicit offline downloads — per-album, per-playlist, or the whole library
- Multi-protocol casting: Chromecast / AirPlay 2 / Sonos / DLNA / Snapcast, with a built-in relay
- Floating always-on-top mini player, synced lyrics, ListenBrainz scrobbling

#### 📥 Installation (Linux)
- 🌐 [GitHub](https://github.com/wolfgangwarehaus/jellytoast)
- 📦 [Releases (.deb / AppImage)](https://github.com/wolfgangwarehaus/jellytoast/releases/latest)
- 🐍 [PyPI](https://pypi.org/project/jellytoast/) — `pipx install jellytoast`

---
```

## For `Servers-Clients/windows.md`

```markdown
### 🎧 jellytoast
- **API Support:** ✅ Jellyfin / ✅ Navidrome / ✅ OpenSubsonic
- **License:** GPL-2.0-or-later
- **Open Source:** ✅ Yes
- **Price:** 🆓 Free
- **Status:** 🟢 Actively maintained

#### 🚀 Highlights
- Native PySide6/Qt6 desktop UI (not Electron), real Windows Acrylic frosted glass
- Bit-perfect gapless mpv playback, ReplayGain, audio visualizer
- Explicit offline downloads — per-album, per-playlist, or the whole library
- Multi-protocol casting: Chromecast / AirPlay 2 / Sonos / DLNA / Snapcast, with a built-in relay
- Floating always-on-top mini player, synced lyrics, ListenBrainz scrobbling

#### 📥 Installation (Windows)
- 🛒 [Microsoft Store](https://apps.microsoft.com/detail/9PNLTPXGHN79)
- 📦 `winget install wolfgangwarehaus.jellytoast`
- 🌐 [GitHub Releases (installer / portable zip)](https://github.com/wolfgangwarehaus/jellytoast/releases/latest)

---
```

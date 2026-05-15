# Competitive audit — jellytoast vs the Navidrome / Subsonic / Jellyfin ecosystem

Date: 2026-05-15. Research conducted by spawning a web-research agent
against the Navidrome app directory + the top 8 desktop / cross-platform
peers. Spot-check the sources before quoting any specific claim — open-
source clients move fast.

This doc pairs with `docs/SPEC.md` (jellytoast's capability sheet) and
`docs/TODO.md` (where we use these findings to prioritise).

---

## Competitor snapshot

| Client | Platforms | Protocols | Last release | Status |
|---|---|---|---|---|
| **Supersonic** (Go/Fyne) | Linux / macOS / Windows | Subsonic, OpenSubsonic, Jellyfin | v0.21.1 Apr 2026 | Active — the closest architectural peer |
| **Feishin** (Electron) | Linux / macOS / Windows / Web | Subsonic, OpenSubsonic, Jellyfin, Navidrome native | v1.11.0 Apr 2026 | Active — design leader of the desktop pack |
| **Sonixd** (Electron) | Linux / macOS / Windows | Subsonic, Jellyfin | 0.15.4 Mar 2023 | **Abandoned** — replaced by Feishin |
| **Sublime Music** (GTK3) | Linux only | Subsonic | v0.12.0 Jun 2023 | **End-of-maintenance Dec 2024** |
| **Finamp** (Flutter) | Android / iOS, desktop WIP | Jellyfin only | 0.6.27 Jan 2025 | Active, mobile-first; desktop rough |
| **Strawberry** (Qt/C++) | Linux / macOS / Windows | Subsonic only (no Jellyfin) | 1.2.19 Apr 2026 | Active — heritage local-library player |
| **Tauon** (Python/SDL) | Linux / Windows (limited macOS) | Subsonic, Jellyfin, Plex, Spotify | 9.1.3 Mar 2026 | Active |
| **Symfonium** (Android) | Android (+ Auto/Wear/TV) | Everything, including local files | Active, paid | Mobile — design-bar reference, not a desktop peer |

Also-rans pulled from Navidrome's directory: Aonsoku, Saxon, Aura,
Tritone, Psysonic, Cassette, Submariner. None have meaningful share.

## A. Parity gaps — features competitors have that jellytoast doesn't

**Must-have (cost us reviews / users if left):**

- **Equalizer / DSP** — Supersonic (15-band), Symfonium (256-band PEQ
  + AutoEQ), Strawberry, Tauon. jellytoast has nothing user-facing in
  the DSP path. mpv `af=` chains make this a cheap win.
- **Smart / dynamic playlists** — Feishin (Navidrome-native),
  Symfonium, Strawberry, Tauon. Subsonic exposes the APIs;
  Jellyfin has `getInstantMix` / `getSimilar`.
- **Internet radio** — Supersonic, Tauon, Symfonium. Subsonic exposes
  `getInternetRadioStations` — small effort.
- **Artist radio / radio-from-this-song** — Supersonic markets it
  explicitly. We have shuffle-library but no seeded-recommendation queue.
- **Tag editing** — Strawberry, Tauon, Symfonium. Less critical for
  streaming-first users but a common ask.

**Nice-to-have:**

- **Visualizers** — Feishin shipped one recently; reviewers fixate.
- **Crossfade** — Sonixd, Tauon, Strawberry. mpv-side, low cost.
- **Sonos / DLNA casting** — Supersonic does uPnP/DLNA.
- **Multi-server / multi-account** — Supersonic supports primary +
  alternate hostnames (LAN vs Tailscale URL for one server).
  jellytoast holds one server config per provider kind.
- **Server-side scrobbling toggle UI** — we already auto-detect
  Navidrome; surface the "scrobbled by: server" indicator visibly.

**Niche / out-of-scope:**

- ASIO / exclusive output (Windows audiophile)
- Sleep timer
- Podcasts (out of music-only scope — skip)
- Server-side playlist import (m3u, etc.)
- CarPlay / Android Auto (mobile — skip)

## B. Differentiators — what jellytoast has that the pack doesn't

These are the genuinely uncommon features, verified against each
peer's README/docs:

1. **AirPlay 2 + Chromecast in one desktop client.** No desktop peer
   ships both. Supersonic = uPnP/DLNA only. Feishin = no casting at all
   (issues open for years). Tauon = Chromecast only. Sublime Music =
   Chromecast only (and dead).
2. **Cast proxy for unreachable servers AND offline blobs.** No
   competitor relays streams through a local HTTP proxy so Chromecasts
   can reach a server behind Tailscale / self-signed / remote. And
   nobody else serves *downloaded* tracks back out via Range-supporting
   HTTP so cast survives full offline.
3. **Mini player with KWin keep-above on Wayland.** Supersonic /
   Feishin / Tauon / Strawberry: none have a real always-on-top mini
   player. Two-mode (96px compact / 320px expanded) floating player is
   genuinely rare on Linux.
4. **Full offline graph (cascade downloads, per-quality download
   setting, prefer-server-when-online, auto-offline mode).** Supersonic
   lists offline as "eventually planned"; Feishin has offline issues
   open since 2023, closed as duplicate not implemented; Finamp has
   offline but mobile-only; Sublime Music had offline but it's dead.
   **No actively-maintained desktop peer has real offline downloads.**
   This is jellytoast's single biggest moat.
5. **Provider parity across Jellyfin + Subsonic in one codebase.**
   Supersonic also does both but leads with Subsonic feel; Feishin's
   smart-playlist features are Navidrome-only; Finamp is Jellyfin-only.
   Nobody else makes provider abstraction a first-class invariant.
6. **Dual-store credentials (keyring + AES-GCM file fallback).**
   "Boot hangs on sleepy KWallet" is a recurring complaint for both
   Supersonic and Feishin.
7. **Client-side scrobbling with offline-queue + Navidrome
   detection.** Most clients either delegate to the server (losing
   plays when offline) or run client-side without an offline queue.
8. **Live-apply accent + frosted_dark, HiDPI / fractional-scale on
   Wayland.** Feishin and Supersonic both have visible HiDPI glitches
   in 2026 issue threads.
9. **MPV under the hood (bit-perfect, gapless, ReplayGain, hardware
   decode).** Parity with the top 2 peers; differentiator against the
   long tail.

## C. Polish observations

**Where competitors are weak (we should capitalize):**

- **Feishin** has active 2026 bugs: Windows crashes, structured-lyrics
  rendering, search-bar swallowing spacebar — basic UX hygiene we
  should beat.
- **Supersonic** has open issues: DLNA cast metadata missing
  artist/album/cover, playlists failing to open, playback stopping
  mid-song, Korean character rendering, UI-scaling bound to language.
  Reliability and i18n are weak.
- **Sublime Music** is dead. The GTK/Linux-native niche has no
  maintained option except jellytoast.
- **Finamp** desktop is "in progress with limitations." If we add a
  Windows backend we have a clean shot at displaced Finamp users.
- **Sonixd** users are still on a 2-year-old build. Real "where do
  Sonixd refugees go" question that Feishin only half-answers.

**Where competitors are ahead:**

- **Symfonium** sets a UX bar nothing else touches (256-band EQ,
  AutoEQ, smart playlists, audiobook controls). Mobile-only but the
  polish budget is visibly higher.
- **Feishin** has more mature visual identity (Spotify-clone) and
  light/dark/auto theme switching — jellytoast's `frosted_dark` is
  good but the only theme.
- **Strawberry / Tauon** have much deeper local-file / tag-editing
  stories. We deliberately ignore local libraries — fine, but call it
  out clearly so heritage-player users self-select away.
- **Supersonic** has Homebrew + Flatpak + AppImage + Windows installer
  all ready. jellytoast's packaging story is still TODO.

## D. Strategic recommendations

Framed around "parity + standout differentiators":

1. **Ship offline before anyone else does.** Single largest moat.
   Supersonic and Feishin have both punted offline for 2+ years.
   Finish Phase 6 (pause/resume/retry, Wi-Fi gating, staleness flag),
   ship the offline-mode UI (mostly done as of 2026-05-15), then lead
   AppStream / Reddit posts with **"the only desktop
   Subsonic/Jellyfin client with real offline downloads."**
2. **Lead the cast pitch with the proxy.** Casting is a checkbox
   feature; cast-proxy-over-Tailscale-to-Chromecast-with-offline-blobs
   is a 30-second demo nobody else can run. Make a screen recording
   showing Chromecast playing from a Tailscale-only Navidrome with
   the laptop offline. That's the killer GIF for the README and
   Flathub page.
3. **Pick off the obvious EQ + smart-playlist parity gaps.** Both are
   cheap relative to their visibility. EQ = mpv `af=` chains + 10-15
   band UI. Smart playlists piggyback on Navidrome's API and
   Jellyfin's `getInstantMix` / `getSimilar`. Without these we lose
   the audiophile + power-user reviews.
4. **Package, package, package.** Until jellytoast is one
   `flatpak install` away, the differentiators don't matter. AUR is
   hours; Flathub is days but the screenshots bake in forever. Do
   these before the Windows backend, before scrobbling polish, before
   anything else cosmetic.
5. **Position around abandonment.** Sublime Music = dead.
   Sonixd = dead. Finamp-desktop = incomplete. Feishin has
   offline/casting holes that won't close in 2026. Clean narrative:
   *"The Linux-first, music-only, actually-finished desktop client
   for self-hosted music."* Compete with Supersonic and Feishin where
   we already win on offline + casting + Wayland polish — not with
   Symfonium (mobile, paid, EQ-obsessed) or Strawberry (local-files
   heritage).

---

## Sources

- [Navidrome apps directory](https://www.navidrome.org/apps/)
- [Supersonic GitHub](https://github.com/dweymouth/supersonic) + [open issues](https://github.com/dweymouth/supersonic/issues)
- [Feishin GitHub](https://github.com/jeffvli/feishin) — offline issue [#47](https://github.com/jeffvli/feishin/issues/47), [#1753](https://github.com/jeffvli/feishin/issues/1753); casting [#1048](https://github.com/jeffvli/feishin/issues/1048)
- [Sonixd GitHub (archived)](https://github.com/jeffvli/sonixd)
- [Finamp GitHub](https://github.com/jmshrv/finamp)
- [Sublime Music EOM announcement](https://github.com/sublime-music/sublime-music)
- [Strawberry homepage](https://www.strawberrymusicplayer.org/)
- [Symfonium homepage](https://symfonium.app/)
- [Tauon homepage](https://tauonmusicbox.rocks/)

# Launch posts — ready-to-paste drafts

Rewrite per venue (never the crosspost button). Order: directory PRs land first
(see [`README.md`](README.md)) → home-turf posts → Show HN → broader waves.
Each draft is headed with the **verified rule** for that venue. Swap `vX.Y.Z` for
the current release.

---

## Reusable one-paragraph blurb

> **jellytoast** is a free, open-source, *native* (PySide6/Qt6, not Electron) desktop
> music player for **Jellyfin** and **Navidrome / Subsonic** servers. It does bit-perfect
> gapless playback via mpv, explicit **offline downloads** (per-album, per-playlist, or
> your whole library), **casting** to Chromecast / AirPlay 2 / Sonos / DLNA / Snapcast
> (with a built-in relay for trickier networks), a floating always-on-top mini player,
> frosted-glass light/dark themes, synced lyrics, a visualizer, and ListenBrainz
> scrobbling. Linux (.deb + AppImage), Windows (Microsoft Store + winget), or `pipx install jellytoast`.

---

## Hacker News — Show HN  ·  one shot, be in the comments all day
**Rules:** title must start `Show HN:`; no hype/uppercase/version-bump framing (those get
flagged/rewritten); no solicited upvotes. Post Tue–Thu ~8–10am ET. Lead "native, not Electron".

**Title:**
```
Show HN: jellytoast – a native Qt music player for Jellyfin and Navidrome
```
**First comment (post immediately after submitting):**
```
I wanted a desktop client for my self-hosted music that wasn't an Electron app or a
browser tab, so I built jellytoast — native PySide6/Qt6, with mpv doing bit-perfect
gapless playback under the hood.

It talks to both Jellyfin and Navidrome/Subsonic (OpenSubsonic) at parity. The pieces
I cared about most: real offline downloads (a SQLite node-graph, not a dumb cache),
casting to Chromecast/AirPlay 2/Sonos/DLNA/Snapcast with a built-in relay for Tailscale
/ firewalled setups, a floating always-on-top mini player, and frosted-glass theming
that uses KWin blur on KDE and Acrylic on Windows.

Install is Microsoft Store / winget on Windows, .deb / AppImage on Linux, or
`pipx install jellytoast`. It's GPL. Happy to talk about the Qt/mpv internals, the
casting/relay design, or the offline graph — I'll be around all day.
```

---

## r/navidrome  ·  home turf (Navidrome ships no first-party GUI → client posts welcome)
**Title:**
```
jellytoast — a native (non-Electron) desktop client for Navidrome, with offline + casting
```
**Body:**
```
[reusable blurb]

I built it because I wanted something native and offline-capable for my Navidrome
library. It uses the OpenSubsonic API, so ReplayGain, synced lyrics, and the extended
metadata all work.

Repo: https://github.com/wolfgangwarehaus/jellytoast
Site + screenshots: https://wolfgangwarehaus.com/jellytoast

Full disclosure: I'm the author. Feedback welcome — especially from anyone on a
non-trivial network setup, since the casting relay is the part I'd most like to harden.
```

---

## r/selfhosted  ·  flair is REQUIRED — pick "Release"
**Rules:** flair required (use **Release** — there is *no* "Release (No AI)" flair); lead with
value not the product; author disclosure expected; a clean post often gets picked up by the
selfh.st newsletter. Include a screenshot/GIF.

**Title:**
```
jellytoast: a native desktop music player for your self-hosted Jellyfin/Navidrome
```
**Body:**
```
If you self-host music on Jellyfin or Navidrome and have wanted a real desktop player
instead of the web UI or an Electron wrapper, this might scratch the itch.

[reusable blurb]

What's actually different from the web apps / existing clients:
- Native Qt + mpv — light, fast, bit-perfect gapless, not a Chromium tab.
- Offline downloads you control (album / playlist / whole library), survives a server
  outage or travel.
- Casts to 5 protocols with a built-in relay that fixes Tailscale / firewalled
  Chromecast/AirPlay setups.
- Floating mini player + media keys + frosted-glass theming.

Install: Microsoft Store / `winget install wolfgangwarehaus.jellytoast` on Windows;
.deb / AppImage on Linux; `pipx install jellytoast` anywhere. GPL, source on GitHub.

Full disclosure: I built it. Happy to answer anything in the comments.
```

---

## forum.jellyfin.org → Client Development board  ·  the Jellyfin venue
**Rules:** r/jellyfin is permanently read-only — this forum (Development → **Client
Development**) is where third-party clients announce (peers there: JellyTunes, HifiMule).
No blanket self-promo ban; just no off-topic spam. Register, New Thread in that board.

**Title:**
```
jellytoast — a native Qt6 music client for Jellyfin (offline, casting, mini player)
```
**Body:**
```
Sharing a third-party client I've been building: jellytoast, a native PySide6/Qt6
desktop music player with first-class Jellyfin support (it treats Jellyfin and
Subsonic/Navidrome at parity).

[reusable blurb]

Jellyfin-specific notes: it authenticates against your Jellyfin server, supports
multiple music libraries/views, and keeps the same offline + casting feature set on
Jellyfin as on Subsonic. It's music-only by design — no TV/movie views.

Source (GPL): https://github.com/wolfgangwarehaus/jellytoast
Downloads + screenshots: https://wolfgangwarehaus.com/jellytoast

Feedback and bug reports very welcome.
```

---

## Lemmy  ·  selfhosted@lemmy.world · navidrome@discuss.tchncs.de · jellyfin@lemmy.world
Reuse the **r/selfhosted** body (trim to ~2 short paragraphs). Headline must match the
post title; link to the site, don't paste the whole README. Disclose authorship.

---

## Discords  ·  post ONCE in the right channel, check #rules first
Reusable intro for the **#showcase / #self-promo / projects** channel (never general chat):
```
👋 I built jellytoast — a free, native (Qt, not Electron) desktop music player for
Jellyfin and Navidrome/Subsonic. Offline downloads, casting to Chromecast/AirPlay 2/
Sonos/DLNA/Snapcast, a floating mini player, gapless mpv playback, synced lyrics.
Microsoft Store / winget / .deb / AppImage / pipx.
Repo: https://github.com/wolfgangwarehaus/jellytoast · https://wolfgangwarehaus.com/jellytoast
Would love feedback from people running these servers!
```
- **Sonixd / Feishin Discord** — `discord.gg/FVKpcMDy5f` (your exact audience; be collegial,
  it's the peer/competitor server — introduce it as another FOSS option, credit mpv/lyrics overlap).
- **/r/SelfHosted Chat** — `discord.gg/UrZKzYZfcS` (projects channel).
- **Navidrome Discord** — `discord.gg/xh7j7yF` (showcase/off-topic; confirm the invite resolves).
- **Homelab Discord** — `discord.gg/homelab` (showcase channel; secondary).
- ⚠️ **Jellyfin Discord** — needs mod permission for self-promo (no #showcase); prefer the forum.

---

## Mastodon / Fediverse  ·  your own account
```
Released jellytoast 🍞 — a free, native (Qt, not Electron) desktop music player for
your self-hosted #Jellyfin and #Navidrome libraries.

Offline downloads · casting to Chromecast/AirPlay 2/Sonos/DLNA/Snapcast · floating
mini player · gapless mpv · synced lyrics.

Microsoft Store / winget / .deb / AppImage / pipx.
https://wolfgangwarehaus.com/jellytoast

#SelfHosted #FOSS #Linux #Music
```

---

## Newsletter / tip-line one-liners  ·  your own account/email
- **OMG! Linux / OMG! Ubuntu** — tip form: "New native Qt music client for self-hosted
  Jellyfin/Navidrome, now on Linux via .deb + AppImage" + the site link.
- **selfh.st** — covered by the email in [`README.md`](README.md); the listing auto-feeds the newsletter.
- **Changelog News** — submit the repo as a new-project tip.

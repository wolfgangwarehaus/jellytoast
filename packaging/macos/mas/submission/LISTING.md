# jellytoast — App Store Connect listing (macOS)

Draft listing copy for the Mac App Store submission. Every field below respects
Apple's current App Store Connect limits (verified 2026-06-24): **Name** ≤30,
**Subtitle** ≤30, **Promotional Text** ≤170, **Description** ≤4000, **Keywords**
≤100 (comma-separated, no spaces). Character counts are shown in `[brackets]`.

Branding rule: always lowercase **jellytoast** — never "JellyToast"/"Jellytoast".

---

## App Name  `[10 / 30]`

```
jellytoast
```

Keep the App Name to the bare brand. Do **not** put "Jellyfin"/"Subsonic"/
"Navidrome" in the Name or Subtitle — that is the 5.2.5 trademark trap. (If you
later want keyword weight in the title, the safe expansion is
`jellytoast: Music Player` `[24/30]` — still no third-party marks.)

## Subtitle  `[30 / 30]`

```
Player for Jellyfin & Subsonic
```

Referential ("Player **for**…") is the allowed phrasing — it describes
compatibility, it does not claim the marks. This is the one place the marks earn
their keyword value. Alternates if you'd rather not spend the marks here:
- `Self-hosted music, done right` `[29/30]`
- `Music for your own server` `[25/30]`

## Promotional Text  `[168 / 170]`

```
Stream your own Jellyfin or Navidrome library. Real offline downloads, casting to five protocols, a floating mini-player, gapless playback, and ListenBrainz scrobbling.
```

(Promotional Text is editable any time without resubmitting — use it for release
beats, e.g. "New in 1.x: …".)

## Description  `[2473 / 4000]`

```
jellytoast is a free, open-source, fully native music player for your own self-hosted Jellyfin or Subsonic/Navidrome server. You connect to a server you control and stream your own library — there is no store, no subscription, no catalog of its own, and nothing to buy. One app talks to both Jellyfin and Subsonic/Navidrome at full feature parity, so you are never locked to a single backend.

Built with Qt and libmpv (not a web wrapper), jellytoast is fast, light, and bit-perfect.

WHAT MAKES IT DIFFERENT

• True offline downloads — pick an album, a playlist, or your whole library and keep it on your Mac. Downloads are tracked in a real SQLite node-graph, not a throwaway cache, so they survive a server outage, a flaky connection, or travel.

• Five-protocol casting — send playback to Chromecast, AirPlay, DLNA, Sonos, or Snapcast. A built-in cast proxy relays audio to devices your network can't normally reach and serves your offline downloads to them too — handy on VPN, Tailscale, or firewalled setups.

• Floating mini-player — a compact, always-on-top window that stays visible over your other apps for at-a-glance control.

• Two servers, one app — Jellyfin and Subsonic/Navidrome are supported side by side, identically. Most clients speak only one protocol.

EVERYTHING ELSE YOU'D EXPECT

• Bit-perfect, gapless playback through libmpv
• Built-in equalizer and ReplayGain volume leveling
• ListenBrainz scrobbling
• Synced lyrics and an audio visualizer
• Native frosted-glass light and dark themes
• Smart playlists, radio, and fast library browsing
• Media-key and Now Playing integration

PRIVACY

jellytoast connects only to the server you tell it to. There are no ads, no trackers, and no accounts with us — your music and your listening stay between you and your own server.

GETTING STARTED

Point jellytoast at your Jellyfin or Subsonic/Navidrome server URL, sign in with your server account, and your library appears. If you don't run a server yet, both Jellyfin and Navidrome are free, open-source, and self-hostable.

OPEN SOURCE

jellytoast is GPL-licensed. Source, issues, and documentation live at https://wolfgangwarehaus.com/jellytoast.

jellytoast is an independent project. It is not affiliated with, endorsed by, or sponsored by the Jellyfin, Subsonic, or Navidrome projects. "Jellyfin", "Subsonic", and "Navidrome" are used only to describe server software jellytoast is compatible with, and are the property of their respective owners.
```

Why it's shaped this way:
- **Leads with the moats** (offline graph, 5-protocol cast + proxy, mini-player,
  dual-backend) before the table-stakes features — this is the 4.3 anti-duplicate
  differentiation, front-loaded.
- **Trademarks are referential only** ("for", "compatible with") and the closing
  paragraph is the explicit **not-affiliated/not-endorsed disclaimer** that 5.2.5
  reviewers look for.
- States **free / no IAP / no storefront / no own content** up top — pre-empts the
  3.1.1 and "what does this app actually do" questions.

## Keywords  `[97 / 100]`

```
jellyfin,navidrome,subsonic,music,player,offline,cast,airplay,chromecast,sonos,dlna,scrobble,flac
```

Notes:
- No spaces after commas (spaces waste the 100-char budget; Apple recommends
  none).
- Don't repeat words already in the Name/Subtitle if you can avoid it — but the
  server marks are worth keeping here for discovery, and Apple permits referential
  use of compatible-product names in keywords.
- Keywords is the **only** field editable between releases without a new
  submission — iterate on it post-launch from App Analytics.

## URLs

| Field | Value |
|---|---|
| **Support URL** (required) | `https://wolfgangwarehaus.com/jellytoast` (or `https://github.com/wolfgangwarehaus/jellytoast/issues`) |
| **Marketing URL** (optional) | `https://wolfgangwarehaus.com/jellytoast` |
| **Privacy Policy URL** (required) | needs a hosted privacy page — see open questions |

## Categories

| Field | Value |
|---|---|
| **Primary Category** | **Music** |
| **Secondary Category** | **Entertainment** (optional; or leave blank — a precise secondary slightly dilutes the primary) |

## Age Rating answers

Target rating: **4+** (no objectionable content of its own — it's a player for
the user's own library). Answer the App Store Connect age-rating questionnaire as:

- Cartoon or Fantasy Violence — **None**
- Realistic Violence — **None**
- Sexual Content or Nudity — **None**
- Profanity or Crude Humor — **None**
- Alcohol, Tobacco, or Drug Use or References — **None**
- Mature/Suggestive Themes — **None**
- Horror/Fear Themes — **None**
- Medical/Treatment Information — **None**
- Gambling (simulated) — **None**
- Contests — **None**
- **Unrestricted Web Access** — **No** (it does not embed a general-purpose
  browser; it connects only to user-configured media servers). ⚠️ See open
  questions — confirm there is no in-app web view that loads arbitrary URLs.
- **User-Generated Content / does the app allow users to access UGC?** — the app
  streams content from a server the *user themselves* hosts; it is not a UGC
  platform with sharing/discovery between strangers. Answer **No** to the
  Apple-mediated UGC questions. (If asked about content the app *displays*, note
  in Review Notes that all content comes from the user's own private server.)

Net result with the above answers: **4+**.

## App Privacy ("nutrition label")

jellytoast does not collect data for itself. In the **App Privacy** section,
declare **"Data Not Collected"** — *provided* the build sends no analytics/crash
telemetry to you or a third party. Caveats to verify before you certify this:
- ListenBrainz scrobbling sends listening data to **the user's** ListenBrainz —
  that's a user-configured third-party service, opt-in, not data *you* collect; it
  does not need to be declared as your collection, but you may mention it in Review
  Notes for clarity.
- The app talks to the user's own server (not a third party you operate).
- If any crash/analytics SDK is present, that flips the answer — confirm none ships.

## Review Notes (App Review → App Information → Notes) — paste this

```
jellytoast is a music player for self-hosted Jellyfin and Subsonic/Navidrome
servers. It has no content of its own — the user signs into a server they
control and streams their own library. It is free, with no in-app purchases
and no subscriptions.

DEMO SERVER (for 2.1 review):
  Server type: <Jellyfin | Navidrome>
  URL: <https://demo.example.com>
  Username: <reviewer>
  Password: <password>
On first launch, choose "<Jellyfin/Subsonic>", enter the URL above, and sign
in with the credentials. The demo library will load; you can browse, play,
download for offline, and open the casting menu.

"Jellyfin", "Subsonic", and "Navidrome" are third-party open-source server
projects. jellytoast is an independent client, not affiliated with or endorsed
by them; the names are used only to describe compatibility.

Source code (GPL): https://github.com/wolfgangwarehaus/jellytoast
```

## "What's New" template (per release)

Keep it factual — no "bug fixes and improvements" filler, no version-bump hype
(Apple discourages it). Pattern:

```
What's new in <X.Y.Z>:

• <Headline feature or fix, user-facing benefit first>
• <Second item>
• <Third item>

Thanks for the feedback on GitHub — keep it coming:
https://github.com/wolfgangwarehaus/jellytoast/issues
```

First-release variant:

```
Welcome to jellytoast on the Mac App Store! A native music player for your own
Jellyfin or Navidrome server: offline downloads, five-protocol casting, a
floating mini-player, gapless playback, and ListenBrainz scrobbling.
```

---

## Field summary (copy-paste cheat sheet)

| Field | Value | Count |
|---|---|---|
| Name | `jellytoast` | 10/30 |
| Subtitle | `Player for Jellyfin & Subsonic` | 30/30 |
| Promotional Text | (see above) | 168/170 |
| Description | (see above) | 2473/4000 |
| Keywords | `jellyfin,navidrome,subsonic,music,player,offline,cast,airplay,chromecast,sonos,dlna,scrobble,flac` | 97/100 |
| Primary Category | Music | — |
| Secondary Category | Entertainment (optional) | — |
| Age Rating | 4+ | — |
| Support URL | https://wolfgangwarehaus.com/jellytoast | — |
| Marketing URL | https://wolfgangwarehaus.com/jellytoast | — |

Sources for the field limits: [App Store Connect Character Limits (2026)](https://www.appconnecttranslate.com/tools/app-store-character-limits/), [Apple — App information reference](https://developer.apple.com/help/app-store-connect/reference/app-information/app-information/), [Apple — Creating Your Product Page](https://developer.apple.com/app-store/product-page/).

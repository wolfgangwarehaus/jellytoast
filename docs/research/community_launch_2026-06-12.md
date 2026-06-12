# Community launch playbook — research (2026-06-12)

> **Status: research / pre-launch plan — execute after v0.1.0 ships.**
> Verified June 2026. Reddit blocks direct rule-fetching — re-read each
> sub's sidebar immediately before posting.

## The one that matters most: Navidrome's client catalog

navidrome.org/apps/ — submission is a **PR to github.com/navidrome/website**
(process: navidrome.org/docs/developers/adding-apps/):

- Create `assets/apps/jellytoast/` with `index.yaml` + images.
- `index.yaml`: `name`, `url` (→ wolfgangwarehaus.com), `platforms:
  [linux, windows]`, `api: OpenSubsonic`, 1–2 sentence `description`,
  `repoUrl` (powers a "last updated" badge fed by GitHub releases — tag
  v0.1.0 FIRST), `isOpenSource: true`, `isFree: true`, ≤6 `keywords`,
  `screenshots.thumbnail` + `gallery`.
- Images: **WebP only, ≤1200×1200, gallery ≤500KB each, thumbnail must
  be a real UI screenshot (not the logo)**. Their repo has
  `npm run convert:images` and `npm run validate:app` — run the
  validator before the PR. Criteria beyond that: just working
  Subsonic/OpenSubsonic support. No review gauntlet.
- Announce alongside: r/navidrome (official, active, client posts are
  normal content), Navidrome Discord (discord.gg/xh7j7yF — devs
  present), and the official Lemmy community
  (discuss.tchncs.de/c/navidrome).

## Jellyfin side

- **Clients page**: PR to github.com/jellyfin/jellyfin.org editing
  `src/data/clients.ts`. Criteria (jellyfin-meta discussion #27): no
  piracy, dev in good standing, Jellyfin support first-class (true —
  co-equal provider), no promoting a hosted instance, clear license.
  ⚠️ Review their **branding guidelines** before submitting — the
  "jelly" name riff may draw comment; riff-named clients do exist.
- **r/jellyfin is permanently READ-ONLY** (2023 API protest). The
  community lives at **forum.jellyfin.org** — announce there
  (Development/Clients area) — plus their Matrix/Discord.
- **awesome-jellyfin** (github.com/awesome-jellyfin/awesome-jellyfin):
  PR editing **`clients.yaml`** (CLIENTS.md is generated — don't touch).
  10 minutes.

## Subreddits

| Sub | Verdict | Notes |
| --- | --- | --- |
| r/selfhosted (~760k) | **Must** | "I built this" is core content WITH disclosure; 90/10 participation ratio; frame as "client for your self-hosted Navidrome/Jellyfin"; check current flair rules |
| r/navidrome | **Must** | official, welcoming to new clients |
| r/musichoarders (~100k) | **Must** | OC tooling with screenshots does well; zero piracy talk |
| r/linux (~1.9M) | **Must — only after packaging exists** | release flair; native-Qt+GPL+screenshots lands; this crowd bounces hard off build-from-source |
| r/kde | Nice — strong fit | lead with KWin blur / Wayland-native / Qt polish |
| r/opensource | Nice | self-promo OK with license link + authorship disclosure |
| r/linuxaudio | Nice — angle only | pro-audio crowd; the bit-perfect/ALSA-direct story is the single hook |
| r/jellyfin | **Skip — read-only** | use forum.jellyfin.org |
| r/archlinux | Skip | no announcements; the AUR package IS the Arch channel |
| r/audiophile | Skip | bans self-promo outright |
| r/DataHoarder, r/degoogle | Skip | off-core |

Cross-post etiquette: stagger 1–2 weeks, rewrite title/body per
community (never the crosspost button), answer every comment fast on
day one.

## Beyond Reddit

- **Show HN** — one shot; only when install is one command
  (pipx/Flatpak) and the README is screenshot-rich. Title `Show HN: …`;
  be present in comments. The "native, not Electron" framing reliably
  resonates (most competing Subsonic desktop clients are Electron).
- **Lemmy** — selfhosted@lemmy.world (very FOSS-friendly, often warmer
  than Reddit), linux@lemmy.ml, + the official Navidrome community.
- **Mastodon/Fosstodon** — screenshot+GIF thread, tags
  #linux #selfhosted #foss #jellyfin #navidrome #musicplayer.
- **forum.jellyfin.org** — the r/jellyfin replacement.
- **LinuxMusicians** — "Linux Music News" board; modest reach (skews
  production). Nice.
- **KDE Discuss** — fine for a general post; **apps.kde.org is
  KDE-projects-only** (needs KDE Incubator) — skip. This Week in KDE
  covers KDE-repo work only — skip.
- **selfh.st** (Self-Hosted Weekly) — the de-facto selfhosted
  newsletter; sources from r/selfhosted, so a good post there often
  gets picked up automatically.
- **alternativeto.net** — submit (account must be ≥1 week old — create
  it NOW); long-tail SEO as "Feishin/Sonixd alternative". Nice.
- **awesome-selfhosted** — server software only; a client is out of
  scope. Skip. **OpenHub/AlternativeOSS** — moribund. Skip.
  **LinuxLinks** — suggest via contact form for their "best music
  players" roundups. Low effort, nice.

## Strategy (consistent across what we found in the wild)

1. **Packaging before posting** — the #1 desktop-app launch failure is
   "looks great, won't build from source."
2. **Show, don't tell** — 2–3 screenshots + a short GIF (now-playing +
   blur + cast menu) lead every post; the cast-proxy demo clip idea in
   TODO pairs perfectly.
3. **Directories first, social second** — land the Navidrome catalog +
   jellyfin.org + awesome-jellyfin PRs BEFORE the Reddit/HN wave so the
   listings exist when people go looking.
4. **One hook per channel** — bit-perfect → r/linuxaudio; KDE polish →
   r/kde; offline+cast → r/selfhosted; native-not-Electron → HN.

## Launch sequence

1. Tag v0.1.0; AUR + pipx live; screenshots shot as 1200px WebP masters
   (same set feeds Flathub, the Navidrome catalog, and the posts).
2. PRs: navidrome/website → jellyfin.org `clients.ts` → awesome-jellyfin.
3. Announce home turf: r/navidrome + Navidrome Discord + forum.jellyfin.org.
4. r/selfhosted + Lemmy selfhosted + Mastodon.
5. r/linux, r/kde, r/musichoarders — staggered, rewritten.
6. Show HN once Flathub is live (install friction at minimum).
7. Cleanup: alternativeto (create the account now — 1-week age gate),
   LinuxLinks suggestion.

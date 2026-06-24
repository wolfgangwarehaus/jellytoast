# Demo server for App Review — setup & operations plan

How App Review (and **every future update review**) gets a working server to point jellytoast at. jellytoast is a **client**: it has no content of its own, so an empty/unreachable server makes the app look non-functional → a near-certain **Guideline 2.1 (App Completeness)** rejection. This document picks the server, seeds it with legally-clean music, locks down a stable login, keeps it **up for the whole review window**, and specs an optional in-app **demo mode** as a belt-and-suspenders fallback.

> Branding is always lowercase **jellytoast**. Bundle ID: `io.github.wolfgangwarehaus.jellytoast`.

---

## TL;DR — the recommendation

1. **Stand up our own always-on server.** A small VPS running **Navidrome** is the primary review target: tiny footprint, music-only by nature, and we fully control credentials + uptime + content. (jellytoast speaks both protocols, but Navidrome/Subsonic is the lightest thing to host; optionally also run a **Jellyfin** instance to exercise that code path — see §5.)
2. **Do NOT rely on the public demos as the primary target.** Use `demo.navidrome.org` / `demo.jellyfin.org` only as a documented *fallback*. We don't control their uptime, content, or credentials, and the Jellyfin public demo is **movie/TV-focused** — wrong for a music-only client (§3).
3. **Seed it with CC0 / public-domain music** (Pixabay, Free Music Archive CC0, Musopen) so there is zero copyright exposure on a publicly-reachable server (§4).
4. **Put the credentials in App Store Connect → App Review Information**, not only in the binary. Keep the server up from submission through final approval, and leave it up permanently for update reviews (§6).
5. **Ship a built-in "demo mode"** that one-tap fills the demo server — Apple explicitly accepts built-in demo modes (§7). Doubles as a nice first-run experience for real users.

## 1. Why this matters (the failure mode)

Apple's reviewer guidance: *"include demo account info (and turn on your back-end service!) if your app includes a login."* jellytoast's first screen **is** a login. No populated server → empty grids → looks broken → **2.1 App Completeness** rejection, every cycle. This recurs on **every update** (updates are re-reviewed and the login re-tested), so the server is **standing infrastructure**, not a one-time spin-up.

## 2. Option A (RECOMMENDED) — self-host an always-on server on a small VPS

**2a. Navidrome on a tiny VPS — primary target.** Music-only by design, single Go binary (~100–250 MB RAM), exposes the Subsonic API jellytoast already supports. Smallest tier is plenty: **1 vCPU / 1 GB / ~20–25 GB** (Hetzner CX22, a $5–6/mo DO/Vultr/Linode droplet, or Scaleway Stardust). Deploy via Docker:

```yaml
services:
  navidrome:
    image: deluan/navidrome:latest
    restart: unless-stopped          # survives reboots → uptime for review
    ports: ["0.0.0.0:4533:4533"]
    environment:
      ND_SCANSCHEDULE: 1h
      ND_SESSIONTIMEOUT: 24h
    volumes:
      - "./data:/data"
      - "./music:/music:ro"          # drop the CC library here
```

Put it behind **Caddy** (auto-Let's-Encrypt) on `demo-music.<our-domain>` so the reviewer gets HTTPS (avoids ATS/mixed-content surprises). Create exactly **one** account, `appreview`; keep the music volume `:ro`; don't expose admin.

**2b. (Optional) also run Jellyfin** to demonstrate the **both-protocols** moat in one review — same `./music` mounted read-only, a single **Music** library (no movie/TV), one `appreview` user.

| Pro | Con |
|---|---|
| We own uptime → 2.1 fully de-risked | ~$5–7/mo standing cost, indefinitely |
| Fixed credentials we control | Must monitor it (down during review = rejection) |
| Music-only, curated, legally clean | A little ops (renewals, security patches) |
| Same server serves real "try it" users + screenshots | Public URL is a small attack surface |

## 3. Option B — public demos (FALLBACK ONLY)

- **Navidrome:** `https://demo.navidrome.org`, login `demo` / `demo`. *Settings disabled.* Works as a Subsonic smoke test; third-party uptime/content/creds we don't control.
- **Jellyfin:** `https://demo.jellyfin.org/stable`, user `demo` (no password), **resets hourly**. **Showstopper:** curated around **movies/TV**, not a guaranteed music library — wrong content for a music-only client.

**Verdict:** acceptable only as a secondary line in the review notes; never the sole target. Don't offer the Jellyfin public demo for the music flow.

## 4. Seeding with legally-clean music

A public server = content we effectively publish → use **CC0 / public-domain** audio only.

**Sources (in order):** 1) **Pixabay Music** (all CC0, no attribution) — `pixabay.com/music`; 2) **Free Music Archive — CC0 curator** (filter to CC0) — `freemusicarchive.org`; 3) **Musopen** (public-domain classical, great metadata/art) — `musopen.org`; 4) **ccMixter** (filter CC0) — `ccmixter.org`.

**Licensing safety rules:** default to **CC0 / public domain** (nothing to attribute). If using **CC BY**, keep a `CREDITS.md` listing author + source + license per track. **Avoid CC BY-SA** (share-alike complicates redistribution) and **avoid CC BY-NC entirely** ("non-commercial" is murky for an App Store submission). **Verify each track's license at download** (FMA mixes licenses per track). **Embed real ID3 tags + cover art** so the demo exercises grids, artist pages, A-Z rail, genres, and the cover pipeline.

**Shape:** ~8–15 albums across ~5–8 artists and a few genres (a few GB) — enough that every view looks populated. Include a multi-disc album and a various-artists compilation for edge cases. Record exact provenance in `submission/demo-library/MANIFEST.md`.

## 5. Which protocol(s) in the review notes

Primary: self-hosted **Navidrome** (Subsonic). Secondary (recommended): self-hosted **Jellyfin** to show the both-protocols moat. Give the reviewer **one** flow that just works (the demo-mode button, §7) and list manual URLs/creds as backup.

## 6. Stable login + uptime

One read-only `appreview` account per server, fixed URL + **non-rotating** password (it guards nothing — a public CC0 jukebox). **Enter it in App Store Connect → App Review Information → Sign-In Information**; use **Notes** for fallback public URLs, a "tap *Try the demo server* to auto-fill" line, and the independent/not-affiliated disclaimer. Any auth code must be supplied **in advance** in Notes.

**Keeping it UP (a down server = guaranteed 2.1 rejection):** `restart: unless-stopped` + Docker-on-boot; an external **uptime monitor** (UptimeRobot/BetterStack/Healthchecks.io) hitting the login URL every 1–5 min, alerting the developer; keep it up **indefinitely** (every update is re-reviewed); ensure the **VPS won't auto-suspend/auto-delete** for inactivity (auto-pay on — same footgun class as the Scaleway "Deletable from" surprise); pin server software to a known-good tag (no surprise `:latest` update right before a review).

## 7. Built-in "demo mode" (RECOMMENDED — Apple-blessed)

Apple's guidance explicitly allows a **built-in demo mode** "in lieu of a demo account" (with prior approval), and a one-tap "connect to the demo" is the smoothest reviewer experience even when we also supply an account. Add a **"Try the demo server"** affordance on the connect screen that **auto-fills** our demo URL + `appreview` creds and connects via the **normal provider/auth path** (so the reviewer exercises the real app and the both-providers claim is genuinely demonstrated). Keep the endpoint/creds in a small config constant or tiny remote JSON so we can **repoint the demo server without an app update**. Demo mode must show **full functionality on real (demo) data** — and is **not** a substitute for the App Store Connect Sign-In fields (do both).

## 8. Pre-submission checklist

- [ ] VPS up; Docker compose running with `restart: unless-stopped`.
- [ ] Navidrome reachable over **HTTPS** at the demo subdomain; login works.
- [ ] (Optional) Jellyfin over HTTPS; single **Music** library only.
- [ ] CC0/PD library seeded; **every view looks populated**.
- [ ] `submission/demo-library/MANIFEST.md` lists source + license per track; CC-BY credited.
- [ ] `appreview` account per server; password fixed + documented.
- [ ] Credentials in **App Store Connect Sign-In Information**; **Notes** include fallback URLs + not-affiliated disclaimer.
- [ ] In-app **"Try the demo server"** button auto-fills + connects (tested on a clean machine).
- [ ] **Uptime monitor** live + alerting; host **auto-suspend/delete disabled**, auto-pay on; server pinned to a known-good tag.
- [ ] Plan to **keep all of this standing** for future update reviews.

## Sources
- [Apple — App Review Guidelines (2.1; demo accounts & built-in demo mode)](https://developer.apple.com/app-store/review/guidelines/)
- [Apple — App Review tips (demo account info, turn on your back-end)](https://developer.apple.com/distribute/app-review/)
- [Navidrome — Demo (demo/demo, settings disabled)](https://www.navidrome.org/demo/)
- [Jellyfin — public demo discussion (demo.jellyfin.org/stable, user demo, hourly reset)](https://github.com/orgs/jellyfin/discussions/9428)
- [Free Music Archive — Creative Commons curator + License Guide](https://freemusicarchive.org/curator/Creative_Commons/)
- [Pixabay Music — CC0 search](https://pixabay.com/music/search/cc0/)
- [Musopen — public-domain music](https://musopen.org/)

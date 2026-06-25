# App Review Information — Mac App Store (paste-ready)

Exact text for App Store Connect → (version) → **App Review Information**.

**Demo server VERIFIED 2026-06-25:** the official public Jellyfin demo is up
(Jellyfin 10.11.11, "Stable Demo"), has a **Music** library (4 albums / 5 tracks),
user `demo`, **no password**, and jellytoast connects to it (it's the same server
the dev-test session used — the "Promo" / "Thraximundar" tracks).

## Sign-In Information
- **Sign-in required:** yes
- **User Name:** `demo`
- **Password:** the demo account has none. Leave blank if App Store Connect allows;
  if it forces a value, put `demo` and rely on the Notes (which tell the reviewer to
  leave the app's password field empty).

## Contact Information
- **Name:** August Mueller (legal: William August Mueller)
- **Email:** augustvontrips@gmail.com
- **Phone:** `<your phone, E.164>`

## Notes  (≤ 4000 bytes — paste verbatim)

```
jellytoast is a music player for self-hosted Jellyfin and Subsonic/Navidrome servers. It has no content of its own — the user signs into a server they control and streams their own library. It is free, with no in-app purchases and no subscriptions.

DEMO SERVER (for 2.1 review) — the official public Jellyfin demo:
  Server type: Jellyfin
  Server URL:  https://demo.jellyfin.org/stable
  Username:    demo
  Password:    (none — leave the password field empty)

To test: open jellytoast, choose "Jellyfin", enter the URL above, type "demo" as the username, leave the password blank, and connect. The Music library loads — browse albums, play a track, download one for offline, and open the casting menu to see the device picker.

WHY THE NETWORK-SERVER ENTITLEMENT: jellytoast runs a small local HTTP server (a "cast proxy") so it can relay audio to Chromecast / AirPlay / DLNA / Sonos / Snapcast receivers on the user's own local network, and serve offline downloads to them. It listens on the local network only and serves only the user's own media. The macOS Local Network prompt is for this optional casting only — a reviewer may deny it and sign-in, browsing, and playback all still work (the demo server is reached over the internet, not the LAN).

"Jellyfin", "Subsonic", and "Navidrome" are third-party open-source server projects. jellytoast is an independent client, not affiliated with or endorsed by them; the names describe compatibility only.

Source code (GPL): https://github.com/wolfgangwarehaus/jellytoast
```

## App Sandbox entitlements (the ➕ "App Sandbox Information" — Optional)
Apple reads these from the build; pre-declaring is optional but smooths review of the
network-server one. Our five:
- `com.apple.security.app-sandbox` — the sandbox itself
- `com.apple.security.network.client` — connect to the user's Jellyfin/Subsonic/Navidrome server
- `com.apple.security.network.server` — local cast proxy (LAN only) — justified in Notes
- `com.apple.security.files.user-selected.read-write` — a folder the user picks for offline downloads
- `com.apple.security.files.bookmarks.app-scope` — persist that folder across launches

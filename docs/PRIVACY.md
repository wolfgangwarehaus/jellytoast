# jellytoast Privacy Policy

jellytoast is a desktop music player. The developer collects nothing — there is
no analytics, telemetry, crash reporting, or advertising, and no data is ever
sent to the jellytoast developer. The app communicates only with the servers and
services **you** configure (your media server, and any optional features you turn
on), as described below.

## What the app stores locally on your device

- Your Jellyfin or Navidrome server address and login credentials — stored in
  your system keychain (Windows Credential Manager on Windows,
  libsecret / KWallet on Linux). Never uploaded anywhere.
- Playback queue, settings, and download cache — stored in your user profile on
  this device only (`%LOCALAPPDATA%\jellytoast` on Windows,
  `~/.local/share/jellytoast` on Linux).

## Network connections

jellytoast connects to the media server URL you configure to stream your music.
No data is sent to the app developer. Optional cast features (Chromecast, DLNA,
AirPlay, Sonos, Snapcast) connect directly to devices on your local network.

## Third-party services (only when you enable them)

Some optional features contact services other than your media server, and only
when you turn them on or use them. None of this data ever reaches the jellytoast
developer:

- **Scrobbling** — if you connect a scrobble account, jellytoast sends your
  playback history (artist, track, album, timestamp) to the service you choose:
  ListenBrainz (`api.listenbrainz.org`) and/or Last.fm (`last.fm`), under that
  service's own privacy policy and your account there.
- **Internet-radio cover art** — when you play an internet-radio station,
  jellytoast may look up cover art by sending the current artist and track to
  MusicBrainz (`musicbrainz.org`) and the Cover Art Archive
  (`coverartarchive.org`).

## No analytics, no telemetry, no advertising

jellytoast contains no analytics, crash reporting, telemetry, or advertising
SDKs of any kind.

## Open source

The full source code is available at:
https://github.com/wolfgangwarehaus/jellytoast

Questions? Open an issue at:
https://github.com/wolfgangwarehaus/jellytoast/issues

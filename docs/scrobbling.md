# Scrobbling — Research & Design

> **📍 Status — 2026-05-20:** Shipped. The scrobble subsystem landed
> 2026-05-15 — ListenBrainz works. **Last.fm is parked:** the client
> code is built and dormant, but registering the in-app API key needs
> a Last.fm account and their signup firewall kept blocking it, so the
> Settings section is hidden until the credentials are populated (see
> `docs/TODO.md` → Parked). The Last.fm design below is kept for
> rationale and for whoever revisits it.

Status: design, 2026-05-14. Decision: **client-side scrobbling in
jellytoast** (research Option B), unified across both providers.
Nothing implemented yet — this doc is the plan.

## 1. Goal

Send "now playing" + completed-play ("scrobble") events to **Last.fm**
and **ListenBrainz** directly from jellytoast, configured in-app, so it
works **identically on Jellyfin and Subsonic/Navidrome** — no server
admin, no per-provider divergence (the provider-parity principle).

A user who prefers their server to scrobble (Navidrome can, natively)
just leaves jellytoast's scrobbling off — see §7, the double-scrobble
hazard.

## 2. Why client-side (the research)

| Path | Works today? | Problem |
|---|---|---|
| Navidrome server-side | **Yes** — jellytoast already calls the Subsonic `scrobble` endpoint (`providers/subsonic.py`); enabling Last.fm/ListenBrainz in Navidrome's own settings is all that's needed | Requires the user to configure it in Navidrome's web UI; nothing in-app |
| Jellyfin server-side | Only with an **admin-installed plugin** (`jellyfin-plugin-lastfm`, `jellyfin-plugin-listenbrainz`) — and the ListenBrainz one is admin-configured *for all users*, no per-user setup | A regular user on someone else's Jellyfin server simply can't |
| **Client-side (this doc)** | — | Has to be built — but once built it covers both providers, in-app, no admin |

Client-side is the only path that gives a consistent, self-serve
experience on both backends. The Subsonic `scrobble` calls jellytoast
already makes stay as-is (they drive Navidrome's *own* play history and
"now playing" — orthogonal to this).

## 3. The two protocols

### 3.1 Shared eligibility rule

Both services use the same rule, and it's what the `ScrobbleManager`
keys off:

> A track is scrobbled when it is **longer than 30 s** and has been
> played for **≥ 50 % of its length, or ≥ 4 minutes, whichever comes
> first**.

jellytoast's player already emits `position_updated` / `duration_set`
on `PlayerBus`, so the manager has everything it needs.

### 3.2 ListenBrainz — the easy one

- `POST {base}/1/submit-listens`, `Authorization: Token <user-token>`,
  `Content-Type: application/json`.
- `listen_type`: `playing_now` (on track start) or `single` (on
  scrobble). Body carries `track_metadata` — `artist_name`,
  `track_name`, `release_name`, plus `additional_info`
  (`recording_mbid`, `duration_ms`, …). `single` listens also need
  `listened_at` (UNIX epoch, UTC).
- `GET {base}/1/validate-token` to check a token + get the username.
- Batches up to 1000 listens/request. Base URL is configurable
  (`api.listenbrainz.org` default; users with Maloja / a self-hosted
  instance point it elsewhere — same knob Navidrome exposes).
- **No app registration, no request signing.** Just a pasted token.

### 3.3 Last.fm — the involved one

- `POST http://ws.audioscrobbler.com/2.0/`, form-urlencoded.
- **Needs a registered API account** — an `api_key` + `api_secret`,
  baked into the app (every Last.fm client does this; the "secret" in a
  desktop app isn't truly secret and Last.fm's model accepts that).
  *This is an august dependency — see §9.*
- Desktop auth (no callback URL): `auth.getToken` → open
  `https://www.last.fm/api/auth/?api_key=…&token=…` in the browser →
  user clicks "allow" → `auth.getSession` returns a **permanent
  session key**. Stored encrypted at rest.
- Every write needs an `api_sig`: sort all params by name, concatenate
  `name+value` pairs, append the secret, MD5.
- `track.updateNowPlaying` on start; `track.scrobble` on completion
  (batches up to 50). Retry only on error 11/16; error 9 = re-auth.

## 4. What's already in place

- `PlayerBus`: `playback_started(NowPlaying)`, `position_updated(ms)`,
  `duration_set(ms)`, `playback_paused/resumed`, `playback_stopped`,
  `playback_ended` — the full event surface the manager needs.
- `settings.py`: `_encrypt_token` / `_decrypt_token` (AES-GCM) — reuse
  for the Last.fm session key and ListenBrainz token at rest.
- `modules/offline/connectivity.py`: `is_server_reachable()` — the gate
  for the offline scrobble queue (§8).
- `modules.async_io.run_async` + `get_qnam()` — all network I/O goes
  through these, never raw threads.

## 5. Architecture

A new **`modules/scrobble/`** package, mirroring `modules/offline/`:

```
modules/scrobble/
  __init__.py       public API + the singleton ScrobbleManager accessor
  manager.py        ScrobbleManager — PlayerBus hook, the 30s/50%/4min
                    rule, fan-out to each enabled service
  listenbrainz.py   ListenBrainz client: validate_token, submit (now /
                    single), batch submit
  lastfm.py         Last.fm client: getToken/getSession auth flow,
                    api_sig, updateNowPlaying, scrobble (batch)
  queue.py          pending-scrobble store for offline plays;
                    flush-on-reconnect
```

**`ScrobbleManager`** — a `QObject` constructed once at startup (next to
`MpvController`), wired to `PlayerBus`:

- `playback_started(np)` → record `(np, start_wall_clock)`, send a
  now-playing ping to every enabled service.
- `position_updated(ms)` → once the eligibility threshold is crossed,
  mark the current track `eligible` (don't scrobble yet — a track only
  scrobbles once, and submitting at the threshold vs at end is a wash;
  submitting at end also catches the real `listened_at`).
- `playback_stopped` / `playback_ended` / next-track → if the
  outgoing track was `eligible` and not yet scrobbled, scrobble it.
- Pause/resume: position-based, so a paused track simply stops
  advancing — no wall-clock bookkeeping needed.

Fan-out is per-service and independent: a Last.fm failure never blocks
ListenBrainz. Network calls via `run_async`.

## 6. Settings + UI

New settings (all under a `scrobble/` QSettings prefix):

- `listenbrainz_enabled: bool`, `listenbrainz_token` (encrypted),
  `listenbrainz_url` (default `https://api.listenbrainz.org`).
- `lastfm_enabled: bool`, `lastfm_session_key` (encrypted),
  `lastfm_username` (display only).

UI: a new **"Scrobbling"** settings page (it's substantial enough — two
services, an auth flow — to not crowd Playback):

- **ListenBrainz** — a token field + "Validate" (calls
  `validate-token`, shows the resolved username), and the custom-server
  URL field.
- **Last.fm** — a "Connect to Last.fm" button that runs the browser
  auth flow (a small modal that waits for the user to authorize, then
  calls `auth.getSession` — same shape as the AirPlay pairing dialog),
  then shows "Connected as <username>" + a Disconnect.
- A **double-scrobble warning** banner (see §7).

## 7. The double-scrobble hazard

If a user has **Navidrome's own** Last.fm/ListenBrainz scrobbling on
*and* jellytoast's, every track scrobbles twice. The client can't
reliably detect Navidrome's server-side config, so this is handled with
a clear warning on the Scrobbling settings page:

> If your music server already scrobbles (e.g. Navidrome's built-in
> Last.fm / ListenBrainz integration), leave this off — otherwise every
> track is counted twice.

Documentation will also keep the "use Navidrome's own scrobbling
instead" path as a supported alternative for users who prefer it.

## 8. Offline scrobble queue

Playing a downloaded track (offline Phase 4) with no connection still
has to scrobble — *later*. `modules/scrobble/queue.py`:

- A small persistent store (JSON or a tiny SQLite table) of pending
  scrobbles: `(service, track_metadata, listened_at)`.
- Anything that fails to send — or is recorded while
  `offline.is_server_reachable()` is False — lands here.
- Flushed on reconnect (the `connectivity` transition signal, once
  Phase 5 wires it) and at startup. Both APIs batch (Last.fm 50,
  ListenBrainz 1000), so a flush is a couple of requests.

This depends on offline Phase 5's connectivity signal; until then the
queue can flush opportunistically on the next successful scrobble or at
startup.

## 9. Dependencies

- **Last.fm API account** — august registers an app at
  `last.fm/api/account/create` to get the `api_key` + `api_secret`.
  Blocks Phase 2 only; Phase 1 (ListenBrainz) has no such dependency.
  (Tracked the same way as the cast-receiver-app TODO.)

## 10. Phased rollout

1. **ListenBrainz** — `listenbrainz.py` + the `ScrobbleManager` core +
   the eligibility rule + the settings page's ListenBrainz half. No app
   registration, no signatures — the fastest path to a working
   end-to-end scrobble, and a proof of the manager design.
2. **Last.fm** — register the app, `lastfm.py` (auth flow + `api_sig` +
   updateNowPlaying + scrobble), the settings page's Last.fm half + the
   browser-auth modal.
3. **Offline queue** — `queue.py`, flush-on-reconnect, batch submit.
4. **Polish (future)** — now-playing ping refinements; optional Last.fm
   "loved tracks" ↔ jellytoast favorites sync.

## 11. Open questions

- Eligibility precision: track *elapsed playback* vs *max position
  reached* (seeking to 90 % isn't "listening to 50 %"). Phase 1 can
  approximate via `position_updated`; refine if it matters.
- Whether to also scrobble offline-played tracks at all, or only
  online plays — leaning yes (queue them), since offline playback is a
  first-class path now.
- Cast playback: scrobbles should still fire when casting (the player
  still tracks position via the cast status feed) — verify the
  `position_updated` signal flows in cast mode.

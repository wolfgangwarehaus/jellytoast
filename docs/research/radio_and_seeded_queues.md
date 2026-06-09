# Radio & Seeded Queues — Design Research

> **📍 Status — 2026-05-20:** Shipped. Internet radio UI landed
> 2026-05-19; the seeded-radio feeder 2026-05-18; album/artist/genre
> and track "Start radio" right-click entries 2026-05-20. Kept for
> rationale — see `docs/SPEC.md` and `CHANGELOG.md`.

Original research draft (2026-05-15, pre-build — see the Shipped banner above).
Scope: bundles "Internet radio stations" and "Artist / track / album / genre
radio (seeded queues)" because they share UI patterns and queue-model
touchpoints. Internet radio is a literal HTTP/Icecast stream URL; seeded
radio is a queue jellytoast builds by chaining provider recommendations.
Both fit the mental model **radio is a queue whose tail keeps refilling itself**.

---

## 1. Goal & non-goals

**Goal.** Two "lean back" entry points matching what Supersonic, Symfonium,
Plexamp, and Spotify offer, staying provider-parity-clean (works identically
on Jellyfin and Subsonic, differences hidden behind the abstraction):

1. **Internet radio** — managed list of live HTTP streams to play, browse,
   and (when permitted) add/edit on the server.
2. **Seeded radio** — "Start radio from track/album/artist/genre" spawns a
   continuously-refilling queue of similar items.

**Non-goals (v1).**
- Server-side sonic analysis (Plexamp's "Musical Universe"); bound by
  Jellyfin `InstantMix` and Subsonic `getSimilarSongs*` results.
- Public-directory discovery (TuneIn, radio-browser.info) — see §10.
- Mobile/iOS — desktop-only.
- Persisting radio across launches — radio is ephemeral.
- Recording streams; cross-fade or station-segue tricks.

---

## 2. Internet radio — Subsonic + Jellyfin reality

### 2.1 Subsonic / OpenSubsonic

Four endpoints, all in the official spec; admin-gating differs:

| Endpoint | Since | Admin? | Notes |
|---|---|---|---|
| `getInternetRadioStations` | 1.9.0 | No | Returns `<internetRadioStation id name streamUrl homepageUrl>`. OpenSubsonic adds optional `coverArt`. |
| `createInternetRadioStation` | 1.16.0 | Yes | Params: `streamUrl`, `name`, optional `homepageUrl`. |
| `updateInternetRadioStation` | 1.16.0 | Yes | Params: `id`, `streamUrl`, `name`, optional `homepageUrl`. |
| `deleteInternetRadioStation` | 1.16.0 | Yes | Param: `id`. |

**Navidrome** implements all four (PR #2063). Edit endpoints stay
admin-only. Navidrome also supports the OpenSubsonic `coverArt` extension
on the response (commit 03608d3).

Implication: non-admin Navidrome users can *play* server stations but
cannot manage them — the "Add station" button should swap to "Add locally"
(§3) when the provider reports non-admin.

### 2.2 Jellyfin

Jellyfin has **no native music-internet-radio concept**. The only path is
the Live TV M3U tuner, a documented kludge — most stations omit the
`#EXTINF:` directive Jellyfin requires. Their feature tracker request for
proper internet radio has been open for years.

Implication: for Jellyfin users jellytoast must keep a **local station
list** (QSettings `radio/stations`, JSON). When the provider is Subsonic
the UI shows the *union* of (server, local) with a small badge per row;
edits flow to the source the row came from.

### 2.3 Stream playback (mpv)

mpv handles Icecast, Shoutcast, HLS, MP3, AAC, and Vorbis natively;
`loadfile <url>` is enough. Three wrinkles:

- **Current-track metadata.** mpv exposes the live ICY title at
  `metadata/by-key/icy-title`; python-mpv supports `observe_property`.
  Wire in `player_backend.py` → emit `PlayerBus.radio_title_changed(str)`
  → drive `NowPlaying.title` while the radio queue is active. Fallback to
  the station name when the stream doesn't emit ICY.
- **No duration / no scrubber.** Hide the seek bar when
  `QueueContext.kind == QueueKind.INTERNET_RADIO`; replace with elapsed
  time + a "LIVE" pip (TYPE_CAPTION, accent).
- **Reconnect.** mpv emits `end-file reason=error` when a stream drops.
  Reuse the Phase 5 connectivity bus; auto-retry once after 2s, toast on
  second failure.

### 2.4 Cover art

Three tiers: (1) OpenSubsonic `coverArt` via `get_image_url`, (2) local
override stored with the local entry, (3) the radio SVG from
`modules/icons.py` painted on accent. Skip `homepageUrl` logo scraping in
v1 — paywalls, regional blocks, mixed content. User pastes a URL if they
care.

### 2.5 Cast / AirPlay

- **Chromecast.** Plays MP3/AAC/Vorbis/HLS over HTTP. Reuse
  `modules/cast_proxy.py` (already redirect-aware, Range-aware) via the
  existing token API — no code change. Same auto routing as Jellyfin.
- **AirPlay 2.** pyatv 0.17 handles MP3/AAC URLs; HLS is inconsistent
  across receivers. Defer HLS-to-AirPlay; fall back to local playback.

---

## 3. Internet radio — UI + settings

**Entry point.** "Radio" in the top-bar dropdown, next to Browse /
Playlists.

**List view.** `QListView` + `QStyledItemDelegate` (model/view rule for
big lists). Row = 48px cover, name (TYPE_BODY), one-line subtitle (host
of `homepageUrl` or "Internet radio"), overflow menu (Edit, Delete, Copy
stream URL, Open homepage).

**Add-station dialog.** Frameless, settings-dialog styling. Fields: name,
stream URL, homepage (optional), cover URL (optional), "Save to:
Server / Local" (Server only when admin + provider supports it).

**Now-playing treatment when radio is active.**
- Seek bar → elapsed time + "LIVE" pip
- Skip-back disabled; Next stops the queue
- Lyrics tab hidden
- Right pane: "RADIO — <station>", station art is the big art, ICY title
  drives the song-title slot

**Settings.**
- `radio/stations` — JSON list; ids prefixed `local-` to avoid collisions.
- `radio/sync_from_server` — bool, default true.
- `radio/cast_via_proxy` — `"auto" | "always" | "never"`, default `"auto"`
  (mirrors `cast_stream_routing`).

---

## 4. Artist radio — recommendation APIs

| Seed | Subsonic | Jellyfin |
|---|---|---|
| Track | `getSimilarSongs2(id=<songId>, count=N)` returns songs | `/Items/{songId}/InstantMix?limit=N` |
| Album | `getSimilarSongs2(id=<albumId>, count=N)` (most servers accept this) | `/Items/{albumId}/InstantMix?limit=N` |
| Artist | `getSimilarSongs2(id=<artistId>, count=N)` | `/Items/{artistId}/InstantMix?limit=N` |
| Genre | `getSongsByGenre(genre, count=N, offset=K)` with random sampling | `/Items/{genreItemId}/InstantMix` |
| Year / decade | not native; derive client-side via `getRandomSongs?fromYear=&toYear=` | `getItems?Years=`, `SortBy=Random` |

Always prefer `getSimilarSongs2` (ID3-organized) over v1 (file-tree). Both
backends rank via **last.fm**; Jellyfin's MusicBrainz tagging makes
`InstantMix` slightly richer on clean libraries. Empty results = small
library / no MB matches → fall back to artist's own tracks + same-genre
random.

Keep `get_instant_mix` and `get_similar_songs` separate on the provider —
Jellyfin's mix is a curated sequence, Subsonic's similar is a bag.
Subsonic aliases mix → `getSimilarSongs2`; Jellyfin implements both
natively (mix via `InstantMix`, similar via `/Items/{id}/Similar`).

---

## 5. Artist radio — queue model

### 5.1 New context

Add `INTERNET_RADIO = "internet_radio"` to `QueueKind`. The existing
`INSTANT_MIX = "instant_mix"` (declared but unused) becomes the
seeded-radio kind. `source_id` = seed item id, `source_label` = "Radio:
<Artist>", `source_icon` = seed cover URL.

`QueueContext` gets two additive optional fields (old session JSON still
loads via `from_dict` defaults):

- `seed_kind: str = ""` — `"track" | "album" | "artist" | "genre" | "decade"`
- `radio_played_ids: List[str] = []` — no-repeat set across refills,
  persisted in `Queue.to_dict()`.

### 5.2 Continuous extension

When the current index is within **5** of queue end and the kind is
`INSTANT_MIX`, fire a `RadioFeeder` (async via `modules.async_io.run_async`
— never raw threads, per the async-io feedback rule):

1. Provider call for next batch (`count=25`).
2. Filter out any id in `radio_played_ids`.
3. Append to `original_items`, extend `play_order` (preserving shuffle).
4. Cap total at **200**; trim oldest *played* items first when over cap.

The "track played → push id to `radio_played_ids`" hook piggybacks on
report-progress.

### 5.3 Refresh, modify, end

- **Refresh radio** on the queue header reseeds from the original seed
  and clears `radio_played_ids`.
- **User adds a manual track** flips `Queue.is_modified = True` (already
  in the model) but radio keeps extending; header changes from
  "RADIO — X" to "QUEUE — X Radio".
- **Zero similar returned.** Fall back to `get_random_audio_items` scoped
  to the seed's library; if that also fails, end with a toast.

### 5.4 Skip-heavy heuristic

Skip ≥3 of last 5 tracks → refetch from seed with advanced `offset`.
Mitigates the Supersonic-known `getSimilarSongs2` artist-cluster issue.

---

## 6. Artist radio — UI surface

**Entry points.**
- Right-click on song / album / artist / genre → "Start radio from…"
- Now-playing overflow → "More like this" (current track as seed)
- Artist page header: "Radio" button next to "Shuffle"
- Top-bar Radio sub-items: "Random library radio", "From a genre…"

**Visual marker.** Queue header gets a radio glyph when
`kind in {INSTANT_MIX, INTERNET_RADIO}`.

**Keyboard.** `R` on the now-playing page = radio from current track.
(File under the keyboard-nav-pickup-untested follow-up.)

---

## 7. Multi-platform notes

- **Linux/Win/Mac.** mpv stream handling identical; python-mpv's
  ICY-title observe works on all three.
- **Windows laptop (available now).** Verify HLS/Icecast playback, ICY
  title observe, and reconnect-on-drop on Windows. Only platform-specific
  test owed in v1.
- **iOS/macOS native (future).** AVPlayerItem metadata observers replace
  python-mpv; provider abstraction unchanged. No untestable Apple code
  in v1.
- **Cast proxy.** Existing proxy serves any token-registered upstream
  (`_ProxyHandler._resolve`), so Icecast/HLS URLs ride the same code path
  as Jellyfin transcode and `file://` blobs. No new code; same ufw caveat.

---

## 8. Provider abstraction additions

Net new methods on `MediaProvider` (both providers implement; "not
supported" returns empty rather than raising):

```python
# Internet radio
def get_internet_radio_stations(self) -> List[Dict[str, Any]]: ...
def can_manage_radio_stations(self) -> bool: ...
def create_internet_radio_station(name, stream_url, homepage_url="") -> str: ...
def update_internet_radio_station(id, name, stream_url, homepage_url="") -> None: ...
def delete_internet_radio_station(id) -> None: ...

# Seeded radio
def get_similar_songs(self, seed_id: str, kind: str = "song",
                     count: int = 50) -> List[Dict[str, Any]]: ...
def get_instant_mix(self, seed_id: str, count: int = 50) -> List[Dict[str, Any]]: ...
def get_genre_radio(self, genre: str, count: int = 50,
                   offset: int = 0) -> List[Dict[str, Any]]: ...
```

**Subsonic.** `can_manage_radio_stations` = `isAdmin` (cached at login).
`get_instant_mix` delegates to `get_similar_songs(seed_id, "artist")`.

**Jellyfin.** `get_internet_radio_stations` returns `[]`,
`can_manage_radio_stations` False → UI falls back to local list.
`get_similar_songs` = `/Items/{id}/Similar?Limit=N`, `get_instant_mix` =
`/Items/{id}/InstantMix?Limit=N`, `get_genre_radio` resolves the genre
item id then calls InstantMix.

Per provider-parity rule, no provider-kind branching outside these
methods — queue manager and UI never check `provider.kind()`.

---

## 9. Edge cases

- **Stream 404 / DNS fail.** mpv `end-file` reason → "Station
  unreachable" toast, stop the queue.
- **ICY missing.** Title = station name; artist = "Internet radio". No
  `KeyError`.
- **Adblock blocks station ad-stream.** Surface mpv's reason verbatim.
- **HLS multi-bitrate.** mpv auto-picks; no bitrate UI yet.
- **0 similar.** Fall back per §5.3.
- **Duplicate similar results.** Dedupe by id in the feeder.
- **Tiny library.** `get_random_audio_items < 5` → stop extending, toast.
- **Cast device on different network.** Proxy auto-routes.
- **Local re-import footgun.** Hoist any helper imports the feeder uses
  to module top — nested `from X import Y` would shadow earlier
  references for the whole function (existing burn).
- **Provider singleton refresh.** Sign-out / kind-switch must reset the
  cached `can_manage_radio_stations` flag on visible Radio views (per
  the singleton-refs rule).

---

## 10. Effort + sequencing

| Piece | Size | Notes |
|---|---|---|
| Subsonic provider: `get_internet_radio_stations` + admin probe | S | Existing client+parser patterns. |
| Local-station QSettings list + Jellyfin fallback | S | JSON in/out. |
| Radio tab + list view + delegate | M | New `radio_view.py`; reuse list patterns from `playlists_view.py`. |
| Add/Edit station dialog | S | Same chassis as settings dialog. |
| mpv ICY-title observer wiring | S | One property hook + bus signal. |
| Now-playing radio-mode treatment (no-scrubber, LIVE pip) | S | Conditional widgets. |
| Cast-proxy radio routing verification | S | No new code expected. |
| Provider: `get_similar_songs` / `get_instant_mix` / `get_genre_radio` | M | Both providers, mostly request glue. |
| QueueKind extensions + RadioFeeder + 200-cap trimming + played-set | M | Real logic lives here. |
| Right-click "Start radio from…" plumbing | M | Touches multiple views. |
| Genre / decade entry points | M | More UI than logic. |
| Future: TuneIn / radio-browser.info directory | L | Out of v1. Whole search surface, paging, country filters. |
| Future: registered Cast receiver app | L | Same deferred item as elsewhere. |

**Recommended v1 shipping order.**

1. **Internet radio v1** — Subsonic endpoints + local list + Radio tab +
   play/stop + ICY title + no-scrubber treatment. Mostly UI, easy
   manual-test loop.
2. **Seeded radio v1** — provider methods + `INSTANT_MIX` kind +
   RadioFeeder + right-click "Start radio from…" for track / album /
   artist. Skip the visual marker, refresh button, and no-repeat trim;
   just prove the loop with a hardcoded 25-track seed.
3. **Polish pass** — refresh-radio, header glyph, no-repeat played-set,
   skip-heavy reseed, settings toggles, cover-art fallbacks.
4. **Genre + decade radio** last (needs a genre/year picker surface that
   doesn't exist yet).
5. **Deferred to v2** — radio-browser.info directory, registered Cast
   receiver app, AirPlay 2 HLS fallback, cross-launch radio resume.

**One-line v1 plan.** Ship **internet radio first** (UI-heavy, proves the
no-scrubber now-playing treatment), then **track/artist/album seeded
radio**, then the polish + genre/decade pass.

---

## 11. Sources

- Subsonic API — `subsonic.org/pages/api.jsp`: getInternetRadioStations
  (1.9), create/update/delete (1.16, admin), getSimilarSongs(2) (1.11),
  getSongsByGenre (1.9).
- OpenSubsonic — `opensubsonic.netlify.app/docs/endpoints/getinternetradiostations/`
  + `…/responses/internetradiostation/` (coverArt extension).
- Navidrome — `github.com/navidrome/navidrome/pull/2063`, commit `03608d3`.
- Jellyfin — `jellyfin.org/docs/general/server/media/internet-radio/`
  (M3U-tuner kludge); feature request `features.jellyfin.org/posts/759/internet-radio`.
- Jellyfin InstantMix — `jellyfin.org/docs/plugin-api/MediaBrowser.Api.Music.GetInstantMixFromItem.html`.
- Supersonic CHANGELOG — `github.com/dweymouth/supersonic` (Song Radio
  0.10.0, Internet Radio 0.11.0, Artist Radio 0.3.1, Autoplay 0.14.0).
- Plexamp Stations — `plex.tv/blog/super-sonic-get-closer-to-your-music-in-plexamp/`,
  `forums.plex.tv/t/how-exactly-does-the-library-radio-work/221588`.
- mpv ICY — issues `mpv-player/mpv#3705`, `#813`, `#10347`;
  `metadata/by-key/icy-title` + `observe_property`.
- Internal: `modules/queue_manager.py`, `modules/player_state.py`,
  `modules/providers/base.py`, `modules/providers/subsonic.py`,
  `modules/providers/jellyfin.py`, `modules/cast_proxy.py`.

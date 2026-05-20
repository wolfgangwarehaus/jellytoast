# Smart / Dynamic Playlists — Design Research

> **📍 Status — 2026-05-20:** Shipped. Smart playlists landed
> end-to-end 2026-05-19 (editor, library tab, live preview); the
> right-click "Create smart playlist from this X" entries 2026-05-20.
> Kept for rationale — see `docs/SPEC.md` and `CHANGELOG.md`.

*Status: research / design proposal. No code yet. Last updated 2026-05-15.*

## 1. Goal & non-goals

**Goal.** Let jellytoast users build playlists from rules ("everything in the
Electronic genre I haven't played in six months, sorted by play count, limit
100") that stay fresh as the library and listening history change. Ship a
single rule-builder UI that works against both supported backends — Jellyfin
and Subsonic/Navidrome — with the same semantics.

**Non-goals (v1).**

- AI / sonic-similarity radios (Plexamp's "Mixes for You", Plex's neural-net
  embeddings). Cool, but we don't have the analysis pipeline and Navidrome's
  similarity is still in discussion upstream.
- Re-implementing iTunes-grade live-update queues (matching tracks rotating
  in/out of the *currently playing* queue while it plays). Snapshot at play
  time is enough for v1.
- Editing other clients' smart-playlist storage formats (writing `.nsp` files
  directly via SSH/SMB). We talk through the server's API or our own local
  store, nothing more.
- Authoring rules on the road for someone else's library (multi-user / shared
  smart playlists). Single-user, single-library scope.

## 2. Competitor approaches

| Client | Where rules live | Editor | Notable picks |
|---|---|---|---|
| **Feishin** (Electron) | Server-side, Navidrome only — POSTs JSON to Navidrome's REST API; `.nsp` file is what Navidrome stores. | Form-style chip builder (any / all groups, field + operator + value). | Saves directly to Navidrome — visible from the Navidrome web UI on next scan. Read-only track list. |
| **Symfonium** (Android) | Client-side rule store; same engine for "smart filters" and "smart playlists". | Form builder; supports nested any/all groups. | Mix Mode evolves from listening history (closer to Plexamp than a rule engine). |
| **Strawberry / Tauon** | Client-side, local-file libraries only. | Foobar-style query language + form. | Have lived through every edge case (empty rule sets, encoding mismatches in tags). |
| **Plexamp** | Two parallel features: (a) **Smart Playlists** — rule-based; (b) **Stations** — neural-net similarity ("Sonic Sage", Mood Radio, Track Radio). | Form for smart playlists; one-tap "play similar" for stations. | "Mood Radio" — 20+ moods beyond "chill / energetic". Worth lifting the *taxonomy*, not the algorithm. |
| **iTunes / Apple Music** | Client-side. | Apple-Music-style chip rows with "+" buttons. | The original; live-updating, limit-by-count or by-duration, "only checked" toggle. |

Common pattern: form-based any/all rule groups, sort + limit, save + show in
the regular playlist library distinguished by a small "smart" badge.

## 3. Provider API capabilities

### 3.1 Navidrome (Subsonic with extensions)

- **Storage.** Smart playlists are JSON documents with a `.nsp` extension
  living next to Navidrome's music library (or in a configured folder). The
  scanner picks them up and materializes them as regular playlists in the
  Subsonic API.
- **Fields supported** (verified against Navidrome docs / wiki):
  - Metadata: `title`, `album`, `artist`, `albumartist`, `tracknumber`,
    `discnumber`, `year`, `date`, `originalyear`, `originaldate`,
    `releaseyear`, `releasedate`, `compilation`, `albumtype`,
    `catalognumber`, `albumcomment`, `discsubtitle`
  - File: `filepath`, `filetype`, `size`, `duration`, `bitrate`, `bitdepth`,
    `channels`, `samplerate`
  - User interaction: `loved`, `dateloved`, `lastplayed`, `playcount`,
    `rating`, `albumloved`, `artistloved`
  - Technical: `hascoverart`, `comment`, `lyrics`, `bpm`, `library_id`,
    `missing`
  - MusicBrainz IDs: `mbz_recording_id`, `mbz_album_id`,
    `mbz_album_artist_id`, `mbz_artist_id`, `mbz_release_group_id`,
    `mbz_release_track_id`
- **Operators:** `is`, `isNot`, `gt`, `lt`, `contains`, `notContains`,
  `startsWith`, `endsWith`, `inTheRange`, `before`, `after`, `inTheLast`,
  `notInTheLast`, `inPlaylist`, `notInPlaylist`.
- **Composition:** top-level `all` and `any` arrays; rule groups can nest a
  `{"any": [...]}` inside an `all` (and vice versa) for boolean composition.
- **Sort / limit:** `sort` accepts comma-separated fields, `+` / `-` prefix
  per field; `order` is `asc` / `desc`; `limit` (count) or `limitPercent`
  (Navidrome 0.55+).
- **Exposure to clients via Subsonic API.** Through `getPlaylists` /
  `getPlaylist`, Navidrome marks smart playlists with the OpenSubsonic
  `readonly: true` field, optionally returns `validUntil` (ISO 8601) so
  clients know when to refetch. Track lists are not editable through
  `updatePlaylist`.
- **Write path.** Navidrome **does not** expose a REST endpoint for creating
  smart playlists. Feishin works around this by writing the `.nsp` JSON via
  Navidrome's *native* (non-Subsonic) admin API. That API requires the
  Navidrome session cookie / token, not the Subsonic credentials we use
  elsewhere — a second auth tier.

### 3.2 Vanilla Subsonic / other servers (Airsonic, Gonic, Astiga…)

No smart-playlist concept in the spec. The closest tools:

- `getAlbumList2` with `type=byGenre|byYear|highest|frequent|recent|starred|
  random|newest|alphabeticalByName|alphabeticalByArtist` and `genre` /
  `fromYear` / `toYear` filters.
- `getSongsByGenre` (single genre + optional musicFolderId).
- `getRandomSongs` (limited filters: genre + year range + folder).
- `getStarred2`.

So on a non-Navidrome Subsonic server we can fake a *subset* of smart
playlists client-side: anything reducible to one of those calls plus a
client-side filter/sort pass. Multi-genre + lastPlayed + rating is **not**
expressible — those fields aren't surfaced by vanilla Subsonic.

### 3.3 Jellyfin

- **`/Items` is a query API.** Parameters relevant to music smart playlists:
  - `IncludeItemTypes=Audio`
  - `Genres`, `GenreIds`, `OfficialRatings`, `Tags`, `Studios`, `Years`,
    `Artists`, `AlbumIds`, `PersonIds`
  - `Filters` — comma-separated set including `IsFavorite`, `IsPlayed`,
    `IsUnplayed`, `IsResumable`, `Likes`, `Dislikes`
  - `MinCommunityRating`, `MinCriticRating`
  - `MinDateLastSaved`, `MinDateLastSavedForUser` (closest thing to
    "lastPlayed")
  - `SortBy` accepts `Album, AlbumArtist, Artist, CommunityRating,
    CriticRating, DateCreated, DatePlayed, PlayCount, PremiereDate,
    ProductionYear, Random, Runtime, SortName`
  - `SortOrder=Ascending|Descending`
  - `Limit`, `StartIndex`, `Recursive=true`
- **`GetInstantMix`** — server-generated radio-style mix off a seed track /
  album / artist. Analog of "Track Radio". Already a viable v1 station
  primitive.
- **Smart-playlist plugins.** Three live community plugins exist
  (jyourstone, ranaldsgift, ankenyr). All ship a server-side rule engine
  with their own JSON schema, exposed via a plugin-specific HTTP API. None
  is bundled with Jellyfin core — we should assume *not installed* and
  treat them as a nice-to-have we can detect later.
- **No native saved-query concept.** Jellyfin doesn't persist queries as
  first-class objects; you can save a regular playlist of materialized
  items, but not a rule set.

## 4. Recommended architecture

**Hybrid, client-side rule storage with provider-rendered evaluation.**

- A rule set is a jellytoast-native JSON document stored locally
  (`smart_playlists.json` in the config dir; one entry per playlist).
- The provider abstraction grows a single new method:

  ```python
  def query_items(self, rules: SmartRules) -> List[Dict[str, Any]]: ...
  ```

  Each provider translates as much of the rule set as it can into a single
  server call, then performs the remaining filtering / sorting in Python
  before returning.
- A new `SmartPlaylistEngine` (in `modules/smart_playlists/`) owns the
  serialization, the rule-set ↔ provider-query translator, and the cache.

Why not server-side via Navidrome's `.nsp`?

1. **Parity.** We hard-require cross-provider feature parity. If Navidrome
   has it but Jellyfin doesn't, the experience splits in two — and the
   `.nsp` write path is undocumented enough (admin-cookie auth, scan-trigger
   semantics) that we'd be on the slow path for a long time.
2. **Privacy / portability.** Rules in our store survive a server move,
   migrate cleanly between Jellyfin and Subsonic, and don't leak our
   listening shape onto a shared server.
3. **Detect-and-defer.** If the server *is* Navidrome with `readonly` smart
   playlists, we still show them — read-only — in the Playlists view next to
   our own. v2 may write back to `.nsp` if the user opts in. For Jellyfin
   with a smartlists plugin installed (detected via the `/Plugins` endpoint)
   we can do the same.

So:

- v1: client-side rules, evaluated by translating to a Subsonic-flavored or
  Jellyfin-flavored query plus a local refine pass.
- v2: surface server-native smart playlists read-only; optionally write
  rules through to Navidrome when authenticated to its admin API.

## 5. Rule model

A rule set is a JSON object. The schema borrows shape from Navidrome's
`.nsp` so we can later round-trip with minimal friction.

```json
{
  "id": "uuid",
  "name": "Recently played electronic",
  "comment": "Optional description",
  "all": [
    { "is":         { "genre": "Electronic" } },
    { "inTheLast":  { "lastPlayed": 30 } },
    { "any": [
      { "gt": { "rating": 3 } },
      { "is": { "loved": true } }
    ]}
  ],
  "sort": "-lastPlayed,-playCount",
  "limit": 100,
  "limitPercent": null,
  "refresh": "on_open",
  "created": "2026-05-15T11:00:00Z",
  "modified": "2026-05-15T11:00:00Z"
}
```

**Fields (v1).** Restricted to what *both* providers can give us, plus
common idioms:

- `title`, `album`, `artist`, `albumartist`, `genre`, `year`
- `dateAdded`, `lastPlayed`, `playCount`, `loved` (favorite), `rating`
- `duration`, `bitrate` (rarely useful, but cheap to surface)

**Operators (v1).** `is`, `isNot`, `contains`, `notContains`, `startsWith`,
`gt`, `lt`, `inTheRange`, `before`, `after`, `inTheLast`, `notInTheLast`.
`inPlaylist` / `notInPlaylist` deferred to v2 (cross-provider playlist-ID
opacity).

**Composition.** Top-level `all` is implicit AND; an entry can itself be a
group `{"any": [...]}` or `{"all": [...]}` for nesting one level deep. v1
ignores deeper nesting and shows a UI warning if loaded from a file.

**Sort.** Comma list, `+` or `-` prefix, last field is the tiebreaker. The
synthetic value `random` is allowed (single field only).

**Limit.** `limit` (int, cap at 10 000) and `limitPercent` (0–100, applied
after rule match). Both `null` = no cap (but UI still pages the view).

**Refresh policy.** `on_open` (default), `cache_5m`, `cache_1h`,
`frozen` (snapshot on save — degenerates to a static playlist). Auto-update
is the value users want most; frozen exists for the "I love this snapshot,
preserve it" case.

## 6. UI surface

**Where it lives.**

- In the existing Playlists tab. No separate "Stations" surface — a
  visual badge (small lightning bolt or "SMART" pill) on the tile cover
  distinguishes smart playlists. Track lists are read-only; the regular
  playlist editor disables Add/Remove/Reorder for smart playlists.

**Create flow.**

- "+ New playlist" menu gains a second entry: **New smart playlist…**.
  Opens a modal rule-builder dialog (frameless to match the settings
  dialog).

**Rule builder.**

- Apple Music-style chip rows: one row per rule, each row = `[ Field ▾ ]
  [ Operator ▾ ] [ Value ]` and a delete button. A "+" at the bottom adds a
  rule; an "Add group" link wraps the next rule in a nested any/all box.
- Top of dialog: name field + match toggle ("Match **all / any** of the
  following").
- Bottom of dialog: sort field + direction + limit count + limit unit
  (tracks / percent of matches / no limit) + refresh policy.
- Live preview pane on the right: top 10 matching tracks plus a "Matches:
  N tracks" counter, debounced 300 ms after the last edit. Uses the same
  `query_items` path the saved playlist will.
- A "Show generated rule" disclosure dumps the JSON for power users — also
  copy-pasteable into a Navidrome `.nsp` for portability.

**Edit / play.**

- Clicking a smart playlist opens the same `LibraryGrid` detail view that
  static playlists use, with the read-only chrome. Play, shuffle, cast,
  download all reuse the existing path (downloads snapshot the current
  evaluation; see §8).
- Edit button on the detail view re-opens the rule builder.

## 7. Storage + persistence

- `~/.config/jellytoast/smart_playlists.json` (single JSON array, one
  entry per playlist). Edited atomically (temp file + rename) to survive
  crashes mid-write.
- Per-entry `id` is a client-generated UUID; never collides with server
  IDs (we prefix with `smart:` when used inside the provider abstraction so
  the queue manager can disambiguate).
- A v2 migration to per-account namespacing is trivial (same shape, key by
  `{provider_kind}:{server_id}:{user_id}` map).
- **Multi-device implication.** Rules are device-local in v1. The user who
  expects "my smart playlists everywhere" is the one who'd lean on
  Navidrome's `.nsp` — that's the v2 opt-in write-through.

## 8. Refresh semantics + queue interaction

- **View refresh.** When the detail view opens, the engine evaluates the
  rule per the playlist's `refresh` policy: `on_open` always re-evaluates,
  `cache_5m` / `cache_1h` use the in-memory cache, `frozen` reads the
  snapshot stored on the entry.
- **Queue snapshot.** Hitting "Play" snapshots the current evaluation into
  the queue. The queue does **not** re-evaluate as tracks finish — this
  matches what every competitor does and avoids "the song that just played
  vanishes from the up-next list" weirdness.
- **Shuffle.** Uses the existing shuffle path on the snapshotted list.
- **Cast / Mini-player.** Inherit the snapshot via the existing queue
  manager. No special handling.
- **Downloads.** "Download playlist" snapshots the rule's current
  evaluation and downloads those tracks. Re-running download on the same
  smart playlist tomorrow may pick up newer matches — that's the
  intended behavior and we surface it ("Download N new matches?") rather
  than re-downloading the whole set.
- **Offline mode.** A smart playlist evaluates against the *downloads
  index* when offline (see `modules/offline/`). Rules that can't be
  evaluated locally (e.g. server-only metadata not mirrored to the
  download cache) are skipped with a banner.

## 9. Recipes to ship

Bundled as defaults the first time the user opens the smart-playlist UI
(creating them in their store, editable like any other):

1. **Recently added** — `dateAdded inTheLast 30`, sort `-dateAdded`, limit
   100.
2. **Forgotten favorites** — `loved is true` AND `lastPlayed notInTheLast
   180`, sort `-dateLoved`, limit 50.
3. **Top played** — `playCount gt 0`, sort `-playCount`, limit 100.
4. **Year X** — `year is 1999` (template, user fills in the year), sort
   `random`, limit 200.
5. **Long-lost gems** — `playCount lt 3` AND `dateAdded before <last year>`,
   sort random, limit 50.
6. **Mood: chill (sample)** — `genre contains "ambient" OR genre contains
   "downtempo" OR genre contains "chill"`, sort random, limit 75. Marked
   "sample — fork me" in the description.

## 10. Edge cases

- **Empty rule set.** Disable the Save button; show "Add at least one
  rule" hint.
- **>10 000 matches.** Hard cap at 10 000 returned tracks (UI paginates
  via the existing `LibraryGrid` paging). A toast warns "Matched 27 000
  tracks; showing first 10 000. Add a stricter rule or a smaller limit."
- **Fields one provider tracks and the other doesn't.** `lastPlayed` is
  the canonical one. On vanilla Subsonic without a `played` flag we
  degrade gracefully: the operator is grayed out in the UI editor when
  the active provider can't satisfy it, and a stored rule that uses an
  unsupported field is reported in the preview pane ("`lastPlayed` not
  available on this server — rule skipped"). The rule is preserved in
  the store so it works again if the user re-points at Navidrome /
  Jellyfin.
- **Server-side smart playlists colliding with client rules.** Both show
  up in the playlist library. Server-side ones get the `readonly` badge
  and a different icon tint; client-side ones can be edited / deleted
  freely. We never silently merge them.
- **Sync conflicts.** Not applicable in v1 (device-local). For the v2
  Navidrome write-through, last-write-wins with a `modified` timestamp
  comparison before each save; a future-dated remote `.nsp` triggers a
  "remote rules are newer; overwrite?" confirm.
- **Random sort + limit semantics.** Random + limit should re-roll on
  each evaluation. We document this — users sometimes expect a sticky
  random order, which is what `frozen` is for.

## 11. Effort + sequencing

### v1 — Minimum viable smart playlists (M)

1. `modules/smart_playlists/` package: `engine.py`, `rules.py` (typed
   dataclass + JSON IO), `store.py` (file-backed), `translator.py`
   (rules → provider call). **S** each.
2. `MediaProvider.query_items()` in `base.py`; implement on
   `JellyfinProvider` (delegates to `/Items` with translated params, then
   refines in Python for fields the API can't filter on) and
   `SubsonicProvider` (uses `getAlbumList2` / `getSongsByGenre` /
   `getRandomSongs` / `getStarred2` as base + Python refine). **M**.
3. Smart-playlist rule-builder dialog + live preview. **M-to-L** — the
   chip-row UX is the big lift; reuse `_OpaqueComboBox`, design tokens,
   typography tokens.
4. Playlists view integration: distinguish badge, read-only detail view,
   "+ New smart playlist…" menu entry. **S**.
5. Six bundled default recipes. **S** (data only).
6. Queue / downloads / cast paths reuse the existing snapshot model. No
   changes expected. **trivial**.

**Pre-reqs.** None blocking — the provider abstraction is mature enough,
the playlists view already handles per-kind chrome.

### v2 — Server-native surfacing + write-through (L)

7. Detect Navidrome smart playlists via OpenSubsonic `readonly` /
   `validUntil`; show them in the library as read-only with a badge.
   **S**.
8. Detect Jellyfin smartlists plugin via `/Plugins` and surface its
   playlists analogously. **S**.
9. Optional **write-through to Navidrome `.nsp`** — needs Navidrome admin
   auth tier in our settings layer; on save, also POST/PUT the rule JSON
   via the admin API (or write the `.nsp` over WebDAV if exposed). Behind
   a per-server toggle "Sync smart playlists to Navidrome". **L**.
10. `inPlaylist` / `notInPlaylist` operator. **S**.
11. `frozen` snapshots, real recipe gallery, import/export `.nsp` files.
    **S**.

### v3 — Algorithmic stations (L+)

12. `getInstantMix`-backed "Track Radio" / "Album Radio" / "Artist
    Radio" buttons on the now-playing page. Server does the heavy lifting
    on Jellyfin; Subsonic's `getSimilarSongs2` is the rough analog. Not a
    rule engine — distinct surface, deferred until v1+v2 stabilize.

## 12. Sources

- [How to Use Smart Playlists in Navidrome (Beta)](https://www.navidrome.org/docs/usage/features/smart-playlists/)
- [Playlist System — Navidrome DeepWiki](https://deepwiki.com/navidrome/navidrome/5.3-playlist-system)
- [Navidrome Smart Playlists: Setup and Management (jenaichat blog)](https://jenaichat.com/blog/navidrome-smart-playlists-setup-and-management/)
- [Smart Playlists wiki (Sick Gitea mirror)](https://gitea.sickgaming.net/sickprodigy/navidrome-smart-playlists/wiki/How-to-Use-Smart-Playlists-in-Navidrome)
- [feat(playlists): percentage-based limits — Navidrome PR #5144](https://github.com/navidrome/navidrome/pull/5144)
- [OpenSubsonic playlist response spec — readonly / validUntil](https://opensubsonic.netlify.app/docs/responses/playlist/)
- [Add readonly and validUntil fields to playlist response — OpenSubsonic PR #204](https://github.com/opensubsonic/open-subsonic-api/pull/204)
- [Subsonic API Compatibility — Navidrome docs](https://www.navidrome.org/docs/developers/subsonic-api/)
- [jeffvli/feishin README — "Smart playlist editor (Navidrome)"](https://github.com/jeffvli/feishin)
- [Feishin issue #547 — inPlaylist / notInPlaylist support](https://github.com/jeffvli/feishin/issues/547)
- [Symfonium — Smart playlists wiki](https://support.symfonium.app/t/smart-playlists/327)
- [Plex Sonic Analysis for Music](https://support.plex.tv/articles/sonic-analysis-music/)
- [Super Sonic: Get Closer to Your Music in Plexamp](https://www.plex.tv/blog/super-sonic-get-closer-to-your-music-in-plexamp/)
- [jyourstone/jellyfin-smartlists-plugin](https://github.com/jyourstone/jellyfin-smartlists-plugin)
- [ranaldsgift/jellyfin-smartplaylist-plugin](https://github.com/ranaldsgift/jellyfin-smartplaylist-plugin)
- [ankenyr/jellyfin-smartplaylist-plugin](https://github.com/ankenyr/jellyfin-smartplaylist-plugin)
- [Jellyfin Items API — Mintlify mirror](https://www.mintlify.com/jellyfin/jellyfin/api/media/items)
- [@jellyfin/sdk ItemsApiGetItemsRequest reference](https://typescript-sdk.jellyfin.org/interfaces/generated-client.ItemsApiGetItemsRequest.html)

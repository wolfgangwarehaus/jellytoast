# Tag Editing — Design Research

> **📍 Status — shipped (corrected 2026-05-28 audit):** Backend landed
> 2026-05-17 (Jellyfin only); the editing UI — right-click "Edit tags…"
> + dialog with cover-art replace and bulk "apply to whole album" —
> shipped 2026-05-25/26 (`modules/tag_editor.py`). Live-Jellyfin
> verification of `upload_cover_art` / `update_album_track_metadata`
> is the only open item (see `docs/manual_test_plan.md`). Kept for
> rationale.

**Status:** shipped (backend + UI; Jellyfin-only)
**Date:** 2026-05-15
**One-line verdict:** Ship as a **Jellyfin-admin-only** v1 (single-track + bulk-album edit, cover art). Subsonic / Navidrome stay read-only at the API tier; revisit with a local annotation overlay only if it's actually requested. Skip vanilla Subsonic forever.

---

## 1. Goal & non-goals

**Goal.** Let the user correct wrong metadata on the *server* — fix a misspelled artist, set a missing year, change album art — from within jellytoast, without leaving the app.

**Non-goals.**

- **Local-file tag editing.** jellytoast is streaming-first (`memory/project_competitive_positioning.md`). Strawberry / Tauon / Picard territory.
- **MusicBrainz "identify this album" wizards.** Jellyfin has its own server-side Identify dialog; we don't duplicate it.
- **Loudness / ReplayGain recalculation.** Tag edits don't change audio data, and we don't edit audio data.
- **Library reorganization** (moving files, renaming folders). Admins do that out-of-band.

The bar is correcting *display metadata* — what shows in search results, library grid, now-playing bar.

---

## 2. Server-side write APIs — reality check

### Subsonic & OpenSubsonic

Subsonic's API surface is read-heavy + annotation-style writes. The only state-changing endpoints are: `setRating`, `star`/`unstar`, `scrobble`, bookmarks, playlists, shares, user/podcast admin, `savePlayQueue`, `startScan`. **There is no endpoint to edit a song, album, or artist's tags** — the file is the source of truth. OpenSubsonic adds fields and extensions (lyrics, podcasts, jukebox tweaks) but **no metadata-editing extensions** in the May 2026 spec.

### Navidrome

Two API surfaces: Subsonic-compatible at `/rest/*`, and a "native" REST API at `/api/*` that powers the web UI. Native is full CRUD for *playlists*, *users*, *shares*, *plugins*, *config* — but **read-only for songs / albums / artists**. Navidrome's stated design intent is that it **does not write to your music folder**, partly for security. A long-running discussion (#3181 / PR #3312) explores sidecar/tagfile overrides, but no "edit a tag via API" path is merged. Annotations (star, rating, play count) and playlists are writable; metadata isn't.

### Jellyfin

Jellyfin is the one backend that genuinely supports server-side metadata editing:

- `POST /Items/{itemId}` with a `BaseItemDto` payload updates the item. Editable fields relevant to us: `Name`, `OriginalTitle`, `SortName`, `ProductionYear`, `PremiereDate`, `Genres`, `Tags`, `Studios`, `AlbumArtists`, `ArtistItems`, `ProviderIds`, `Overview`, `LockedFields`, `LockData`.
- `GET /Items/{itemId}/MetadataEditor` returns the editor schema (which fields are editable, lock state, available genres/people).
- `POST /Items/{itemId}/Images/{imageType}` uploads new cover art (base64 body, content-type is the image MIME).
- `DELETE /Items/{itemId}/Images/{imageType}` removes art.
- `POST /Items/{itemId}/Refresh` triggers a per-item rescan if needed.

**Caveats.**

- The metadata-editing endpoints are **administrator-gated** today. There is no `EnableMetadataEditing` flag on `UserPolicy`; the closest neighbors (`EnableCollectionManagement`, `EnableSubtitleManagement`, `EnableLyricManagement`) don't cover generic item-tag edits. There's an open feature request (#2830) for non-admin metadata editing. Plan accordingly — for almost every user, this means "only the admin account on a self-hosted server can edit."
- Known bug (jellyfin #10724): posting `{"Tags": ["x"]}` standalone returns 400 and can leave the item temporarily unfetchable until a rescan. Mitigation: always send the *full* `BaseItemDto` we received from `/Items/{id}` with our changes merged in, never a sparse patch.
- Jellyfin maintains a `LockedFields` list per item — fields the user has explicitly pinned so the metadata refresher won't overwrite them. We should set the lock on any field we edit (otherwise the next scheduled refresh can revert the user's correction silently).

### Provider parity scoreboard

| Capability | Jellyfin | Navidrome | Subsonic / OpenSubsonic |
|---|---|---|---|
| Edit track name / year / genre | yes (admin) | no | no |
| Edit album artist | yes (admin) | no | no |
| Replace cover art | yes (admin) | no | no |
| Trigger rescan | yes | yes (`startScan`) | yes (`startScan`) |
| Star / rate | yes | yes | yes |

Parity breaks. Acknowledge it.

---

## 3. Strategy options

**A. Jellyfin-only feature.** Hide the Edit affordance when `provider.kind != "jellyfin"`. Honest, minimal, matches the underlying reality. Cost: breaks the cross-provider parity invariant (`feedback_provider_parity.md`) — treat as a documented exception, not a slippery slope.

**B. Client-side annotation overlay.** Store user overrides in `~/.config/jellytoast/annotations/{server_id}.json`, merge at display time. Works on any backend; very large display-side merge surface (library grid, search, suggestions, NP page, mini player, cast metadata, scrobbles, downloaded blob tags) and changes never sync — second device sees the wrong tag again. Papers over a server-side problem.

**C. Navidrome native API when present, annotation fallback.** Only justified if Navidrome ships a real edit endpoint. Today it doesn't and the read-only-on-files stance suggests it won't soon. Reduces to (A).

**D. Suggest fix to admin / open MP3Tag.** Useless for streaming clients. Drop.

**Recommendation: A, door cracked for B.** Ship Jellyfin-admin tag editing as the v1 feature; hide the affordance cleanly on other backends. Defer (B) until somebody actually asks. Scrobble / rating writes (`toggle_favorite`, `setRating`, played/unplayed) keep working everywhere — tag editing is a narrower scope.

---

## 4. UI surface

### Affordances

- **Track row context menu** → "Edit tags…" (single track).
- **Album page three-dot menu** → "Edit album…" (album-level fields + "apply to all tracks" affordances).
- **Artist page three-dot menu** → "Edit artist…" (name, sort name, image).
- **Multi-select track rows** → context menu → "Edit tags…" (bulk).

Affordances are *hidden entirely* when the provider can't edit or the current user lacks permission (Section 5). Don't show greyed-out items — they look broken.

### Single-item dialog

Frameless modal, KDE-style chrome (matches Settings, Cast picker). Pre-populated form: Title, Album, Album Artist, Track Artists, Year, Genres, Tags, Disc #, Track #, Sort Name, Overview. "Lock these fields" checkbox group — default to locking anything the user actually changed (Jellyfin's auto-revert on scheduled refresh is silent and confusing otherwise). Save / Cancel; Save is optimistic — close immediately, POST in the background via `modules.async_io.run_async`, toast + Retry on failure.

### Bulk dialog

Same form. Empty fields = "leave unchanged". A tri-state checkbox per field: clear = unchanged, dot = "set this value for every selected track". For "apply to all tracks in album", treat multi-disc albums as one set by default — there's no real use case for Genre per disc. Confirmation toast: "Updated 24 tracks across 2 discs."

### Cover art

Drag-and-drop onto the cover area, or "Replace cover" → `QFileDialog` (`*.png *.jpg *.jpeg *.webp`). Preview pre-upload. We don't crop the upload — Jellyfin stores arbitrary aspect ratios; center-cropping happens at display time. "Clear cover" deletes the Primary image.

### Audit / undo

Local rolling log at `~/.config/jellytoast/edit_log.jsonl` — one line per successful edit `{ts, server_id, item_id, before, after, fields}`. Visible from Settings → "Recent edits". No automatic undo in v1 — log lets the user manually re-edit if they realize they were wrong.

---

## 5. Permission detection

- **Jellyfin.** Call `GET /Users/{userId}` at session start, cache the policy on `JellyfinProvider`. Expose `provider.can_edit_metadata: bool` on the base. For Jellyfin v10 today, the safe rule is `policy.IsAdministrator == true`. Don't check `EnableContentDeletion` — gates deletion, not editing. Re-fetch policy on foreground/reconnect so demoted users don't keep stale affordances.
- **Subsonic / OpenSubsonic.** `can_edit_metadata = False`. No probe needed.
- **Navidrome.** `can_edit_metadata = False` today. Re-evaluate if Navidrome ever ships a native edit endpoint.

A single boolean drives every affordance — no view-by-view conditionals.

---

## 6. Multi-platform notes

- Everything is HTTPS — works identically on Linux/Windows/macOS desktop and any future mobile build.
- `QFileDialog` covers desktop. iOS will need `UIDocumentPicker` / Photos picker (deferred — see `user_hardware.md`).
- Cover-art upload is a single multipart POST; no platform-specific paths.
- Edits hit the cover-art cache (`feedback_now_playing_cover_pipeline.md`). On a successful image change, invalidate the keyed entry for the album's `image_id` and the in-memory pixmap cache used by the library grid. Don't blow the whole cache — just the specific keys.

---

## 7. Edge cases

- **Empty field on save.** Allow empty for free-text fields (Overview, Tags). Disallow empty Title — silently revert + toast.
- **Renaming an artist.** Jellyfin matches artist entities by name on next refresh; "Beatles" → "The Beatles" can merge or fork the artist. Warn: "Renaming an artist will re-group their albums on the server — affects everyone on this Jellyfin instance."
- **Multi-disc albums.** Bulk-apply spans discs by default. Confirmation shows the breakdown.
- **Cover aspect ratio.** Server stores whatever we send; display-time center-crop already happens everywhere (`feedback_now_playing_cover_pipeline.md`).
- **LockedFields silently reverting edits.** Always send the merged `LockedFields` list with the user's changes appended. This is the #1 silent-failure mode.
- **Stale singletons after edit.** Push invalidation through `invalidate_meta_cache(item_id)` so every cached view re-fetches.
- **Edit while offline.** Phase-5 offline detection (`architecture_offline_phase5.md`) gates writes — show inline "You're offline" instead of failing mid-save. No edit queue in v1.
- **Audit-log size.** Append-only JSONL, 10 MB cap with rotation. Edits are rare.

---

## 8. Effort + sequencing

| Phase | Scope | Size |
|---|---|---|
| 1 | `provider.can_edit_metadata` + Jellyfin `update_item()` + single-track edit dialog + LockedFields handling | S–M |
| 2 | Bulk edit (multi-select), album-level edit ("apply to all tracks"), audit log | M |
| 3 | Cover art upload (drag-drop + file picker), preview, cache invalidation | M |
| 4 | Artist edit + rename-warning UX | S |
| 5 | (Conditional, deferred) Navidrome native edit integration *only if* Navidrome ships an endpoint | M |
| 6 | (Conditional, deferred) Local annotation overlay for Subsonic — display-time merge across every view | L |

**Recommended v1 ship:** phases 1 + 3 (single-track edit + cover art). Phase 2 (bulk) close behind. Phases 5–6 parked.

**Provider-base changes** for v1: `can_edit_metadata: bool` (default `False`, Jellyfin sets via policy), `update_item(item_id, patch) -> dict`, `update_item_image(item_id, image_type, bytes, mime)`, `delete_item_image(item_id, image_type)`. Non-Jellyfin implementations raise `NotSupportedError`. UI lives under a new `modules/edit_tags/` package.

---

## 9. Sources

- [Jellyfin ItemUpdateApi.UpdateItem (SDK)](https://typescript-sdk.jellyfin.org/classes/generated-client.ItemUpdateApi.html)
- [Jellyfin issue #10724 — Updating tags via API corrupts items until rescan](https://github.com/jellyfin/jellyfin/issues/10724)
- [Jellyfin UserPolicy.cs source](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Model/Users/UserPolicy.cs)
- [Jellyfin Feature Request — Non-admin users edit metadata (#2830)](https://features.jellyfin.org/posts/2830/non-admin-users-identify-items-and-edit-their-metadata)
- [Jellyfin Feature Request — Refresh-metadata-only permission (#2893)](https://features.jellyfin.org/posts/2893/give-users-permission-to-refresh-metadata-only)
- [Subsonic API reference (subsonic.org)](https://www.subsonic.org/pages/api.jsp)
- [OpenSubsonic API changes & extensions](https://opensubsonic.netlify.app/docs/opensubsonic-changes/)
- [Navidrome Subsonic API compatibility](https://www.navidrome.org/docs/developers/subsonic-api/)
- [Navidrome Native REST API overview (DeepWiki)](https://deepwiki.com/navidrome/navidrome/4.2-native-rest-api)
- [Navidrome FAQ — read-only design](https://www.navidrome.org/docs/faq/)
- [Navidrome Discussion #3181 — sidecar/tagfile overrides](https://github.com/navidrome/navidrome/discussions/3181)
- [Navidrome Discussion #2418 — edit metadata feature request](https://github.com/navidrome/navidrome/discussions/2418)
- [JEMM (Jellyfin Easy Metadata Manager) — reference UX for bulk edit](https://github.com/CesarBianchi/JellyfinEasyMetadataManager)
- [MusicBrainz Picard — canonical metadata-editing UX bar](https://picard.musicbrainz.org/)

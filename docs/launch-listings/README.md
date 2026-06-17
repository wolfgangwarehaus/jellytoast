# Launch directory listings — ready-to-submit drafts

Three community directory entries for jellytoast, drafted against each repo's
**current** schema (verified 2026-06-17). **You submit each one** — they go to
third-party repos, so the PR is yours to open. Do these **before** social posts;
the catalog/badge entries are what the announcements link to.

**Two corrections already applied** to the auto-drafts:
- Name is lowercase **`jellytoast`** everywhere (our branding rule — never "Jellytoast"/"JellyToast").
- **No Flathub links** — Flathub is parked (see `docs/TODO.md`); listing a Flathub URL that 404s would fail review.

> ⚠️ Schemas drift. Before opening each PR, skim the live target file and a
> neighbouring entry to confirm field names still match. Run each repo's
> validation command (below) locally first.

---

## 1. Navidrome apps catalog → `navidrome/website`

- **File:** `assets/apps/jellytoast/index.yaml` (new) + the 6 screenshots beside it.
- **Content:** `navidrome/index.yaml` in this folder.
- **Screenshots to copy** into `assets/apps/jellytoast/` (all already WebP, 1600×900, <60 KB, in `docs/screenshots/webp/`):
  `now-playing.webp` (thumbnail), `library.webp`, `cast.webp`, `downloads.webp`, `smart-playlists.webp`, `radio.webp`.
- **Steps:**
  1. Fork `navidrome/website`; clone it.
  2. `mkdir -p assets/apps/jellytoast` and copy `index.yaml` + the 6 webp files in.
  3. `npm install && npm run validate:app jellytoast` — must pass.
  4. Commit, push, open a PR to `navidrome/website` (default branch).
- **Notes:** `api: OpenSubsonic` is case-sensitive and required. Thumbnail must be real UI (it is). The release-date badge auto-populates from the v0.1.0 GitHub release.

## 2. Jellyfin website clients page → `jellyfin/jellyfin.org`

- **File:** `src/data/clients.ts` (edit) — insert the entry in the third-party
  clients array, alphabetical by `id` (after `jellyamp`, before `supersonic`).
- **Content:** `jellyfin-org/clients-entry.ts` in this folder.
- **Steps:**
  1. Fork `jellyfin/jellyfin.org`; branch `add/jellytoast-client`.
  2. Paste the entry into `src/data/clients.ts` at the right alphabetical spot.
  3. `npm install && npm run build` — confirm no TypeScript errors.
  4. Commit, push, open the PR.
- **Notes:** The `Jelly[word]` name may draw a review comment under Jellyfin's
  branding guidance — it's tolerated, not auto-rejected; be ready to note jellytoast
  predates nothing and is a clear third-party project. Model field names on the
  Feishin/Jellyamp entries if the schema differs from the draft.

## 3. awesome-jellyfin → `awesome-jellyfin/awesome-jellyfin`

- **File:** `assets/clients/clients.yaml` (edit) — **not** `CLIENTS.md` (generated).
- **Content:** `awesome-jellyfin/clients-entry.yaml` in this folder.
- **Steps:**
  1. Fork the repo; branch `add/jellytoast`.
  2. Add the entry to `assets/clients/clients.yaml` in alphabetical order by canonical name.
  3. Run the repo's validation (see its `CONTRIBUTING.md`).
  4. Commit with a **Conventional Commit**: `feat(clients): add jellytoast`. Push, open PR.
- **Notes:** `types: [Music]` puts it in the Music Clients section. `targets: [Windows, Linux]` (macOS not yet supported).

---

## Suggested order
Navidrome (home turf) → awesome-jellyfin (quick) → jellyfin.org (branding review may be slower).
Keep these files updated on each release so future version bumps are a one-line edit.

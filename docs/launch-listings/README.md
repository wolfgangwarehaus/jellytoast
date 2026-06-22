# Launch directory listings — ready-to-submit drafts

Directory/catalog entries for jellytoast, each drafted against the target repo's
**current** schema (re-verified 2026-06-22 against live files). **You open each PR**
— they go to third-party repos. Do these **before** the social posts; the posts link
to these catalog pages. The copy-paste announcement text lives in
[`../launch-posts.md`](../launch-posts.md).

**Standing corrections applied to every entry:**
- Name is lowercase **`jellytoast`** everywhere (branding rule — never "Jellytoast"/"JellyToast").
- **No Flathub / Flatpak links** — jellytoast ships none; a 404 link fails review.
- The install story is now complete (Microsoft Store + winget + .deb + AppImage + PyPI),
  so every entry surfaces the **Microsoft Store** link.

> ⚠️ Schemas drift. Before each PR, skim the live target file + a neighbouring entry,
> and run the repo's validation command (below).

---

## 1. Navidrome apps catalog → `navidrome/website`  (home turf)

- **File:** `assets/apps/jellytoast/index.yaml` (new) + `thumbnail.webp` + gallery beside it.
- **Content:** [`navidrome/index.yaml`](navidrome/index.yaml).
- **Screenshots** (copy from `docs/screenshots/webp/`, all WebP, max 1200×1200):
  `now-playing.webp` (thumbnail — must be a real screenshot, not a logo),
  `library.webp`, `cast.webp`, `downloads.webp`, `smart-playlists.webp`, `radio.webp`.
- **Steps:**
  1. Fork + clone `navidrome/website`.
  2. `mkdir -p assets/apps/jellytoast`; copy `index.yaml` + the 6 webp files in.
  3. `npm install && npm run convert:images jellytoast` (converts/resizes to WebP).
  4. `npm run validate:app jellytoast` — must pass before the PR will.
  5. Commit, push, PR to `navidrome/website`.
- **Notes:** `api: OpenSubsonic` is required + case-sensitive. `repoUrl` drives both the
  open-source badge and the auto last-release-date badge. Template to copy: the
  `assets/apps/aonsoku/` entry (a real, accepted cross-platform Subsonic client).

## 2. awesome-jellyfin → `awesome-jellyfin/awesome-jellyfin`  (quick)

- **File:** `assets/clients/clients.yaml` on `main` — **not** `CLIENTS.md` (generated).
- **Content:** [`awesome-jellyfin/clients-entry.yaml`](awesome-jellyfin/clients-entry.yaml).
- **Steps:**
  1. Fork; branch `add/jellytoast`.
  2. Add the entry to `assets/clients/clients.yaml` in **alphabetical** order
     (a `/sort-check -fix` bot flags out-of-order PRs).
  3. Commit with a Conventional Commit: `feat(clients): add jellytoast`. Push, PR.
- **Notes:** `types: [Music]` → the Music Client section. The `github` download type takes
  **separate `owner:` + `repo:` keys** (not `owner/repo`). Microsoft Store uses the
  recognized `shield` icon `"Microsoft Store"`; PyPI uses a `text` download. OMIT `official`.

## 3. jellyfin.org clients page → `jellyfin/jellyfin.org`  (branding review may be slower)

- **File:** `src/data/clients.ts` on `master` — insert in the third-party array,
  alphabetical by `id` (after `jellyamp`, before `supersonic`).
- **Content:** [`jellyfin-org/clients-entry.ts`](jellyfin-org/clients-entry.ts).
- **Steps:**
  1. Fork; branch `add/jellytoast-client`.
  2. Paste the entry at the right alphabetical spot.
  3. `npm install && npm run build` — confirm no TypeScript errors.
  4. Commit, push, PR.
- **Notes:** Store/Releases go in `primaryLinks`, repo+site in `secondaryLinks`; OMIT
  `recommended` (first-party only). Inclusion requires "first-rate Jellyfin support" —
  satisfied (Jellyfin + Subsonic at parity). The `Jelly[word]` name may draw a branding
  comment; it's tolerated, not auto-rejected. `Platform.Linux` renders as "Generic Linux".

## 4. Awesome-SelfHosted-Music → `Tal0na/Awesome-SelfHosted-Music-Awesome`  (new)

- **File:** `Servers-Clients/linux.md` **and** `Servers-Clients/windows.md` on `main`.
- **Content:** [`music-awesome/entry.md`](music-awesome/entry.md) (full block per file).
- **Steps:** Fork; add the `### 🎧 jellytoast` block to each file next to
  Feishin/Supersonic; end with `---`; PR (no CONTRIBUTING — "follow existing structure").
- **Notes:** The old slug `SelfHosted-Music-Awesome` redirects to the `Awesome-`-prefixed
  name — target the new one. Skip `mac.md` (no verified Mac build).

---

## Web-form listings (no repo PR — needs your account)

- **AlternativeTo** (`alternativeto.net`): **create the account ≥1 week before submitting**
  (anti-spam gate). User-icon → "Suggest new application" → category *Audio Player* →
  then mark it as an alternative to **Feishin / Supersonic / Sonixd**.
- **selfh.st/apps** (companion-app directory): email **hello@selfh.st** (or
  `selfh.st/contact/`) with name, repo, site, and "companion **client** for
  Navidrome/Jellyfin". *Not* `/submit/` (that's the general newsletter form). Being
  listed auto-generates a release RSS feed that feeds the **Self-Host Weekly** newsletter.

## Suggested order
Navidrome → awesome-jellyfin → Awesome-SelfHosted-Music → jellyfin.org → AlternativeTo + selfh.st.
Keep these files updated each release so version bumps are a one-line edit.

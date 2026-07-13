# Navidrome clients-page entry (prepared, not yet submitted)

Ready-to-submit entry for jellytoast on Navidrome's [apps catalog](https://www.navidrome.org/docs/developers/adding-apps/).
Everything here is copied verbatim into a PR against
[`navidrome/website`](https://github.com/navidrome/website) — no edits needed.

**Validated** against the repo's live `assets/apps/app-schema.json` (jsonschema
PASS, 2026-07-13). Eligibility is met by **app-store availability** (Mac App
Store + Microsoft Store), so the 15-GitHub-stars rule doesn't apply.

## Contents

- `index.yaml` — the entry (schema-valid). `api: OpenSubsonic`; open-source
  badge is automatic (`repoUrl` set, `isOpenSource` omitted → treated as OSS).
- `library.webp` — catalog thumbnail (a real screenshot, per the "must NOT be a
  logo" rule).
- `now-playing.webp`, `cast.webp`, `theme-picker.webp`, `smart-playlists.webp`,
  `mini-expanded.webp` — gallery (their max is 5).
- All images: WebP, 1200×750 (under the 1200×1200 cap), each < 500 KB.

## To submit (when ready)

```bash
# 1. Fork + clone navidrome/website, then:
mkdir -p assets/apps/jellytoast
cp packaging/navidrome/index.yaml       <website>/assets/apps/jellytoast/
cp packaging/navidrome/*.webp           <website>/assets/apps/jellytoast/

# 2. In the website repo, validate (their gatekeeper):
npm run validate:app jellytoast
#   (npm run convert:images jellytoast is NOT needed — images are already
#    WebP within spec; run it only if you swap in fresh PNG/JPEG sources.)

# 3. Open the PR against navidrome/website.
```

## Refreshing the screenshots

Regenerate from the canonical WebP set with the same downscale used here:

```python
from PIL import Image; import os
order = ["library","now-playing","cast","theme-picker","smart-playlists","mini-expanded"]
for n in order:
    im = Image.open(f"docs/screenshots/webp/{n}.webp").convert("RGB")
    im.thumbnail((1200,1200), Image.LANCZOS)
    im.save(f"packaging/navidrome/{n}.webp", "WEBP", quality=82, method=6)
```

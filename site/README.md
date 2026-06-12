# Landing page (wolfgangwarehaus.com)

`index.html` is a self-contained static page (inline CSS, one progressive
JS fetch that points the download buttons at the exact latest release
assets — degrades to the Releases page link without JS).

Before publishing: drop in the hero screenshot when it exists (commented
block). Ko-fi handle (`wolfgangwarehaus`) and the brand SVG are already
wired in.

## Publish via GitHub Pages + custom domain

1. Repo Settings → Pages → "Deploy from a branch" → `main`, folder `/site`
   (or add a 5-line deploy workflow later — branch-deploy is fine at this
   scale). Site appears at `wolfgangwarehaus.github.io/jellytoast`.
2. Custom domain: in the same Pages settings enter `wolfgangwarehaus.com`;
   GitHub writes the `CNAME` file. At the registrar add:
   - apex `A` records → `185.199.108.153`, `.109.153`, `.110.153`, `.111.153`
   - `www` `CNAME` → `wolfgangwarehaus.github.io`
3. Tick "Enforce HTTPS" once the cert provisions (minutes to an hour).

## Ko-fi everywhere else

Done — `.github/FUNDING.yml` (`ko_fi: wolfgangwarehaus`) puts the
"Sponsor ♡" button on the repo header, and the README badge row carries
a Ko-fi badge.

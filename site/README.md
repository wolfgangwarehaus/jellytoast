# Landing page (wolfgangwarehaus.com/jellytoast)

`index.html` is a self-contained static page (inline CSS + JS: a
crossfade screenshot carousel, plus one progressive fetch that points the
download buttons at the exact latest release assets — both degrade
gracefully without JS to the first screenshot and the Releases page link).

The carousel screenshots (`now-playing.webp`, `library.webp`, `cast.webp`,
`mini-compact.webp`, `mini-expanded.webp`) are copied from
`docs/screenshots/webp/`; the Ko-fi handle is `wolfgangwarehaus`.

## How it's served

`pages.yml` deploys this `site/` folder to GitHub Pages on every push to
`main` that touches `site/**`. This repo has **no custom domain of its
own** — the apex `wolfgangwarehaus.com` is owned by the
[`wolfgangwarehaus.github.io`](https://github.com/wolfgangwarehaus/wolfgangwarehaus.github.io)
user-site repo (the umbrella landing page; it holds the `CNAME`). GitHub
then serves every project Pages site under that account at
`wolfgangwarehaus.com/<repo>`, so this page lives at
**`wolfgangwarehaus.com/jellytoast`** automatically — and future apps get
their own subpath for free.

Keep asset links relative (`now-playing.webp`, not `/now-playing.webp`) so
they resolve under the `/jellytoast/` prefix; the only absolute URLs are
the `og:` tags, which point at the full subpath.

DNS lives on Cloudflare (apex `A` → `185.199.108–111.153`, `www` `CNAME` →
`wolfgangwarehaus.github.io`) and is attached to the umbrella repo, not
this one.

## Ko-fi everywhere else

`.github/FUNDING.yml` (`ko_fi: wolfgangwarehaus`) puts the "Sponsor ♡"
button on the repo header; the landing page carries a Ko-fi tip link.

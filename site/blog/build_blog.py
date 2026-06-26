#!/usr/bin/env python3
"""Build the jellytoast blog: render site/blog/posts/*.md into styled HTML pages
plus an index, matching the landing-page look (see blog.css).

Authoring a post — drop a Markdown file in ``site/blog/posts/`` named
``YYYY-MM-DD-some-slug.md`` with a small frontmatter block at the top:

    ---
    title: A little update
    date: 2026-06-26
    summary: One line shown on the blog index (optional).
    ---

    Your **Markdown** body goes here.

Then commit + push. The GitHub Pages workflow (.github/workflows/pages.yml) runs
this script and deploys the result to https://wolfgangwarehaus.com/jellytoast/blog/.

Preview locally:

    pip install markdown
    python site/blog/build_blog.py
    # then open site/blog/index.html in a browser

The generated *.html files are git-ignored (see .gitignore) — they are rebuilt
fresh on every deploy, so the repo only ever holds your Markdown.
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

try:
    import markdown as md
except ModuleNotFoundError:
    sys.exit("build_blog.py needs the 'markdown' package — run `pip install markdown`")

BLOG_DIR = Path(__file__).resolve().parent
POSTS_DIR = BLOG_DIR / "posts"
SITE_TITLE = "jellytoast"
BLOG_TITLE = "jellytoast blog"
BLOG_DESC = "Release notes and little updates on jellytoast."

# One template for both the index and each post. The body is supplied fully by
# the caller; only the <head> fields and footer are shared. Paths are relative to
# site/blog/ (where every generated page lives), so "../" is the jellytoast home.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta property="og:title" content="{ogtitle}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="{ogtype}">
<link rel="icon" href="../jellytoast.svg" type="image/svg+xml">
<link rel="stylesheet" href="blog.css">
</head>
<body>
<div class="wrap">
{body}
  <footer>
    <a href="../">jellytoast home</a> ·
    <a href="./">Blog</a> ·
    <a href="https://github.com/wolfgangwarehaus/jellytoast">Source</a>
  </footer>
</div>
</body>
</html>
"""


def parse_post(path: Path) -> tuple[dict[str, str], str]:
    """Split a post file into (frontmatter dict, markdown body)."""
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            _, frontmatter, body = parts
            body = body.lstrip("\n")
            for line in frontmatter.strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    value = value.strip()
                    # CMS-authored YAML may quote values — strip one matching pair.
                    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                        value = value[1:-1]
                    meta[key.strip().lower()] = value
    # A datetime value (e.g. an ISO timestamp from the editor) → keep the date.
    if "date" in meta:
        meta["date"] = meta["date"].split("T")[0].strip()
    return meta, body


def build() -> int:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    posts: list[dict[str, str]] = []

    for path in sorted(POSTS_DIR.glob("*.md")):
        meta, body = parse_post(path)
        if not meta.get("title"):
            print(f"  skip {path.name}: no 'title:' in frontmatter", file=sys.stderr)
            continue
        title = meta["title"]
        date = meta.get("date", "")
        summary = meta.get("summary", "")
        slug = path.stem  # e.g. 2026-06-26-welcome -> 2026-06-26-welcome.html
        rendered = md.markdown(body, extensions=["extra", "sane_lists"])

        article = (
            '  <a class="backhome" href="../">&larr; jellytoast</a>\n'
            "  <article>\n"
            + (f'    <p class="date">{html.escape(date)}</p>\n' if date else "")
            + f"    <h1>{html.escape(title)}</h1>\n"
            + rendered
            + "\n  </article>\n"
        )
        (BLOG_DIR / f"{slug}.html").write_text(
            PAGE.format(
                title=f"{html.escape(title)} — {SITE_TITLE} blog",
                desc=html.escape(summary or BLOG_DESC),
                ogtitle=html.escape(title),
                ogtype="article",
                body=article,
            ),
            encoding="utf-8",
        )
        posts.append({"title": title, "date": date, "summary": summary, "slug": slug})

    # Newest first — ISO YYYY-MM-DD dates sort correctly as plain strings.
    posts.sort(key=lambda p: p["date"], reverse=True)

    if posts:
        items = "\n".join(
            "    <li>\n"
            f'      <a class="title" href="{p["slug"]}.html">{html.escape(p["title"])}</a>\n'
            + (f'      <div class="date">{html.escape(p["date"])}</div>\n' if p["date"] else "")
            + (f'      <div class="summary">{html.escape(p["summary"])}</div>\n' if p["summary"] else "")
            + "    </li>"
            for p in posts
        )
        listing = f'  <ul class="posts">\n{items}\n  </ul>'
    else:
        listing = '  <p class="empty">No posts yet — check back soon.</p>'

    index_body = (
        "  <header>\n"
        '    <a href="../" aria-label="Back to the jellytoast home page">'
        '<img src="../jellytoast.svg" alt="jellytoast logo"></a>\n'
        f"    <h1>{BLOG_TITLE}</h1>\n"
        f'    <p class="sub">{BLOG_DESC}</p>\n'
        "  </header>\n" + listing + "\n"
    )
    (BLOG_DIR / "index.html").write_text(
        PAGE.format(
            title=BLOG_TITLE,
            desc=BLOG_DESC,
            ogtitle=BLOG_TITLE,
            ogtype="website",
            body=index_body,
        ),
        encoding="utf-8",
    )
    print(f"blog: built {len(posts)} post(s) + index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())

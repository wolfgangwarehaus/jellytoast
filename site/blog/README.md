# jellytoast blog

Little updates, published at **<https://wolfgangwarehaus.com/jellytoast/blog/>**.

Posts are plain Markdown. You write a file; a GitHub Action turns it into a
styled page and deploys it. You never touch HTML.

**Voice:** short and casual — these are little updates, not announcements. Same
as the changelog: get the facts right, leave the gloss out, keep it brief.

## Write a post

1. Create a file in [`posts/`](posts/) named `YYYY-MM-DD-some-slug.md`
   (the date prefix keeps them ordered; the slug becomes the page URL).
2. Start it with a small frontmatter block, then write your Markdown:

   ```markdown
   ---
   title: A little update
   date: 2026-06-26
   summary: One line shown on the blog index (optional).
   ---

   Your **Markdown** body goes here. Headings, lists, links, code, images —
   all the usual Markdown works.
   ```

   - `title` is required (a file without one is skipped).
   - `date` should be `YYYY-MM-DD`; posts sort newest-first by it.
   - `summary` is optional — it's the blurb under the title on the index.

3. Commit and push to `main`. That's it — the
   [Pages workflow](../../.github/workflows/pages.yml) rebuilds the blog and
   deploys it within a minute or two. The post appears on the index and at
   `…/jellytoast/blog/YYYY-MM-DD-some-slug.html`.

## Preview locally (optional)

```bash
pip install markdown
python site/blog/build_blog.py
# open site/blog/index.html in your browser
```

## How it works

- [`build_blog.py`](build_blog.py) renders every `posts/*.md` into a styled HTML
  page plus `index.html`, using [`blog.css`](blog.css) (which mirrors the
  landing page's look).
- The generated `*.html` files are git-ignored — they're rebuilt on every
  deploy, so the repo only ever holds your Markdown.

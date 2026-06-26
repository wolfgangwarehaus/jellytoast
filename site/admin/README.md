# Blog editor (Sveltia CMS)

A private web editor for the jellytoast blog, served at
**<https://wolfgangwarehaus.com/jellytoast/admin/>**. Sign in with GitHub, write a
post, hit publish — it commits the Markdown into [`../blog/posts/`](../blog/posts/),
and the Pages build renders it. The page is public to load, but **publishing
requires GitHub auth as the repo owner — so only you can post.**

## One-time setup (do this once — it then covers every project's blog)

The editor needs a tiny auth relay so it can log in with GitHub. **One** Cloudflare
Worker serves all your blogs (jellytoast, dough, butterPDF, …).

### 1. Deploy the auth Worker (Cloudflare, free tier)
- Deploy **sveltia-cms-auth**: <https://github.com/sveltia/sveltia-cms-auth>
  (one-click "Deploy to Cloudflare", or `wrangler deploy`).
- Note the Worker URL, e.g. `https://jellytoast-cms-auth.<sub>.workers.dev`.

### 2. Create a GitHub OAuth App
- GitHub → Settings → Developer settings → **OAuth Apps** → **New OAuth App**.
- **Homepage URL:** `https://wolfgangwarehaus.com`
- **Authorization callback URL:** `https://<your-worker-url>/callback`
- Copy the **Client ID**, then generate a **Client secret**.

### 3. Configure the Worker
In the Cloudflare dashboard → your Worker → Settings → Variables, set:
- `GITHUB_CLIENT_ID` — the OAuth app's client id
- `GITHUB_CLIENT_SECRET` — the OAuth app's client secret
- `ALLOWED_DOMAINS` — `wolfgangwarehaus.com` (comma-separated; add future blog
  domains, or use a wildcard such as `*.wolfgangwarehaus.com`)

### 4. Point the editor at the Worker
In [`config.yml`](config.yml), set `backend.base_url` to your Worker URL (replace
the `REPLACE-ME` placeholder), then commit + push.

Done — open `/jellytoast/admin/`, click **Sign in with GitHub**, and write.

## Writing a post
- **New Post** → fill Title, Date, an optional Summary, and write the body.
- **Publish** commits `site/blog/posts/YYYY-MM-DD-title.md`; the Pages action
  rebuilds the blog in a minute or two.
- It's the same Markdown as writing the file by hand (see
  [`../blog/README.md`](../blog/README.md)) — the editor just does the file +
  commit for you.

## Reusing this for dough / butterPDF (later)
When those sites exist, no new Worker or OAuth app is needed:
1. Copy `site/admin/` into the new repo's site folder.
2. In its `config.yml`, set `backend.repo` to that repo (e.g. `wolfgangwarehaus/dough`).
3. Add the new site's domain to the Worker's `ALLOWED_DOMAINS` (or rely on the wildcard).

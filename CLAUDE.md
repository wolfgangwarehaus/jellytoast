# jellytoast — Claude Code project guide

A native PySide6 / Qt6 **music-only** client for Jellyfin and Subsonic / Navidrome.
Package: `jellytoast/` · run it with `python3 -m jellytoast`.

> Branding is always lowercase **jellytoast** — never "JellyToast".

---

## Working across machines (read this for delegated work)

This repo is worked by several Claude Code instances on different machines:

| Label | Machine | Owns |
|---|---|---|
| `needs:linux` | primary CachyOS / Arch dev box | most development |
| `needs:ubuntu` | the Ubuntu box | Linux `.deb` / packaging / X11 verification (Docker + real X11) |
| `needs:windows` | the Windows 11 box | MSIX / Microsoft Store / winget / Windows verification |

**Claude's local memory (`~/.claude/…`) does NOT sync between machines.** The only
channel all the machines share is **this GitHub repo**. So cross-machine work lives
in **PRs + committed files** — never assume another machine can see your local
memory, session notes, or this conversation.

### Picking up delegated work (you are the receiving machine)
Identify your box (`cat /etc/os-release` → `ID=ubuntu` ⇒ `needs:ubuntu`,
`ID=cachyos`/`arch` ⇒ `needs:linux`; on Windows ⇒ `needs:windows`), then:

```bash
gh pr list --label needs:<your-machine>     # the shared task board for your box
gh pr checkout <N>                           # check the branch out locally
```

- The **PR body is the task brief** — an imperative checklist of exactly what to do.
- The **detailed steps** live in an in-branch worklist doc (e.g.
  `packaging/*SESSION*.md`, `packaging/deb/*WORKLIST*.md`); the PR body links to it.
- Tick the checkboxes as you go. When the work is done **and verified**, squash-merge.
- If you get **blocked**, post a **PR comment** with what you found — that's how the
  next instance (on any machine) sees it.

### Delegating work TO another machine (you are the sending machine)
1. Push the branch **and open the PR immediately** (draft if WIP) — never leave a
   dangling branch; `gh pr list` is the only thing the other box can discover.
2. Write the PR body as an **imperative checklist** of exactly what to do + how to
   know it's done ("run X, expect Y green").
3. **Label it** `needs:<target-machine>`.
4. Commit the detailed worklist as a doc on the branch and link it from the body.

---

## Build / test / release

- **Run:** `python3 -m jellytoast`
- **Test:** `pytest -n auto -q` · **lint:** `ruff check .`
- `main` is branch-protected: **4 required CI checks** (build + test on Python
  3.11 / 3.12 / 3.13), **squash-merge** only. Admins can override.
- **Cut a release:** `dev/cut_release.sh X.Y.Z [--push]` — bumps the version in
  every source-of-truth file, snips the CHANGELOG `[Unreleased]` block into a dated
  one, commits + tags. `--push` pushes the tag, which triggers `release.yml`.

### Release lifecycle — use these terms consistently
**prepare** → **cut** → **build (draft)** → **publish (release)**

| Stage | What happens | Public? |
|---|---|---|
| **prepare** | changes merged to `main` (fixes + CHANGELOG) | no |
| **cut** | `cut_release.sh` stamps the version + creates the `vX.Y.Z` tag | no |
| **build** | tag push → `release.yml` compiles `.deb`/`.exe`/wheel into a **draft** Release | no (maintainer-only) |
| **publish** | the draft is **manually** made public → users can download | **yes** |

"Release" means the final **publish** step. A failed *build* or an unpublished
draft means nothing reached users.

---

## Where things live

- **Backlog / status:** `docs/TODO.md` · **changelog:** `docs/CHANGELOG.md`
- **Packaging:** `packaging/` — `deb/`, `msix/`, `winget/`, `windows/`; the
  step-by-step session docs are named `*SESSION*.md` / `*WORKLIST*.md`.
- **Release automation:** `dev/cut_release.sh`, `.github/workflows/release.yml`,
  `.github/workflows/ci.yml`.

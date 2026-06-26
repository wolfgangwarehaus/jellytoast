# jellytoast — Claude Code project guide

A native PySide6 / Qt6 **music-only** client for Jellyfin and Subsonic / Navidrome.
Package: `jellytoast/` · run it with `python3 -m jellytoast`.

> Branding is always lowercase **jellytoast** — never "JellyToast".

## Build / test / release

- **Run:** `python3 -m jellytoast` (or `bash dev/run.sh`)
- **Test:** `pytest -n auto -q` · **lint:** `ruff check .`
- `main` is branch-protected: **4 required CI checks** (build + test on Python
  3.11 / 3.12 / 3.13), **squash-merge** only. Admins can override.
- **Cut a release:** `dev/cut_release.sh X.Y.Z [--push]` — bumps the version in
  every source-of-truth file, snips the CHANGELOG `[Unreleased]` block into a dated
  one, commits + tags. `--push` pushes the tag, which triggers `release.yml` to
  build a **draft** release; publishing it is a manual step.

## Where things live

- **Spec (what it does today):** `docs/SPEC.md` · **Decisions (why):** `docs/decisions.md`
- **Changelog:** `docs/CHANGELOG.md`
- **Packaging:** `packaging/` — `deb/`, `msix/`, `winget/`, `windows/`, `macos/`, `appimage/`
- **Release automation:** `dev/cut_release.sh`, `.github/workflows/`

_Operational runbooks (the full release process, store submission, signing, the
blog-editor setup, and the cross-machine workflow) live in a separate **private**
ops repo, not here._

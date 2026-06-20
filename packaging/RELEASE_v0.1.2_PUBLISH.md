# Release worklist — finish & publish **v0.1.2** (`needs:linux`)

The 0.1.2 **draft** is built and green. This is the **publish** stage — making it
public and propagating it to every channel. Tick the boxes as you go; squash-merge
when done. If a channel blocks, leave a PR comment so the next instance sees it.

## Where 0.1.2 stands (do not redo)

`prepare ✅ → cut ✅ → build/draft ✅ → publish ⬜` (see `CLAUDE.md` for the terms).

- **#164 merged** (`main` @ `b52657d`): the `.deb` now declares the complete Qt
  `xcb` dependency closure (22 pkgs). This fixes an X11/XWayland launch abort that
  affected **v0.1.0 and v0.1.1's `.deb`** too.
- Verified on the Ubuntu box: `ldd` closure complete · real `xcb` boot · all 22
  pkg names valid on noble/resolute/trixie · **clean-container smoke green on
  ubuntu:24.04 + ubuntu:26.04 + debian:stable** (the gold-standard proof — the
  same smoke that *failed* the premature v0.1.2 tag). See
  `packaging/deb/XCB_DEPS_WORKLIST.md` for the derivation.
- Version already stamped **0.1.2** in `pyproject.toml`, `jellytoast/version.py`,
  the metainfo, and `docs/CHANGELOG.md` (`[0.1.2]` block, X11 fix is the headline).
- `v0.1.2` tag re-pointed to `2089605` (was a premature pre-fix tag whose build
  failed); release run **27881102649** all-green built the **draft**:
  - `jellytoast_0.1.2_amd64.deb` (the fixed one) · `…windows-x64-setup.exe` ·
    `…windows-x64-portable.zip` · `…py3-none-any.whl` · `…0.1.2.tar.gz` ·
    `SHA256SUMS`

---

## 0. Pre-publish gate

- [ ] **Sanity-check the draft.** `gh release view v0.1.2 --web` — confirm all six
      assets are present, the notes match `docs/CHANGELOG.md` `[0.1.2]`, and the
      title is `jellytoast v0.1.2`.
- [ ] **License consistency (verify-only — expected to already be clean).** Confirm
      `LICENSE` (GPL-2.0 text) + `pyproject.toml` `license` + metainfo
      `<project_license>` + `docs/LICENSING.md` all say **GPL-2.0-or-later**, and
      that **no channel doc claims GPL-3.0** (`grep -rniE 'gpl-?3|gplv3' packaging
      docs` should be empty). `docs/LICENSING.md` already documents the
      "or-later is load-bearing" rationale — just confirm nothing drifted.
      *(This was a flagged worry from the Ubuntu/MSIX notes; current state looks
      consistent — close it or fix any stray reference.)*

## 1. Publish the GitHub release  *(the public moment)*

- [ ] `gh release edit v0.1.2 --draft=false --latest`
- [ ] Verify: `gh release view v0.1.2 --json isDraft,url` → `isDraft:false`, and
      the canonical URL is `…/releases/tag/v0.1.2`.
- [ ] Spot-check a download URL resolves (used by AUR/winget below), e.g.
      `…/releases/download/v0.1.2/jellytoast_0.1.2_amd64.deb`.

## 2. PyPI  *(manual — CI only runs `twine check`, never uploads)*

- [ ] Build from the tag (or reuse the release's `.whl` + `.tar.gz`):
      `python -m build` → `dist/jellytoast-0.1.2*`.
- [ ] `python -m twine check dist/*` (must pass — same gate as `ci.yml`).
- [ ] `python -m twine upload dist/jellytoast-0.1.2*` (needs the PyPI token).
- [ ] Verify: `pipx install jellytoast==0.1.2` (or `pip index versions jellytoast`).

## 3. AUR  *(first real publish — PKGBUILD is still at 0.1.0, never pushed)*

See `packaging/aur/README.md` for the SSH/clone-and-push flow.
- [ ] Bump `packaging/aur/PKGBUILD`: `pkgver=0.1.2`, `pkgrel=1`.
- [ ] `cd packaging/aur && updpkgsums` (pins the real sha256 of the v0.1.2 source
      tarball) → then `makepkg --printsrcinfo > .SRCINFO`.
- [ ] `makepkg -si` in a clean chroot / on the CachyOS box — **must build & launch**.
- [ ] Push to AUR (`ssh aur@aur.archlinux.org`; see the README) and commit the
      updated PKGBUILD + `.SRCINFO` back here.
- [ ] Verify: package page shows 0.1.2; `paru -S jellytoast` (or yay) installs it.
      *(Note: 0.1.1 was skipped on AUR — going straight 0.1.0 → 0.1.2 is fine.)*

## 4. winget + Microsoft Store / MSIX  *(coordinates with `needs:windows` #146)*

`packaging/winget/` manifests are at 0.1.0; **PR #146 (`needs:windows`) carries
winget v0.1.1 + the MSIX/Store bring-up.** Don't duplicate Windows work here —
coordinate:
- [ ] Decide with #146 whether winget jumps **0.1.0 → 0.1.2** (skip 0.1.1) or lands
      0.1.1 first. Prefer going straight to **0.1.2** so the live channel matches
      this release. `wingetcreate update wolfgangwarehaus.jellytoast --version
      0.1.2 --urls …/v0.1.2/jellytoast-0.1.2-windows-x64-setup.exe` (see
      `packaging/winget/README.md`).
- [ ] Comment on **#146** with the 0.1.2 `setup.exe` URL + SHA so the Windows box
      can submit winget + the Store MSIX against the published release.
- [ ] (Tracking only — the actual MSIX/Store submission is `needs:windows`.)

## 5. Universal-Linux channels (AppImage / Flathub)  *(heavier — split out if it stalls)*

`docs/TODO.md` planned these for 0.1.2; both are net-new and may deserve their own
PR rather than blocking the publish.
- [ ] **AppImage**: build the universal-Linux AppImage for 0.1.2 (research is in
      `docs/TODO.md`); attach to the release or a separate artifact. If not ready,
      **defer to a follow-up PR and uncheck — don't block 0.1.2 publish on it.**
- [ ] **Flathub**: hand-submit the Flatpak (`packaging/flatpak/`) to Flathub. Also
      deferrable — note status in a PR comment.

## 6. Docs & download channels → 0.1.2

- [ ] `README.md` — bump any pinned version / download links to 0.1.2.
- [ ] `site/index.html` (landing page) — download buttons + version → 0.1.2.
- [ ] `docs/TODO.md` — move 0.1.2 publish items to "closed work"; note AUR
      first-publish, any deferred channel.
- [ ] Any directory-listing drafts under `docs/launch-listings/` that reference a
      version.

## 7. Close-out

- [ ] All boxes above ticked or explicitly deferred-with-a-comment.
- [ ] Squash-merge this PR.
- [ ] Sanity: `winget`/`pipx`/`paru` each install **0.1.2**; the GitHub release is
      public and `--latest`.

---

### Cross-references
- `.deb` X11 fix + verification: **#164** (merged), `packaging/deb/XCB_DEPS_WORKLIST.md`.
- Windows MSIX / winget: **#146** (`needs:windows`).
- Release lifecycle & machine ownership: `CLAUDE.md`.

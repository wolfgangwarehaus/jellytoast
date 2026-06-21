# Release worklist — finish & publish **v0.1.2** (`needs:linux`)

The 0.1.2 **draft** is built and green. This is the **publish** stage — making it
public and propagating it to every channel. Tick the boxes as you go; squash-merge
when done. If a channel blocks, leave a PR comment so the next instance sees it.

## Where 0.1.2 stands (do not redo)

`prepare ✅ → cut ✅ → build/draft ✅ → publish ✅` (see `CLAUDE.md` for the terms).
**GitHub release PUBLISHED + Latest (2026-06-20).** Remaining channels: PyPI
(deferred — token), AUR (deferred — registration frozen), winget/MSIX (#146, commented).

- **#164 merged** (`main` @ `b52657d`): the `.deb` now declares the complete Qt
  `xcb` dependency closure (22 pkgs). This fixes an X11/XWayland launch abort that
  affected **v0.1.0 and v0.1.1's `.deb`** too.
- Verified on the Ubuntu box: `ldd` closure complete · real `xcb` boot · all 22
  pkg names valid on noble/resolute/trixie · **clean-container smoke green on
  ubuntu:24.04 + ubuntu:26.04 + debian:stable** (the gold-standard proof — the
  same smoke that *failed* the premature v0.1.2 tag). See
  `packaging/deb/XCB_DEPS_WORKLIST.md` for the derivation.
- **X11 on-box test (`needs:ubuntu`, DONE):** ran the installed `.deb` under
  `QT_QPA_PLATFORM=xcb` — xcb plugin loads, main window renders, Navidrome
  auto-connect + **library/album-art load**, MPRIS responsive, tray
  (`StatusNotifierWatcher`) present, **zero render/Qt/xcb errors**. Screenshot
  captured. ⚠️ This box is **GNOME 49 / Wayland-only** — no Xorg login session
  exists (no Xorg server even installed), so the test is via **XWayland** (the
  real X11 path the app uses; same `xcb` plugin + lib closure #164 fixed). A
  *standalone-Xorg* login test would need the CachyOS box — optional, since
  container smoke + XWayland cover the launch path. Audio is display-independent
  (libmpv→PipeWire) and was proven in round-2 QA; synthetic-input play didn't
  trigger here (known `xdotool` flakiness), not an X11/app fault.
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
- [x] **License consistency — verified, consistent (gate corrected).** The
      "`grep gpl-3` should be empty" expectation was WRONG — the GPL-3.0 references
      are *intentional and correct*: PySide6/Qt is **LGPL-3.0** (a dependency
      license; `THIRD-PARTY-NOTICES.md`), and the **MSIX Store** build is conveyed
      under **GPL-3.0** because it bundles a GPL libmpv/FFmpeg (`docs/LICENSING.md`,
      where GPL-2.0-**or-later** is load-bearing). The jellytoast **source** stays
      GPL-2.0-or-later (LICENSE / pyproject / metainfo) — unchanged.
      ⚠️ **Open question, NOT a publish blocker (worth a human/legal eye):** the
      GitHub **Windows `.exe`/portable** *also* bundles libmpv (GPL) like the MSIX,
      so it arguably warrants the same GPL-3.0 conveyance notice. The `.deb` uses
      *system* libmpv, so GPL-2.0-or-later is clean for it. Track separately.

## 1. Publish the GitHub release  *(the public moment)*

- [x] `gh release edit v0.1.2 --draft=false --latest` — **done.**
- [x] Verified: `isDraft=false`, prerelease=false, **Latest**; canonical URL
      `…/releases/tag/v0.1.2`.
- [x] Download URL resolves: `…/v0.1.2/jellytoast_0.1.2_amd64.deb` → HTTP 200.

## 2. PyPI  ✅ **DONE — via Trusted Publishing (no token)**

Superseded the manual `twine` flow: **#167** added `.github/workflows/pypi-publish.yml`
(OIDC Trusted Publishing). 0.1.2 was published by `gh workflow run pypi-publish.yml
-f tag=v0.1.2` (run 27882715944, success).
- [x] **0.1.2 live on PyPI** (`pypi.org/project/jellytoast` → 0.1.0, 0.1.2).
- [x] **Future releases auto-publish** on `release: published` — no token, no more
      missed versions (0.1.1 was the last manual miss).

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
- [x] Commented on **#146** with the 0.1.2 `setup.exe` URL + SHA256 (+ portable.zip
      SHA) so the Windows box can submit winget + the Store MSIX. **done.**
- [ ] (Tracking only — the actual MSIX/Store submission is `needs:windows`.)

## 5. Universal-Linux channel (AppImage)  *(heavier — split out if it stalls)*

- [ ] **AppImage**: planned, **deferred to 0.1.3** — net-new universal-Linux
      channel (research in `docs/TODO.md`); does NOT block 0.1.2.
- [x] **Flatpak / Flathub**: ⛔ **RETIRED — not a channel.** jellytoast does not
      ship Flatpak or Flathub (see `docs/TODO.md`); no manifest is maintained.

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

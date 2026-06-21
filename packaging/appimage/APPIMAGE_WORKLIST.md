# AppImage — the universal-Linux channel (0.1.3)

A self-contained `jellytoast-<ver>-x86_64.AppImage` that runs on any
glibc-compatible distro with **no install and no root**. It exists because the
`.deb` only covers Debian/Ubuntu and (now that Flatpak/Flathub are retired) there
is no other "any distro" Linux path.

## How it works

| Piece | File |
|---|---|
| Build script (onedir → AppDir → `appimagetool`) | `packaging/appimage/build_appimage.sh` |
| Clean-container self-containment smoke | `packaging/appimage/smoke_test_appimage.sh` |
| CI job + clean-container smoke | `.github/workflows/release.yml` → `build-linux-appimage` |
| Runtime libmpv hook (loads the bundled lib) | `jellytoast/player_backend.py` (the `APPDIR` guard) |

**Key difference from the `.deb`:** the `.deb` uses the *system* libmpv (declared
as `Depends`); the AppImage has no dependency mechanism, so `build_appimage.sh`
**vendors** `libmpv.so.2` + its FFmpeg closure + `libxcb-cursor0` into the AppDir.
`AppRun` puts that lib dir first on `LD_LIBRARY_PATH`. The PyInstaller spec is run
**unchanged** (it still strips libmpv's host closure from the `_internal` bundle —
correct, because we re-vendor a clean copy in `usr/lib`).

**glibc floor:** built on **ubuntu-22.04 (glibc 2.35)** to match the `.deb` and the
rest of the pipeline → runs on every current distro (Ubuntu 22.04+, Debian 12+,
Fedora 36+, Arch, openSUSE…). AppImage is forward-compatible only. To widen reach
to Debian 11 / RHEL-8 (glibc 2.31), build inside an `ubuntu:20.04` container
(the hosted 20.04 runner is retired) — left as a follow-up if older-distro demand
appears.

**No FUSE required on the user's machine:** `appimagetool` (continuous) embeds the
static type-2 runtime. `APPIMAGE_EXTRACT_AND_RUN=1` is the escape hatch (and is
what CI/containers use, since `appimagetool` itself wants FUSE otherwise).

**Auto-update:** the build embeds `gh-releases-zsync` update-info and emits a
`.AppImage.zsync` alongside the AppImage, so users running `AppImageUpdate` /
`appimageupdatetool` get delta updates. jellytoast ships no updater of its own.

## ⚠️ Status — needs a real CI build before shipping

Authored on the Arch dev box; **not yet built end-to-end** (PyInstaller isn't on
that box, and an Arch-built AppImage's glibc floor is non-distributable). The
authoritative verification is the CI job. Before relying on this for 0.1.3:

- [ ] **Run the build:** `gh workflow run release.yml` (workflow_dispatch dry-run)
      or push a `-rc` tag, and confirm the `build-linux-appimage` job is green —
      especially the clean-container smoke (`ubuntu:24.04` + `debian:stable` with
      NO libmpv installed). That step is the real "is libmpv self-contained?" test.
- [ ] **Verify the `import mpv` path** on a real desktop: download the artifact,
      `chmod +x`, run it on a distro WITHOUT system libmpv (e.g. a clean Fedora),
      and confirm audio plays. The `APPDIR` hook in `player_backend.py` +
      `AppRun`'s `LD_LIBRARY_PATH` are the two things that make python-mpv find the
      bundled lib; if they regress, the symptom is the "Missing dependency" dialog.
- [ ] **Tune the vendored-lib excludelist** if the smoke names a missing `.so`:
      `build_appimage.sh`'s `case` keeps glibc / GL / X-Wayland / the C++ runtime
      on the host (per the AppImage excludelist) and vendors everything else
      libmpv pulls. A "cannot open shared object" line in the smoke log names the
      exact lib to move out of the excludelist.
- [ ] **Landing page / install table:** flip AppImage from "coming soon" to live
      once the first 0.1.3 release attaches it.

## Local dry-run (mechanics only, non-distributable)

On any Linux box with `libmpv-dev` + PyInstaller:

```bash
pip install pyinstaller
pyinstaller packaging/pyinstaller/jellytoast.spec --noconfirm
bash packaging/appimage/build_appimage.sh 0.1.3-local
APPIMAGE_EXTRACT_AND_RUN=1 QT_QPA_PLATFORM=offscreen \
  timeout 15 dist/jellytoast-0.1.3-local-x86_64.AppImage || [ $? -eq 124 ]
```

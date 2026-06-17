# AUR packaging — `jellytoast`

This directory holds the [AUR](https://aur.archlinux.org) `PKGBUILD` for
jellytoast. `v0.1.0` is tagged + released and `sha256sums` is filled with the
tag-archive digest — the recipe is **ready to submit** (see "Submit to the
AUR" below); it is not yet pushed to the AUR.

## What it does

Builds the pure-Python wheel via the standard PEP 517 flow
(`python -m build` → `python -m installer`) and installs it plus the
`.desktop` entry, the AppStream `metainfo.xml`, the scalable icon (named by
app-id so the desktop `Icon=` resolves), and the `LICENSE`. `arch=('any')`
— no compiled extension.

## Dependency notes

- **`mpv` is a hard dep and load-bearing.** `python-mpv` is a ctypes binding
  that `dlopen`s `libmpv.so` *at import time*; without `mpv` the player
  module refuses to load. `python-mpv` does not pull `mpv` automatically, so
  it is listed explicitly in `depends=`.
- **All hard deps are in the official `extra` repo.** No AUR dep is forced
  on a default install.
- **`pyatv` is `optdepends`, not a hard dep** — even though `pip install`
  pulls it by default on Linux. jellytoast lazy-imports it
  (`jellytoast/airplay2.py:_ensure_pyatv`, guarded by `try/except ImportError`),
  so AirPlay simply no-ops when it's absent. It's AUR-only, so demoting it to
  optdepends keeps a base install free of AUR transitive deps. The other
  cast/visualizer extras (`python-async-upnp-client`, `python-soco`,
  `python-snapcast`, `python-numpy`) are optdepends for the same reason.

## Validate locally before submitting

```bash
# 1. Tag the release the PKGBUILD points at, then fill the checksum:
git tag v0.1.0 && git push origin v0.1.0
cd packaging/aur
updpkgsums                       # replaces sha256sums=('SKIP') with the real digest

# 2. Build a clean package and install it (smoke-tests the recipe itself):
makepkg -si

# 3. Lint the recipe AND the built package (catches missing/excess deps):
namcap PKGBUILD
namcap jellytoast-*.pkg.tar.zst

# Best practice: build in a clean chroot to catch undeclared deps:
#   sudo pacman -S --needed devtools && pkgctl build
```

## Submit to the AUR

Requires an AUR account with your SSH public key uploaded.

```bash
makepkg --printsrcinfo > .SRCINFO            # REQUIRED; regenerate on every change
git clone ssh://aur@aur.archlinux.org/jellytoast.git aur-jellytoast
cp PKGBUILD .SRCINFO aur-jellytoast/
cd aur-jellytoast
git add PKGBUILD .SRCINFO
git commit -m "Initial import: jellytoast 0.1.0"
git push origin master
```

After this, Arch users install with `paru -S jellytoast` (or any AUR helper).

## On every release

1. Bump `pkgver` (and reset `pkgrel=1`).
2. `updpkgsums` for the new tag tarball.
3. `makepkg --printsrcinfo > .SRCINFO`.
4. Commit + push `PKGBUILD` + `.SRCINFO`.

Refs: [Python package guidelines](https://wiki.archlinux.org/title/Python_package_guidelines),
[AUR submission guidelines](https://wiki.archlinux.org/title/AUR_submission_guidelines).

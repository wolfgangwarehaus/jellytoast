# Flatpak / Flathub runbook

The manifest (`io.github.wolfgangwarehaus.jellytoast.yaml`) is authored and
pinned; this file is the step-by-step from here to "users install from
Flathub". Steps 1–3 are local (one sitting); 4–6 are the submission.

## 1. Generate the pinned Python dependency module (one command)

```bash
git clone --depth 1 https://github.com/flatpak/flatpak-builder-tools /tmp/fbt
python3 /tmp/fbt/pip/flatpak-pip-generator.py \
  --runtime org.kde.Sdk//6.8 \
  --requirements-file packaging/flatpak/requirements-flatpak.txt \
  --output packaging/flatpak/python3-requirements
```

Commit the generated `python3-requirements.json`. Regenerate whenever
pyproject dependencies change (keep `requirements-flatpak.txt` in lockstep —
it's pyproject minus PySide6, which comes from the BaseApp).

## 2. Local build + run

```bash
flatpak install --user flathub org.kde.Platform//6.8 org.kde.Sdk//6.8 io.qt.PySide.BaseApp//6.8
flatpak-builder --user --install --force-clean /tmp/jt-flatpak-build \
  packaging/flatpak/io.github.wolfgangwarehaus.jellytoast.yaml
flatpak run io.github.wolfgangwarehaus.jellytoast
```

Verify in the sandbox: playback (PipeWire), cast discovery (real LAN, not the
portal), keyring via Secret Service portal, mini-player keep-above + the
drag-repaint KWin effect (the `xdg-data/kwin` filesystem grant), tray, MPRIS.

## 3. Screenshots

Flathub requires screenshots in the metainfo. Shot list (Frosted dark, real
library): Library grid · Now Playing (lyrics or visualizer) · Cast menu ·
Downloads/offline · Settings → Playback (bit-perfect legend) · Smart
playlists · Radio. Then in
`packaging/io.github.wolfgangwarehaus.jellytoast.metainfo.xml`: uncomment the
`<screenshots>` block and point it at raster PNGs hosted in the repo
(`docs/screenshots/`, referenced by raw.githubusercontent URL). Re-validate:

```bash
appstreamcli validate packaging/io.github.wolfgangwarehaus.jellytoast.metainfo.xml
```

## 4. Submit to Flathub

Fork `https://github.com/flathub/flathub`, branch from `new-pr`, add ONLY the
manifest + python3-requirements.json (Flathub builds from our git tag — the
`dir` source in the manifest must be switched to a `git` source pinned to the
`v0.1.0` tag for submission), open a PR against the `new-pr` branch.
A Flathub reviewer walks the manifest; typical first-app review is days to a
couple of weeks. After merge, we get a `flathub/io.github.wolfgangwarehaus.jellytoast`
repo with build hooks + an invite as maintainer.

## 5. Updates after acceptance

Bump the tag in the manifest in the flathub repo (PR there, buildbot builds,
merges publish). Keep `python3-requirements.json` regenerated on dep changes.

## 6. Verification badge

Flathub "verified" checkmark: prove control of the GitHub org via the
website-or-login methods in the Flathub dashboard (Settings → Verification on
the app page). Do this — verified apps rank better and Mint's Software
Manager defaults to showing verified flatpaks only.

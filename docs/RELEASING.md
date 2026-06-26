# Releasing jellytoast

The unified, multi-channel release process. The goal: **one `cut_release.sh`
push + one *Publish* click reaches every channel** (Store excepted, deliberately
manual). It builds on two triggers that mirror the `prepare → cut → build →
publish` lifecycle.

```
 prepare            cut                     build (draft)            publish              fan-out
 ───────            ───                     ─────────────            ───────              ───────
 merge fixes  →  dev/cut_release.sh   →   v* tag fires        →   you flip the   →   release: published fires
 + CHANGELOG     X.Y.Z --push             release.yml:             draft public       → PyPI   (pypi-publish.yml)
 to main         (stamps EVERY            builds .deb +           (gh release        → winget (winget.yml)
                 channel's version,       AppImage + Windows      edit --latest)     → AUR    (aur.yml, when live)
                 tags, pushes)            + wheel into a DRAFT                        (Store = manual, see below)
```

## The two commands

```bash
# 1. cut: stamps the version everywhere + tags + pushes (triggers the draft build)
dev/cut_release.sh X.Y.Z --push

# 2. publish: after the draft build is green, make it public (fires the fan-out)
gh release edit vX.Y.Z --draft=false --latest
```

On the tag push, `release-checklist.yml` also opens a **propagation checklist
issue** (from `.github/release-checklist-template.md`) — the per-release board.

## Channels

| Channel | Trigger | Automated? | Notes |
|---|---|---|---|
| **GitHub Releases** | `v*` tag → `release.yml` | draft auto; **publish manual** | the artifact hub |
| **PyPI** | `release: published` | ✅ full | Trusted Publishing (OIDC, no token) |
| **winget** | `release: published` → `winget.yml` | ✅ when `WINGET_TOKEN` set | else `wingetcreate` one-liner |
| **AUR** | `release: published` → `aur.yml` | ✅ when key set **and** AUR unfrozen | dormant today |
| **macOS `.dmg`** (Developer ID) | `v*` tag → `release.yml` | draft auto; **publish manual** | signed + notarized arm64 `.dmg`, in the GitHub Releases hub |
| **Mac App Store** (`.pkg`) | `workflow_dispatch` → `build-mas` | semi-auto: builds + signs + **uploads to App Store Connect**; submit-for-review is manual in ASC | arm64 sandboxed `.pkg`; see below |
| **Microsoft Store / MSIX** | — | ❌ **manual today** (automatable now the Store is live — see below) | Windows tooling; `packaging/msix/STORE-SUBMISSION.md` |
| **Landing page** | download buttons auto-track `/releases/latest` | mostly | version *text* in `site/` is manual |

### Version single-sourcing (no channel can ship a stale version)

`dev/cut_release.sh` stamps the version in **every** manifest in one atomic pass:
`pyproject.toml`, `jellytoast/version.py`, the AppStream metainfo, **all three
winget manifests** (`PackageVersion` + the `InstallerUrl`), and the **AUR
`PKGBUILD`** (`pkgver`, `pkgrel=1`). `tests/test_version_consistency.py` **fails
CI** if any of them drifts from `pyproject` — this is the gate that closes the
class that made v0.1.1 silently miss PyPI + AUR.

The artifact-dependent digests are NOT stamped at cut time (the `.exe` / tag
tarball don't exist yet): the winget `InstallerSha256` is computed by
winget-releaser at publish, and the AUR `sha256sums` by `updpkgsums`.

## One-time activation (the dormant workflows)

The fan-out workflows are committed but **inert** until you add their secret —
nothing runs without it.

### winget (`winget.yml`)
**The only step left is the token** — the `wolfgangwarehaus/winget-pkgs` fork
already exists, and jellytoast is live in winget-pkgs (0.1.0/0.1.1/0.1.2), so
winget-releaser has a previous version to template from.

1. Create a **classic** PAT (fine-grained PATs are *not* supported) with the
   `public_repo` scope: https://github.com/settings/tokens → *Generate new token
   (classic)* → check **`public_repo`**.
2. Store it as the repo secret **`WINGET_TOKEN`**:
   `gh secret set WINGET_TOKEN --repo wolfgangwarehaus/jellytoast` (paste at the
   prompt — never commit it), or via *Settings → Secrets and variables → Actions*.

Then **every published release auto-opens the winget PR**. To submit (or
backfill) a *specific* already-published release — e.g. 0.1.3, which published
before the token existed — run the manual path:
`gh workflow run winget.yml -f tag=vX.Y.Z`. The action only works on a PUBLISHED
(non-draft) release. (Before the token is set, the fallback stays the
`wingetcreate update wolfgangwarehaus.jellytoast --version X.Y.Z --urls
<setup.exe-url> --submit --token $(gh auth token)` one-liner.)

### AUR (`aur.yml`)
1. Wait for the AUR to reopen new-package registration (frozen after the 2026
   malware wave).
2. Generate a dedicated AUR SSH keypair; add the **public** half to your AUR
   account, store the **private** half as repo secret **`AUR_SSH_PRIVATE_KEY`**.
3. Do the one-time initial import by hand (see `packaging/aur/README.md`); after
   that, each published release auto-pushes the bumped PKGBUILD.

### Mac App Store — one dispatch per release (no Mac needed)
The MAS `.pkg` build is `workflow_dispatch`-only (the from-source LGPL/no-Lua
libmpv build is slow, so it's deliberately off the auto-tag path). Get in the
habit of uploading a fresh build to App Store Connect **every release** — it runs
entirely on the GitHub `macos-14` runner, no Mac of your own required:

```bash
gh workflow run release.yml --ref vX.Y.Z   # runs build-mas → signs → uploads to ASC
```

(A `workflow_dispatch` run also rebuilds the other artifacts as throwaway
workflow artifacts and creates **no** GitHub release — only `build-mas` does
anything external: it uploads to App Store Connect.) The build number is
`CFBundleVersion` = the CI **run number** (monotonic, so every upload is
accepted); the marketing version is the tag. Then finish in App Store Connect:
attach the build to the version, update any changed metadata, and **submit for
review** — the one genuinely manual step (Apple has no worthwhile API for the
final submit). Certs/secrets are already set (`APPLE_DIST_*`,
`APPLE_INSTALLER_*`, `MAS_PROVISION_PROFILE`); first-time setup is in
`packaging/macos/mas/`.

### Microsoft Store / MSIX — manual today, now automatable
The first submission was a manual Windows-box trip
(`packaging/msix/STORE-SUBMISSION.md`) — and jellytoast **is now live** in the
Store (product `9PNLTPXGHN79`). That live product is exactly the precondition
that unlocks automation: subsequent version updates can be pushed by the
`msstore` CLI / the Store submission API from CI (free-products flow, which
jellytoast qualifies for), with the `.msix` built on a free GitHub-hosted
`windows-latest` runner — **no physical Windows box**. See
`packaging/msix/STORE-AUTOMATION.md` for the wiring plan + one-time Entra/Partner
Center setup.

## Safety properties (preserved)

- **Human gate on the irreversible step.** Publishing to a public registry can't
  be undone (PyPI forbids version reuse; winget/AUR are public history). The
  draft → manual-publish gate stays; only the cheap-to-fix registries auto-fan-out.
- **Idempotent re-runs.** `release.yml` create-or-update + `--clobber`; PyPI
  `skip-existing`; winget-releaser re-runs just refresh the PR.
- **Least privilege.** Each registry secret is scoped to its own workflow; build
  jobs stay `contents: read`.
- **Dry runs.** `release.yml` supports `workflow_dispatch` (builds + uploads
  workflow artifacts, creates no release) to validate a change before a real tag.

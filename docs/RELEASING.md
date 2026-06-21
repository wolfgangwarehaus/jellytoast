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
| **Microsoft Store / MSIX** | — | ❌ **deliberately manual** | Windows box; `packaging/msix/STORE-SUBMISSION.md` |
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
1. Create a **classic** PAT (fine-grained PATs are unsupported) with the
   `public_repo` scope → store as repo secret **`WINGET_TOKEN`**.
2. **Fork** `microsoft/winget-pkgs` under `wolfgangwarehaus`.
3. Ensure the current version's manifest already exists in winget-pkgs (it does —
   v0.1.0/0.1.1 are merged; winget-releaser templates off the previous version).

Then every published release auto-opens the winget PR. Until activated, run:
`wingetcreate update wolfgangwarehaus.jellytoast --version X.Y.Z --urls <setup.exe-url> --submit --token $(gh auth token)`.

### AUR (`aur.yml`)
1. Wait for the AUR to reopen new-package registration (frozen after the 2026
   malware wave).
2. Generate a dedicated AUR SSH keypair; add the **public** half to your AUR
   account, store the **private** half as repo secret **`AUR_SSH_PRIVATE_KEY`**.
3. Do the one-time initial import by hand (see `packaging/aur/README.md`); after
   that, each published release auto-pushes the bumped PKGBUILD.

### Microsoft Store / MSIX — stays manual
First submission is a manual Windows-box trip (`packaging/msix/STORE-SUBMISSION.md`).
Only AFTER the app is live in the Store can it be automated, via
`microsoft/microsoft-store-apppublisher` + the `msstore` CLI (free-products only —
jellytoast qualifies). Wire that as a follow-up once a live Store product ID exists.

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

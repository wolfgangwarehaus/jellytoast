# Release automation rollout + cadence (2026-06-15)

> **Status: active roadmap.** How a single version bump deploys jellytoast
> across every medium, the order we build it in, and when to actually cut a
> release. Companion to `distribution_channels_2026-06-12.md` (which decides
> *which* channels) and `project_release_workflow` in memory (CI/merge/
> `dev/cut_release.sh` mechanics). Derived from a 7-channel research sweep
> with adversarial verification (2026-06-15).

## Where we are
`release.yml` already builds the `.deb`, Windows installer + portable zip,
and sdist+wheel on a `v*` tag, emits SHA256SUMS, and creates a **DRAFT**
GitHub release. Every channel's packaging file is checked in
(`packaging/aur/PKGBUILD`, the Flathub manifest, the winget manifests, the
Windows `.iss`). **What's missing is the *publish* half** — nothing pushes
to PyPI / AUR / Flathub / winget, binaries carried no provenance, the site
deploys by hand.

## Ideal pipeline (target)
```
dev/cut_release.sh X.Y.Z --push
   │  bump pyproject + version.py + metainfo + CHANGELOG, commit, tag, push
   ▼
ON TAG PUSH — release.yml — AUTOMATIC
   ├─ build deb / windows / python   (+ signed Sigstore provenance — DONE)
   ├─ publish-pypi  (needs build-python; FINAL tags → PyPI, prerelease → TestPyPI)
   └─ draft-release (curated notes from CHANGELOG — DONE)  →  DRAFT release
   ▼
HUMAN GATE — august reviews the draft, clicks PUBLISH   ← the one deliberate go-signal
   ▼
ON release:[released] — publish.yml — AUTOMATIC FAN-OUT
   ├─ winget   → PR to microsoft/winget-pkgs
   ├─ AUR      → push PKGBUILD to aur.archlinux.org
   ├─ pages    → redeploy site/ (also on push to main touching site/**)
   └─ Flathub  → x-checker-data bot PR in the flathub/<app-id> repo (semi-auto)
   ▼
non-store users (.deb / portable / source): in-app update check (suppressed for store builds)
```
Keep the **draft → publish** seam: tag push builds + drafts + ships PyPI;
one Publish click fans out to the stores. (Decision: keep-draft over
auto-publish — the Publish click is the natural human go-signal.)

## Phased plan
| Phase | What | Effort | Gated on august |
|---|---|---|---|
| **0 ✅** | Build-provenance attestations + curated CHANGELOG notes (PR #95) | S | none — free |
| **1** | PyPI Trusted Publishing (OIDC, no token) — publish-pypi job | S | PyPI acct+2FA, **pending** Trusted Publisher (`jellytoast`/`wolfgangwarehaus`/`release.yml`/env `pypi`), GitHub `pypi` environment |
| **2** | GitHub Pages auto-deploy + hero/og:image | M | Settings→Pages source = GitHub Actions; **capture hero screenshot** |
| **3** | winget auto-PR on publish (`vedantmgoyal9/winget-releaser@v2`) | S | classic PAT `public_repo` → `WINGET_TOKEN`; fork winget-pkgs; first manual submission |
| **4** | AUR auto-push on publish (`KSXGitHub/github-actions-deploy-aur@v4.1.3`) | M | AUR acct+2FA; **dedicated** ed25519 deploy key → `AUR_SSH_PRIVATE_KEY`/`AUR_USERNAME`/`AUR_EMAIL`; first manual import |
| **5** | Flathub submission + `x-checker-data` auto-update | M | **screenshots** (hard blocker) + switch manifest `type:dir`→`type:git`; fork flathub/flathub PR; accept per-app repo |
| **6** | Windows Authenticode signing (kills SmartScreen warning) | M | country of residence → Azure Trusted Signing (~$10/mo, US/CA) **or** SignPath Foundation (free, publisher shows "SignPath") |
| **7** | In-app update check for non-store users | M | decide build-channel sentinel so store builds suppress the nag |
| **8** | macOS notarization | L | **deferred — needs a Mac** (Apple Dev $99/yr) |

Verified pins (don't drift): `pypa/gh-action-pypi-publish@v1.14.0` (TAG, not
SHA — Docker action), `KSXGitHub/github-actions-deploy-aur@v4.1.3` (not @v2),
`actions/deploy-pages@v5` + `actions/upload-pages-artifact@v5` (not v3/v4),
`actions/attest-build-provenance@v4`, `vedantmgoyal9/winget-releaser@v2`.

## One-time setup checklist (unblocks the automation)
- [ ] **PyPI**: account + 2FA → add *pending* Trusted Publisher; create GitHub `pypi` environment. (~15 min; also defensively claims the unclaimed `jellytoast` name.) → unblocks Phase 1.
- [ ] **Screenshots**: 4–7 captioned shots (Library, Now Playing+lyrics, Cast, Downloads, Settings), window ≤1000×700, not maximized (keep shadow/rounding), Linux, `spectacle -f -b -n`; commit under `docs/screenshots/`. → unblocks Phase 2 hero + Phase 5 Flathub.
- [ ] **winget**: classic PAT (`public_repo`) → secret `WINGET_TOKEN`; fork microsoft/winget-pkgs. → unblocks Phase 3.
- [ ] **AUR**: account + 2FA; dedicated ed25519 deploy key (public→AUR, private→`AUR_SSH_PRIVATE_KEY`); first manual import of `jellytoast`. → unblocks Phase 4.
- [ ] **Flathub**: GitHub 2FA; fork flathub/flathub; submission PR (base `new-pr`). → unblocks Phase 5.
- [ ] **Pages**: Settings → Pages → Source = GitHub Actions. → unblocks Phase 2 deploy.
- [ ] **Signing**: confirm legal country → pick Azure vs SignPath. → unblocks Phase 6.

## Open decisions
- **Keep-draft vs auto-publish stable** → recommend keep-draft (Publish click drives the store fan-out).
- **TestPyPI** for prereleases — worth a 2nd account, or skip prerelease publishing? (judgment call).
- **AUR in-repo PKGBUILD** = CI-overwritten template (recommended) vs commit-back.
- **In-app update check** — only helps .deb/portable/source users; ship it?
- **Custom domain** for the site now? (changes og:image to an absolute URL).

---

## Release cadence — when to bundle/hold vs ship lined-up items

**Principle:** `main` is always green and shippable, but **`main` ≠ a
release.** Cutting a release has real per-channel cost (winget review, AUR
push, Flathub build+review, store latency, users updating by hand). So
**batch by default; release on purpose.** Merge to main freely; release
deliberately.

**Three release types**
- **Patch `0.1.x`** — fixes only, no new features. Cut **on-demand** the
  moment a change clears the *ship-now bar*.
- **Minor `0.x.0`** — features + accumulated fixes. Cut on a **cadence**
  (~every 2–4 weeks, or when a coherent bundle is ready). Gets the full
  treatment: refreshed screenshots if the UI changed, curated notes, all
  channels.
- **Major `1.0`** — stability milestone, later.

**Ship-now bar (cut a patch immediately):** crash/hang, data loss,
security, auth/playback broken for a class of users, or a regression from
the last release. I.e. anything that makes a *current* user's app worse
than before.

**Hold / batch (ride the next minor):** new features, UI polish, perf,
non-urgent bug fixes, refactors. These *benefit* from batching — one set of
release notes, one round of store reviews, screenshots updated once.

**When to roll out the lined-up batch — any of:**
- a theme is complete (e.g. "the casting improvements"), or
- the CHANGELOG `[Unreleased]` has grown to where a user would clearly
  notice/benefit, or
- a ~2–4 week time-box hits with meaningful accumulated changes, or
- a hotfix forces a release anyway → **fold in everything ready + green**
  ("ride-along": the urgent patch carries the done, low-risk lined-up items).

**Mechanics tie-in:** every squash-merged PR adds a line under
`## [Unreleased]` in `docs/CHANGELOG.md` — that section **is** the staging
area / "lined-up items," always visible. `dev/cut_release.sh` snips it into
the dated version block when you cut.

**Risk valve:** for a big/risky batch, tag a prerelease `vX.Y.0-rc1` →
TestPyPI + a GitHub *prerelease* → smoke-test → promote to the final tag.
Don't push risky batches straight to all channels.

**Anti-patterns:** don't release per-PR (per-channel overhead + update
fatigue); don't sit on a fix that unbreaks users waiting for a "bigger"
release; don't bundle a security fix with unrelated features — ship the
security patch alone, fast.

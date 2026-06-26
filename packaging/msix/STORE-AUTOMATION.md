# Microsoft Store — automate updates off the Windows box

**Bottom line: yes.** Once the app is **published and live** in the Store (the
first submission must be manual — done: product `9PNLTPXGHN79`), every later
version update can be pushed by GitHub Actions with **no physical Windows
machine**. You drive it entirely from the Linux box: push/publish a release, and
a free `windows-latest` runner packs the MSIX and the Store CLI submits it.

This supersedes "Microsoft Store stays manual" — the manual Windows trip was only
ever needed for the **first** submission. (Researched + adversarially verified
2026-06-26; sources at the bottom.)

> ## ⚠️ Hard prerequisite: a COMPANY Store account
> The `msstore` submission API authenticates through a Microsoft Entra
> *application* added to the Partner Center account — and **adding an Entra
> application (the "Microsoft Entra applications" tab) is a Company-account-only
> feature. Individual accounts cannot do it**, so this automation is impossible on
> an individual account no matter how the tenant/app are set up (verified
> 2026-06-26 against the live Partner Center UI).
>
> jellytoast's Store account (`9PNLTPXGHN79`) is currently an **Individual**
> account, so **everything below requires first moving to a Company account.**
> Two catches: (1) Individual→Company conversion is **not supported** — you must
> create a **new** Company account (free since May 2026; needs a D-U-N-S number or
> business docs + a domain-matching work email + a few days' verification), and
> (2) the existing live listing does **not** auto-transfer to the new account —
> you either re-publish (new Store ID) or open a support ticket for a transfer.
> See the bottom of this doc / the chat research for the full trade-off. The
> lower-friction alternative (stay Individual, CI builds the `.msix`, you upload
> it manually in the browser) avoids all of this.

## Why no Windows machine — and why no code-signing cert

- **The Store re-signs the package.** You upload an **unsigned** MSIX; Microsoft
  re-signs it with a Microsoft certificate after certification. So none of the
  usual blockers (a `.pfx`, USB token, HSM, or Azure signing) apply to the
  *Store* channel. The only reason Windows was ever "required" — signing — is
  moot here. *(This is Store-only. The direct-download `.exe`/`.zip` and any
  side-loaded/winget-hosted `.msix` are NOT re-signed and still need their own
  Authenticode signing — the separate Azure Artifact Signing track.)*
- **The build runs on a GitHub-hosted `windows-latest` runner** (free for public
  repos — the repo already runs `build-windows` there). `makeappx` packs the
  PyInstaller onedir into an `.msix`. We use the real Windows runner (not
  Docker/Linux `makemsix`): Linux packing works for the Store and Mozilla does it
  for Firefox, but Microsoft's own tool says `makemsix` lacks feature parity with
  `makeappx`, its manifest validation is shallow (packable ≠ installable), and
  WACK can't run on Linux at all. The Windows runner is already in the pipeline
  and strictly better — keep Linux `makemsix` only as a no-Windows-runner fallback.
- **You operate it from Linux.** The Windows work happens in the cloud runner;
  you only push a tag and click *Publish*. No Mac-mini-style rented box, no laptop.

## What's automated vs. what stays manual

| Step | Automated? |
|---|---|
| Pack the `.msix` + attach it to the release (`makeappx` on `windows-latest`) | ✅ CI (active now, any account) |
| Submit the package update to the Store (`msstore` CLI) | ✅ CI **only on a Company account**; on Individual you upload the attached `.msix` by hand |
| **Microsoft certification review** (runFullTrust ⇒ a human reviewer, ~1–5 business days **every** update) | ❌ Microsoft-side, unavoidable |
| End users receiving the update | ✅ the Store auto-updates installed apps — already automatic, nothing to build |
| First-ever submission, IARC age rating, listing copy | ❌ one-time, already done |
| Rotating the Entra client secret before it expires | ❌ recurring (see risks) |

So: this automates the **developer-side submission**. It does **not** make a
release instant-to-users — runFullTrust routes every update through a manual
Microsoft cert review, and a PyInstaller-bootloader false positive can still hold
a submission (appeal via `reportapp@microsoft.com`, pre-declared in the
certification notes). "Hands-off submit," not "hands-off publish."

## One-time setup (mostly browser, ~1 day incl. a dry run)

Precondition 0 — **the Store account must be a COMPANY account** (see the callout
above). On an Individual account steps 1 & 3 are impossible; the rest is moot.

Precondition 1 — **confirm in Partner Center that `9PNLTPXGHN79` has a genuinely
PUBLISHED submission** (not just a reserved name / live-looking page). The CLI
only does *updates*; it errors if the app isn't live.

1. **Associate a Microsoft Entra tenant** with the (Company) Partner Center
   account (Account settings → Tenants) — or onboard the Company account with an
   Entra/work account, which associates the tenant automatically.
2. **Register an app** in Entra (App registrations) → create a **client secret**.
3. In Partner Center → Account settings → User management → **Microsoft Entra
   applications** tab (Company accounts only), add that app and assign it the
   **Manager** role.
4. Collect four values and add them as repo secrets:
   `AZURE_AD_TENANT_ID`, `AZURE_AD_APPLICATION_CLIENT_ID`,
   `AZURE_AD_APPLICATION_SECRET`, `SELLER_ID` (Partner Center "Seller/Publisher
   ID").

## The workflow (`.github/workflows/msstore.yml` — committed, dormant)

This is already wired and committed, **dormant until the four secrets above
exist** (same committed-but-inert pattern as `winget.yml`/`aur.yml`). It mirrors
`build-windows`'s freeze, packs the MSIX with native `makeappx` (resolved from
the SDK on the runner), and submits via the `msstore` CLI. Triggers on
**`release: released`** — matching jellytoast's lifecycle
(`prepare → cut → build(draft) → publish → fan-out`), the same trigger winget /
PyPI / AUR already use. Do **not** submit on tag push: that would push to
Microsoft before you've reviewed/published the draft.

Sketch:

```yaml
name: msstore
on:
  release:
    types: [released]   # a non-prerelease publish, like winget.yml
  workflow_dispatch:
    inputs: { tag: { description: "Tag to submit (e.g. v0.1.4)", required: true } }
permissions: { contents: read }
jobs:
  store:
    runs-on: windows-latest
    env:
      HAVE_CREDS: ${{ secrets.AZURE_AD_TENANT_ID != '' }}
    steps:
      - uses: actions/checkout@v7
        with: { ref: ${{ github.event.release.tag_name || inputs.tag }} }
      # 1. Freeze the app (same PyInstaller spec as build-windows) — needs
      #    packaging/windows/libmpv/libmpv-2.dll staged first.
      # 2. Resolve the Windows SDK (makeappx isn't on PATH): restore the
      #    Microsoft.Windows.SDK.BuildTools NuGet, or glob "Windows Kits\10\bin\*\x64".
      # 3. Stamp a unique 4-segment MSIX version (1.0.N.0 — 4th segment MUST be 0,
      #    first non-zero; the Store rejects a duplicate). Decoupled from 0.1.x.
      # 4. Stage AppxManifest.xml + Assets/ into the onedir; makeappx pack (UNSIGNED).
      - if: env.HAVE_CREDS == 'true'
        uses: microsoft/microsoft-store-apppublisher@v1.1
      - if: env.HAVE_CREDS == 'true'
        run: |
          msstore reconfigure --tenantId ${{ secrets.AZURE_AD_TENANT_ID }} `
            --sellerId ${{ secrets.SELLER_ID }} `
            --clientId ${{ secrets.AZURE_AD_APPLICATION_CLIENT_ID }} `
            --clientSecret ${{ secrets.AZURE_AD_APPLICATION_SECRET }}
          msstore publish path\to\jellytoast-1.0.N.0.msix -id 9PNLTPXGHN79
```

(Metadata — description, screenshots — has its own `msstore submission
updateMetadata` path if we ever want listing changes in CI; package-only updates
don't need it.)

## Risks / gotchas (the parts that bite)

- **runFullTrust = a manual cert review on every update** (~1–5 business days).
  Submission automates; reaching users does not.
- **Client-secret expiry.** Entra client secrets expire (≤24 months, often a
  6–12-month default). When it lapses the submission silently 401s with no build
  failure to warn you — calendar a rotation.
- **`makeappx` isn't on PATH** on `windows-latest` (it's under `Windows
  Kits\10\bin\<ver>\x64`, and the version dir changes with the runner image).
  Resolve via the `Microsoft.Windows.SDK.BuildTools` NuGet, not a hardcoded path.
- **Version monotonicity.** The manifest hardcodes `1.0.0.0`; the Store rejects a
  duplicate version, so each release must bump it (e.g. `1.0.<run>.0`).
- **No WACK pre-flight in CI by default** (it's Windows-only and being retired;
  Partner Center certifies server-side regardless). Errors surface after upload —
  keep the manifest identity (`Publisher=CN=C9FAE1C4-…`, the PFN) byte-for-byte
  matched to Partner Center, since a Linux/quick build won't catch drift for you.
- **Free products only.** Adding a paid tier / IAP later forfeits this path.

## Effort

- Build step (pack MSIX in the existing Windows job): **~0.5 day**.
- Submission wiring + the one-time Entra/Partner Center auth + a dry run:
  **~1 day**, almost all of it the auth plumbing, not code.

## Sources

- [Publish app updates to Microsoft Store with GitHub Actions — Microsoft Learn](https://learn.microsoft.com/en-us/windows/apps/publish/msstore-dev-cli/github-actions) (the `microsoft/microsoft-store-apppublisher@v1.1` action + `msstore` CLI; prereqs; "free products only"; "must already be published and live")
- [App package requirements — the Store re-signs MSIX/AppX](https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/app-package-requirements)
- [MSIX certification process (Partner Center certifies server-side)](https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/app-certification-process)
- [Build an MSIX on Linux (makemsix) — the fallback path](https://learn.microsoft.com/en-us/windows/msix/msix-sdk/msix-linux)
- [makeappx vs makemsix — no feature parity (microsoft/msix-packaging Disc. 598)](https://github.com/microsoft/msix-packaging/discussions/598)
- [Mozilla builds Firefox's Store MSIX on Linux (proof Linux packing works)](https://firefox-source-docs.mozilla.org/browser/installer/windows/installer/MSIX.html)

# Windows session — release channels (winget + Microsoft Store)

**Open this on the Win 11 laptop in Claude Code and work top to bottom**,
ticking boxes as you go. Deep detail lives in `packaging/msix/README.md`
(build playbook) and `packaging/windows/azure-signing.md` (optional
`.exe`-signing track). This file is the ordered checklist + the things that
can only be done/verified on real Windows.

This session lands **two** Windows release channels:
- **Track A — winget** (fast): submit the existing direct-download Inno `.exe`
  to `microsoft/winget-pkgs`. Manifests are already bumped to v0.1.1 in this
  branch; the only gate is validating the published `.exe` on real Windows.
- **Track B — Microsoft Store (MSIX)**: the only **free + immediate** fix for
  the SmartScreen "unknown publisher" warning (Microsoft re-signs the MSIX).
  Packaging is scaffolded and the code is already changed in this branch; this
  session verifies it on real Windows and submits.

Do **Track A first** (it shares the `.exe` validation and is a ~15-min win),
then Track B.

---

## 🧭 CURRENT STATUS — refreshed 2026-06-20 (read this first)

A lot is already done; this trip is mostly **build → verify → submit for the
v0.1.2 release**. The authoritative, paste-ready submission steps now live in
**`packaging/msix/STORE-SUBMISSION.md`** — this file is the laptop checklist;
that file is what you paste into Partner Center.

**Done (verify, don't redo):** winget is LIVE (Track A); Partner Center account
+ identity stamped into the manifest (PFN `…_yswr9h87xar1w`, Store ID
`9PNLTPXGHN79`, manifest `1.0.0.0`); MSIX has been built, packed, and
self-signed once; `COPYING`/GPL-3.0 conveyance + privacy disclosures done.
**Verified live from Linux 2026-06-20:** the hosted privacy URL
(`…/jellytoast/privacy.html`, with the third-party-flow disclosures) returns
200, and the Jellyfin demo server (`demo.jellyfin.org/stable`) is reachable —
Store gates 2 and 6 are clear.

**🛠️ Build staged 2026-06-20 (Windows box, non-admin session):** the MSIX was
rebuilt from this v0.1.2-synced branch and the no-admin steps are all done:
- `C:\Temp\jellytoast-1.0.0.0.msix` (130 MB) — real-Publisher, **UNSIGNED**
  Store-upload package. `libmpv-2.dll` (117 MB) confirmed bundled at
  `_internal\libmpv-2.dll`; `makeappx` reported "Package creation succeeded".
  ⚠️ This is the **diagnostic** build — it still contains the TEMP `_msix.py`
  shim. Rebuild clean (shim removed) right before the *real* upload.
- `C:\Temp\jellytoast-test.msix` (130 MB) — test-signed sideload copy
  (`Publisher=CN=jellytoast-test`, cert thumbprint `75B04B…BD82`).
- `C:\Temp\jellytoast-test.cer` / `.pfx` (pw `test`) — cert to trust.
- **Elevated helper:** `C:\Temp\jt-elevated-sideload.ps1` does the two admin
  steps (trust cert + `Add-AppxPackage`) and prints the QA gate. Run it from an
  **elevated** PowerShell, then do the manual in-package QA (audio = #1 risk;
  autostart = Blocker 2, check `%TEMP%\jt_msix_debug.txt` after toggling).
- ⚠️ **WACK (`appcert.exe`) is NOT installed** on this box (NuGet gave only
  makeappx/signtool). Install the Windows App Certification Kit for a local
  pre-check, or rely on the Store's server-side certification.

**The release order matters — do it in this sequence:**
1. **Cut + push `v0.1.2`** (`dev/cut_release.sh 0.1.2 --push`, from the Linux
   box) so every `…/v0.1.2/` source-offer / license / privacy link resolves and
   the Store build carries the right marketing version. The review fixes that
   make up 0.1.2 are on `fix/v0.1.2-review-fixes`; merge that first.
2. **Rebuild the MSIX from the v0.1.2 tag** on the laptop (PyInstaller spec →
   `makeappx`), with the **real** identity (already in the manifest — no
   self-sign needed for the upload package).
3. **WACK + the in-package QA gate** (Part 1 / Phase C in STORE-SUBMISSION.md) —
   **audio playing under read-only WindowsApps is the #1 risk.**
4. **Submit** via the STORE-SUBMISSION.md runbook.

> ⚠️ **Two things MUST be cleared before the final Store build:**
> 1. **Remove the TEMP debug shim** in `jellytoast/autostart/_msix.py` (the
>    `jt_msix_debug.txt` writer added 2026-06-20 for StartupTask diagnosis). It
>    must not ship in the submitted package.
> 2. **Resolve the StartupTask autostart diagnosis** the shim was added for —
>    confirm "Launch at login" actually arms/disarms in-package (Phase C item 6)
>    before relying on it. If it can't be fixed this trip, ship 0.1.2 with the
>    Run-key fallback understanding that packaged autostart is unverified, and
>    keep it off the Store listing's feature claims.

---

## Track A — winget submission ✅ DONE (winget install is live)
The manifests in `packaging/winget/` are already pointed at the live
**v0.1.1** `setup.exe` (URL + sha256 from the release `SHA256SUMS`). Steps:
- [x] **Validate the published installer on real Windows** — SHA256 verified,
      installed silently, app launches ("jellytoast" window), libmpv-2.dll
      loaded from `_internal\`, WASAPI active (audiodg CPU), settings in
      registry. ✅ 2026-06-18
- [x] `winget validate --manifest packaging\winget` — passes. Note: winget
      has a bug with spaces in paths; run from `C:\Temp\wg-validate` or any
      space-free copy. Fixed root cause with `.gitattributes eol=lf` (CRLF
      from Windows git autocrlf was breaking the YAML scanner). ✅ 2026-06-18
- [x] **winget is LIVE.** v0.1.0 PR microsoft/winget-pkgs#389422 merged
      2026-06-18; v0.1.1 PR #390782 merged 2026-06-20 (full validation pipeline
      passed). `winget install wolfgangwarehaus.jellytoast` → 0.1.1 works
      globally. (The earlier "v0.1.1 submitted 6-18" note was WRONG — that
      submission never produced a PR; #390782 via `wingetcreate update` was the
      real one.)
- [x] **v0.1.2 submitted 2026-06-20** — microsoft/winget-pkgs **#391020**
      (`wingetcreate update … --version 0.1.2`; InstallerSha256
      `54febef…80cd2` validated, matches the published setup.exe). Pipeline
      auto-validates + merges (same path as #389422 / #390782); `winget upgrade
      jellytoast` → 0.1.2 once it lands. Tracked `packaging/winget/*.yaml` bumped
      to 0.1.2 to match. **winget track DONE through 0.1.2.**
- [x] Future bumps: `wingetcreate update wolfgangwarehaus.jellytoast --version <v> --urls <setup.exe> --submit --token (gh auth token)` (`manifests/` is gitignored scratch).

> If the `.exe` validation **fails**, stop and report back — both winget and the
> direct-download channel depend on that same Inno build.

---

## Track B — Microsoft Store (MSIX) bring-up

## ✅ Already done in this branch (verify, don't redo)
- `packaging/msix/AppxManifest.xml` — full-trust manifest (Identity = placeholders)
- `packaging/msix/Assets/` — 23 Store logos (regenerate with `bash packaging/msix/make-assets.sh`)
- `packaging/msix/README.md` — full build → submit playbook
- Code changes, all gated behind `is_msix_packaged()` (inert off-MSIX, 163 tests green on Linux):
  - libmpv `os.add_dll_directory` — `jellytoast/player_backend.py`
  - skip AUMID stamp + Start-menu sync when packaged — `jellytoast/app.py`
  - package-AUMID toasts — `jellytoast/notifications/_windows.py`
  - `_msix` autostart backend — `jellytoast/autostart/_msix.py` + `__init__.py`
- `.github/workflows/release.yml` — gated Azure signing steps (off until secrets set)

## ⚠️ Two spots flagged untestable on Linux — verify/fix here first
- [x] **`autostart/_msix.py` WinRT resolution** — `winrt.windows.applicationmodel`
      imports fine. `StartupTask.get_async` IS callable (static methods live on
      the metaclass in PyWinRT 3.x). `_resolve()` fallback verified. BUT:
      `winrt-Windows.ApplicationModel` was **missing from `pyproject.toml`** —
      fixed and committed. ✅ 2026-06-18
- [ ] **libmpv loads in-package** — the #1 risk. Pending MSIX sideload install
      (requires cert trust — see Phase 2 notes below). MSIX is built and signed;
      `_internal\libmpv-2.dll` present in the package. ⏳

---

## Prereqs
- [x] Windows SDK installed → `makeappx.exe` + `signtool.exe` available at
      `C:\SDK-Tools\` (extracted from NuGet `Microsoft.Windows.SDK.BuildTools`
      — no admin needed, no system-wide install). ✅ 2026-06-18
- [x] Python 3.11 + this repo checked out to branch `feat/windows-store-msix`
- [x] `pip install . pyinstaller` — PyInstaller 6.21.0 in venv ✅

## Phase 1 — free Microsoft Store account ✅ DONE
- [x] Registered an **individual** developer account (personal MS account, $0).
- [x] Reserved the app name **"jellytoast"**.
- [x] Stamped the assigned **Product identity** into `packaging/msix/AppxManifest.xml`
      — `Name=wolfgangwarehaus.jellytoast`, `Publisher=CN=C9FAE1C4-…`, PFN
      `…_yswr9h87xar1w`, Store ID `9PNLTPXGHN79`, Version `1.0.0.0`. Verified
      byte-for-byte against Partner Center in `STORE-SUBMISSION.md` (Phase A).

## Phase 2 — build, pack, local test-install
> Self-signing here is **local-only** (to sideload-test). The Store re-signs
> for real. For local testing, temporarily set `Publisher="CN=jellytoast-test"`
> in the manifest so it matches the self-signed cert.
- [x] `pyinstaller packaging\pyinstaller\jellytoast.spec --noconfirm` ✅ 2026-06-18
      Note: `libmpv-2.dll` must be at `packaging\windows\libmpv\libmpv-2.dll`
      before building — copied from the installed Inno app's `_internal\`.
- [x] Stage: manifest + Assets copied to `dist\jellytoast\`; Identity patched
      to `CN=jellytoast-test` / `jellytoast-local-test` for local test. ✅
- [x] `makeappx pack /d dist\jellytoast /p C:\Temp\jellytoast-0.1.0.0-test.msix /o`
      → 130 MB; structure validated (exe + libmpv + 23 assets). ✅
- [x] Self-signed cert created (thumbprint `E0AB7C85EAD00E8B4347DCC26E01C69D12CB33A3`);
      MSIX signed with signtool. ✅
- [ ] **Trust cert + install** — BLOCKED: needs admin to add cert to
      `LocalMachine\TrustedPeople`. When back at laptop, run elevated PowerShell:
      ```powershell
      Import-Certificate -FilePath "C:\Temp\jellytoast-test.cer" -CertStoreLocation Cert:\LocalMachine\TrustedPeople
      Add-AppxPackage -Path "C:\Temp\jellytoast-0.1.0.0-test.msix"
      ```
- [ ] Run the **Windows App Certification Kit (WACK)** Store certification test
      on the `.msix`; fix anything it flags.

## Phase 3 — in-package QA checklist
- [ ] App launches from Start menu with the brand tile/icon
- [ ] **Audio plays** (`MPV_AVAILABLE` True) — #1 packaging risk
- [ ] Taskbar groups under the brand icon (not generic Python)
- [ ] A toast fires showing "jellytoast" + icon
- [ ] SMTC: hardware media keys + the volume-flyout transport work
- [ ] Acrylic backdrop renders; frameless chrome intact
- [ ] "Launch at login" on → survives reboot; off → doesn't start
- [ ] Settings/cache/downloads persist across restarts (`%LOCALAPPDATA%`)
- [ ] Single-instance: second launch focuses the existing window

## Phase 4 — Partner Center submission (manual, first time)
**Use `packaging/msix/STORE-SUBMISSION.md` — every field below is paste-ready
and adversarially verified there. This list is just the spine.**
- [ ] Rebuild + repack from the **v0.1.2 tag** with the **real** Identity
      (already in the manifest) — no self-sign needed for the upload package.
- [ ] **runFullTrust justification** — paste from STORE-SUBMISSION.md Part 2.
- [ ] **License terms = GPL-3.0-or-later** (App Developer Agreement FOSS
      carve-out; the bundled PySide6 forces v3 — see `docs/LICENSING.md`).
      Paste-ready text in STORE-SUBMISSION.md.
- [ ] **Source offer** (GPL §3): link `https://github.com/wolfgangwarehaus/jellytoast/tree/v0.1.2`
      (must resolve → cut the tag first).
- [ ] **Privacy policy URL** — `https://wolfgangwarehaus.com/jellytoast/privacy.html`
      (LIVE + discloses the third-party flows; verified 2026-06-20).
- [ ] Submit → first cert review ~1–5 business days (the runFullTrust manual review).

## Phase 5 (optional, separate track) — sign the GitHub `.exe` too
The Store only clears SmartScreen for the **Store copy**. To also fix the
direct-download `.exe`/zip, set up **Azure Artifact Signing** (~$9.99/mo,
you're eligible as a US/Canada individual). Full runbook:
`packaging/windows/azure-signing.md`. Once the 6 repo secrets exist, the
release pipeline signs automatically. (Note: signing shows your verified name
+ builds reputation; it does NOT clear the warning on day one.)

## Phase 6 — after it's live
- [ ] Wire the `msstore` CLI + `microsoft/microsoft-store-apppublisher` action
      into `release.yml` for automated **updates** (free products only; first
      submission must be manual). See README §"Updates later".
- [ ] **ARM64 native build** (future, separate track): Partner Center shows an
      advisory that AArch32 support is being dropped — we're x64 (runs via
      emulation on ARM64 Windows, which is fine for now). Native ARM64 requires:
      ARM64 Python build + ARM64 libmpv-2.dll + PyInstaller on ARM64 hardware →
      then ship an `.msixbundle` with both x64 + arm64 slices.

---

_When done, update `docs/TODO.md` (the winget, Microsoft Store / Azure rows)
and tell the Linux session so memory gets the outcome._

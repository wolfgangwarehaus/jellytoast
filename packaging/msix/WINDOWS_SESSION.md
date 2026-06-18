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

## Track A — winget submission (do this first)
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
- [ ] `winget install --manifest packaging\winget` — needs
      `winget settings --enable LocalManifestFiles` (admin elevation); skip if
      you trust the SHA256-verified direct install above.
- [ ] Submit: `wingetcreate submit --prtitle "Add jellytoast v0.1.1" --token $(gh auth token) --no-open C:\Temp\wg-validate`
      (wingetcreate installed ✅; gh token has repo scope ✅; needs your
      go-ahead since it creates a public PR on microsoft/winget-pkgs).
      Their CI runs sandbox install + SmartScreen checks; respond to any bot
      feedback.
- [ ] On merge, `winget install jellytoast` works globally.

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
- [ ] **`autostart/_msix.py` WinRT resolution** — confirm `StartupTask.get_async(...).get()`
      works with the bundled `winrt` projection. If there's no `.get()`, the
      `_resolve()` helper already falls back to `asyncio.run`; confirm one path
      works and that the `from winrt.windows.applicationmodel import StartupTask`
      import resolves.
- [ ] **libmpv loads in-package** — the #1 risk. Confirm audio actually plays
      (QA below). If `import mpv` fails, check the `add_dll_directory` target.

---

## Prereqs
- [ ] Windows SDK installed → provides `makeappx.exe` + `signtool.exe`
      (in `...\Windows Kits\10\bin\<ver>\x64\`)
- [ ] Python 3.12 + this repo checked out to this branch
- [ ] `pip install . pyinstaller`

## Phase 1 — free Microsoft Store account
- [ ] Register an **individual** developer account at https://partner.microsoft.com
      (Apps & games) — government ID + selfie, **personal** Microsoft account
      (NOT a work/Entra account). $0, no credit card.
- [ ] Reserve the app name **"jellytoast"**.
- [ ] Copy the assigned **Product identity** into `packaging/msix/AppxManifest.xml`:
      - `Identity/@Name` ← Package/Identity/Name
      - `Identity/@Publisher` ← Package/Identity/Publisher (`CN=…` + GUID)
      - `PublisherDisplayName` ← Package/Properties/PublisherDisplayName

## Phase 2 — build, pack, local test-install
> Self-signing here is **local-only** (to sideload-test). The Store re-signs
> for real. For local testing, temporarily set `Publisher="CN=jellytoast-test"`
> in the manifest so it matches the self-signed cert.
- [ ] `pyinstaller packaging\pyinstaller\jellytoast.spec --noconfirm`
- [ ] Stage: `Copy-Item packaging\msix\AppxManifest.xml dist\jellytoast\` and
      `Copy-Item packaging\msix\Assets dist\jellytoast\Assets -Recurse`
- [ ] `makeappx pack /d dist\jellytoast /p jellytoast-0.1.0.0.msix /o`
- [ ] Self-sign + trust + install (see README "Local test-sign" block for the
      `New-SelfSignedCertificate` + `signtool` + `Add-AppxPackage` commands)
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
- [ ] Rebuild + repack with the **real** Identity (Phase 1) — no self-sign needed for upload
- [ ] **runFullTrust justification** — paste the paragraph from README §"Partner Center"
- [ ] **License terms = the GPL** (App Developer Agreement FOSS carve-out): supply
      your own terms = `https://github.com/wolfgangwarehaus/jellytoast/blob/v0.1.0/LICENSE`;
      convey the Store build under **GPL-2.0** (the `-or-later` permits it)
- [ ] **Source offer** (GPL §3): in the listing description, link
      `https://github.com/wolfgangwarehaus/jellytoast/tree/v0.1.0`
- [ ] **Privacy policy URL** (required for Win32 apps) — reuse the Flathub one
- [ ] Submit → first cert review ~1–5 business days

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

---

_When done, update `docs/TODO.md` (the winget, Microsoft Store / Azure rows)
and tell the Linux session so memory gets the outcome._

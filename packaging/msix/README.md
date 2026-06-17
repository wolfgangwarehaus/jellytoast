# Microsoft Store / MSIX packaging

Full-trust MSIX so jellytoast can ship on the Microsoft Store. **This is the
only free, immediate fix for the Windows SmartScreen "unknown publisher"
warning** — Microsoft re-signs Store MSIX packages with its own certificate,
so Store installs never warn. (Buying an OV/EV cert no longer clears SmartScreen
on day one — EV's instant-reputation bypass was removed in 2024; certs only
build reputation over download volume. See `docs/decisions.md` if recorded.)

Scope note: the Store re-sign covers the **Store copy only**. The GitHub
Inno `.exe` / portable `.zip` stay unsigned and keep warning on direct
download — that's a separate problem (Azure Artifact Signing, ~$10/mo, is the
fix there, and you're eligible as a US/Canada individual).

---

## Status

| Item | State |
| --- | --- |
| `AppxManifest.xml` (full-trust, startupTask, tiles) | ✅ scaffolded (Identity placeholders) |
| `Assets/` Store logos (Store/44/150/71/310/wide/splash + scales) | ✅ generated from the master SVG via `make-assets.sh` |
| Free individual Store account | ⬜ register at https://partner.microsoft.com (ID + selfie) |
| 3 code blockers (libmpv DLL, AUMID, autostart) | ⬜ apply + test on the Win 11 laptop (specs below) |
| Build → pack → local test-sign → WACK | ⬜ on the Win 11 laptop |
| GPL custom license terms + source offer | ⬜ in Partner Center (wording below) |
| First Store submission (manual) | ⬜ |
| CI update automation (`msstore` CLI) | ⬜ after first publish |

What's already MSIX-safe (audited, no change needed): QSettings/QStandardPaths
config, disk cache, image cache, offline `downloads.db`, scrobble queue (all
resolve to `%LOCALAPPDATA%`); `win_frameless`, `blur/_dwm` (Acrylic),
`single_instance`, `power` keep-awake, `media_controls` (SMTC — actually
*improves* with a real package identity), taskbar badge overlays (write to
`%LOCALAPPDATA%`).

---

## The 3 blockers — code changes (apply + test on the Win 11 laptop)

These touch the live Windows startup/audio path and can only be validated
inside a real package, so they're specified here rather than committed blind.
All are gated so the existing Inno `.exe` and the Linux build are untouched.

### 0. Add an MSIX-detection helper — `jellytoast/platform_compat.py`

Canonical detection via package identity (`GetCurrentPackageFullName` returns
`APPMODEL_ERROR_NO_PACKAGE` = 15700 when unpackaged):

```python
def is_msix_packaged() -> bool:
    """True iff running inside an MSIX/AppX package (has package identity).
    False for the Inno .exe, pip/pipx, and dev runs."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        n = ctypes.c_uint32(0)
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), None)
        return rc != 15700  # 15700 == APPMODEL_ERROR_NO_PACKAGE
    except Exception:
        return False
```

### 1. libmpv-2.dll fails to load (BLOCKER — kills all audio)

Under read-only `WindowsApps`, python-mpv's "next to `mpv.py`" fallback misses
the bundled DLL and `import mpv` raises `OSError` → `MPV_AVAILABLE = False` →
playback dead. Register the frozen-exe dir on the DLL search path **before**
the import. In `jellytoast/player_backend.py`, immediately above the
`try: import mpv` block (line ~108):

```python
import os, sys
if sys.platform == "win32" and getattr(sys, "frozen", False):
    # libmpv-2.dll ships next to the frozen exe (PyInstaller dest '.').
    # The package dir is read-only but loadable once on the search path.
    try:
        os.add_dll_directory(os.path.dirname(sys.executable))
    except OSError:
        pass
```

This also hardens the Inno build (no behavior change there). **QA gate:** after
install, confirm `MPV_AVAILABLE` is True and a track actually plays.

### 2. AUMID stamping conflicts with package identity (BLOCKER)

The package gets its own OS-assigned AUMID (`PackageFamilyName!jellytoast`).
The manual stamp + manual Start-menu `.lnk` conflict with it, and toasts bound
to the hardcoded string fail to render.

- `app.py:1823` — skip the manual stamp when packaged:
  ```python
  from jellytoast.platform_compat import is_msix_packaged
  if not is_msix_packaged():
      set_process_app_user_model_id()
  ```
- `app.py:2266` — skip the manual Start-menu shortcut sync when packaged (the
  manifest generates the Start entry):
  ```python
  if not is_msix_packaged():
      windows_shortcut.sync()
  ```
- `jellytoast/notifications/_windows.py:21,39` — under MSIX, bind the toaster to
  the OS package AUMID instead of the hardcoded `_AUMID`:
  ```python
  def _runtime_aumid() -> str:
      from jellytoast.platform_compat import is_msix_packaged
      if is_msix_packaged():
          import ctypes
          n = ctypes.c_uint32(0)
          ctypes.windll.kernel32.GetCurrentApplicationUserModelId(ctypes.byref(n), None)
          buf = ctypes.create_unicode_buffer(n.value)
          if ctypes.windll.kernel32.GetCurrentApplicationUserModelId(ctypes.byref(n), buf) == 0:
              return buf.value
      return "wolfgangwarehaus.jellytoast"
  # ...then: _toaster = WindowsToaster(_runtime_aumid())
  ```

**QA gate:** taskbar groups under the brand icon (not generic Python); a toast
fires and shows "jellytoast" with the app icon.

### 3. Autostart Run-key is ignored (BLOCKER)

Packaged apps can't autostart via `HKCU\...\Run`. The manifest already declares
a `windows.startupTask` (`TaskId="jellytoastStartup"`, `Enabled="false"`). Add a
backend that drives it and wire backend selection:

- `jellytoast/autostart/__init__.py` — select an MSIX backend first:
  ```python
  from jellytoast.platform_compat import IS_FLATPAK, IS_LINUX, IS_WINDOWS, is_msix_packaged
  if IS_LINUX and IS_FLATPAK:
      from jellytoast.autostart import _flatpak as _backend
  elif IS_LINUX:
      from jellytoast.autostart import _linux as _backend
  elif IS_WINDOWS and is_msix_packaged():
      from jellytoast.autostart import _msix as _backend
  elif IS_WINDOWS:
      from jellytoast.autostart import _windows as _backend
  else:
      from jellytoast.autostart import _unsupported as _backend
  ```
- New `jellytoast/autostart/_msix.py` — implement the public API
  (`is_supported/is_enabled/enable/disable`) against the StartupTask WinRT API.
  Sketch (needs the `winrt`/`winsdk` projection; validate on the laptop):
  ```python
  # StartupTask.get_async("jellytoastStartup") -> .request_enable_async() / .disable()
  # state: Enabled / Disabled / DisabledByUser (user override wins — surface it)
  ```
  The existing Settings checkbox needs no change — the public API is
  platform-agnostic. If `DisabledByUser`, the OS forbids re-enabling
  programmatically; show guidance pointing to Settings > Apps > Startup.

**QA gate:** tick "Launch at login", reboot, confirm jellytoast starts; untick,
reboot, confirm it doesn't.

---

## Build pipeline (Win 11 laptop, Windows SDK installed)

`makeappx.exe` / `signtool.exe` ship with the Windows SDK (in
`...\Windows Kits\10\bin\<ver>\x64\`).

1. **Freeze** with the existing PyInstaller spec → `dist\jellytoast\`
   (onedir: `jellytoast.exe` + `libmpv-2.dll` + runtime):
   ```powershell
   pyinstaller packaging\pyinstaller\jellytoast.spec
   ```
2. **Stage** the package layout (frozen app at root + manifest + assets):
   ```powershell
   Copy-Item packaging\msix\AppxManifest.xml dist\jellytoast\
   Copy-Item packaging\msix\Assets dist\jellytoast\Assets -Recurse
   ```
3. **Pack**:
   ```powershell
   makeappx pack /d dist\jellytoast /p jellytoast-0.1.0.0.msix /o
   ```
4. **Local test-sign only** (the Store re-signs for real — this is just to
   sideload-test). The cert subject MUST equal the manifest `Identity Publisher`,
   so for local testing temporarily set `Publisher="CN=jellytoast-test"`:
   ```powershell
   New-SelfSignedCertificate -Type Custom -Subject "CN=jellytoast-test" `
     -KeyUsage DigitalSignature -CertStoreLocation Cert:\CurrentUser\My `
     -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3")
   # export to test.pfx, then:
   signtool sign /fd SHA256 /a /f test.pfx /p <pw> jellytoast-0.1.0.0.msix
   # trust it (admin), then install:
   Add-AppxPackage jellytoast-0.1.0.0.msix
   ```
5. **Validate** with the Windows App Certification Kit (run the Store
   certification test on the `.msix`) — fix anything it flags.
6. **QA** — run the checklist below.

---

## Partner Center submission (first time, manual)

1. **Register** the free individual developer account
   (https://partner.microsoft.com → Windows & Xbox) — government ID + selfie,
   personal Microsoft account (not a work/Entra account). $0, no credit card.
2. **Reserve the name** "jellytoast" → Partner Center shows your **Product
   identity**. Copy these into `AppxManifest.xml`:
   - `Package/Identity/Name` → `Identity Name`
   - `Package/Identity/Publisher` → `Identity Publisher` (`CN=...` + GUID)
   - `Package/Properties/PublisherDisplayName`
   Rebuild + repack with the real identity (no self-sign needed for upload).
3. **runFullTrust justification** (Submission options → restricted capability):
   > jellytoast is a native desktop music player. It needs full trust
   > (runFullTrust) to load the bundled native libmpv-2.dll audio engine via
   > ctypes for bit-perfect gapless playback, and to use classic Win32/WinRT
   > desktop APIs — System Media Transport Controls, DWM Acrylic backdrop,
   > taskbar overlay/progress (ITaskbarList3), and SetThreadExecutionState to
   > prevent sleep during playback — none of which are available to a
   > sandboxed AppContainer app. No code is downloaded or executed at runtime;
   > everything ships inside the signed package.
4. **GPL license terms** — jellytoast is **GPL-2.0-or-later** (`docs/LICENSING.md`).
   The Store's App Developer Agreement has a FOSS carve-out: *"your license
   terms may conflict with the limitations in Section 3 of the Standard
   Application License Terms… but only to the extent required by the FOSS that
   you use."* So:
   - Properties → **provide your own license terms** = the GPL (paste the text
     or link `https://github.com/wolfgangwarehaus/jellytoast/blob/v0.1.0/LICENSE`).
   - **Convey the Store build specifically under GPL-2.0** (the `-or-later`
     permits this) to avoid GPLv3 anti-tivoization friction with app-store DRM.
   - **Source offer** (GPL §3): in the listing description, link the exact
     tagged source for the published version, e.g.
     `Source for this build: https://github.com/wolfgangwarehaus/jellytoast/tree/v0.1.0`.
   Precedent: VLC (a GPL media player) ships on the Store via this mechanism.
5. **Privacy policy URL** is required for Win32/Desktop-Bridge apps — reuse the
   one prepared for Flathub.
6. Submit. First cert review for a runFullTrust app typically takes ~1–5
   business days.

---

## Updates later (CI automation)

After the app is **live**, automate version bumps with the free `msstore` CLI +
`microsoft/microsoft-store-apppublisher` GitHub Action (free products only —
jellytoast qualifies). The **first** submission must be manual; only updates
automate. Wire `msstore publish` into `release.yml` after the MSIX build step.

---

## QA checklist (on the Win 11 laptop, in-package)

- [ ] App launches from the Start menu (manifest tile, brand icon)
- [ ] **Audio plays** (`MPV_AVAILABLE` True) — the #1 packaging risk
- [ ] Taskbar groups under the brand icon, not generic Python
- [ ] A toast notification fires and shows "jellytoast" + icon
- [ ] SMTC: hardware media keys + the volume-flyout transport work
- [ ] Acrylic backdrop renders; frameless chrome intact
- [ ] "Launch at login" on → survives reboot; off → doesn't start
- [ ] Settings/cache/downloads persist across restarts (`%LOCALAPPDATA%`)
- [ ] Single-instance: second launch focuses the existing window
- [ ] WACK certification test passes

---

## Regenerating assets

```bash
bash packaging/msix/make-assets.sh   # rsvg-convert + ImageMagick; edits the SVG -> re-run
```

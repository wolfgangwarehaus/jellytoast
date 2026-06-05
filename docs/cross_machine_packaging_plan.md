<!--
Generated 2026-06-02 by a multi-agent research workflow: parallel web research on
Arch/AUR packaging, Windows 11 (PyInstaller / libmpv-2.dll), and cross-platform
distribution methodology, plus a read-only codebase-readiness audit. All
codebase-specific claims (the libmpv import guard, the _unsupported.py backend
stubs, the modules/assets icon, the pyproject sys_platform markers, dbus-next for
MPRIS) were spot-verified against modules/ before this doc was committed.
-->

# jellytoast — Cross-Machine Packaging & Test Plan

*Target: get jellytoast running and smoke-tested on a second **Arch Linux laptop** and a **Windows 11 laptop** tomorrow, then graduate to real distribution channels. Repo: `github.com/wolfgangwarehaus/jellytoast`. Version `0.1.0`, requires Python `>=3.11`, launcher `jellytoast` (gui-scripts entry point).*

---

## 1. TL;DR — recommended path

**Arch Linux laptop.** Build one pure-Python wheel on the dev box (`python -m build`), copy it over, install it into an isolated env with **pipx** after pulling the native libs from pacman. The decisive system dep is **`mpv`** (it provides `libmpv.so`, which python-mpv `dlopen`s at import time) plus the Qt6 runtime libs your KDE desktop already has. One-liner once `mpv` + pipx are present: `pipx install ./jellytoast-0.1.0-py3-none-any.whl && jellytoast`. (A `venv --system-site-packages` over pacman's `pyside6`/`python-mpv` is an equally fine alternative if you'd rather not re-download the Qt wheels; pipx is recommended because it isolates cleanly and `pipx uninstall jellytoast` makes iteration trivial.)

**Windows 11 laptop.** Same wheel. Install **Python 3.12 64-bit from python.org** (tick "Add to PATH", **not** the Microsoft Store build), `pipx install` the wheel, and — the one critical extra step — **drop the 64-bit `libmpv-2.dll` somewhere on `PATH`** (next to `python.exe`, or in the venv `Scripts` dir, or any dir you prepend to PATH). python-mpv has **no `MPV_LIBRARY` env override** — it finds the DLL via `PATH` only. Run the first launch from a **real console** (`JT_LOG_LEVEL=DEBUG`), because the gui-scripts entry point suppresses the console window and a missing-DLL / Qt-plugin traceback would otherwise vanish silently.

Do **not** reach for a PKGBUILD or a PyInstaller bundle for tomorrow — those are publish channels, not test channels. The same pure-Python wheel that pipx installs is what the eventual AUR/Flatpak packages will install, so "works via pipx" de-risks the real packaging.

---

## 2. Prep tonight (dev box)

Do all of this on `/home/august/Projects/jellytoast` before bed so tomorrow is pure execution.

- [ ] **Build the wheel.** `python -m pip install --upgrade build && python -m build` → produces `dist/jellytoast-0.1.0-py3-none-any.whl` + `dist/jellytoast-0.1.0.tar.gz`. (Mirrors the CI build job.)
- [ ] **Sanity-check the wheel in a throwaway venv** before trusting it — install it for real, then run the install doctor against that venv's python:
  ```bash
  python -m venv /tmp/wv && /tmp/wv/bin/pip install dist/*.whl
  QT_QPA_PLATFORM=offscreen /tmp/wv/bin/python dev/install_doctor.py   # expect: 0 critical failures
  ```
  `dev/install_doctor.py` is the install-target diagnostic the laptops run too (see §6 step 0) — it checks libmpv, the Qt platform plugin, the entry point + bundled icon, and prints the `pip install 'jellytoast[extra]'` for any off-by-default cast/visualizer feature.
- [ ] **Obtain `libmpv-2.dll` for Windows** — `libmpv.so` will NOT work on Windows. Download the **64-bit** `mpv-dev-x86_64` archive (a `.7z`, the *dev/lib* package, **not** the player package) from the shinchiro builds and extract `libmpv-2.dll`. Verify it's x86_64 (not i686/aarch64) or you'll get `OSError [WinError 193] not a valid Win32 application`. Stage it on a USB stick / Syncthing alongside the wheel.
  - Source A: <https://sourceforge.net/projects/mpv-player-windows/files/libmpv/>
  - Source B: <https://github.com/shinchiro/mpv-winbuild-cmake/releases>
- [ ] **Decide the transfer mechanism** for the wheel + DLL: USB / scp / Syncthing, **or** the throwaway `v0.1.0-test` pre-release so both laptops pull from one place. **The tag + release already exist** (cut 2026-06-02, refreshed since), so *creating* them again fails — `--clobber`-upload the new artifacts onto the existing release instead:
  ```bash
  (cd dist && sha256sum *.whl *.tar.gz > SHA256SUMS)
  gh release upload v0.1.0-test dist/jellytoast-0.1.0-py3-none-any.whl \
    dist/jellytoast-0.1.0.tar.gz dist/SHA256SUMS dev/install_doctor.py --clobber
  # First time only (tag/release don't exist yet):
  #   git tag v0.1.0-test && git push origin v0.1.0-test
  #   gh release create v0.1.0-test dist/* dev/install_doctor.py --prerelease \
  #     --title 'v0.1.0 test build' --notes 'Smoke-test artifacts; not a public release'
  ```
  The release also carries `install_doctor.py` so each laptop can `gh release download v0.1.0-test -p install_doctor.py` alongside the wheel. *(GitHub auto-exposes a per-asset SHA256 digest after upload, so `SHA256SUMS` is belt-and-suspenders. See the [GitHub changelog](https://github.blog/changelog/2025-06-03-releases-now-expose-digests-for-release-assets/).)*
- [ ] **Confirm Python floor.** Both laptops need **Python ≥ 3.11** (`pychromecast>=14` won't resolve on 3.10). The pipx install needs network on each laptop (PySide6 + pychromecast + zeroconf pull large platform wheels).
- [ ] *(Optional, for §7 later — not tonight)* tag a real `v0.1.0` once stable so the AUR source tarball can be pinned by tag rather than a moving branch.

---

## 3. Arch Linux laptop — runbook

### 3.1 Install native system deps via pacman
`mpv` is the load-bearing one — it pulls `libmpv.so` plus drags in Qt/EGL/xkb/dbus. On a KDE Wayland box the Qt runtime libs are already present; on a minimal install add them explicitly.

```bash
sudo pacman -S --needed mpv python-pipx
# If PySide6 fails to init its platform plugin on a minimal box, also add:
sudo pacman -S --needed libxkbcommon libegl glib2 qt6-svg
python -m pipx ensurepath   # reopen the shell afterward
```

### 3.2 Fetch + install the wheel (isolated)
**If you pushed the test pre-release:**
```bash
gh release download v0.1.0-test -p '*.whl'
gh release download v0.1.0-test -p SHA256SUMS && sha256sum -c SHA256SUMS 2>/dev/null
pipx install ./jellytoast-0.1.0-py3-none-any.whl
```
**If you copied the wheel by USB/scp:**
```bash
pipx install /path/to/jellytoast-0.1.0-py3-none-any.whl
```

### 3.3 Launch
```bash
jellytoast
# To see startup diagnostics:
JT_LOG_LEVEL=DEBUG jellytoast
```

### 3.4 Notes
- **Optional cast/visualizer extras** lazy-import and no-op when absent. If you want them: `pipx inject jellytoast numpy soco snapcast async-upnp-client` (and `pyatv` on Linux only). They are NOT required for the smoke test.
- The KDE-only features (keep-above, drag-repaint, MPRIS media keys, PipeWire visualizer) work on this laptop **only if it's also KDE Wayland + PipeWire**. If it isn't, those degrade gracefully — see §5, don't log them as bugs.
- Clean reset between iterations: `pipx uninstall jellytoast`.

---

## 4. Windows 11 laptop — runbook

### 4.1 Install Python (the right one)
- Install **Python 3.12 64-bit from python.org**, tick **"Add python.exe to PATH"**.
- **Avoid the Microsoft Store Python** — it sandboxes AppData and breaks keyring + DLL-folder behavior.
```powershell
py -m pip install --user pipx
py -m pipx ensurepath
# Reopen the terminal so PATH updates take effect.
```

### 4.2 CRITICAL — place `libmpv-2.dll`
python-mpv loads the DLL via ctypes **at import time**, trying `mpv-2.dll` → `libmpv-2.dll` → `mpv-1.dll`, searching system `PATH` then the python-mpv module dir. There is **no env-var override — PATH only.** The import is guarded in `modules/player_backend.py`, so a missing DLL surfaces a "requires libmpv" dialog rather than a hard crash.

Put the **64-bit** `libmpv-2.dll` (from prep) in one of:
- next to `python.exe`, **or**
- in the venv `Scripts\` folder that pipx creates for jellytoast, **or**
- any folder you prepend to `PATH`, **or** (quick test only) `C:\Windows\System32`.

### 4.3 Install the wheel
```powershell
# copied over, or: gh release download v0.1.0-test -p '*.whl'
pipx install .\jellytoast-0.1.0-py3-none-any.whl
```
The platform markers in `pyproject.toml` auto-skip `pyatv`, `dbus-next`, and `python-xlib` on Windows, so the resolve is clean. `keyring` uses Windows Credential Manager automatically (no Windows-specific code).

### 4.4 First launch — from a real console
The gui-scripts entry point creates `jellytoast.exe` with **no console window**, so a clean traceback (missing DLL, Qt plugin, login crash) would vanish. For the first run, force a console so you see errors:
```powershell
$env:JT_LOG_LEVEL="DEBUG"
jellytoast
# If the window never appears, run main() directly to surface the traceback:
#   <pipx venv python> -c "import jellytoast; jellytoast.main()"
```
- "requires libmpv" dialog → DLL not on PATH or wrong arch (re-check §4.2 / 64-bit).
- `OSError [WinError 193]` → 32-bit DLL with 64-bit Python; get the x86_64 build.

### 4.4b Doctor (recommended before launch)
Run `install_doctor.py` with the pipx venv's interpreter to confirm libmpv + Qt + the entry point resolved (see §6 step 0). On Windows the venv python is `%USERPROFILE%\pipx\venvs\jellytoast\Scripts\python.exe`.

### 4.5 Notes
- Several integrations are **expected absent on Windows** (media keys/SMTC, autostart, toast notifications, keep-above, visualizer, AirPlay). The backend packages are empty stubs that no-op via `_unsupported.py`. See §5 — these are **not regressions**.
- **DLNA / Sonos / Snapcast casting + the visualizer are bundled now** (required deps — `async-upnp-client` / `soco` / `snapcast` / `numpy`, all cross-platform), so a plain `pipx install` ships them on every platform; no extras to add. Only AirPlay (pyatv) is excluded on Windows by the platform marker.
- Clean reset between iterations: `pipx uninstall jellytoast`.

---

## 5. What to expect to work vs not, per platform

Core music functionality is cross-platform (Qt + python-mpv + `requests` + stdlib). The Linux/KDE/PipeWire integrations degrade gracefully off their native environment — **flag, don't bug-report**.

| Capability | Arch (KDE Wayland + PipeWire) | Windows 11 | Notes |
|---|---|---|---|
| Qt6 GUI (frameless window, tooltips, geometry) | ✅ | ✅ | PySide6, fully cross-platform |
| **Server login (Jellyfin / Subsonic)** | ✅ | ✅ | `requests` HTTP |
| **Library browse / search / smart playlists** | ✅ | ✅ | Python + Qt |
| **Bit-perfect mpv playback (gapless, ReplayGain)** | ✅ (needs `mpv`/`libmpv.so`) | ✅ (needs `libmpv-2.dll`) | The #1 trap on both OSes |
| Transport controls (play/pause/seek/next) | ✅ | ✅ | python-mpv |
| Credential storage (keyring) | ✅ (kwallet/secret-service) | ✅ (Credential Manager) | stdlib backends |
| Settings persistence (QSettings) | ✅ | ✅ (registry) | Qt abstraction |
| Offline playback + disk cache | ✅ | ✅ | pathlib |
| Scrobbling (ListenBrainz / Last.fm) | ✅ | ✅ | pure HTTP |
| Tag editing (Jellyfin) | ✅ | ✅ | pure HTTP |
| Chromecast cast | ✅ | ✅ | pychromecast |
| DLNA / Sonos / Snapcast cast | ✅ (opt deps) | ✅ (opt deps) | cross-platform; cast proxy works |
| **AirPlay 2 cast** | ✅ (pyatv) | ❌ | pyatv gated off Windows; soft no-op |
| **MPRIS / media-key integration** | ✅ (dbus-next) | ❌ | Windows SMTC backend not implemented (stub) |
| **Desktop notifications** | ✅ (libnotify) | ❌ | Windows WinToast backend not implemented (silent no-op) |
| **Audio visualizer** | ✅ (PipeWire `pw-record`, fallback `parec`) | ❌ | WASAPI loopback backend not implemented (silence stub) |
| **Mini-player keep-above** | ✅ (KWin window rule) | ⚠️ degraded | Windows no-ops via `_unsupported.py`; Qt `WindowStaysOnTopHint` would work but isn't wired |
| **KWin drag-repaint effect** | ✅ (KWin scripted effect) | ❌ | KDE-Wayland-specific; Windows no-op |
| **Autostart / launch-on-login** | ✅ (XDG `~/.config/autostart`) | ❌ | Windows backend not implemented (no-op) |
| X11 cursor startup-notification cleanup | ✅ (X11 only; no-op on Wayland) | ❌ | Xlib Linux-only; guarded no-op |

> **On the Arch laptop specifically:** the KDE/PipeWire rows are ✅ only if that laptop is *also* KDE Wayland + PipeWire. If it's GNOME/X11/PulseAudio, expect keep-above / drag-repaint / MPRIS / visualizer to degrade just like Windows.

---

## 6. Smoke-test checklist (per machine)

The minimal pass/fail gate for "this build works on this machine." Record pass/fail per row, per laptop.

1. **Launches clean** — `jellytoast` opens a window with no traceback (run with `JT_LOG_LEVEL=DEBUG` the first time). ☐
2. **Login** — connect to the server and authenticate; credentials persist across a restart (keyring works). ☐
3. **Browse** — albums/artists/songs load; cover art renders; search returns results. ☐
4. **Play a track with audible audio** — pick a track, press play, **confirm sound comes out of the machine's speakers/headphones**. ☐
5. **Transport controls** — pause/resume, seek within a track, next/previous, volume change all respond. ☐
6. **(Bonus) Cast** — if a Chromecast is on the network, cast a track and confirm it plays on the device. ☐

If 1–5 pass, the build is good on that machine. Anything in the ❌/⚠️ rows of §5 failing is **expected**, not a smoke-test failure.

---

## 7. Distribution roadmap (the larger goal)

All channels hang off **git-tag-driven GitHub Releases**. Order below is by effort/payoff — do the cheap, high-leverage ones first.

### Step 0 — Tag-driven releases (foundation, do first)
- Make the version single-source. You currently pin `0.1.0` in `pyproject.toml`, `version.py` fallback, **and** the metainfo `<release>` — a tag without bumping all three fails `test_version_consistency`. Either keep bumping all three, or adopt **[setuptools-scm](https://setuptools-scm.readthedocs.io/en/latest/usage/)** so `git tag vX.Y.Z` *is* the bump (drop the hardcoded fallback).
- Add `.github/workflows/release.yml` triggered on `push: tags: ['v*']` that builds wheel + sdist (and, on a `windows-latest` matrix leg, a PyInstaller onedir zip), generates `SHA256SUMS`, and attaches everything via **[softprops/action-gh-release](https://github.com/softprops/action-gh-release)** with `--generate-notes`.
- **Effort: low. Payoff: high** — every downstream channel pulls from these tagged release artifacts.

### Step 1 — PyPI wheel (enables `pipx install jellytoast`)
- Publish the same pure-Python wheel to PyPI so the technical audience can `pipx install jellytoast` directly. Lower-value than the OS channels for a GUI app (users still need libmpv + Qt system bits), but trivial once Step 0 exists.
- **Effort: low. Payoff: medium.**

### Step 2 — AUR (`jellytoast` PKGBUILD) — idiomatic Arch channel
- Package name is the **bare `jellytoast`**, not `python-jellytoast` (the `python-` prefix is reserved for importable libraries; this is an end-user app). `arch=('any')`.
- Standard PEP 517 build:
  - `makedepends=(python-build python-installer python-wheel python-setuptools)`
  - `build()`: `python -m build --wheel --no-isolation`
  - `package()`: `python -m installer --destdir="$pkgdir" dist/*.whl`, then `install -Dm644` the `.desktop`, `metainfo.xml`, and hicolor icon (you already ship all three in `packaging/`).
- **`depends=` MUST explicitly include `mpv`** for `libmpv.so` (ctypes-dlopen, so namcap/pip won't infer it). Map the rest: `pyside6 python-mpv python-pychromecast python-zeroconf python-ifaddr python-dbus-next python-requests python-keyring python-cryptography python-xlib`.
- **`optdepends=`** for the AUR-only / lazy-imported extras: `python-numpy` (visualizer), `python-async-upnp-client` (DLNA), `python-soco` (Sonos), `python-snapcast`, `pyatv` (AirPlay — note it's `pyatv`, not `python-pyatv`).
- Pin source by a **release tag tarball** (not a moving branch): `source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")`, fill the sum with `updpkgsums`.
- Validate before publishing: `makepkg -si` (also smoke-tests the package), `namcap PKGBUILD`, `namcap jellytoast-*.pkg.tar.zst`, ideally build in a clean chroot (`pkgctl build`) to catch undeclared deps. Then `makepkg --printsrcinfo > .SRCINFO`, `git clone ssh://aur@aur.archlinux.org/jellytoast.git`, commit `PKGBUILD` + `.SRCINFO`, `git push origin master`. **Regenerate `.SRCINFO` on every metadata change.**
- **Effort: medium. Payoff: high for Arch users** (`paru -S jellytoast`). Refs: [Python package guidelines](https://wiki.archlinux.org/title/Python_package_guidelines), [AUR submission](https://wiki.archlinux.org/title/AUR_submission_guidelines).

### Step 3 — Flatpak / Flathub — broadest Linux reach
- You already have the Flathub-ready `io.github.wolfgangwarehaus.jellytoast.metainfo.xml` + `.desktop` + icon; the **manifest is the missing piece** (deferred per `docs/TODO.md`). Add **screenshots** to the metainfo (Flathub catalog bakes these in — and the cast / mini-player surfaces are your differentiators, so polish them first). Ref: [Flathub submission](https://docs.flathub.org/docs/for-app-authors/submission).
- **Effort: medium-high. Payoff: high** (verified-app badge, every Linux distro).

### Step 4 — Windows installer + winget
- **Build:** PyInstaller 6.x **onedir** (one-folder, *not* onefile — onefile extracts to a temp dir each launch and complicates DLL discovery). Commit a `.spec` with:
  - `--add-binary` for `libmpv-2.dll` (PyInstaller never auto-bundles it — the ctypes load is invisible to the analyzer),
  - `--add-data` for the brand SVG (`modules/assets/jellytoast.svg`, loaded via `importlib.resources`),
  - `console=False`, an `.ico` icon (convert the SVG first),
  - **only PySide6 in the build venv** — PyQt5/PySide2/PyQt6 present → PyInstaller aborts on multiple Qt bindings,
  - `--collect-submodules keyring` if a frozen build drops a keyring backend.
  - Windows uses `;` (not `:`) as the `--add-data`/`--add-binary` separator. *(Nuitka via `pyside6-deploy` is a smaller/faster alternative but harder to debug for the native deps.)*
- **Installer:** wrap the dist folder in an **[Inno Setup](https://jrsoftware.org/isinfo.php)** script — per-user `DefaultDirName` (no UAC), Start Menu + desktop icons, uninstaller, `SolidCompression`.
- **Signing:** sign exe + installer with `signtool` via **Azure Trusted Signing** (cheapest no-token option) or an OV cert. Note: since 2024, even EV certs no longer instantly bypass SmartScreen — reputation accrues over time; for the test laptop tomorrow just click **More info → Run anyway**. Ref: [code signing options](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options).
- **winget:** publish the signed installer on GitHub Releases, then submit a manifest to `microsoft/winget-pkgs`. Inno Setup's `/VERYSILENT /NORESTART` satisfies winget's silent-install requirement. Ref: [winget installer schema](https://github.com/microsoft/winget-pkgs/blob/master/doc/manifest/schema/1.6.0/installer.md).
- **Effort: high. Payoff: medium-high** (Windows is a secondary platform until the missing backends land — see §8).

---

## 8. Open risks & decisions

- **libmpv sourcing (highest-risk).** The single most likely failure on both laptops. Linux is handled by `pacman -S mpv`. **Windows requires you to ship `libmpv-2.dll` yourself** — it's not on the wheel, not on a fresh Windows box, and has no env-var override (PATH only). **Decision:** for the eventual Windows installer, bundle `libmpv-2.dll` via PyInstaller `--add-binary` and pin a specific shinchiro build so you control the libmpv version users get. Consider adding a small win32-only `os.add_dll_directory()` hint in `jellytoast.py` (computing the base from `sys._MEIPASS` if frozen, else the script dir) so the DLL is discoverable in both venv and frozen modes without polluting global PATH.
- **Empty Windows platform backends.** `autostart/`, `media_controls/` (SMTC), `notifications/` (WinToast), `visualizer` (WASAPI loopback) are `_unsupported.py` stubs. **Decision:** ship Windows as a "playback + browse + cast" client now and document the gaps, or invest in the WinRT backends (SMTC media keys + toast notifications are the highest-value) before any winget/Store push. The smoke test (§6) deliberately doesn't gate on these.
- **Code signing.** Unsigned Windows builds trigger SmartScreen warnings indefinitely; signing builds reputation only over time. **Decision:** pick Azure Trusted Signing (cheap, no hardware token) vs an OV cert vs shipping unsigned-with-instructions for the first public Windows release.
- **Version single-source drift.** `0.1.0` is pinned in three places guarded by `test_version_consistency`. **Decision:** adopt setuptools-scm (tag is the only source) before the release workflow lands, or commit to manually bumping all three every release.
- **AUR maintenance burden.** A `-git` package needs a `pkgver()` function and `makedepends=(git)`; a tagged package needs `.SRCINFO` regenerated and the checksum updated every release. **Decision:** start with the tagged-tarball `jellytoast` (reproducible) and only add a separate `jellytoast-git` if there's demand.
- **Don't conflate sideload with publish.** The `v0.1.0-test` pre-release / loose wheel is for your two laptops only. AUR/Flathub/winget have submission rules and can be removed if the manifest breaks — graduate each channel only when the app is stable *and* you'll maintain the manifest.

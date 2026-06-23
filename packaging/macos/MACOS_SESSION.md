# macOS release — session worklist

Brings jellytoast to macOS as a **Developer-ID-signed, notarized `.dmg`**
(download-and-run), **not** the Mac App Store. Rationale: the App Store mandates
the App Sandbox, which fights this app on three fronts at once (libmpv/LuaJIT
JIT, the bundled Qt/Python/libmpv dylibs, and the local cast-proxy + LAN
multicast discovery), plus the GPL↔App-Store-terms conflict — for an audience of
technical self-hosters who download `.dmg`s without friction, the App Store is
weeks of unproven, bespoke signing R&D for no real benefit. The Developer-ID
path needs **no sandbox**, so casting, the local server, and mpv all "just
work"; the only entitlements required are `allow-unsigned-executable-memory`
(LuaJIT) and `disable-library-validation` (bundled dylibs), both freely
permitted outside the App Store. See `packaging/macos/entitlements.plist`.

> Distribution model parity: like the Linux AppImage and the Windows Inno/
> portable builds, the macOS `.app` **bundles its own libmpv** (there is no
> system libmpv on a clean Mac). Unlike the `.deb`, which depends on the host.

---

## Status

### ✅ Done — landed in this branch (authored on Linux, CI-verifiable)

- **libmpv loader** (`jellytoast/player_backend.py`): a `darwin` + frozen branch
  redirects `ctypes.util.find_library("mpv")` at the bundled `libmpv*.dylib`
  (python-mpv's macOS path has **no** in-bundle fallback, and SIP strips
  `DYLD_*` from the notarized binary, so an env-var redirect is unreliable).
- **Autostart** (`jellytoast/autostart/_macos.py` + router): a per-user
  LaunchAgent `.plist` with `RunAtLoad`. Pure stdlib; unit-tested in
  `tests/test_autostart.py` (`test_macos_*`).
- **PyInstaller `.app`** (`packaging/pyinstaller/jellytoast.spec`): a `darwin`
  branch stages `packaging/macos/libmpv/libmpv*.dylib` into the bundle and a
  `BUNDLE()` step wraps the COLLECT tree into `jellytoast.app` with an Info.plist
  (incl. `NSLocalNetworkUsageDescription` for the macOS 15 Local-Network prompt).
- **Packaging scripts** (`packaging/macos/`): `get_libmpv.sh` (Homebrew),
  `sign_app.sh` (inside-out codesign + hardened runtime + entitlements),
  `build_dmg.sh` (hdiutil), `notarize.sh` (notarytool + stapler). Linted by the
  CI `shellcheck` gate.
- **Release CI** (`.github/workflows/release.yml`): a `build-macos` job on the
  free Apple-Silicon runner builds the `.app`, smoke-tests it headless, builds
  the `.dmg`, and **signs + notarizes once the Apple secrets exist** (gated like
  the Windows Azure signing — unsigned `.dmg` until then). Wired into
  `draft-release` `needs`. `pyobjc` added to the `macos` extra in `pyproject.toml`.

### ⏳ To do — on the rented Scaleway M1 (needs real Mac hardware)

Tick these as you go; squash-merge only when the relevant ones are **verified**.

#### 1. One-time accounts / setup (at-the-computer)
- [x] Enroll the **Apple Developer Program** ($99/yr, **Individual**). ✅ **DONE — account live 2026-06-23.**
- [ ] Create a **Developer ID Application** certificate; export the `.p12`.
      **→ exact steps in `packaging/macos/SIGNING_SETUP.md` (do on the Mac).**
- [x] Create an **App Store Connect API key** (`.p8` + Key ID + Issuer) for
      headless notarization. ✅ **DONE — have the `.p8`, Key ID, Issuer ID.**
- [ ] Stand up the **Scaleway Mac mini M1** (~€0.11/hr, **24h min** ≈ €2.64/day),
      access via VNC. `brew install python@3.12 mpv`. (Will also run a Claude
      Code instance here — `needs:mac` pickup.)

#### 2. Verify the build end-to-end (on the Mac)
- [ ] Trigger `release.yml` via **workflow_dispatch** (dry run) and confirm the
      `build-macos` job goes green: `.app` builds, **smoke test survives 12s**
      (proves `import mpv` + Qt boot), `.dmg` is produced and uploaded.
- [ ] Pull the unsigned `.dmg`, run the app on the Scaleway Mac, and **verify
      live**: playback (bit-perfect), cast discovery (Chromecast/AirPlay/DLNA/
      Sonos — expect the one-time Local-Network prompt), the cast proxy, offline
      downloads, keyring (Keychain), and the mini-player keep-above.
- [ ] Confirm PyInstaller actually bundled libmpv's **dep closure** (FFmpeg,
      libass…) with rewritten install names: `otool -L` the bundled `libmpv` and
      `dist/jellytoast.app/Contents/MacOS/jellytoast`. If deps resolve to
      `/opt/homebrew/...` absolute paths, fix in the spec / a post-build step.

#### 3. Add the secrets, sign + notarize
**→ Full step-by-step (cert → `.p12` → 7 secrets → re-run → verify): `packaging/macos/SIGNING_SETUP.md`.**
- [ ] Add repo secrets: `APPLE_CERTIFICATE` (base64 `.p12`),
      `APPLE_CERTIFICATE_PWD`, `APPLE_SIGNING_IDENTITY`
      (`Developer ID Application: NAME (TEAMID)`), `APPLE_KEYCHAIN_PWD`,
      `APPLE_API_KEY_ID`, `APPLE_API_ISSUER`, `APPLE_API_KEY_B64` (base64 `.p8`).
      Gate them behind a protected Environment so fork PRs can't read them.
- [ ] Re-run the release; confirm the sign + notarize steps now run and
      `xcrun stapler validate` passes. Download the `.dmg` on a **fresh** Mac and
      confirm it opens with **no** Gatekeeper warning (clean first-launch).

#### 4. Native polish (Tier 2 — needs the Mac; judge over local GUI, not VNC)
- [ ] **Media controls**: implement `jellytoast/media_controls/_macos.py`
      (pyobjc `MPNowPlayingInfoCenter` + `MPRemoteCommandCenter`), wire an
      `IS_MACOS` branch into `media_controls/__init__.py` (try/except →
      `_unsupported`), add `pyobjc` (move from the `macos` extra to a
      `sys_platform == "darwin"` hard dep once imported). Verify Now Playing +
      media keys in Control Center.
- [ ] **Vibrancy**: replace the `jellytoast/blur/_macos.py` stub with the real
      `NSVisualEffectView` bridge (already routed in `blur/__init__.py`). **Judge
      blur quality on a local display — VNC latency misrepresents it.**
- [ ] (Optional) native traffic-light window buttons / titlebar.

#### 5. Distribution wiring
- [ ] Add an `.icns` at `packaging/macos/jellytoast.icns` (the spec picks it up
      automatically) — generate from the brand SVG with `iconutil`.
- [ ] Add a **macOS test leg** to `ci.yml` `matrix.os` (`macos-14`),
      `continue-on-error` first, then skip/fix the Linux-only tests it surfaces;
      promote to a required check once green.
- [ ] Consider a **universal2** (arm64 + x86_64) build for Intel Macs — needs a
      universal libmpv chain; otherwise ship arm64-only and note it.
- [ ] Update the install docs (site + README) and `pyproject` description /
      classifiers to include macOS **once the notarized `.dmg` is verified**.
- [ ] Add a Homebrew **cask** (later channel) pointing at the notarized `.dmg`.

---

## Gotchas (from the research — don't relearn these the hard way)

- LuaJIT needs `allow-unsigned-executable-memory`, **not** the narrower
  `allow-jit` (LuaJIT doesn't use Apple's `MAP_JIT` flag → "Code Signature
  Invalid" crash). Alternative: build libmpv `--disable-lua` and drop the
  entitlement entirely.
- **Sign inside-out**, never `codesign --deep` for Python/Qt bundles. Local
  `codesign --verify` can pass while notarization still fails.
- Apple forbids non-executable/data files in `Contents/MacOS` and
  `Contents/Frameworks` — a recurring notarization rejection cause.
- Preserve symlinks through any zip/cp (`zip -y`, `cp -R`); a naive `zip -r`/
  `cp -r` mangles PyInstaller's framework symlinks and invalidates signatures.
- `notarytool` only (altool is removed); always `--timestamp`; staple so
  Gatekeeper verifies **offline** on first launch.

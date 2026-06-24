# macOS platform playbook (dough-portable)

Everything learned shipping a **PySide6 (Qt6) + libmpv** app on macOS — the
reusable, app-agnostic knowledge, written so **dough** (and future apps) can
lift it wholesale. jellytoast is the first app through this; nothing here is
jellytoast-specific except where noted.

> Companions: `SIGNING_SETUP.md` (exact cert→secret→notarize steps),
> `MACOS_SESSION.md` (Developer-ID `.dmg` worklist), `MAS_SESSION.md` +
> `mas/` (Mac App Store track). This file is the **index + the gotchas + the
> reusable patterns**.

---

## 1. Two distribution channels, one codebase

| | **Developer-ID `.dmg`** (ship now) | **Mac App Store `.pkg`** (spike) |
|---|---|---|
| Sandbox | NO | **YES (mandatory)** |
| Hardened runtime | **YES (required for notarization)** | optional |
| libmpv | Homebrew GPL + Lua (fine) | **from-source LGPL, `-Dlua=disabled`** |
| Sign with | Developer ID Application | Apple Distribution (+ Mac Installer Distribution for the `.pkg`) |
| `disable-library-validation` | allowed | **forbidden** → re-sign every nested Mach-O inside-out |
| Submit via | notarytool → staple → `.dmg` | **productbuild `.pkg` → Transporter → App Review** (NOT notarized) |

**Keep both artifacts.** The `.dmg` is the canonical channel; MAS is a separate
parallel build. Verified working on macOS 26 Tahoe.

---

## 2. Code-signing gotchas (the expensive ones)

1. **Sign EVERY nested Mach-O, not just `*.dylib`/`*.so`.** Qt ships its
   libraries as `.framework` bundles whose real binary is **extensionless**
   (`QtCore.framework/Versions/A/QtCore`), and the bundled CPython framework
   binary (`Python.framework/Versions/3.12/Python`) likewise. The Apple notary
   rejects any unsigned Mach-O. Detect by **content** (`file -b "$f" | grep
   Mach-O`), never by name. Sign inside-out (nested first, the `.app` last);
   never `codesign --deep`.
2. **`security import` needs `-f pkcs12`** when the cert is written to an
   extensionless temp file (CI does `mktemp`) — otherwise
   `SecKeychainItemImport: Unknown format in import`.
3. **`disable-library-validation` + macOS 26 Tahoe** — flagged as risky;
   moot once you sign every nested binary with your own identity (then you
   don't need the entitlement at all — required for the MAS sandbox anyway).
4. **GUI-subsystem bundles have no Python `stderr`** — `logging.basicConfig`
   to stderr is swallowed in the frozen `.app`. Debug from source.
5. **libmpv loader on macOS**: python-mpv has no in-bundle fallback and SIP
   strips `DYLD_*` from a notarized binary, so redirect
   `ctypes.util.find_library("mpv")` at the bundled dylib in a `darwin`+frozen
   branch (see `player_backend.py`). `LC_NUMERIC=C` is required for libmpv.

---

## 3. LGPL / no-Lua libmpv build (PROVEN — for the MAS sandbox)

The hard MAS licensing gate: bundle **LGPL-only** native media. A music player
loses nothing by dropping GPL (all audio decoders — FLAC/MP3/AAC/ALAC/Vorbis/
Opus/WavPack/DSD — are LGPL `libavcodec`), and `-Dlua=disabled` removes LuaJIT
so the `allow-unsigned-executable-memory` JIT entitlement is unnecessary.

```sh
# FFmpeg (LGPL): --disable-gpl --disable-nonfree, shared libs, no programs
./configure --prefix=$PREFIX --enable-shared --disable-static \
  --disable-gpl --disable-nonfree --disable-programs --disable-doc \
  --disable-postproc --disable-avdevice --disable-encoders --disable-muxers \
  --enable-audiotoolbox
# libmpv (LGPL, no Lua): link the LGPL FFmpeg via PKG_CONFIG_PATH
meson setup build --prefix=$PREFIX -Dgpl=false -Dlibmpv=true -Dcplayer=false \
  -Dlua=disabled -Djavascript=disabled -Dvapoursynth=disabled \
  -Ddefault_library=shared
```
Verify: `meson configure build | grep -E 'gpl|lua'` shows `false`/`disabled`,
and `otool -L libmpv.2.dylib` shows **no** `libx264`/`libx265`/`libpostproc`
and links **your** LGPL `libav*` (not Homebrew's GPL ones). PyInstaller's
macholib then pulls the LGPL dep-closure into the bundle automatically — just
drop the LGPL `libmpv.2.dylib` where the spec stages it.

---

## 4. Mac App Store — the gate status (researched + spiked)

Verdict: **feasible but pioneering** — no confirmed public PySide6+libmpv MAS
app exists (the one "precedent" was only a Transporter *upload*). Every gate is
solvable; the worst (a Qt rebuild) is **off the table**.

- ✅ **PySide6 wheel is clean** of the private `_responsibility_*`/`_lzma_`
  symbols on arm64 → **no Qt-from-source rebuild**. (Re-probe per version:
  `nm -m .../QtCore.framework/.../QtCore | grep -E 'responsibility|disclaim|_lzma_'`.)
- ✅ **LGPL/no-Lua libmpv** built + verified (§3).
- ✅ **Inside-out Apple-Distribution signing** = the same technique proven for
  Developer-ID, just swap the identity + sandbox entitlements.
- ✅ **Sandbox entitlements** settled (`mas/entitlements.mas.plist`):
  app-sandbox + network.client + network.server + files.user-selected.read-write
  + bookmarks.app-scope + keychain-access-groups. **No multicast entitlement
  exists on native macOS** (it's iOS-only) — LAN cast discovery rides the
  network entitlements + the macOS 15 Local-Network prompt
  (`NSLocalNetworkUsageDescription` + `NSBonjourServices` in Info.plist).
  Incoming sockets (a cast-proxy listener) are NOT gated by Local Network.
- ✅ **Storage** auto-redirects: under the sandbox `QStandardPaths`/`$HOME`
  point into `~/Library/Containers/<bundle-id>/Data` with zero code change
  (only a one-time migration of existing non-container data is needed).
- 🟡 **CPython `itms-services`** string (urllib/parse.py, an automated
  App-Review reject) → build CPython `--with-app-store-compliance`, or
  post-build patch the string in the bundle (App-Review-time, not
  validation-time).
- ⏳ **Sandbox code changes** (for a shippable build, not the validation
  spike): autostart via **SMAppService** (a sandbox can't write LaunchAgents),
  security-scoped bookmarks for an out-of-container user folder.
- ⏳ **Needs the human:** Apple Distribution cert + Mac Installer Distribution
  cert + explicit App ID + a sandbox provisioning profile (embedded at
  `Contents/embedded.provisionprofile`).

Realistic time-box outcome: a `.pkg` that passes **Transporter validation +
launches sandboxed + plays audio** = the real go/no-go. App-Review *approval*
is days–weeks (human review), out of any spike's scope.

---

## 5. Native-integration patterns (reusable — dough-grade)

All gated on `IS_MACOS`, all lazy-importing pyobjc so they're no-ops elsewhere.

- **Media controls** — `MPNowPlayingInfoCenter` (push title/artist/album/
  duration/elapsed + playback state) + `MPRemoteCommandCenter` (play/pause/
  toggle/next/prev/stop/seek handlers route media keys back). Marshal handlers
  onto the GUI thread via a queued Qt signal. (`media_controls/_macos.py`.)
- **Vibrancy** — install an `NSVisualEffectView` (blending mode behind-window)
  as the window content view, re-parent Qt's translucent view on top; fully
  reversible (restore the original content view + opacity). (`blur/_macos.py`.)
  NOT Tahoe "Liquid Glass" (`NSGlassEffectView`) — that's reserved for the
  navigation layer, not window backdrops.
- **Global menu bar** — a `QMenuBar` (App/File/Edit/View/Window/Help). Set
  `QAction.setMenuRole()` **explicitly** (About/Preferences/Quit relocate into
  the native app menu by role; everything else → `NoRole` so the text
  heuristic can't mis-relocate it). Don't create a named app-menu — it'd be
  left empty after the relocation. (`macos_menubar.py`.)
- **Dock menu** — `QMenu().setAsDockMenu()` with transport actions (pure Qt).
- **Transparent titlebar** — keep the real NSWindow (never frameless on Mac),
  set `NSWindowStyleMaskFullSizeContentView` + `titlebarAppearsTransparent` +
  hidden title + no separator + movable-by-background; reserve a ~28pt top
  inset in the chrome so content clears the traffic lights. (`macos_window.py`.)
- **Notifications** — shell out to `osascript -e 'display notification …'`
  (the macOS `notify-send`); pass title/body as AppleScript run-args, never
  interpolated. (`notifications/_macos.py`.)
- **Dark mode** follows the system for free via Qt's `colorScheme()` +
  `colorSchemeChanged`. **System accent** needs `QPalette::Accent` (Qt 6.6+) or
  a pyobjc read of `NSColor.controlAccentColor`.
- **Still TODO** for "a great Mac app": honor Reduce-Transparency / Reduce-
  Motion (`NSWorkspace` a11y flags), VoiceOver labels on delegate-drawn lists,
  native About panel, Dock-reopen semantics. See the audit in `MACOS_SESSION.md`.

---

## 6. Driving / testing a rented Mac headlessly

- **Rent:** Scaleway Apple-silicon, zone **PAR-3**, hourly, auto-delete-after-24h.
  SSH-key-first; VNC via KRDC/TigerVNC (`vncviewer <ip>:<port>`). The VNC
  password is also the `m1` account/sudo password.
- **Homebrew sudo-free:** `sudo mkdir -p /opt/homebrew && sudo chown -R user:admin
  /opt/homebrew`, then untar the tarball into it (never write a NOPASSWD sudoers).
- **Test bridge** (`JT_TEST_BRIDGE=1`) binds a `QLocalServer` you can drive over
  a raw Unix socket (no PySide6 client needed). **It binds under the cocoa
  platform, NOT offscreen** — and the socket lands in the per-user temp
  (`$TMPDIR`), not `/tmp`. Connect from a plain SSH as the same user.
- **GUI launch from SSH:** `sudo launchctl asuser $(id -u) sudo -u user <cmd>`
  puts it in the Aqua session so windows render.
- **Screenshots:** `screencapture` over SSH fails (no Screen-Recording TCC);
  instead screenshot the *Linux* box whose VNC viewer shows the Mac. Keep the
  Mac display awake with `caffeinate -dimsu` or the framebuffer dims.

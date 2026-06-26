# Mac App Store spike — status + cert/secrets setup

Where the MAS (sandboxed `.pkg`) track stands, what's proven, and the exact
steps **you** do (the cert paperwork) so CI can finish it. Built on #178's
research scaffold (`entitlements.mas.plist`, `MAS_SESSION.md`).

## TL;DR

**Feasible, and the hard parts are done.** The only true showstopper — a
GPL/LuaJIT libmpv that the App Sandbox rejects — is solved (LGPL/no-Lua libmpv
built + verified). The remaining work is the **cert paperwork (you)** + a
**CI sign/package job** (because code-signing can't run on the rented Mac over
SSH — it needs a real login session, which CI has, exactly like the `.dmg`).

## What's proven ✅

| Gate | State |
|---|---|
| **LGPL/no-Lua libmpv** | built from source + verified GPL-clean (`build_libmpv_lgpl.sh`). Kills the `allow-unsigned-executable-memory` + GPL problems. |
| **MAS `.app`** | built with the LGPL libmpv (`build_mas_app.sh`) — 199 MB, no x264/x265/postproc anywhere, deps `@rpath` self-contained. |
| **PySide6 wheel** | **clean** of the private `_responsibility_*`/`_lzma_` symbols on arm64 → **no Qt-from-source rebuild** (the big de-risk). |
| **Sandbox entitlements** | settled (`entitlements.mas.plist` parent + `entitlements.child.plist` inherit). Multicast entitlement is iOS-only/N-A on macOS; LAN discovery rides the network entitlements + the Local-Network prompt. |
| **Inside-out signing** | proven on the `.dmg` (every nested Mach-O by `file -b`); transfers directly — just swap the identity to Apple Distribution + sandbox entitlements. |

## What's blocked (and why it's fine)

- **Code-signing can't run on the rented Mac over SSH** — `codesign`
  gets `errSecInternalComponent` (the SSH session can't do the keychain
  crypto), and via `launchctl asuser` it hangs on a GUI keychain prompt.
  This is the same class as the `screencapture` TCC block. **Fix: sign in
  CI** (a fresh login session — the `.dmg` already signs there fine), behind
  the secrets below. The rented Mac is for interactive testing + the libmpv
  build, not signing.
- **CPython `itms-services`** string → patch the bundled `urllib/parse.py`
  in the CI job (App-Review-time; doesn't block Transporter validation).

## YOUR step — create the certs + App ID + profile

All at [developer.apple.com/account](https://developer.apple.com/account) +
Keychain Access (same flow as the Developer-ID cert, different types):

1. **Apple Distribution certificate** → Certificates → (＋) → **Apple
   Distribution** → upload a CSR (Keychain Access → Certificate Assistant →
   Request a Cert From a CA → Saved to disk) → download `.cer` → install →
   in Keychain, export the identity (cert **+** key) as `apple-distribution.p12`.
2. **Mac Installer Distribution certificate** → Certificates → (＋) →
   **Mac Installer Distribution** (a.k.a. "3rd Party Mac Developer Installer")
   → same CSR flow → export as `mac-installer.p12`.
3. **App ID** → Identifiers → (＋) → **App IDs** → App → Bundle ID
   **explicit** = `io.github.wolfgangwarehaus.jellytoast` → enable the
   **App Sandbox** capability → register.
4. **Provisioning profile** → Profiles → (＋) → **Mac App Store** distribution
   → pick the App ID above + the Apple Distribution cert → download
   `jellytoast.provisionprofile`.

## Then add these CI secrets (Repo → Settings → Secrets → Actions)

Reuse the App Store Connect API key you already have (`APPLE_API_KEY_*`).

| Secret | Value |
|---|---|
| `APPLE_DIST_CERT` | base64 of `apple-distribution.p12` |
| `APPLE_DIST_CERT_PWD` | its export password |
| `APPLE_INSTALLER_CERT` | base64 of `mac-installer.p12` |
| `APPLE_INSTALLER_CERT_PWD` | its export password |
| `APPLE_DIST_IDENTITY` | `Apple Distribution: WILLIAM AUGUST MUELLER (UNP3CF774H)` |
| `APPLE_INSTALLER_IDENTITY` | `3rd Party Mac Developer Installer: WILLIAM AUGUST MUELLER (UNP3CF774H)` |
| `MAS_PROVISION_PROFILE` | base64 of `jellytoast.provisionprofile` |

(`base64 -i file | pbcopy` on the Mac; `base64 -w0 file` on Linux.)

## Then CI does (a `build-mas` job — to be added, mirrors `build-macos`)

1. `build_libmpv_lgpl.sh` → LGPL libmpv. 2. `build_mas_app.sh` → MAS `.app`.
3. Import the 2 certs into a temp keychain (the proven CI pattern).
4. Embed `MAS_PROVISION_PROFILE` at `Contents/embedded.provisionprofile`.
5. Sign inside-out: nested Mach-O with `entitlements.child.plist`, the `.app`
   with `entitlements.mas.plist` (+ the `keychain-access-groups` prefix the
   profile authorizes), using the Apple Distribution identity.
6. `productbuild --component jellytoast.app /Applications --sign
   "$APPLE_INSTALLER_IDENTITY" jellytoast.pkg`.
7. `xcrun altool --validate-app -f jellytoast.pkg -t macos --apiKey … --apiIssuer …`
   → **the real go/no-go.** Then a sandboxed launch smoke test.

App-Review *approval* (human, days–weeks) is the final step, out of scope here.

## Files

- `build_libmpv_lgpl.sh` — proven LGPL/no-Lua libmpv recipe.
- `build_mas_app.sh` — MAS `.app` build (LGPL libmpv swap + itms-services patch).
- `entitlements.mas.plist` (#178) — parent sandbox entitlements.
- `entitlements.child.plist` — nested-binary (inherit) entitlements.
- See also `../MACOS_PLAYBOOK.md` §4 for the dough-portable summary.

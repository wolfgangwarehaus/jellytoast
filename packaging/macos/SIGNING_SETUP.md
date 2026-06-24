# Signing & notarization setup — Developer-ID `.dmg`

How to turn the green-but-**unsigned** `build-macos` CI job into a **signed +
notarized** `.dmg`. One-time setup: create the signing cert, add 7 GitHub
secrets, re-run. The build itself is already proven (run on the free
Apple-Silicon runner — no Mac needed for the build).

Prereqs (all done once the account is live):
- ✅ Apple Developer Program membership (live) + **Team ID** (10 chars).
- ✅ **App Store Connect API key** for notarization — created at
  appstoreconnect.apple.com → Users and Access → **Integrations** → App Store
  Connect API → **Team Keys**. You have: the `AuthKey_XXXX.p8` file, the
  **Key ID** (10 chars), and the **Issuer ID** (UUID). *(No Mac needed.)*
- ⏳ **Developer ID Application certificate** → `.p12` — the remaining piece;
  easiest on the Mac (below).

---

## 1. Create the Developer ID Application cert → `.p12` (on the Scaleway Mac)

Keychain Access path (no Xcode required):

1. **Keychain Access** → menu **Certificate Assistant → Request a Certificate
   From a Certificate Authority**. Enter your email + a name; choose **Saved to
   disk**. → produces `CertificateSigningRequest.certSigningRequest`.
2. [developer.apple.com](https://developer.apple.com/account) → **Certificates**
   → **(+)** → **Developer ID Application** → upload the CSR → **Continue** →
   **Download** the `.cer`.
3. Double-click the `.cer` to install it into the **login** keychain.
4. In Keychain Access → **login** keychain → **My Certificates**: find
   `Developer ID Application: <Your Name> (<TEAMID>)`. **Expand it** and confirm
   there's a **private key** underneath. Select the certificate **and** its key
   → right-click → **Export 2 items…** → save as a `.p12` and set an **export
   password**. ⚠️ Export the *identity* (cert **+** key), not just the cert.
   - *Even easier if Xcode is installed:* Xcode → Settings → Accounts → Manage
     Certificates → **+** → Developer ID Application; then export from Keychain.
5. Get the **exact** signing-identity string (paste it verbatim into the secret):
   ```sh
   security find-identity -v -p codesigning
   # → "Developer ID Application: Your Name (TEAMID)"  ← copy without the quotes
   ```

## 2. Base64-encode the two files (for the secrets)

```sh
# on the Mac:
base64 -i jellytoast-devid.p12 | pbcopy      # → APPLE_CERTIFICATE
base64 -i AuthKey_XXXX.p8 | pbcopy           # → APPLE_API_KEY_B64
# (on Linux the flag is: base64 -w0 <file>)
```

## 3. Add the 7 GitHub repo secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Names must match exactly (the `build-macos` job reads these):

| Secret | Value |
|---|---|
| `APPLE_CERTIFICATE` | base64 of the `.p12` |
| `APPLE_CERTIFICATE_PWD` | the `.p12` export password (from step 1.4) |
| `APPLE_SIGNING_IDENTITY` | `Developer ID Application: Your Name (TEAMID)` (step 1.5, exact) |
| `APPLE_KEYCHAIN_PWD` | any random string (CI's temp-keychain password) |
| `APPLE_API_KEY_ID` | the Key ID (10 chars) |
| `APPLE_API_ISSUER` | the Issuer ID (UUID) |
| `APPLE_API_KEY_B64` | base64 of the `.p8` |

> No `APPLE_TEAM_ID` secret is needed: notarization uses the API key, and the
> signing identity string already embeds the Team ID.
> Tip: put these in a protected GitHub **Environment** so fork PRs can't read them.

## 4. Re-run the build → signed + notarized `.dmg`

```sh
gh workflow run release.yml --ref feat/macos-packaging-foundation
```
With the secrets present, `build-macos`'s `SIGN`/`NOTARIZE` gates flip on:
import-cert → codesign (hardened runtime, inside-out) → smoke test → build `.dmg`
→ `notarytool submit --wait` → `stapler staple` → upload. (Claude can trigger +
watch this for you.)

## 5. Verify the notarized `.dmg`

- CI: the **Notarize + staple** step must succeed (it runs `stapler staple` +
  `stapler validate`).
- On a Mac: `xcrun stapler validate jellytoast-<ver>-macos.dmg`, and open it on a
  **clean** Mac → it should launch with **no** Gatekeeper warning.

## Troubleshooting

- **codesign chain / "unable to build chain"**: import the **Developer ID
  Certification Authority** (WWDR) intermediate into the keychain too. GitHub's
  macOS runners usually already have Apple's intermediates, so this is rare.
- **notarytool "You must first sign the relevant contracts"**: the Account
  Holder must re-accept the latest agreement in App Store Connect, then re-run.
- **`security import` hangs / signing prompt in CI**: the job already runs
  `security set-key-partition-list` to allow non-interactive codesign — don't
  remove it.
- Wrong `APPLE_SIGNING_IDENTITY` is the most common failure — it must match
  `security find-identity` output character-for-character.

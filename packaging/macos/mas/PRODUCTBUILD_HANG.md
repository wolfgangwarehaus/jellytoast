# Native `.pkg` signing — the uncracked path (fallback checkpoint)

> **Status: PARKED.** MAS `.pkg` signing currently ships via **rcodesign** (off-keychain) in
> `.github/workflows/release.yml` → `build-mas`. That path works end-to-end (build → sign →
> `altool --validate-app` reaches App Store Connect). **This document is the fallback:** the
> Apple-native `productbuild --sign` path that we never got working on the headless GitHub
> runner. Resume this **only if** rcodesign is rejected by App Store Connect on real upload
> (its one unproven step — see "Fallback trigger"), or if rcodesign becomes unusable.

## The problem in one line

`productbuild --sign "3rd Party Mac Developer Installer: …"` **hangs forever** (zero output)
at startup on the headless GitHub `macos-14` runner while accessing the installer private key
in the keychain. Only the job's `timeout-minutes` ends it. This burned the first ~4 build-mas
runs (one to the default 6 h, the rest to a 50-min cap).

## Verified facts — do NOT re-test these

- **Hangs before any output**, i.e. before productbuild even prints `Adding component`. So the
  stall is in **identity resolution / key access**, not packaging.
- **`codesign` works on the same runner, same keychain.** The `.app` inside-out signing (build-mas
  step 9) passes every run, and the `.dmg` job's `codesign --options runtime --timestamp` succeeds
  on an identical runner. ⇒ the keychain itself, **network egress to Apple** (timestamp/OCSP), and
  runner auth are all fine.
- **Unsigned `productbuild --component` (no `--sign`) succeeds instantly** on the runner
  (run `28165883198`, step 11). ⇒ the hang is **specifically the `--sign` keychain key-access**,
  not productbuild, not the `.app`, not the build.
- **On a real Mac it NEVER reproduced.** On the rented Scaleway box — both interactive (VNC) and
  over plain SSH — `productbuild --sign` signs a `.pkg` in **<1 s in every keychain permutation
  tried**: login-keychain present, temp-keychain-only, `-T /usr/bin/productbuild` ACL, `-A`
  allow-all, WWDR-as-sole-trust, default-vs-search-list.

## Theories tried and DISPROVEN

1. **Missing Apple WWDR intermediate (`CSSMERR_TP_NOT_TRUSTED`).** A real, separate bug — the build
   keychain had the leaf installer cert but not the WWDR intermediate it chains through. Fixed by
   `curl`-ing AppleWWDRCAG3 + G6 and `security import`-ing them → `find-identity -p basic` then
   showed the cert TRUSTED. **The hang continued.** (Moot on the rcodesign path — rcodesign bundles
   its own Apple CA certs.)
2. **Dropped `codesign:` partition token.** A 4-agent research workflow's high-confidence call: the
   working `.dmg` job uses `set-key-partition-list -S apple-tool:,apple:,codesign:` and the MAS job
   had dropped `codesign:` (it *is* a valid client-name token, not an invalid one as earlier
   assumed). Restored it (run `28162926816` — confirmed applied in the log). **The hang continued.**
3. **Network / timestamp stall at startup.** Disproven by fact #2 above (the `.dmg` job's
   `codesign --timestamp` hits `timestamp.apple.com` on the same runner and succeeds). Adding
   `--timestamp=none` to productbuild also didn't help.
4. **The real 199 MB `.app` (nested signed frameworks) vs the tiny dummy used in Mac tests.**
   Disproven: the hang is *before* `Adding component` (before the `.app` is read), and unsigned
   `productbuild` on the real `.app` succeeded instantly.

## Leading UNTESTED theory

The headless runner has **no logged-in GUI / Aqua / SecurityAgent session** to answer the
**non-interactive authorization prompt** productbuild raises for the installer key. `codesign`
takes a key-access path that doesn't trigger that prompt; `productbuild` (or a helper it spawns)
does. It never reproduced on the Mac because the VNC desktop has a logged-in `SecurityAgent` that
can satisfy the prompt. (cf. Apple Developer Forums thread 666107: a hung codesign/productbuild is
"blocked in an IPC request to securityd, waiting for the user to respond.")

## Resume checklist — things NOT yet tried (roughly prioritized)

1. **Get a fast LOCAL reproduction first.** Every idea below otherwise costs a 6–50 min CI cycle.
   On a Mac, force a truly sessionless context (run productbuild from a launchd job with no Aqua
   session, `launchctl asuser` into a non-GUI session, or boot the GUI session out) until the hang
   reproduces locally.
2. **Observe the actual prompt.** While it hangs, capture what securityd asks:
   `log stream --predicate 'process == "securityd" OR process == "SecurityAgent"'`
   (or `sudo log show --last 5m`), or run productbuild under `fs_usage`/`sudo dtruss`, or
   `ssh -tt` with a pty. Turn the guess into a fact.
3. **Pre-authorize the key non-interactively.** Beyond `set-key-partition-list`: import the installer
   key with `-A` (allow-all) *plus* the partition list; try
   `-S apple-tool:,apple:,codesign:,teamid:UNP3CF774H`; or `sudo security authorizationdb` for the
   relevant right.
4. **Give the runner a session.** A **self-hosted macOS runner logged into a real desktop** (where
   productbuild --sign is known to work) — heaviest, but most likely to "just work."
5. **Different native code path.** `productsign --sign` on the unsigned pkg (likely the same hang,
   but untested in CI); `pkgbuild` + `productbuild --distribution`. Cheap once a local repro exists.
6. **Search fresh.** This is a known class of pain on GH runners — re-check `actions/runner-images`
   issues and fastlane for a current canonical fix before sinking time in.

## How to reproduce / debug

- Job: `.github/workflows/release.yml` → `build-mas` (workflow_dispatch only).
  Dispatch: `gh workflow run release.yml --ref <branch>` · watch: `gh run watch <id>`.
- The rented Mac (while leased): installer cert at `~/mac-installer.p12` (pw in the session, not
  here), `productbuild` at `/usr/bin/productbuild`. The A/B + permutation test scripts from the
  cracking session lived in the session scratchpad (not committed) — re-derive from the facts above.

## The removed code (restore from git if cracking this)

The full native keychain setup (Mac Installer cert import + WWDR import +
`apple-tool:,apple:,codesign:` partition) and the `productbuild --component … --sign … pkg`
step were removed when we switched to rcodesign. To restore the native attempt, recover them from
the git history of `release.yml` on `feat/macos-app-store-prep` (the rcodesign switch is
`5617e90`; the keychain trim is `c692674`; the last native `--sign` + WWDR + `codesign:` versions
are in the commits just before those).

## Fallback trigger

Crack this **only if** rcodesign's signature is rejected by App Store Connect on real **upload**
(currently unverifiable — blocked behind the `-19000` "no app record" gate; resolves once the
App Store Connect app record exists for `io.github.wolfgangwarehaus.jellytoast`), **or** if
rcodesign becomes unmaintained / breaks on an Apple format change. Until then, the off-keychain
rcodesign path is correct and this stays parked.

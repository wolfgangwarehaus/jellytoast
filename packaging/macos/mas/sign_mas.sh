#!/usr/bin/env bash
# Code-sign a macOS .app for MAC APP STORE (sandboxed) distribution.
#
# Inside-out (no --deep): every nested Mach-O with the child/inherit
# entitlements, then the .app with the sandbox entitlements — ALL with the
# Apple Distribution identity. Same Team ID on every binary is what lets MAS
# library validation pass; MAS forbids `disable-library-validation`, so
# re-signing each nested binary with our own identity is the only path
# (and it's what the .dmg's sign_app.sh already proves works).
#
# Differences from the Developer-ID sign_app.sh:
#   - identity is "Apple Distribution: …" (not "Developer ID Application: …")
#   - the .app carries the SANDBOX entitlements (entitlements.mas.plist);
#     nested binaries inherit (entitlements.child.plist)
#   - NO --options runtime: MAS's security model is the App Sandbox, not the
#     Hardened Runtime (adding it re-introduces the library-validation friction
#     the entitlements set is built to avoid). Easy to add if validation asks.
#
# The provisioning profile must already be embedded at
# Contents/embedded.provisionprofile BEFORE this runs (the CI job does that) so
# the .app signature covers it.
#
# Requires:  APPLE_DIST_IDENTITY  e.g. "Apple Distribution: NAME (TEAMID)"
#            (its key in an unlocked keychain — CI imports it first)
# Usage:     bash packaging/macos/mas/sign_mas.sh dist/jellytoast.app
set -euo pipefail

APP="${1:?usage: sign_mas.sh <path-to.app>}"
IDENTITY="${APPLE_DIST_IDENTITY:?APPLE_DIST_IDENTITY not set}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENT_APP="${DIR}/entitlements.mas.plist"
ENT_CHILD="${DIR}/entitlements.child.plist"

[ -d "${APP}" ] || { echo "error: ${APP} is not a .app bundle" >&2; exit 1; }
if [ ! -f "${APP}/Contents/embedded.provisionprofile" ]; then
    echo "warn: no Contents/embedded.provisionprofile — MAS validation will reject this" >&2
fi

echo "Signing nested Mach-O (NO entitlements — Apple requires entitlements only on the main executable; ITMS-91166)…"
# Detect Mach-O by content, not name: Qt .framework binaries + the bundled
# CPython framework binary are extensionless. -print0/-d '' for spaces.
# NO --entitlements here: nested dylibs/.so run inside the host process's sandbox;
# entitlements belong ONLY on the main executable (applied via the .app sign below).
find "${APP}" -type f -print0 \
    | while IFS= read -r -d '' f; do
        case "$(file -b "${f}")" in
            *Mach-O*)
                codesign --force --timestamp --sign "${IDENTITY}" "${f}" ;;
        esac
    done

echo "Signing the .app bundle (sandbox entitlements)…"
codesign --force --timestamp \
    --entitlements "${ENT_APP}" --sign "${IDENTITY}" "${APP}"

echo "Verifying signature + sandbox entitlement…"
codesign --verify --strict --verbose=2 "${APP}"
if codesign -d --entitlements - "${APP}" 2>/dev/null | grep -q "app-sandbox"; then
    echo "  app-sandbox entitlement present ✓"
else
    echo "  WARNING: app-sandbox not found on the signed .app" >&2
fi
echo "Signed (Mac App Store): ${APP}"

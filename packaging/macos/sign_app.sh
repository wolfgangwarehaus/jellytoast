#!/usr/bin/env bash
# Code-sign a macOS .app for Developer-ID (notarized) distribution.
#
# Signs INSIDE-OUT — every nested Mach-O (dylibs / .so extension modules)
# before the outer .app — with the Hardened Runtime enabled and our
# entitlements. We deliberately do NOT use `codesign --deep`: it is unreliable
# for complex Python/Qt bundles (Apple's own guidance is "do not use --deep").
#
# Requires the signing identity in the environment:
#   APPLE_SIGNING_IDENTITY  e.g. "Developer ID Application: NAME (TEAMID)"
# The identity's private key must already be in an unlocked keychain (CI imports
# it into a temporary keychain first — see .github/workflows/release.yml).
#
# Usage: bash packaging/macos/sign_app.sh dist/jellytoast.app
set -euo pipefail

APP="${1:?usage: sign_app.sh <path-to.app>}"
IDENTITY="${APPLE_SIGNING_IDENTITY:?APPLE_SIGNING_IDENTITY not set}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTITLEMENTS="${SCRIPT_DIR}/entitlements.plist"

if [ ! -d "${APP}" ]; then
    echo "error: ${APP} is not a directory (.app bundle)." >&2
    exit 1
fi

echo "Signing nested binaries inside-out…"
# -print0 / -d '' so paths with spaces survive.
find "${APP}" -type f \( -name '*.dylib' -o -name '*.so' \) -print0 \
    | while IFS= read -r -d '' lib; do
        codesign --force --options runtime --timestamp \
            --entitlements "${ENTITLEMENTS}" --sign "${IDENTITY}" "${lib}"
    done

echo "Signing the .app bundle…"
codesign --force --options runtime --timestamp \
    --entitlements "${ENTITLEMENTS}" --sign "${IDENTITY}" "${APP}"

echo "Verifying signature…"
codesign --verify --strict --verbose=2 "${APP}"
echo "Signed and verified: ${APP}"

#!/usr/bin/env bash
# Notarize + staple a macOS artifact (.dmg / .app.zip) via notarytool.
#
# notarytool runs headless. Two credential modes (the API key is preferred for
# CI — scoped, individually revocable, not coupled to an Apple ID's 2FA):
#
#   App Store Connect API key:
#     APPLE_API_KEY_ID    the key's Key ID
#     APPLE_API_ISSUER    the issuer UUID
#     APPLE_API_KEY_PATH  path to the AuthKey_XXXXXX.p8 file
#
#   Apple ID + app-specific password:
#     APPLE_ID            the Apple ID email
#     APPLE_APP_PWD       an app-specific password (account has 2FA)
#     APPLE_TEAM_ID       the Developer Team ID
#
# Stapling attaches the ticket so Gatekeeper verifies OFFLINE on first launch.
#
# Usage: bash packaging/macos/notarize.sh dist/jellytoast-<ver>-macos.dmg
set -euo pipefail

TARGET="${1:?usage: notarize.sh <path-to.dmg-or-.zip>}"

if [ ! -e "${TARGET}" ]; then
    echo "error: ${TARGET} does not exist." >&2
    exit 1
fi

echo "Submitting ${TARGET} to the Apple notary service…"
if [ -n "${APPLE_API_KEY_ID:-}" ]; then
    : "${APPLE_API_ISSUER:?APPLE_API_ISSUER not set}"
    : "${APPLE_API_KEY_PATH:?APPLE_API_KEY_PATH not set (path to AuthKey .p8)}"
    xcrun notarytool submit "${TARGET}" \
        --key "${APPLE_API_KEY_PATH}" \
        --key-id "${APPLE_API_KEY_ID}" \
        --issuer "${APPLE_API_ISSUER}" \
        --wait
elif [ -n "${APPLE_ID:-}" ]; then
    : "${APPLE_APP_PWD:?APPLE_APP_PWD not set (app-specific password)}"
    : "${APPLE_TEAM_ID:?APPLE_TEAM_ID not set}"
    xcrun notarytool submit "${TARGET}" \
        --apple-id "${APPLE_ID}" \
        --password "${APPLE_APP_PWD}" \
        --team-id "${APPLE_TEAM_ID}" \
        --wait
else
    echo "error: no notarization credentials set (API key or Apple ID)." >&2
    exit 1
fi

echo "Stapling the notarization ticket…"
xcrun stapler staple "${TARGET}"
xcrun stapler validate "${TARGET}"
echo "Notarized and stapled: ${TARGET}"

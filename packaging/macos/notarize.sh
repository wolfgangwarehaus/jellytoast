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
# Usage: bash packaging/macos/notarize.sh <path-to.dmg-or-.zip> [staple-target]
#   staple-target: what to staple once the submission clears. Defaults to the
#   submitted file. Pass it when submitting a .app as a zip: notarytool only
#   accepts the ZIP, but a zip cannot carry a ticket — the staple must land on
#   the .app directory itself (0.2.0 QA: an un-stapled installed .app can't
#   prove notarization OFFLINE on first launch; only the dmg could).
set -euo pipefail

TARGET="${1:?usage: notarize.sh <path-to.dmg-or-.zip> [staple-target]}"
STAPLE_TARGET="${2:-${TARGET}}"

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

# Stapling fetches the ticket from Apple's CloudKit ticket service, and
# validating asks it again. Both go over the network AFTER notarization has
# already been accepted, and both failed on a dropped connection on the 0.2.4
# release ("The network connection was lost", stapler error 68), which sank
# the whole draft. Retry a few times with a growing pause before giving up.
retry_stapler() {
    local action="$1" attempt
    for attempt in 1 2 3 4 5; do
        if xcrun stapler "${action}" "${STAPLE_TARGET}"; then
            return 0
        fi
        [ "${attempt}" -lt 5 ] || break
        echo "stapler ${action} failed (attempt ${attempt}) — retrying in $((attempt * 15)) s…" >&2
        sleep $((attempt * 15))
    done
    echo "error: stapler ${action} kept failing" >&2
    return 1
}

echo "Stapling the notarization ticket…"
retry_stapler staple
retry_stapler validate
echo "Notarized and stapled: ${STAPLE_TARGET}"

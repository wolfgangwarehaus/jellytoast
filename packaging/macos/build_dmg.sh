#!/usr/bin/env bash
# Build a distributable .dmg from a (signed) .app using hdiutil — no extra
# dependencies. A symlink to /Applications gives the standard drag-to-install
# window. The .dmg is what gets notarized + stapled (see notarize.sh).
#
# Usage: bash packaging/macos/build_dmg.sh <version> <path-to.app> [arch]
#   arch (optional): arm64 | x86_64 — appended to the filename so the two
#   per-arch builds don't collide (jellytoast-<ver>-macos-arm64.dmg etc.).
#   Omitted → the legacy unsuffixed jellytoast-<ver>-macos.dmg.
set -euo pipefail

VERSION="${1:?usage: build_dmg.sh <version> <path-to.app> [arch]}"
APP="${2:?usage: build_dmg.sh <version> <path-to.app> [arch]}"
ARCH="${3:-}"

if [ ! -d "${APP}" ]; then
    echo "error: ${APP} is not a directory (.app bundle)." >&2
    exit 1
fi

OUT_DIR="$(cd "$(dirname "${APP}")" && pwd)"
SUFFIX=""
[ -n "${ARCH}" ] && SUFFIX="-${ARCH}"
DMG="${OUT_DIR}/jellytoast-${VERSION}-macos${SUFFIX}.dmg"
STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

cp -R "${APP}" "${STAGING}/"
ln -s /Applications "${STAGING}/Applications"

rm -f "${DMG}"
hdiutil create \
    -volname "jellytoast ${VERSION}" \
    -srcfolder "${STAGING}" \
    -ov -format UDZO \
    "${DMG}"

echo "Built ${DMG}"

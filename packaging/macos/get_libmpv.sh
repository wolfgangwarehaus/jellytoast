#!/usr/bin/env bash
# Stage libmpv for the macOS PyInstaller build.
#
# There is no system libmpv on a clean Mac, so the .app must bundle it (unlike
# the Linux .deb, which depends on the host's libmpv). We get it from Homebrew
# and copy the resolved dylib into packaging/macos/libmpv/, where
# packaging/pyinstaller/jellytoast.spec picks it up. PyInstaller's macholib
# analysis then follows the dylib's load commands and pulls libmpv's own
# dependency closure (FFmpeg, libass, libplacebo, …) into the bundle.
#
# Mirrors packaging/windows/get_libmpv.ps1. Run from the repo root (CI does),
# but it resolves its own paths so it works from anywhere.
#
# Usage: bash packaging/macos/get_libmpv.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${SCRIPT_DIR}/libmpv"

if ! command -v brew >/dev/null 2>&1; then
    echo "error: Homebrew (brew) not found — required to install libmpv." >&2
    exit 1
fi

echo "Installing mpv (provides libmpv) via Homebrew…"
brew install mpv

# Record the resolved bottle versions in the CI log. The universal merge
# (merge_universal.sh) requires BOTH arch legs to stage the same dylib
# closure — if the arm64 and x86_64 runner images ever resolve different
# mpv/ffmpeg bottles (an ffmpeg soname bump renames libavcodec.NN.dylib),
# the merge fails closed on a Mach-O set mismatch and THESE two log lines
# are how you diagnose which leg drifted.
brew list --versions mpv ffmpeg || true

# brew --prefix mpv gives the keg dir; the dylib lives under lib/.
MPV_PREFIX="$(brew --prefix mpv)"
LIBMPV="$(find "${MPV_PREFIX}/lib" -maxdepth 1 -name 'libmpv*.dylib' | sort | tail -n1)"

if [ -z "${LIBMPV}" ] || [ ! -f "${LIBMPV}" ]; then
    echo "error: could not locate libmpv*.dylib under ${MPV_PREFIX}/lib" >&2
    exit 1
fi

mkdir -p "${DEST_DIR}"
# Resolve the real file (Homebrew often symlinks libmpv.dylib -> libmpv.2.dylib);
# copy the concrete versioned dylib so the bundle is self-contained.
REAL_LIBMPV="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${LIBMPV}")"
cp -f "${REAL_LIBMPV}" "${DEST_DIR}/"

echo "Staged $(basename "${REAL_LIBMPV}") -> ${DEST_DIR}/"
ls -l "${DEST_DIR}"

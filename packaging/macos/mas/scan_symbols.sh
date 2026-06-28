#!/usr/bin/env bash
# Pre-submission non-public-API scan for the Mac App Store build.
#
# Apple's MAS review does a symbol/string scan and rejects bundles that
# reference private symbols or flagged strings. The three documented offenders
# for a PySide6 + CPython app (see packaging/macos/MAS_SESSION.md):
#   - Qt:     _responsibility_get_pid_responsible_for_pid / _responsibility_spawnattrs_setdisclaim
#             (fixed in PySide6/Qt >= 6.5; older needs -feature-appstore-compliant)
#   - CPython: the literal string "itms-services" in urllib/parse.py (Py 3.12+)
#             (fixed by building CPython with --with-app-store-compliance, 3.13+)
#   - liblzma/FFmpeg: linked _lzma_* symbols (strip / avoid linking)
#
# Run this on macOS over the built .app BEFORE every Transporter upload — a
# local nm/strings sweep mirrors Apple's check and saves slow rejection cycles.
# Runs on macOS only (needs nm/otool/strings that read Mach-O); the Linux CI
# lint gate only checks its syntax.
#
# Usage: bash packaging/macos/mas/scan_symbols.sh dist/jellytoast.app
# Exit:  0 = clean, 1 = flagged symbols/strings found (or bad usage)
set -uo pipefail

APP="${1:?usage: scan_symbols.sh <path-to.app>}"

if [ ! -d "${APP}" ]; then
    echo "error: ${APP} is not a directory (.app bundle)." >&2
    exit 1
fi

# Each pattern is an extended-regex matched against nm output / strings.
SYMBOL_PATTERNS='_responsibility_get_pid_responsible_for_pid|_responsibility_spawnattrs_setdisclaim|_lzma_'
STRING_PATTERNS='itms-services'

found=0

echo "Scanning Mach-O symbols under ${APP} …"
while IFS= read -r -d '' bin; do
    # Only inspect Mach-O files; `file` tags them "Mach-O".
    if file "${bin}" | grep -q 'Mach-O'; then
        if nm -u "${bin}" 2>/dev/null | grep -Eq "${SYMBOL_PATTERNS}"; then
            echo "  FLAGGED symbol in: ${bin}"
            nm -u "${bin}" 2>/dev/null | grep -E "${SYMBOL_PATTERNS}" | sed 's/^/    /'
            found=1
        fi
    fi
done < <(find "${APP}" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0)

echo "Scanning bundled stdlib for flagged strings …"
if grep -rEl "${STRING_PATTERNS}" "${APP}" 2>/dev/null | grep -q .; then
    echo "  FLAGGED string(s) '${STRING_PATTERNS}' found in:"
    grep -rEl "${STRING_PATTERNS}" "${APP}" 2>/dev/null | sed 's/^/    /'
    found=1
fi

if [ "${found}" -ne 0 ]; then
    echo "RESULT: non-public-API references found — fix before submitting (see MAS_SESSION.md)." >&2
    exit 1
fi
echo "RESULT: clean — no known non-public-API symbols/strings detected."

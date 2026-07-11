#!/usr/bin/env bash
# Fuse two native PyInstaller .app trees (arm64 + x86_64) into ONE universal
# .app via lipo. Strategy B for the 0.2.0 universal .dmg: instead of asking
# PyInstaller for a universal2 build (which aborts because numpy/cffi ship no
# universal2 wheels), we build each arch natively and merge the two bundles
# file-by-file here.
#
# Precedent: this is the same "walk the tree, lipo every Mach-O, copy the rest,
# keep symlinks as symlinks" algorithm Briefcase uses for its universal apps and
# that faaxm/lipomerge implements. We start from an exact copy of the arm64 tree
# (so the PyInstaller 6 Frameworks<->Resources symlink cross-linking and the Qt
# / Python .framework Versions symlinks are reproduced byte-for-byte) and then
# surgically replace each thin Mach-O in place with a fat one.
#
# IMPORTANT ordering: the merge does not produce a signed result — PyInstaller's
# ad-hoc/linker signatures must be replaced after the fuse and the bundle's
# outer seal cannot survive content changes. That is fine — this script does
# NOT sign. Sign the merged bundle ONCE afterward with
# packaging/macos/sign_app.sh, THEN build_dmg.sh, THEN notarize.sh. Until it is
# (re-)signed, macOS will refuse to exec the arm64 slice at all — even a smoke
# test needs at least an ad-hoc re-sign first.
#
# Bootloader/PKG nuance (why onedir merge works when onefile famously doesn't):
# every PyInstaller exe carries an appended PKG archive, lipo preserves each
# thin file VERBATIM as its slice (appended data included), and the bootloader
# scans the fat file back-to-front — so it only ever finds the LAST slice's
# PKG. PyInstaller's docs call the onefile merge broken for exactly this
# reason: there the PKG contains the arch-specific binaries. In onedir mode the
# PKG holds only options + PYZ bytecode — arch-independent — so either slice's
# PKG serves both arches, PROVIDED both legs were built from the same commit
# with the same dep versions (the Mach-O parity check below enforces the binary
# half of that). CI must still smoke-test BOTH slices of the merged bundle
# (natively + under Rosetta via `arch -x86_64`) to prove it end-to-end.
#
# Usage:
#   bash packaging/macos/merge_universal.sh <arm64.app> <x86_64.app> <out.app>
#
# Exits non-zero (and removes the half-built bundle) if the two trees disagree
# on which Mach-O files exist — a half-thin universal that silently drops one
# arch's slice is the exact failure this guards against; the boot-time smoke
# tests would miss a missing slice in a lazily-loaded plugin/dylib.
set -euo pipefail

ARM_APP="${1:?usage: merge_universal.sh <arm64.app> <x86_64.app> <out.app>}"
X86_APP="${2:?usage: merge_universal.sh <arm64.app> <x86_64.app> <out.app>}"
OUT_APP="${3:?usage: merge_universal.sh <arm64.app> <x86_64.app> <out.app>}"

for d in "$ARM_APP" "$X86_APP"; do
    [ -d "$d" ] || { echo "error: $d is not a directory (.app bundle)." >&2; exit 1; }
done

command -v lipo >/dev/null 2>&1 || { echo "error: lipo not found (need Xcode CLT)." >&2; exit 1; }

# is_macho <file> — true for a thin OR fat (universal) Mach-O. Detect by content,
# never by extension: Qt ships its libraries as .framework bundles whose binary
# is extensionless (QtCore.framework/Versions/A/QtCore), CPython's
# Python.framework binary is extensionless too, and the app's own bootloader
# (Contents/MacOS/jellytoast) has no extension. This matches sign_app.sh's
# `file -b | *Mach-O*` detection so the set of files we fuse == the set that
# gets signed.
is_macho() {
    case "$(file -b "$1" 2>/dev/null)" in
        *Mach-O*) return 0 ;;
        *) return 1 ;;
    esac
}

# Fresh copy of the arm64 tree as the skeleton. `cp -R` copies symlinks AS
# symlinks (POSIX; GNU and BSD cp both default to no-dereference under -R) — so
# the Frameworks<->Resources cross-links and every .framework Versions/Current
# symlink are preserved, not flattened. Flattening them would both bloat the
# bundle and break codesigning, which seals the symlink structure.
rm -rf "$OUT_APP"
mkdir -p "$(dirname "$OUT_APP")"
cp -R "$ARM_APP" "$OUT_APP"

echo "Merging x86_64 slices into $OUT_APP …"

fused=0
kept_universal=0
declare -a MISSING=()   # Mach-O in arm64 tree but absent from x86_64 tree
declare -a EXTRA=()     # Mach-O in x86_64 tree but absent from arm64 tree

# --- Pass 1: for every regular-file Mach-O in the (arm64-derived) output, fuse
# in the matching x86_64 slice. `-type f` excludes symlinks, so we only ever
# touch the real binary behind a framework's Versions/A/... path.
while IFS= read -r -d '' f; do
    is_macho "$f" || continue
    rel="${f#"$OUT_APP"/}"
    s="$X86_APP/$rel"

    if [ ! -e "$s" ]; then
        MISSING+=("$rel")
        continue
    fi

    # Byte-identical => a universal2 file that shipped in BOTH trees (some
    # wheels vend fat dylibs even on a thin install). Nothing to fuse.
    if cmp -s "$f" "$s"; then
        continue
    fi

    # Already fat in the arm64 tree (carries both arches)? Keep it — feeding an
    # overlapping arch to `lipo -create` would error ("have the same
    # architectures").
    archs_f="$(lipo -archs "$f" 2>/dev/null || true)"
    if [[ "$archs_f" == *arm64* && "$archs_f" == *x86_64* ]]; then
        kept_universal=$((kept_universal + 1))
        continue
    fi

    tmp="$(mktemp)"
    if ! lipo -create "$f" "$s" -output "$tmp" 2>"$tmp.err"; then
        echo "error: lipo failed to fuse '$rel':" >&2
        sed 's/^/    /' "$tmp.err" >&2 || true
        rm -f "$tmp" "$tmp.err"
        exit 1
    fi
    rm -f "$tmp.err"
    # Overwrite the file's CONTENT (not the path) so its permission bits survive
    # and every Resources->Frameworks cross-link symlink still resolves.
    chmod u+w "$f"
    cat "$tmp" > "$f"
    rm -f "$tmp"
    fused=$((fused + 1))
done < <(find "$OUT_APP" -type f -print0)

# --- Pass 2: x86_64-only path detection. A Mach-O present only in the x86_64
# tree never made it into the arm64-derived skeleton, so the universal bundle
# would lack it for BOTH arches — a hard defect, not a warning. Non-Mach-O
# extras (a data file or symlink only the Intel leg emitted) are dropped by
# construction (the arm64 tree is the skeleton); surface those as warnings so
# a data-file skew between the legs is at least visible in the CI log.
declare -a EXTRA_DATA=()   # non-Mach-O paths only in the x86_64 tree (warn)
while IFS= read -r -d '' s; do
    rel="${s#"$X86_APP"/}"
    if [ -e "$OUT_APP/$rel" ] || [ -L "$OUT_APP/$rel" ]; then
        continue
    fi
    if [ -f "$s" ] && [ ! -L "$s" ] && is_macho "$s"; then
        EXTRA+=("$rel")
    else
        EXTRA_DATA+=("$rel")
    fi
done < <(find "$X86_APP" \( -type f -o -type l \) -print0)

if [ "${#EXTRA_DATA[@]}" -gt 0 ]; then
    echo "warning: ${#EXTRA_DATA[@]} non-Mach-O path(s) exist only in the x86_64 tree" >&2
    echo "         (dropped — the arm64 tree is the skeleton). Check for dep-version skew:" >&2
    printf '    %s\n' "${EXTRA_DATA[@]}" >&2
fi

fail=0
if [ "${#MISSING[@]}" -gt 0 ]; then
    fail=1
    echo "error: ${#MISSING[@]} Mach-O file(s) exist in the arm64 tree but NOT in the x86_64 tree" >&2
    echo "       (the universal bundle would carry only the arm64 slice for these):" >&2
    printf '    %s\n' "${MISSING[@]}" >&2
fi
if [ "${#EXTRA[@]}" -gt 0 ]; then
    fail=1
    echo "error: ${#EXTRA[@]} Mach-O file(s) exist in the x86_64 tree but NOT in the arm64 tree" >&2
    echo "       (these would be missing from the universal bundle entirely):" >&2
    printf '    %s\n' "${EXTRA[@]}" >&2
fi
if [ "$fail" -ne 0 ]; then
    echo "error: the two arch trees disagree — refusing to ship a half-thin bundle." >&2
    echo "       Usual cause: the two runners resolved different dependency" >&2
    echo "       versions (e.g. a Homebrew ffmpeg soname bump, or an arch-only" >&2
    echo "       wheel). Pin the deps so both trees match, then re-run." >&2
    rm -rf "$OUT_APP"
    exit 1
fi

# Sanity: the main executable must actually be fat now.
MAIN="$OUT_APP/Contents/MacOS/$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$OUT_APP/Contents/Info.plist" 2>/dev/null || echo jellytoast)"
if [ -f "$MAIN" ]; then
    echo "Main executable arches: $(lipo -archs "$MAIN" 2>/dev/null || echo '?')"
    case "$(lipo -archs "$MAIN" 2>/dev/null)" in
        *arm64*x86_64*|*x86_64*arm64*) : ;;
        *) echo "error: $MAIN is not universal after merge." >&2; exit 1 ;;
    esac
fi

echo "Merged: fused $fused Mach-O file(s), kept $kept_universal already-universal file(s)."
echo "Universal (UNSIGNED) bundle at: $OUT_APP"
echo "Next: sign_app.sh -> build_dmg.sh -> notarize.sh (lipo stripped all signatures)."

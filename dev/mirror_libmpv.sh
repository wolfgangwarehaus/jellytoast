#!/usr/bin/env bash
# Re-pin the Windows libmpv builds in one command.
#
#   dev/mirror_libmpv.sh 20261018     # a shinchiro/mpv-winbuild-cmake release tag
#
# shinchiro keeps only its last ~30 daily releases, so a pin straight to
# upstream 404s about a month later (the 0.2.3 cut hit that). This copies the
# x86_64 + aarch64 dev archives of <tag> into our own
# wolfgangwarehaus/jellytoast-deps repo (release libmpv-windows-<tag>), after
# checking each against GitHub's published SHA-256 digest and checking the
# contained libmpv-2.dll is the right PE architecture, then rewrites $Url /
# $UpstreamUrl / $Sha256 in packaging/windows/get_libmpv{,_arm64}.ps1.
#
# Releases on jellytoast-deps trigger nothing in jellytoast (a release on the
# jellytoast repo itself would fire the PyPI / winget / AUR / Store fan-out).
#
# Needs: gh (authenticated as wolfgangwarehaus), curl, 7z, python3.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

TAG="${1:-}"
if ! printf '%s' "$TAG" | grep -Eq '^[0-9]{8}$'; then
  echo "usage: dev/mirror_libmpv.sh <shinchiro release tag, e.g. 20261018>" >&2
  exit 2
fi
UPSTREAM=shinchiro/mpv-winbuild-cmake
DEPS=wolfgangwarehaus/jellytoast-deps
REL="libmpv-windows-$TAG"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

declare -A NAME SHA
for arch in x86_64 aarch64; do
  line="$(gh api "repos/$UPSTREAM/releases/tags/$TAG" \
    --jq ".assets[] | select(.name | test(\"^mpv-dev-$arch-$TAG-git-[0-9a-f]+\\\\.7z\$\")) | .name + \" \" + .digest")"
  [ -n "$line" ] || { echo "error: no mpv-dev-$arch asset in $UPSTREAM@$TAG" >&2; exit 1; }
  NAME[$arch]="${line%% *}"
  SHA[$arch]="${line##*sha256:}"
  echo "→ $arch: ${NAME[$arch]}"
  curl -fsSL -o "$work/${NAME[$arch]}" \
    "https://github.com/$UPSTREAM/releases/download/$TAG/${NAME[$arch]}"
  echo "${SHA[$arch]}  $work/${NAME[$arch]}" | sha256sum -c - >/dev/null
  echo "  sha256 ok"
  mkdir -p "$work/$arch"
  7z e -y "$work/${NAME[$arch]}" libmpv-2.dll -o"$work/$arch" >/dev/null
  machine="$(python3 - "$work/$arch/libmpv-2.dll" <<'EOF'
import struct, sys
b = open(sys.argv[1], "rb").read(4096)
off = struct.unpack_from("<I", b, 0x3C)[0]
print(hex(struct.unpack_from("<H", b, off + 4)[0]))
EOF
)"
  want=$([ "$arch" = x86_64 ] && echo 0x8664 || echo 0xaa64)
  [ "$machine" = "$want" ] || { echo "error: $arch libmpv-2.dll is PE machine $machine, want $want" >&2; exit 1; }
  echo "  libmpv-2.dll is PE $machine"
done

notes="Unmodified copies of shinchiro/mpv-winbuild-cmake release $TAG (https://github.com/$UPSTREAM/releases/tag/$TAG), kept here because upstream prunes old releases. Pinned by SHA-256 in jellytoast's packaging/windows/get_libmpv*.ps1.

- ${NAME[x86_64]} — sha256 ${SHA[x86_64]}
- ${NAME[aarch64]} — sha256 ${SHA[aarch64]}

libmpv/FFmpeg are GPL in this build; source: https://github.com/$UPSTREAM and the projects it builds."
if gh release view "$REL" -R "$DEPS" >/dev/null 2>&1; then
  gh release upload "$REL" -R "$DEPS" --clobber "$work/${NAME[x86_64]}" "$work/${NAME[aarch64]}"
else
  gh release create "$REL" -R "$DEPS" --title "libmpv (Windows) $TAG" --notes "$notes" \
    "$work/${NAME[x86_64]}" "$work/${NAME[aarch64]}"
fi
echo "✓ mirrored to https://github.com/$DEPS/releases/tag/$REL"

repin() {  # file arch
  local f="$1" arch="$2"
  python3 - "$f" \
    "https://github.com/$DEPS/releases/download/$REL/${NAME[$arch]}" \
    "https://github.com/$UPSTREAM/releases/download/$TAG/${NAME[$arch]}" \
    "${SHA[$arch]}" <<'EOF'
import re, sys
path, url, upstream, sha = sys.argv[1:]
s = open(path).read()
for key, val in (("Url", url), ("UpstreamUrl", upstream), ("Sha256", sha)):
    s, n = re.subn(rf'^\${key} = "[^"]*"', f'${key} = "{val}"', s, flags=re.M)
    if n != 1:
        sys.exit(f"{path}: expected one ${key} line, found {n}")
open(path, "w").write(s)
EOF
  echo "✓ re-pinned $f"
}
repin packaging/windows/get_libmpv.ps1 x86_64
repin packaging/windows/get_libmpv_arm64.ps1 aarch64
echo "Review + commit: git diff packaging/windows/"

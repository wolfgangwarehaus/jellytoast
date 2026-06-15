#!/usr/bin/env bash
# Build the Ubuntu/Debian .deb from the PyInstaller onedir bundle.
#
# The bundle ships its own Python + PySide6 under /opt/jellytoast, so the
# package works on any apt distro new enough for the build runner's glibc
# (CI builds on ubuntu-22.04 → Ubuntu 22.04+/Debian 12+). libmpv stays a
# real Depends — python-mpv dlopens the SYSTEM libmpv.so.2 so the audio
# stack (libmpv→ffmpeg) is the distro's own, matching how the AUR package
# works on Arch.
#
# Usage (CI: .github/workflows/release.yml):
#   pyinstaller packaging/pyinstaller/jellytoast.spec --noconfirm
#   bash packaging/deb/build_deb.sh <version>
#
# Output: dist/jellytoast_<version>_amd64.deb

set -euo pipefail

VERSION="${1:?usage: build_deb.sh <version>}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUNDLE="$ROOT/dist/jellytoast"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

[ -d "$BUNDLE" ] || { echo "PyInstaller bundle missing at $BUNDLE — build the spec first" >&2; exit 1; }

APP_ID="io.github.wolfgangwarehaus.jellytoast"

# ── payload ───────────────────────────────────────────────────────────
mkdir -p "$STAGE/opt"
cp -a "$BUNDLE" "$STAGE/opt/jellytoast"

mkdir -p "$STAGE/usr/bin"
ln -s /opt/jellytoast/jellytoast "$STAGE/usr/bin/jellytoast"

mkdir -p "$STAGE/usr/share/applications"
# The repo .desktop's bare Exec=jellytoast resolves via the /usr/bin
# symlink, so it works unmodified.
install -m644 "$ROOT/packaging/$APP_ID.desktop" "$STAGE/usr/share/applications/"

mkdir -p "$STAGE/usr/share/metainfo"
install -m644 "$ROOT/packaging/$APP_ID.metainfo.xml" "$STAGE/usr/share/metainfo/"

mkdir -p "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps"
install -m644 "$ROOT/packaging/icons/jellytoast.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -m644 "$ROOT/packaging/icons/hicolor/256x256/apps/$APP_ID.png" \
    "$STAGE/usr/share/icons/hicolor/256x256/apps/"

mkdir -p "$STAGE/usr/share/doc/jellytoast"
install -m644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/jellytoast/copyright"
install -m644 "$ROOT/packaging/THIRD-PARTY-NOTICES.md" \
  "$STAGE/usr/share/doc/jellytoast/THIRD-PARTY-NOTICES.md"

# Debian changelog (Policy-mandated; lintian warns no-changelog without it).
{
  echo "jellytoast ($VERSION) unstable; urgency=low"
  echo ""
  echo "  * Release $VERSION — see https://github.com/wolfgangwarehaus/jellytoast/releases"
  echo ""
  echo " -- wolfgangwarehaus <augustvontrips@gmail.com>  $(date -R)"
} | gzip -9n > "$STAGE/usr/share/doc/jellytoast/changelog.Debian.gz"
chmod 644 "$STAGE/usr/share/doc/jellytoast/changelog.Debian.gz"

# ── control ───────────────────────────────────────────────────────────
INSTALLED_SIZE=$(du -ks "$STAGE" | cut -f1)
mkdir -p "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: jellytoast
Version: $VERSION
Architecture: amd64
Maintainer: wolfgangwarehaus <augustvontrips@gmail.com>
Installed-Size: $INSTALLED_SIZE
Depends: libmpv2 | libmpv1
Recommends: ffmpeg, libnotify4
Section: sound
Priority: optional
Homepage: https://github.com/wolfgangwarehaus/jellytoast
Description: Desktop music player for Jellyfin and Navidrome servers
 jellytoast is a desktop music player for Jellyfin, Navidrome and other
 Subsonic-compatible servers: bit-perfect playback via mpv,
 offline downloads, Chromecast/AirPlay/DLNA/Sonos/Snapcast casting,
 MPRIS media keys, scrobbling, smart playlists, and a frosted-glass UI.
 .
 Self-contained build: bundles Python and Qt under /opt/jellytoast and
 uses the system libmpv for audio.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
EOF
chmod 755 "$STAGE/DEBIAN/postinst"
cp "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# ── build ─────────────────────────────────────────────────────────────
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/jellytoast_${VERSION}_amd64.deb"
dpkg-deb --build --root-owner-group -Zxz "$STAGE" "$OUT"
echo "built: $OUT"

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
# The bundled Qt links several system libs that PyInstaller does NOT bundle and
# that aren't pulled in transitively by libmpv, so they're explicit Depends.
#
# ⚠️ INCOMPLETE — the xcb plugin's DT_NEEDED closure below still has a gap: the
# v0.1.2 release smoke test boots the xcb plugin under Xvfb and it ABORTS, so at
# least one more libxcb-*/X/xkbcommon lib is missing here. Enumerate the WHOLE
# closure (readelf -d) and declare it all — see packaging/deb/XCB_DEPS_WORKLIST.md.
#   libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1 — all hard DT_NEEDED of the
#     bundled Qt "xcb" platform plugin (libqxcb.so / libQt6XcbQpa.so.6). Without
#     ALL THREE, an X11/XWayland session aborts at startup ("could not load the
#     Qt platform plugin xcb"). libxcb-cursor0 alone (Qt 6.5+'s documented need)
#     is NOT enough — icccm4/keysyms1 are equally hard-linked and aren't deps of
#     cursor0. (Wayland sessions use the bundled wayland plugin and need none of
#     these, but we can't know the session at install time.)
#   libgl1 — hard DT_NEEDED of the bundled libQt6Gui.so.6 (every session); not
#     bundled and not pulled by libmpv2 (which depends on glvnd libegl1 only,
#     and EGL doesn't drag in GL). Without it the app aborts at Qt startup.
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

# Reproducible build: pin every timestamp to the commit so rebuilds are
# byte-identical. dpkg-deb clamps the ar timestamp + all tar mtimes to
# SOURCE_DATE_EPOCH; PyInstaller/install mtimes are always >= the commit time so
# they collapse to it. Falls back to "now" outside a git checkout.
: "${SOURCE_DATE_EPOCH:=$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}"
export SOURCE_DATE_EPOCH
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
# DEP-5 machine-readable copyright. (Shipping the raw GPL text as `copyright`
# trips lintian copyright-without-copyright-notice; Policy also wants the license
# referenced from /usr/share/common-licenses, which Debian/Ubuntu always ship.)
cat > "$STAGE/usr/share/doc/jellytoast/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: jellytoast
Source: https://github.com/wolfgangwarehaus/jellytoast

Files: *
Copyright: 2026 august <augustvontrips@gmail.com>
License: GPL-2.0-or-later
 This program is free software: you can redistribute it and/or modify it under
 the terms of the GNU General Public License as published by the Free Software
 Foundation, either version 2 of the License, or (at your option) any later
 version.
 .
 This program is distributed in the hope that it will be useful, but WITHOUT ANY
 WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
 PARTICULAR PURPOSE.  See the GNU General Public License for more details.
 .
 On Debian systems, the complete text of the GNU General Public License version
 2 can be found in /usr/share/common-licenses/GPL-2.
EOF
chmod 644 "$STAGE/usr/share/doc/jellytoast/copyright"
install -m644 "$ROOT/packaging/THIRD-PARTY-NOTICES.md" \
  "$STAGE/usr/share/doc/jellytoast/THIRD-PARTY-NOTICES.md"

# Debian changelog (Policy-mandated; lintian warns no-changelog without it).
{
  echo "jellytoast ($VERSION) unstable; urgency=low"
  echo ""
  echo "  * Release $VERSION — see https://github.com/wolfgangwarehaus/jellytoast/releases"
  echo ""
  echo " -- wolfgangwarehaus <augustvontrips@gmail.com>  $(date -u -R -d "@$SOURCE_DATE_EPOCH")"
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
Depends: libmpv2 | libmpv1, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libgl1
Recommends: ffmpeg, libnotify-bin
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

# Maintainer scripts must act per dpkg argument (Policy 6.5): refresh the icon
# cache + desktop database on install (configure) and on removal (remove|purge),
# not on every phase (upgrade/abort/triggered).
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
case "$1" in
  configure)
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
    ;;
esac
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
  remove|purge)
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
    ;;
esac
EOF
chmod 755 "$STAGE/DEBIAN/postrm"

# ── build ─────────────────────────────────────────────────────────────
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/jellytoast_${VERSION}_amd64.deb"

# Normalize modes so the package doesn't inherit the build host's umask.
# `cp -a` of the bundle (above) preserves its source modes, and dpkg-deb
# --root-owner-group fixes ownership but NOT modes — so a maintainer building
# under a group-writable umask (Ubuntu's default 0002) would otherwise ship
# 0775 dirs / 0664 files (lintian non-standard-dir-perm + non-reproducible).
# Dirs → 0755; files → drop group/other write but KEEP the exec bit (the bundle
# has 200+ executable .so/.bin files), so use `go-w` not a blanket 0644.
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod go-w {} +

dpkg-deb --build --root-owner-group -Zxz "$STAGE" "$OUT"
echo "built: $OUT"

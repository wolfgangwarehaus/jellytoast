#!/usr/bin/env bash
# Build a self-contained jellytoast AppImage from the PyInstaller onedir bundle.
#
# Unlike the .deb (which uses the SYSTEM libmpv via `Depends: libmpv2 | libmpv1`),
# the AppImage must carry its own libmpv + FFmpeg closure + libxcb-cursor0 so it
# runs on any glibc-compatible distro with no install and no root.
#
# Build on the OLDEST supported base — Ubuntu 20.04 (glibc 2.31, the floor the
# PySide6 manylinux wheels require) — so the result runs FORWARD onto every newer
# distro. AppImage gives forward compatibility only: built against glibc X runs on
# >= X, never older. glibc, libGL/glvnd and the X/Wayland client libs stay on the
# host per the AppImage excludelist (bundling driver-coupled GL libs is a classic
# "works on the builder, crashes elsewhere").
#
# The spec is run UNCHANGED: it strips libmpv's host-provided closure from the
# PyInstaller bundle (correct for the .deb AND here), and this script then
# vendors a fresh libmpv + FFmpeg closure into usr/lib, which AppRun puts first
# on LD_LIBRARY_PATH. Vendoring explicitly is more predictable than relying on
# PyInstaller's view of a library python-mpv dlopens at runtime.
#
# Usage (from the repo root):
#   pyinstaller packaging/pyinstaller/jellytoast.spec --noconfirm
#   bash packaging/appimage/build_appimage.sh <version>
#
# Output: dist/jellytoast-<version>-x86_64.AppImage  (+ .AppImage.zsync)
set -euo pipefail

VERSION="${1:?usage: build_appimage.sh <version>}"
ROOT="$(git rev-parse --show-toplevel)"
APP_ID="io.github.wolfgangwarehaus.jellytoast"
BUNDLE="$ROOT/dist/jellytoast"
APPDIR="$ROOT/dist/jellytoast.AppDir"

[ -x "$BUNDLE/jellytoast" ] || {
  echo "error: $BUNDLE/jellytoast missing — run pyinstaller first." >&2
  exit 1
}

# ── Stage the AppDir: the frozen onedir goes under usr/bin ────────────────────
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" \
         "$APPDIR/usr/share/metainfo" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$BUNDLE/." "$APPDIR/usr/bin/"   # jellytoast binary + _internal/

# ── Vendor the system shared-lib closure the frozen app + Qt plugins need ─────
# PyInstaller's onedir bundles Python + Qt under usr/bin/_internal, but leaves a
# set of system libs to the host: libmpv + its FFmpeg closure, and the Qt xcb
# platform plugin's libxcb-cursor/icccm/util + fontconfig/freetype closure (the
# same set the .deb declares as Depends). The .deb gets these from the host; the
# AppImage can't, so vendor them into usr/lib. EXCLUDE the AppImage excludelist —
# glibc, the C++ runtime, GL/glvnd, base X/xcb, wayland — those MUST come from the
# user's host so the GL stack matches their driver and forward-compat holds.
# NB: no early `awk exit` — that closes the pipe while ldconfig is still writing
# and SIGPIPEs (141) under `set -o pipefail` on systems with a large ldconfig
# cache (any real desktop; a minimal CI container is small enough to dodge it).
# Read all input and print the first match.
resolve_so() { ldconfig -p | awk -v n="$1" '$1==n && !f {print $NF; f=1}'; }

is_excluded() {
  case "$1" in
    libc.so.*|libm.so.*|libdl.so.*|libpthread.so.*|librt.so.*|libresolv.so.*|\
    ld-linux*.so.*|libstdc++.so.*|libgcc_s.so.*|\
    libGL.so.*|libEGL.so.*|libGLdispatch.so.*|libGLX.so.*|libOpenGL.so.*|libdrm.so.*|\
    libX11.so.*|libX11-xcb.so.*|libxcb.so.*|libwayland-*.so.*) return 0 ;;
    *) return 1 ;;
  esac
}

# ldd a binary (with _internal on the path so the bundled Qt libs resolve and ldd
# recurses into THEIR deps) and copy each system-resolved dep that is neither
# excludelisted nor already inside the bundle.
vendor_closure() {
  # Key off the RESOLVED path ($3) and its basename, not ldd's left column ($1):
  # for the dynamic loader (and on some distros) $1 is an absolute path, which
  # would otherwise corrupt the dest path. The basename is the real soname.
  LD_LIBRARY_PATH="$APPDIR/usr/bin/_internal" ldd "$1" 2>/dev/null \
    | awk '/=> \// {print $3}' | while read -r path; do
    [ -e "$path" ] || continue
    case "$path" in "$APPDIR"/*) continue ;; esac   # already bundled in _internal
    bn="$(basename "$path")"
    is_excluded "$bn" && continue
    [ -e "$APPDIR/usr/lib/$bn" ] && continue
    cp -Ln "$path" "$APPDIR/usr/lib/$bn" && echo "  + $bn"
  done
}

# 1. libmpv + its FFmpeg closure. Soname varies by distro: Ubuntu 22.04 ships
#    libmpv.so.1 (libmpv1), newer distros libmpv.so.2 (libmpv2).
MPV_SONAME=""
for _cand in libmpv.so.2 libmpv.so.1; do
  if [ -n "$(resolve_so "$_cand")" ]; then MPV_SONAME="$_cand"; break; fi
done
[ -n "$MPV_SONAME" ] || {
  echo "error: no libmpv.so.{2,1} on the build host — 'apt-get install libmpv-dev'." >&2
  exit 1
}
MPV_PATH="$(resolve_so "$MPV_SONAME")"
echo "Vendoring $MPV_SONAME + the FFmpeg closure:"
cp -Ln "$MPV_PATH" "$APPDIR/usr/lib/$MPV_SONAME" && echo "  + $MPV_SONAME"
vendor_closure "$MPV_PATH"

# 2. The Qt xcb platform plugin's closure — libxcb-cursor0 (Qt 6.5+ hard-needs it,
#    the #1 "could not load the Qt platform plugin xcb" failure) + the xcb-util /
#    fontconfig / freetype libs PyInstaller leaves to the host.
echo "Vendoring the Qt xcb platform-plugin closure:"
# -print -quit stops at the first match cleanly (no `| head` SIGPIPE).
XCB_PLUGIN="$(find "$APPDIR/usr/bin/_internal" -name 'libqxcb.so' -print -quit 2>/dev/null)"
if [ -n "$XCB_PLUGIN" ]; then
  vendor_closure "$XCB_PLUGIN"
else
  echo "  ! libqxcb.so not found in the bundle — the AppImage may not start on X11" >&2
fi

# 3. The frozen launcher itself, to catch anything else it pulls from the host.
vendor_closure "$APPDIR/usr/bin/jellytoast"

# ── AppRun: bundled libs on the loader path, then exec the frozen app ─────────
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
# usr/lib = vendored libmpv + ffmpeg; usr/bin/_internal = PyInstaller's Qt/runtime.
export LD_LIBRARY_PATH="$HERE/usr/lib:$HERE/usr/bin/_internal${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$HERE/usr/bin/jellytoast" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# ── Desktop entry + icon + metainfo (appimagetool reads the AppDir-root pair) ──
cp "$ROOT/packaging/$APP_ID.desktop"      "$APPDIR/$APP_ID.desktop"
cp "$ROOT/packaging/$APP_ID.metainfo.xml" "$APPDIR/usr/share/metainfo/"
ICON="$ROOT/packaging/icons/hicolor/256x256/apps/$APP_ID.png"
cp "$ICON" "$APPDIR/$APP_ID.png"          # root icon (matches Icon= key)
cp "$ICON" "$APPDIR/.DirIcon"             # thumbnailer icon
cp "$ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/"

# ── appimagetool: static type-2 runtime → the AppImage needs NO libfuse on the
#    user's machine. appimagetool itself needs no FUSE on CI with EXTRACT_AND_RUN.
TOOL="${APPIMAGETOOL:-$ROOT/dist/appimagetool}"
if [ ! -x "$TOOL" ]; then
  echo "Fetching appimagetool (continuous, static runtime)…"
  curl -fsSL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL"
fi

OUT="$ROOT/dist/jellytoast-${VERSION}-x86_64.AppImage"
export APPIMAGE_EXTRACT_AND_RUN=1
# -u embeds gh-releases zsync update-info so AppImageUpdate can do delta updates;
# appimagetool also emits the companion .AppImage.zsync next to the output.
ARCH=x86_64 "$TOOL" \
  --updateinformation "gh-releases-zsync|wolfgangwarehaus|jellytoast|latest|jellytoast-*-x86_64.AppImage.zsync" \
  "$APPDIR" "$OUT"

echo "✓ built $OUT"
ls -lh "$OUT" "${OUT}.zsync" 2>/dev/null || true

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

# ── Vendor libmpv + its FFmpeg closure ───────────────────────────────────────
# The .deb gets these from the host; the AppImage can't, so copy libmpv.so.2 and
# every shared lib it pulls EXCEPT the excludelist (glibc / GL / X-Wayland client
# libs / the C++ runtime — those must come from the host so the GL stack matches
# the user's driver and forward-compat holds).
resolve_so() { ldconfig -p | awk -v n="$1" '$1==n {print $NF; exit}'; }
copy_so() {
  local src; src="$(resolve_so "$1")"
  if [ -n "$src" ] && [ -e "$src" ]; then
    cp -Ln "$src" "$APPDIR/usr/lib/" && echo "  + $1"
  else
    echo "  ! $1 not found on the build host" >&2
  fi
}

MPV_PATH="$(resolve_so libmpv.so.2)"
[ -n "$MPV_PATH" ] || {
  echo "error: libmpv.so.2 not on the build host — 'apt-get install libmpv-dev'." >&2
  exit 1
}
echo "Vendoring libmpv + closure into the AppDir:"
cp -Ln "$MPV_PATH" "$APPDIR/usr/lib/" && echo "  + libmpv.so.2"
ldd "$MPV_PATH" | awk '/=> \// {print $1}' | while read -r so; do
  case "$so" in
    # Host-provided (AppImage excludelist + forward-compat runtimes):
    libc.so.*|libm.so.*|libdl.so.*|libpthread.so.*|librt.so.*|libresolv.so.*|\
    ld-linux*.so.*|libstdc++.so.*|libgcc_s.so.*|\
    libGL.so.*|libEGL.so.*|libGLdispatch.so.*|libGLX.so.*|libOpenGL.so.*|libdrm.so.*|\
    libX11.so.*|libX11-xcb.so.*|libxcb.so.*|libwayland-*.so.*|libxkbcommon*.so.*) ;;
    *) copy_so "$so" ;;
  esac
done

# Qt 6.5+ hard-requires libxcb-cursor0 to load the xcb platform plugin, and
# PyInstaller bundles the plugin but NOT this lib — the #1 "could not load the Qt
# platform plugin xcb" failure. Vendor it explicitly.
copy_so libxcb-cursor.so.0

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

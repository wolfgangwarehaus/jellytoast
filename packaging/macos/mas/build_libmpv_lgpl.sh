#!/bin/bash
# Build LGPL-only, no-Lua libmpv from source for the Mac App Store track.
# FFmpeg --disable-gpl --disable-nonfree (audio-only player loses nothing) +
# libmpv -Dgpl=false -Dlua=disabled (drops the LuaJIT JIT entitlement).
PREFIX="${LGPL_PREFIX:-/tmp/lgpl}"
SRC="${LGPL_SRC:-/tmp/lgpl_src}"
LOG="${JT_LOG:-/tmp/lgpl_build.log}"
# CI sets JT_LOG_STDOUT=1 so the build is visible in the workflow log.
[ -n "${JT_LOG_STDOUT:-}" ] || exec > "$LOG" 2>&1
set -x
fail() { set +x; echo "LGPL_BUILD_FAIL: $1"; exit 1; }
eval "$(/opt/homebrew/bin/brew shellenv)"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1
NCPU=$(sysctl -n hw.ncpu)

echo "=== STEP 1: build tools + LGPL deps (libass/libplacebo/freetype/etc are already LGPL) ==="
brew install meson ninja nasm pkg-config libass libplacebo little-cms2 2>&1 | tail -2 || fail "brew deps"

mkdir -p "$PREFIX" "$SRC" || fail mkdir
cd "$SRC" || fail cd

echo "=== STEP 2: LGPL FFmpeg from source (n7.1) ==="
[ -d FFmpeg ] || git clone --depth 1 --branch n7.1 https://github.com/FFmpeg/FFmpeg.git FFmpeg || fail "ffmpeg clone"
cd FFmpeg || fail
# --enable-securetransport: the TLS backend. Without one, FFmpeg silently
# builds WITHOUT the https protocol — every TLS media URL fails as `loading
# failed` while plain http plays (found by the 0.2.0 Steam Deck flatpak QA;
# THIS script had the identical hole, i.e. MAS playback against an https
# server was silently broken since 0.1.4). SecureTransport is Apple's system
# TLS — no new deps, sandbox-clean, LGPL-compatible.
./configure --prefix="$PREFIX" \
  --enable-shared --disable-static \
  --disable-gpl --disable-nonfree \
  --disable-programs --disable-doc --disable-postproc \
  --disable-avdevice --disable-encoders --disable-muxers \
  --enable-audiotoolbox --enable-securetransport || fail "ffmpeg configure"
make -j"$NCPU" || fail "ffmpeg make"
make install || fail "ffmpeg install"
cd "$SRC" || fail

echo "=== STEP 3: LGPL libmpv from source (no Lua, no GPL) ==="
# PINNED to a release tag (was an unpinned master clone — every MAS build
# shipped whatever mpv master was that day; one bad upstream day = a broken
# App Store build, and no two builds were reproducible). v0.41.0 matches the
# generation the shinchiro Windows builds ship. Bump deliberately, with a
# validate run — deps-watch.yml flags when upstream has a newer release.
MPV_TAG="v0.41.0"
[ -d mpv ] || git clone --depth 1 --branch "$MPV_TAG" https://github.com/mpv-player/mpv.git mpv || fail "mpv clone"
cd mpv || fail
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:/opt/homebrew/lib/pkgconfig"
rm -rf build
meson setup build --prefix="$PREFIX" \
  -Dgpl=false -Dlibmpv=true -Dcplayer=false \
  -Dlua=disabled -Djavascript=disabled -Dvapoursynth=disabled \
  -Ddefault_library=shared || fail "mpv meson setup"
meson compile -C build || fail "mpv compile"
meson install -C build || fail "mpv install"
cd "$SRC" || fail

echo "=== STEP 4: VERIFY LGPL + no-Lua ==="
echo "--- meson config (want gpl=false, lua=disabled) ---"
meson configure "$SRC/mpv/build" 2>/dev/null | grep -iE "^\s*(gpl|lua)\b" | head
LIBMPV=$(find "$PREFIX/lib" -name 'libmpv*.dylib' 2>/dev/null | head -1)
echo "libmpv: $LIBMPV"
echo "--- GPL/Lua contamination check (otool -L) ---"
if otool -L "$LIBMPV" 2>&1 | grep -iE "x264|x265|postproc|/liblua|luajit|/libmpv.*gpl"; then
  echo ">>> CONTAMINATION FOUND"
else
  echo ">>> CLEAN: no x264/x265/postproc/lua in libmpv ✓"
fi
echo "--- full dep tree ---"
otool -L "$LIBMPV" 2>&1 | sed -n '1,40p'

set +x
echo "LGPL_BUILD_DONE"

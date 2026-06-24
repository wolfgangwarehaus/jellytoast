#!/bin/bash
# Build LGPL-only, no-Lua libmpv from source for the Mac App Store track.
# FFmpeg --disable-gpl --disable-nonfree (audio-only player loses nothing) +
# libmpv -Dgpl=false -Dlua=disabled (drops the LuaJIT JIT entitlement).
PREFIX=/tmp/lgpl
SRC=/tmp/lgpl_src
LOG=/tmp/lgpl_build.log
exec > "$LOG" 2>&1
set -x
fail() { set +x; echo "LGPL_BUILD_FAIL: $1"; exit 1; }
eval "$(/opt/homebrew/bin/brew shellenv)"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1
NCPU=$(sysctl -n hw.ncpu)

echo "=== STEP 1: build tools + LGPL deps (libass/libplacebo/freetype/etc are already LGPL) ==="
brew install meson ninja nasm pkg-config libass libplacebo little-cms2 2>&1 | tail -2 || fail "brew deps"

mkdir -p "$PREFIX" "$SRC" && cd "$SRC" || fail cd

echo "=== STEP 2: LGPL FFmpeg from source (n7.1) ==="
[ -d FFmpeg ] || git clone --depth 1 --branch n7.1 https://github.com/FFmpeg/FFmpeg.git FFmpeg || fail "ffmpeg clone"
cd FFmpeg || fail
./configure --prefix="$PREFIX" \
  --enable-shared --disable-static \
  --disable-gpl --disable-nonfree \
  --disable-programs --disable-doc --disable-postproc \
  --disable-avdevice --disable-encoders --disable-muxers \
  --enable-audiotoolbox || fail "ffmpeg configure"
make -j"$NCPU" || fail "ffmpeg make"
make install || fail "ffmpeg install"
cd "$SRC" || fail

echo "=== STEP 3: LGPL libmpv from source (no Lua, no GPL) ==="
[ -d mpv ] || git clone --depth 1 https://github.com/mpv-player/mpv.git mpv || fail "mpv clone"
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
LIBMPV=$(ls "$PREFIX"/lib/libmpv*.dylib 2>/dev/null | head -1)
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

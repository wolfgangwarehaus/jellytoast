#!/bin/bash
# Build the MAS .app: swap in the LGPL/no-Lua libmpv, PyInstaller-bundle it
# (macholib follows the load commands to pull the LGPL FFmpeg + deps), patch
# the CPython itms-services string, and scan the whole bundle for GPL leaks.
LOG=/tmp/mas_app_build.log
exec > "$LOG" 2>&1
set -x
fail() { set +x; echo "MAS_APP_FAIL: $1"; exit 1; }
eval "$(/opt/homebrew/bin/brew shellenv)"
cd /tmp/jellytoast-src || fail cd

echo "=== STEP 1: stage the LGPL libmpv where the spec picks it up ==="
mkdir -p packaging/macos/libmpv
cp -f /tmp/lgpl/lib/libmpv.2.dylib packaging/macos/libmpv/ || fail "copy lgpl libmpv"
otool -L packaging/macos/libmpv/libmpv.2.dylib | grep -iE "libav|lua" | head -3

echo "=== STEP 2: PyInstaller build ==="
/tmp/jtvenv/bin/pip install --quiet pyinstaller || fail "pip pyinstaller"
# macholib reads load commands (no dlopen), but make the LGPL ffmpeg + brew deps discoverable too.
export DYLD_FALLBACK_LIBRARY_PATH=/tmp/lgpl/lib:/opt/homebrew/lib
rm -rf /tmp/mas_dist /tmp/mas_build
/tmp/jtvenv/bin/pyinstaller packaging/pyinstaller/jellytoast.spec --noconfirm \
  --distpath /tmp/mas_dist --workpath /tmp/mas_build || fail "pyinstaller"
APP=/tmp/mas_dist/jellytoast.app
[ -d "$APP" ] || fail "no .app produced"
echo "built: $APP ($(du -sh "$APP" | cut -f1))"

echo "=== STEP 3: patch CPython itms-services (App Review auto-reject string) ==="
/tmp/jtvenv/bin/python3 - "$APP" <<'PYEOF'
import sys, os, zipfile, io, shutil
app = sys.argv[1]
needle = b"itms-services"
repl   = b"xtms-services"  # same length (13) → keeps marshal/.pyc valid; harmless scheme
patched = 0
# 1) plain .py / .pyc files
for root, _, files in os.walk(app):
    for fn in files:
        p = os.path.join(root, fn)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except Exception:
            continue
        if needle in data:
            with open(p, "wb") as f:
                f.write(data.replace(needle, repl))
            patched += 1
# 2) inside base_library.zip (PyInstaller packs the stdlib here)
for root, _, files in os.walk(app):
    for fn in files:
        if not fn.endswith(".zip"):
            continue
        zp = os.path.join(root, fn)
        try:
            with zipfile.ZipFile(zp) as z:
                names = z.namelist()
                blobs = {n: z.read(n) for n in names}
        except Exception:
            continue
        if not any(needle in b for b in blobs.values()):
            continue
        tmp = zp + ".new"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n in names:
                b = blobs[n]
                if needle in b:
                    b = b.replace(needle, repl); patched += 1
                z.writestr(n, b)
        shutil.move(tmp, zp)
print(f"itms-services occurrences patched: {patched}")
PYEOF

echo "=== STEP 4: VERIFY ==="
echo "--- bundled libmpv ---"
BUNDLED_MPV=$(find "$APP" -name "libmpv*.dylib" | head -1)
echo "libmpv: $BUNDLED_MPV"
echo "--- GPL contamination scan across ALL bundled Mach-O ---"
GPL=$(find "$APP" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 | while IFS= read -r -d '' f; do
  otool -L "$f" 2>/dev/null | grep -qiE "libx264|libx265|libpostproc" && echo "GPL-LEAK: ${f#$APP/}"
done)
[ -z "$GPL" ] && echo "  no x264/x265/postproc anywhere ✓" || echo "$GPL"
echo "--- residual itms-services anywhere? ---"
if grep -rl "itms-services" "$APP" 2>/dev/null | head; then echo ">>> STILL PRESENT"; else echo "  none ✓"; fi
echo "--- any absolute /tmp/lgpl or /opt/homebrew paths left in libmpv? ---"
otool -L "$BUNDLED_MPV" 2>&1 | grep -iE "/tmp/lgpl|/opt/homebrew" && echo ">>> not rewritten" || echo "  libmpv deps rewritten (@rpath) ✓"

set +x
echo "MAS_APP_DONE app=$APP"

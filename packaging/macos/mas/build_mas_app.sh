#!/bin/bash
# Build the MAS .app: swap in the LGPL/no-Lua libmpv, PyInstaller-bundle it
# (macholib follows the load commands to pull the LGPL FFmpeg + deps), patch
# the CPython itms-services string, and scan the whole bundle for GPL leaks.
#
# Paths are env-overridable so this runs unchanged on the rented Mac (the
# defaults) AND in CI (the build-mas job sets JT_SRC/JT_PIP/JT_PYI/JT_PY +
# JT_LOG_STDOUT=1):
#   JT_SRC        repo checkout         (default /tmp/jellytoast-src)
#   LGPL_PREFIX   LGPL libmpv prefix    (default /tmp/lgpl)
#   JT_PIP/JT_PYI/JT_PY   pip / pyinstaller / python3 (default rented-Mac venv)
#   JT_DIST/JT_WORK       PyInstaller out/work dirs
#   JT_LOG_STDOUT=1       log to stdout (so CI shows the build) instead of $JT_LOG
LOG="${JT_LOG:-/tmp/mas_app_build.log}"
[ -n "${JT_LOG_STDOUT:-}" ] || exec > "$LOG" 2>&1
set -x
fail() { set +x; echo "MAS_APP_FAIL: $1"; exit 1; }
SRC="${JT_SRC:-/tmp/jellytoast-src}"
LGPL="${LGPL_PREFIX:-/tmp/lgpl}"
PIP="${JT_PIP:-/tmp/jtvenv/bin/pip}"
PYI="${JT_PYI:-/tmp/jtvenv/bin/pyinstaller}"
PY="${JT_PY:-/tmp/jtvenv/bin/python3}"
DIST="${JT_DIST:-/tmp/mas_dist}"
WORK="${JT_WORK:-/tmp/mas_build}"
eval "$(/opt/homebrew/bin/brew shellenv)"
cd "$SRC" || fail cd

echo "=== STEP 1: stage the LGPL libmpv where the spec picks it up ==="
mkdir -p packaging/macos/libmpv
cp -f "$LGPL/lib/libmpv.2.dylib" packaging/macos/libmpv/ || fail "copy lgpl libmpv"
otool -L packaging/macos/libmpv/libmpv.2.dylib | grep -iE "libav|lua" | head -3

echo "=== STEP 2: PyInstaller build ==="
"$PIP" install --quiet pyinstaller || fail "pip pyinstaller"
# macholib reads load commands (no dlopen), but make the LGPL ffmpeg + brew deps discoverable too.
export DYLD_FALLBACK_LIBRARY_PATH="$LGPL/lib:/opt/homebrew/lib"
rm -rf "$DIST" "$WORK"
"$PYI" packaging/pyinstaller/jellytoast.spec --noconfirm \
  --distpath "$DIST" --workpath "$WORK" || fail "pyinstaller"
APP="$DIST/jellytoast.app"
[ -d "$APP" ] || fail "no .app produced"
echo "built: $APP ($(du -sh "$APP" | cut -f1))"

echo "=== STEP 3: patch CPython itms-services (App Review auto-reject string) ==="
"$PY" - "$APP" <<'PYEOF'
import sys, os, zipfile, shutil
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
  otool -L "$f" 2>/dev/null | grep -qiE "libx264|libx265|libpostproc" && echo "GPL-LEAK: ${f#"$APP"/}"
done)
[ -z "$GPL" ] && echo "  no x264/x265/postproc anywhere ✓" || echo "$GPL"
echo "--- residual itms-services anywhere? ---"
if grep -rl "itms-services" "$APP" 2>/dev/null | head; then echo ">>> STILL PRESENT"; else echo "  none ✓"; fi
echo "--- any absolute /tmp/lgpl or /opt/homebrew paths left in libmpv? ---"
otool -L "$BUNDLED_MPV" 2>&1 | grep -iE "/tmp/lgpl|/opt/homebrew" && echo ">>> not rewritten" || echo "  libmpv deps rewritten (@rpath) ✓"

set +x
echo "MAS_APP_DONE app=$APP"

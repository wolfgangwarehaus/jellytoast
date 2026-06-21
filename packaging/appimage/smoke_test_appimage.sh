#!/usr/bin/env bash
# Smoke-test the built AppImage inside a CLEAN container that has NEITHER libmpv
# NOR the Qt xcb libs installed — the only way to prove the AppImage truly
# self-contains them (the exact trap that shipped a broken .deb twice; see
# packaging/deb/XCB_DEPS_WORKLIST.md). If libmpv weren't bundled, `import mpv`
# fails -> MPV_AVAILABLE False -> the app shows "Missing dependency" and exits
# rc=1; a missing xcb lib aborts the xcb plugin. A clean boot to the timeout
# (rc=124) proves the closure is self-contained.
#
# Run in CI across distros NEWER than the build base:
#   for img in ubuntu:24.04 debian:stable; do
#     docker run --rm -v "$PWD:/src:ro" "$img" bash /src/packaging/appimage/smoke_test_appimage.sh
#   done
set -euo pipefail

apt-get update -qq
# A real X server + the GL baseline (libgl1/libegl1). GL/EGL are driver-coupled
# and on the AppImage excludelist, so they come from the host — present on any
# real graphical desktop, so the container must have them too. DELIBERATELY still
# NOT libmpv, NOT libxcb-cursor0, NOT the xcb-util/fontconfig closure: those MUST
# come from inside the AppImage, or this self-containment test is meaningless.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xvfb libgl1 libegl1 >/dev/null

shopt -s nullglob
_imgs=(/src/dist/jellytoast-*-x86_64.AppImage)
APPIMAGE="${_imgs[0]:-}"
test -n "$APPIMAGE" || { echo "FAIL: no AppImage in /src/dist"; exit 1; }

# No FUSE in a container → run via the extract-and-run fallback (the static
# type-2 runtime needs no libfuse on the user's machine, but in-container needs
# this to avoid the mount).
export APPIMAGE_EXTRACT_AND_RUN=1
cp "$APPIMAGE" /tmp/jt.AppImage
chmod +x /tmp/jt.AppImage

echo "Booting the AppImage under Xvfb (xcb) in a clean container ($(cat /etc/os-release | sed -n 's/^PRETTY_NAME=//p'))…"
set +e
QT_DEBUG_PLUGINS=1 QT_QPA_PLATFORM=xcb xvfb-run -a timeout 25 /tmp/jt.AppImage \
  >/tmp/jt-appimage-boot.log 2>&1
rc=$?
set -e

if grep -qi "requires libmpv" /tmp/jt-appimage-boot.log; then
  echo "FAIL: libmpv is NOT self-contained (app hit the Missing-dependency path)."
  tail -30 /tmp/jt-appimage-boot.log
  exit 1
fi
if [ "$rc" != 124 ] && [ "$rc" != 0 ]; then
  echo "FAIL: the AppImage did not boot cleanly (rc=$rc) — a bundled lib is missing."
  grep -iE "cannot open shared object|Cannot load library|undefined symbol" \
    /tmp/jt-appimage-boot.log || echo "(no 'cannot open shared object' line — inspect the full log)"
  echo "--- last 30 lines ---"
  tail -30 /tmp/jt-appimage-boot.log
  exit 1
fi
echo "OK: AppImage boots self-contained under Xvfb (rc=$rc) — libmpv + xcb closure bundled"
echo "appimage smoke test passed"

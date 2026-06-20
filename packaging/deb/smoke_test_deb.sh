#!/usr/bin/env bash
# Smoke-test the built .deb inside a clean, NEWER Ubuntu than the 22.04 build
# runner. The frozen-bundle boot test in release.yml runs on the SAME 22.04 the
# bundle was built on — where the bundled libs still match the host — so it
# cannot catch the crashes a real user on a modern distro hits:
#
#   BUG-1 (#148): the bundle used to ship libmpv's host-provided dependency
#   closure (libstdc++, libmount, glib, ffmpeg…). On Ubuntu 24.04/26.04 those
#   stale copies shadowed the system libs and aborted libmpv load → no audio.
#   BUG-2 (#149 + siblings): the Qt xcb platform plugin dlopens libxcb-cursor0,
#   libxcb-icccm4 and libxcb-keysyms1; ALL must be package Depends or X11/
#   XWayland sessions abort at startup ("could not load the Qt platform plugin
#   xcb"). libgl1 is a hard DT_NEEDED of libQt6Gui and aborts every session.
#
# Run in CI (GitHub-hosted runners have Docker), across distros:
#   for img in ubuntu:24.04 ubuntu:26.04 debian:stable; do
#     docker run --rm -v "$PWD:/src:ro" "$img" bash /src/packaging/deb/smoke_test_deb.sh
#   done
set -euo pipefail

apt-get update -qq
# Install the .deb (apt resolves its Depends) plus what the boot probe +
# validators need (xvfb for a real X server, the two metadata validators).
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  /src/dist/jellytoast_*_amd64.deb \
  python3 xvfb desktop-file-utils appstream >/dev/null

# ── dependency guards ───────────────────────────────────────────────────
# The bundled Qt xcb plugin hard-links cursor0 + icccm4 + keysyms1, and
# libQt6Gui hard-links libGL; installing the .deb must pull ALL of them.
for dep in libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libgl1; do
  dpkg -s "$dep" >/dev/null
done
echo "OK: Qt runtime deps present (libxcb-cursor0/icccm4/keysyms1, libgl1)"
# One libmpv alternative must be installed (the audio stack comes from the host).
dpkg -s libmpv2 >/dev/null 2>&1 || dpkg -s libmpv1 >/dev/null 2>&1
echo "OK: a libmpv alternative is installed"
# The /usr/bin entrypoint symlink must resolve to a real executable.
test -x /usr/bin/jellytoast && test -x "$(readlink -f /usr/bin/jellytoast)"
echo "OK: /usr/bin/jellytoast resolves to the bundle"

# ── metadata validators (catch build_deb.sh staging drift) ───────────────
desktop-file-validate \
  /usr/share/applications/io.github.wolfgangwarehaus.jellytoast.desktop
echo "OK: .desktop validates"
# Advisory: appstreamcli can be strict on style; ci.yml validates the source
# metainfo authoritatively — here we just guard against gross install-time drift.
appstreamcli validate --no-net \
  /usr/share/metainfo/io.github.wolfgangwarehaus.jellytoast.metainfo.xml || true

# ── BUG-1 guard (#148): system libmpv must LOAD with the bundle's lib dir on
# the search path — the exact reproduction that failed before the closure strip.
LD_LIBRARY_PATH=/opt/jellytoast/_internal python3 - <<'PY'
import ctypes
import ctypes.util

soname = ctypes.util.find_library("mpv")
assert soname, "libmpv not found by the linker (is libmpv2 installed?)"
ctypes.CDLL(soname)  # raises OSError if the bundled stale closure shadows it
print(f"OK: libmpv loads with the bundle on the path (BUG-1 / #148 guard): {soname}")
PY

# ── boot probe: force the Qt xcb platform plugin to load under Xvfb, so a
# missing X DT_NEEDED (the cursor0/icccm4/keysyms1 class) aborts HERE (SIGABRT,
# rc=134) instead of in a user's session. rc 0 = clean exit, 124 = survived the
# timeout (Qt + xcb initialized). Anything else fails the smoke test.
echo "Booting the installed bundle under Xvfb (xcb)…"
set +e
QT_QPA_PLATFORM=xcb xvfb-run -a timeout 15 /usr/bin/jellytoast >/tmp/jt-boot-xcb.log 2>&1
rc=$?
set -e
if [ "$rc" != 124 ] && [ "$rc" != 0 ]; then
  echo "FAIL: xcb platform plugin did not load (rc=$rc) — missing X DT_NEEDED?"
  tail -25 /tmp/jt-boot-xcb.log
  exit 1
fi
echo "OK: xcb platform plugin loads under Xvfb (rc=$rc)"

echo "deb smoke test passed"

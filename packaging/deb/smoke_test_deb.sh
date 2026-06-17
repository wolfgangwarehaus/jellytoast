#!/usr/bin/env bash
# Smoke-test the built .deb inside a clean, NEWER Ubuntu than the 22.04 build
# runner. The frozen-bundle boot test in release.yml runs on the SAME 22.04 the
# bundle was built on — where the bundled libs still match the host — so it
# cannot catch the two crashes a real user on a modern distro hit:
#
#   BUG-1 (#148): the bundle used to ship libmpv's host-provided dependency
#   closure (libstdc++, libmount, glib, ffmpeg…). On Ubuntu 24.04/26.04 those
#   stale copies shadowed the system libs and aborted libmpv load → no audio.
#   BUG-2 (#149): the Qt 6.5+ xcb plugin dlopens libxcb-cursor0, which must be a
#   package Depends or X11/XWayland sessions abort at startup.
#
# Run in CI (GitHub-hosted runners have Docker):
#   docker run --rm -v "$PWD:/src:ro" ubuntu:24.04 bash /src/packaging/deb/smoke_test_deb.sh
set -euo pipefail

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /src/dist/jellytoast_*_amd64.deb python3 >/dev/null

# BUG-2 guard (#149): installing the package must pull in the xcb runtime dep.
dpkg -s libxcb-cursor0 >/dev/null
echo "OK: libxcb-cursor0 present (BUG-2 / #149 guard)"

# BUG-1 guard (#148): the system libmpv must LOAD with the bundle's lib dir on
# the search path — the exact reproduction that failed before the closure strip.
LD_LIBRARY_PATH=/opt/jellytoast/_internal python3 - <<'PY'
import ctypes
import ctypes.util

soname = ctypes.util.find_library("mpv")
assert soname, "libmpv not found by the linker (is libmpv2 installed?)"
ctypes.CDLL(soname)  # raises OSError if the bundled stale closure shadows it
print(f"OK: libmpv loads with the bundle on the path (BUG-1 / #148 guard): {soname}")
PY

echo "deb smoke test passed"

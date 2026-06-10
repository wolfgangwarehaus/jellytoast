#!/bin/bash
set -e
# This script lives in dev/; cd to the repo root before launching.
cd "$(dirname "$(readlink -f "$0")")/.."
unset LC_ALL
export LC_NUMERIC=C
export LANG="${LANG:-C.UTF-8}"
# Run native Wayland by default — drops the XWayland surface
# allocation, the cursor-bootstrap subprocess, and the off-screen
# show-and-restore dance the X11 path uses to avoid pre-paint flicker.
# Frosted blur works natively on KDE Wayland via the KF6 KWindowSystem
# plugin (see jellytoast/blur/ and README "Themes & blur"); on no-blur
# setups the body falls back to a near-opaque panel. Set
# QT_QPA_PLATFORM=xcb in the environment to force XWayland if you need
# to debug X11-specific behaviour.
export QT_LOGGING_RULES="${QT_LOGGING_RULES:-qt.qpa.*=false}"
exec python3 -m jellytoast "$@"

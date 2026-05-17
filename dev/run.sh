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
# Tradeoff: KWin blur (org_kde_kwin_blur) has no PySide6 binding yet,
# so the frosted body falls back to plain translucency on Wayland —
# the theme is already tuned a touch darker/more opaque to compensate.
# Set QT_QPA_PLATFORM=xcb in the environment to force XWayland and
# get blur back.
export QT_LOGGING_RULES="${QT_LOGGING_RULES:-qt.qpa.*=false}"
exec python3 jellytoast.py "$@"

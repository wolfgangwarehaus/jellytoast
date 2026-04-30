#!/bin/bash
set -e
cd "$(dirname "$(readlink -f "$0")")"
unset LC_ALL
export LC_NUMERIC=C
export LANG="${LANG:-C.UTF-8}"
if [ -n "$WAYLAND_DISPLAY" ] && [ -z "$QT_QPA_PLATFORM" ]; then
    export QT_QPA_PLATFORM=xcb
fi
export QT_LOGGING_RULES="${QT_LOGGING_RULES:-qt.qpa.*=false}"
exec python3 jellytoast.py "$@"

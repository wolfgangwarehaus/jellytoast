#!/usr/bin/env bash
# Rasterize packaging/icons/jellytoast.svg into the user's hicolor
# icon tree, install the SVG itself for scalable surfaces, and refresh
# the icon caches KDE / GTK consult. Runtime jellytoast already reads
# the SVG directly via QSvgRenderer; this script propagates new art to
# everything OUTSIDE the running process: KDE app menu, taskbar,
# startup bouncer, system tray fallback.
#
# Run after every edit of packaging/icons/jellytoast.svg.
# Never kills plasmashell — cache rebuild only.
set -euo pipefail

SVG="$(cd "$(dirname "$0")" && pwd)/icons/jellytoast.svg"
HICOLOR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

[[ -f "$SVG" ]] || { echo "missing source: $SVG" >&2; exit 1; }
command -v rsvg-convert >/dev/null || { echo "rsvg-convert not installed" >&2; exit 1; }

for size in 16 24 32 48 64 128 256 512; do
  dir="$HICOLOR/${size}x${size}/apps"
  mkdir -p "$dir"
  rsvg-convert -w "$size" -h "$size" "$SVG" -o "$dir/jellytoast.png"
  echo "  ${size}x${size}"
done

mkdir -p "$HICOLOR/scalable/apps"
cp "$SVG" "$HICOLOR/scalable/apps/jellytoast.svg"
echo "  scalable"

gtk-update-icon-cache -f -t "$HICOLOR" >/dev/null 2>&1 || true
rm -f "${XDG_CACHE_HOME:-$HOME/.cache}/icon-cache.kcache"

echo "done. KDE menu / taskbar / bouncer will pick up the new icon on next lookup."
echo "(already-running jellytoast keeps its current taskbar pixmap until restart.)"

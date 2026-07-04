#!/usr/bin/env bash
# Render the Microsoft Store / MSIX visual assets from the master SVG.
#
# MSIX picks the right file by filename convention (scale-NNN / targetsize-NN),
# so we emit the named logos plus their high-DPI scale variants and the
# app-list/taskbar target sizes. Re-run after editing the icon.
#
# Deps: rsvg-convert (librsvg) for crisp square renders, ImageMagick for the
# wide-tile letterboxing.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
svg="$here/../icons/jellytoast.svg"
out="$here/Assets"
mkdir -p "$out"

render() { # render <w> <h> <file>
  rsvg-convert -w "$1" -h "$2" "$svg" -o "$out/$3"
}

# --- StoreLogo (Store listing) : 50 base + 200% ---
render 50  50  StoreLogo.png
render 100 100 StoreLogo.scale-200.png

# --- Square44x44 (app list, taskbar, title bar) : base + 200% + target sizes ---
render 44  44  Square44x44Logo.png
render 88  88  Square44x44Logo.scale-200.png
for s in 16 24 32 48 256; do
  render "$s" "$s" "Square44x44Logo.targetsize-${s}.png"
  # unplated variants render identically here (transparent bg, no plate)
  render "$s" "$s" "Square44x44Logo.targetsize-${s}_altform-unplated.png"
done

# --- Square150x150 (medium tile) : base + 200% ---
render 150 150 Square150x150Logo.png
render 300 300 Square150x150Logo.scale-200.png

# --- Square71x71 (small tile) : base + 200% ---
render 71  71  Square71x71Logo.png
render 142 142 Square71x71Logo.scale-200.png

# --- Square310x310 (large tile) ---
render 310 310 Square310x310Logo.png

# --- Wide310x150 (wide tile): center the 150-tall square on a transparent canvas ---
render 150 150 _wide_src.png
magick "$out/_wide_src.png" -background none -gravity center -extent 310x150 "$out/Wide310x150Logo.png"
render 300 300 _wide_src2.png
magick "$out/_wide_src2.png" -background none -gravity center -extent 620x300 "$out/Wide310x150Logo.scale-200.png"
rm -f "$out/_wide_src.png" "$out/_wide_src2.png"

# --- SplashScreen (shown briefly on launch): center 300-tall on 620x300 ---
render 300 300 _splash_src.png
magick "$out/_splash_src.png" -background none -gravity center -extent 620x300 "$out/SplashScreen.png"
render 600 600 _splash_src2.png
magick "$out/_splash_src2.png" -background none -gravity center -extent 1240x600 "$out/SplashScreen.scale-200.png"
rm -f "$out/_splash_src.png" "$out/_splash_src2.png"

echo "Rendered $(find "$out" -maxdepth 1 -type f | wc -l) assets into $out"

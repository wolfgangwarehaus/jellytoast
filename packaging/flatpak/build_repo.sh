#!/usr/bin/env bash
# Build jellytoast's self-hosted Flatpak repository from release bundles.
#
#   packaging/flatpak/build_repo.sh OUTDIR BUNDLE.flatpak [BUNDLE.flatpak…]
#
# OUTDIR gets a static site, ready to serve as-is:
#   repo/                              the OSTree repository (+ AppStream data)
#   jellytoast.flatpakrepo             add the source once:  flatpak remote-add --if-not-exists jellytoast <url>
#   io.github.wolfgangwarehaus.jellytoast.flatpakref
#                                      one-step install (also pulls the KDE runtime from Flathub)
#   index.html                         how to install
#
# Once added, jellytoast shows up in GNOME Software / KDE Discover and updates
# like any other Flatpak. The repo is rebuilt from scratch on every release
# with just that release (x86_64 + aarch64): Flatpak updates to whatever commit
# the ref points at, so no history is needed, and the site stays small.
#
# Signing: GPG_KEY_ID (and optionally GPG_HOMEDIR) sign the commits and the
# summary; the public key goes into the .flatpakrepo / .flatpakref so clients
# verify every update. Unsigned builds are refused unless ALLOW_UNSIGNED=1
# (local testing only).
#
# BASE_URL is where OUTDIR will be served (default the production site).
set -euo pipefail

OUT="${1:?usage: build_repo.sh OUTDIR BUNDLE.flatpak [BUNDLE.flatpak…]}"
shift
[ "$#" -ge 1 ] || { echo "error: no bundles given" >&2; exit 2; }
BASE_URL="${BASE_URL:-https://wolfgangwarehaus.com/flatpak/}"
BASE_URL="${BASE_URL%/}/"
APP=io.github.wolfgangwarehaus.jellytoast
ICON_URL="https://wolfgangwarehaus.com/jellytoast/jellytoast.svg"
HOMEPAGE="https://wolfgangwarehaus.com/jellytoast/"

sign=()
if [ -n "${GPG_KEY_ID:-}" ]; then
    sign=(--gpg-sign="$GPG_KEY_ID")
    [ -n "${GPG_HOMEDIR:-}" ] && sign+=(--gpg-homedir="$GPG_HOMEDIR")
elif [ "${ALLOW_UNSIGNED:-0}" != "1" ]; then
    echo "error: set GPG_KEY_ID (or ALLOW_UNSIGNED=1 for a local test)" >&2
    exit 2
fi

rm -rf "$OUT"
mkdir -p "$OUT"
ostree init --mode=archive-z2 --repo="$OUT/repo"

for bundle in "$@"; do
    echo "importing $bundle"
    flatpak build-import-bundle "${sign[@]}" --no-update-summary "$OUT/repo" "$bundle"
done

# No static deltas: a from-scratch delta is one ~170 MB file per arch, past
# GitHub Pages' file-size limit, and it buys little — clients fetch the
# objects individually instead.
flatpak build-update-repo "${sign[@]}" --title="jellytoast" \
    --homepage="$HOMEPAGE" --icon="$ICON_URL" \
    --comment="Music player for Jellyfin and Navidrome" \
    --prune "$OUT/repo"

branch="$(ostree refs --repo="$OUT/repo" | sed -n "s|^app/$APP/[^/]*/||p" | sort -u | head -1)"
[ -n "$branch" ] || { echo "error: no app/$APP ref in the repo" >&2; exit 1; }

gpg_line=""
if [ -n "${GPG_KEY_ID:-}" ]; then
    key="$(gpg ${GPG_HOMEDIR:+--homedir "$GPG_HOMEDIR"} --export "$GPG_KEY_ID" | base64 -w0)"
    [ -n "$key" ] || { echo "error: couldn't export the public key for $GPG_KEY_ID" >&2; exit 1; }
    gpg_line="GPGKey=$key"
fi

cat > "$OUT/jellytoast.flatpakrepo" <<EOF
[Flatpak Repo]
Title=jellytoast
Url=${BASE_URL}repo/
Homepage=$HOMEPAGE
Comment=Music player for Jellyfin and Navidrome
Icon=$ICON_URL
$gpg_line
EOF

cat > "$OUT/$APP.flatpakref" <<EOF
[Flatpak Ref]
Title=jellytoast
Name=$APP
Branch=$branch
Url=${BASE_URL}repo/
SuggestRemoteName=jellytoast
Homepage=$HOMEPAGE
Icon=$ICON_URL
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
IsRuntime=false
$gpg_line
EOF

cat > "$OUT/index.html" <<EOF
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jellytoast Flatpak repository</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 16px; }
  code, pre { font-family: ui-monospace, monospace; }
  pre { padding: .75rem 1rem; border-radius: 8px; background: color-mix(in srgb, currentColor 8%, transparent); overflow-x: auto; }
</style>
</head>
<body>
<h1>jellytoast Flatpak repository</h1>
<p>Install <a href="$HOMEPAGE">jellytoast</a> from here and it updates through
GNOME Software, KDE Discover or <code>flatpak update</code> like any other app.
Works on x86_64 and ARM (aarch64) — PCs, Steam Deck, and Linux phones.</p>
<p><a href="$APP.flatpakref">Install jellytoast</a> (opens your software centre), or:</p>
<pre>flatpak install --user ${BASE_URL}$APP.flatpakref</pre>
<p>To just add the source:</p>
<pre>flatpak remote-add --user --if-not-exists jellytoast ${BASE_URL}jellytoast.flatpakrepo</pre>
<p>Everything here is signed; the key is in those two files.</p>
</body>
</html>
EOF

echo "built $OUT (branch $branch, $(du -sh "$OUT" | cut -f1))"
ostree refs --repo="$OUT/repo"

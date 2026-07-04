#!/usr/bin/env bash
# Cut a jellytoast release in one command: bump the version in every
# source-of-truth file, snip the CHANGELOG [Unreleased] section into a
# dated version block, run the consistency gate, commit, tag, and
# (optionally) push the tag so .github/workflows/release.yml drafts the
# GitHub release.
#
#   dev/cut_release.sh 0.1.1           # bump + commit + tag locally, then
#                                      #   print the push command (you push)
#   dev/cut_release.sh 0.1.1 --push    # ... and push main + tag immediately
#
# Pushing the tag triggers the DRAFT release build — that outward step is
# gated behind --push (or your manual push) on purpose; everything before
# it is local and reversible (git reset --hard HEAD~1 && git tag -d vX).
#
# Version single-source-of-truth (kept in lockstep by
# tests/test_version_consistency.py):
#   • pyproject.toml                              [project].version
#   • jellytoast/version.py                       both __version__ fallbacks
#   • packaging/…jellytoast.metainfo.xml          newest <release version=…>
#   • packaging/winget/*.yaml                     PackageVersion + InstallerUrl
#   • packaging/aur/PKGBUILD                      pkgver (pkgrel reset to 1)
# So one cut stamps every distribution channel's manifest; the per-channel
# fan-out workflows (winget/aur on release: published) then ship from them.
# NOT stamped here (filled at publish from the built artifact): the winget
# InstallerSha256 (winget-releaser computes it) and the AUR sha256sums
# (`updpkgsums` refreshes it) — the tag tarball / .exe don't exist yet.
# The CHANGELOG [Unreleased] block is moved verbatim into the new version's
# section, leaving a fresh empty [Unreleased] at the top.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

NEW_VERSION="${1:-}"
PUSH=0
[ "${2:-}" = "--push" ] && PUSH=1

if [ -z "$NEW_VERSION" ]; then
  echo "usage: dev/cut_release.sh X.Y.Z[-suffix] [--push]" >&2
  exit 2
fi
if ! printf '%s' "$NEW_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$'; then
  echo "error: '$NEW_VERSION' is not a valid version (expected X.Y.Z[-suffix])" >&2
  exit 2
fi

# ── Pre-flight: clean, up-to-date main, tag free ────────────────────────
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
  echo "error: on '$BRANCH', not 'main' — releases are cut from main." >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is dirty — commit or stash first." >&2
  exit 1
fi
git fetch --quiet origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "error: local main differs from origin/main — pull/push to sync first." >&2
  exit 1
fi
if git rev-parse "v$NEW_VERSION" >/dev/null 2>&1; then
  echo "error: tag v$NEW_VERSION already exists." >&2
  exit 1
fi

TODAY="$(date -u +%Y-%m-%d)"

# ── Bump every source-of-truth file in one atomic Python pass ───────────
NEW_VERSION="$NEW_VERSION" RELEASE_DATE="$TODAY" python3 - <<'PY'
import os
import re
import sys
from pathlib import Path

new = os.environ["NEW_VERSION"]
date = os.environ["RELEASE_DATE"]
root = Path(".")

# Current (pre-bump) version — used to rewrite URLs that embed it literally
# (the winget InstallerUrl), robust to any version format incl. -suffixes.
old = re.search(
    r'(?m)^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text("utf-8")
).group(1)

# 1. pyproject.toml — [project].version (first `version = "…"` line).
pp = root / "pyproject.toml"
text = pp.read_text(encoding="utf-8")
text, n = re.subn(r'(?m)^(version\s*=\s*")[^"]+(")', rf'\g<1>{new}\g<2>', text, count=1)
assert n == 1, "did not bump [project].version in pyproject.toml"
pp.write_text(text, encoding="utf-8")

# 2. jellytoast/version.py — every hardcoded __version__ fallback literal.
vp = root / "jellytoast" / "version.py"
text = vp.read_text(encoding="utf-8")
text, n = re.subn(r'(__version__\s*=\s*")[^"]+(")', rf'\g<1>{new}\g<2>', text)
assert n >= 1, "did not bump any __version__ fallback in version.py"
vp.write_text(text, encoding="utf-8")

# 3. AppStream metainfo — insert the new (newest) <release> at the top of
#    <releases>; the consistency test reads the FIRST release entry.
mi = root / "packaging" / "io.github.wolfgangwarehaus.jellytoast.metainfo.xml"
text = mi.read_text(encoding="utf-8")
entry = (
    f'    <release version="{new}" type="stable" date="{date}">\n'
    f"      <description>\n"
    f"        <p>See the release notes for {new}.</p>\n"
    f"      </description>\n"
    f"    </release>\n"
)
text, n = re.subn(r"(<releases>\n)", rf"\g<1>{entry}", text, count=1)
assert n == 1, "did not find <releases> to insert into metainfo.xml"
mi.write_text(text, encoding="utf-8")

# 4. CHANGELOG — snip [Unreleased] into a dated version block, leaving a
#    fresh empty [Unreleased] at the top (Keep a Changelog flow). This is the
#    tight, user-facing root CHANGELOG.md; docs/CHANGELOG.md is a frozen archive.
cl = root / "CHANGELOG.md"
text = cl.read_text(encoding="utf-8")
marker = "## [Unreleased]"
idx = text.find(marker)
assert idx != -1, "no '## [Unreleased]' section in CHANGELOG.md"
after = idx + len(marker)
rest = text[after:]
m = re.search(r"\n## \[", rest)  # next version header
cut = m.start() if m else len(rest)
body = rest[:cut].strip("\n")
tail = rest[cut:]
# HTML comments (the release-notes voice guidance) stay under the fresh
# [Unreleased] heading instead of being snipped into the dated block —
# 0.1.5 shipped the guidance inside its release-notes body.
comments = re.findall(r"<!--.*?-->", body, flags=re.S)
body = re.sub(r"<!--.*?-->\n*", "", body, flags=re.S).strip("\n")
if not body and os.environ.get("JT_ALLOW_EMPTY_CHANGELOG") != "1":
    sys.exit(
        "CHANGELOG [Unreleased] is EMPTY — the GitHub release would silently "
        "fall back to auto-generated notes. Write the block first, or re-run "
        "with JT_ALLOW_EMPTY_CHANGELOG=1 to cut anyway."
    )
out = [text[:after], "\n"]
for c in comments:
    out.append("\n" + c + "\n")
out.append("\n" + f"## [{new}] — {date}\n")
if body:
    out.append("\n" + body + "\n")
out.append(tail if tail.startswith("\n") else "\n" + tail)
cl.write_text("".join(out), encoding="utf-8")

# 5. winget manifests — PackageVersion in all three + the InstallerUrl that
#    embeds the version (the `/v…/` path segment and the `-…-` filename). The
#    InstallerSha256 stays (it's for the not-yet-built .exe; winget-releaser /
#    `wingetcreate` recompute it at publish).
import glob

for wf in sorted(glob.glob("packaging/winget/*.yaml")):
    p = root / wf
    t = p.read_text(encoding="utf-8")
    t, n = re.subn(r"(?m)^(PackageVersion:\s*)\S+\s*$", rf"\g<1>{new}", t)
    assert n >= 1, f"did not bump PackageVersion in {wf}"
    if old != new:
        # Literal old→new so any version format (incl. -rc suffixes) is exact.
        t = t.replace(f"/download/v{old}/", f"/download/v{new}/")
        t = t.replace(f"jellytoast-{old}-windows", f"jellytoast-{new}-windows")
    p.write_text(t, encoding="utf-8")

# 6. AUR PKGBUILD — pkgver + reset pkgrel=1. The source URL uses $pkgver so it
#    follows; sha256sums are refreshed by `updpkgsums` at publish (the tag
#    tarball doesn't exist yet).
pk = root / "packaging" / "aur" / "PKGBUILD"
text = pk.read_text(encoding="utf-8")
text, n = re.subn(r"(?m)^pkgver=\S+\s*$", f"pkgver={new}", text)
assert n == 1, "did not bump pkgver in PKGBUILD"
text, n = re.subn(r"(?m)^pkgrel=\S+\s*$", "pkgrel=1", text)
assert n == 1, "did not reset pkgrel in PKGBUILD"
pk.write_text(text, encoding="utf-8")

print(
    f"bumped pyproject + version.py + metainfo + CHANGELOG + winget + AUR "
    f"→ {new} ({date})"
)
PY

# ── Gate: the source-of-truth files must agree before we commit ─────────
# A self-contained file check (NOT the pytest suite): the runtime test in
# tests/test_version_consistency.py reads the INSTALLED package metadata,
# which is stale after an editable install until a reinstall — so it would
# spuriously fail here. That runtime check runs in CI on the release commit
# against a fresh install; locally we verify the three files we just edited
# all carry the same version, which is the drift this script could cause.
echo "→ verifying pyproject / version.py / metainfo / winget / AUR agree…"
python3 - <<'PY'
import glob
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

pyv = re.search(r'(?m)^version\s*=\s*"([^"]+)"', Path("pyproject.toml").read_text("utf-8")).group(1)
fallbacks = set(re.findall(r'__version__\s*=\s*"([^"]+)"', Path("jellytoast/version.py").read_text("utf-8")))
mi = (
    ET.parse("packaging/io.github.wolfgangwarehaus.jellytoast.metainfo.xml")
    .getroot()
    .find("releases")
    .findall("release")[0]
    .get("version")
)
wg = set()
for wf in glob.glob("packaging/winget/*.yaml"):
    m = re.search(r"(?m)^PackageVersion:\s*(\S+)\s*$", Path(wf).read_text("utf-8"))
    if m:
        wg.add(m.group(1))
aur = re.search(r"(?m)^pkgver=(\S+)\s*$", Path("packaging/aur/PKGBUILD").read_text("utf-8")).group(1)
errs = []
if fallbacks != {pyv}:
    errs.append(f"version.py fallbacks {sorted(fallbacks)} != pyproject {pyv}")
if mi != pyv:
    errs.append(f"metainfo newest <release> {mi} != pyproject {pyv}")
if wg != {pyv}:
    errs.append(f"winget PackageVersion {sorted(wg)} != pyproject {pyv}")
if aur != pyv:
    errs.append(f"AUR pkgver {aur} != pyproject {pyv}")
if errs:
    sys.exit("consistency check FAILED: " + "; ".join(errs))
print(f"✓ pyproject, version.py, metainfo, winget, and AUR all agree at {pyv}")
PY

# ── Commit + annotated tag ──────────────────────────────────────────────
# winget via the same glob the stamping pass uses — a hand-list here let a
# stamped-but-uncommitted 4th manifest leave the tree dirty after commit.
git add pyproject.toml jellytoast/version.py \
  packaging/io.github.wolfgangwarehaus.jellytoast.metainfo.xml CHANGELOG.md \
  packaging/winget/*.yaml \
  packaging/aur/PKGBUILD
git commit -q -m "release: v$NEW_VERSION"
git tag -a "v$NEW_VERSION" -m "jellytoast v$NEW_VERSION"
echo "✓ committed + tagged v$NEW_VERSION"

# ── Push — the outward step that kicks off the DRAFT release build ──────
if [ "$PUSH" -eq 1 ]; then
  git push origin main "v$NEW_VERSION"
  echo "✓ pushed main + v$NEW_VERSION — release.yml will build the DRAFT release"
else
  echo
  echo "Local only. Review:   git show v$NEW_VERSION"
  echo "Undo:                 git reset --hard HEAD~1 && git tag -d v$NEW_VERSION"
  echo "Ship (triggers the DRAFT release build):"
  echo "    git push origin main v$NEW_VERSION"
fi

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
from pathlib import Path

new = os.environ["NEW_VERSION"]
date = os.environ["RELEASE_DATE"]
root = Path(".")

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
#    fresh empty [Unreleased] at the top (Keep a Changelog flow).
cl = root / "docs" / "CHANGELOG.md"
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
out = [text[:after], "\n\n", f"## [{new}] — {date}\n"]
if body:
    out.append("\n" + body + "\n")
out.append(tail if tail.startswith("\n") else "\n" + tail)
cl.write_text("".join(out), encoding="utf-8")
print(f"bumped pyproject + version.py + metainfo + CHANGELOG → {new} ({date})")
PY

# ── Gate: the source-of-truth files must agree before we commit ─────────
# A self-contained file check (NOT the pytest suite): the runtime test in
# tests/test_version_consistency.py reads the INSTALLED package metadata,
# which is stale after an editable install until a reinstall — so it would
# spuriously fail here. That runtime check runs in CI on the release commit
# against a fresh install; locally we verify the three files we just edited
# all carry the same version, which is the drift this script could cause.
echo "→ verifying pyproject / version.py / metainfo agree…"
python3 - <<'PY'
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
errs = []
if fallbacks != {pyv}:
    errs.append(f"version.py fallbacks {sorted(fallbacks)} != pyproject {pyv}")
if mi != pyv:
    errs.append(f"metainfo newest <release> {mi} != pyproject {pyv}")
if errs:
    sys.exit("consistency check FAILED: " + "; ".join(errs))
print(f"✓ pyproject, version.py, and metainfo all agree at {pyv}")
PY

# ── Commit + annotated tag ──────────────────────────────────────────────
git add pyproject.toml jellytoast/version.py \
  packaging/io.github.wolfgangwarehaus.jellytoast.metainfo.xml docs/CHANGELOG.md
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

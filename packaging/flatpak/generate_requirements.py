#!/usr/bin/env python3
"""Generate python3-requirements.json for the Flatpak manifest.

In-repo replacement for flatpak-pip-generator: uses pip's own
cross-environment resolver (`pip install --dry-run --report`) to pin the
full dependency closure of requirements-flatpak.txt as (url, sha256)
sources targeting the KDE 6.8 runtime (Python 3.12, manylinux x86_64),
then emits a single flatpak-builder module that installs them offline.

Run from the repo root after any dependency change:

    python3 packaging/flatpak/generate_requirements.py

Needs network (PyPI) and a pip new enough for --report (>=22.2).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIREMENTS = HERE / "requirements-flatpak.txt"
OUTPUT = HERE / "python3-requirements.json"

# The KDE 6.8 runtime (freedesktop 24.08 base) ships Python 3.12.
PYTHON_VERSION = "312"
PLATFORMS = [
    "manylinux_2_28_x86_64",
    "manylinux_2_17_x86_64",
    "manylinux2014_x86_64",
    "linux_x86_64",
]

# Packages with no wheel on PyPI — resolved as sdists via the JSON API.
# All must be dependency-free (verified at generation time below).
SDIST_ONLY = ["snapcast"]


def resolve_wheels() -> list[dict]:
    """pip's resolver does the real work; the report gives URL + sha256."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        reqs = [
            line.split("#")[0].strip()
            for line in REQUIREMENTS.read_text().splitlines()
            if line.split("#")[0].strip()
        ]
        reqs = [r for r in reqs if not any(r.startswith(s) for s in SDIST_ONLY)]
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--dry-run", "--ignore-installed", "--quiet",
            "--report", str(report),
            "--python-version", PYTHON_VERSION,
            "--only-binary=:all:",
            "--target", str(Path(tmp) / "resolve"),
        ]
        for p in PLATFORMS:
            cmd += ["--platform", p]
        cmd += reqs
        subprocess.run(cmd, check=True)
        data = json.loads(report.read_text())
    out = []
    for pkg in data["install"]:
        info = pkg["download_info"]
        sha = info["archive_info"]["hashes"]["sha256"]
        out.append({"type": "file", "url": info["url"], "sha256": sha})
    return sorted(out, key=lambda s: s["url"])


def resolve_sdists() -> list[dict]:
    out = []
    for name in SDIST_ONLY:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json") as r:
            d = json.load(r)
        if d["info"]["requires_dist"]:
            sys.exit(
                f"{name} grew dependencies ({d['info']['requires_dist']}) — "
                "the dependency-free sdist assumption broke; rework needed."
            )
        sdist = next(f for f in d["urls"] if f["packagetype"] == "sdist")
        out.append({
            "type": "file",
            "url": sdist["url"],
            "sha256": sdist["digests"]["sha256"],
        })
    return out


def main() -> None:
    sources = resolve_wheels() + resolve_sdists()
    names = [
        line.split("#")[0].strip()
        for line in REQUIREMENTS.read_text().splitlines()
        if line.split("#")[0].strip()
    ]
    module = {
        "name": "python3-requirements",
        "buildsystem": "simple",
        "build-commands": [
            'pip3 install --verbose --exists-action=i --no-index '
            '--find-links="file://${PWD}" --prefix=${FLATPAK_DEST} '
            "--no-build-isolation " + " ".join(f'"{n}"' for n in names)
        ],
        "sources": sources,
    }
    OUTPUT.write_text(json.dumps(module, indent=4) + "\n")
    print(f"wrote {OUTPUT.relative_to(Path.cwd())} — {len(sources)} pinned sources")


if __name__ == "__main__":
    main()

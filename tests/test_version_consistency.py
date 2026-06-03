"""Enforce a single source of truth for the application version.

The version lives in ``pyproject.toml`` ``[project].version`` (the build
source of truth). Three derived copies must never drift from it:

1. ``modules.version.__version__`` — at runtime this is read from the
   installed package metadata, but in a source checkout (no install) it
   falls back to a hardcoded literal. That fallback is what every Python
   site (User-Agent, MPRIS, scrobble client, About dialog) gets, so it
   MUST equal the pyproject version.
2. The hardcoded fallback literal in ``modules/version.py`` — asserted
   directly against the source so a stale local install of the package
   (which would make ``__version__`` resolve to the installed version)
   can never mask a drifted fallback.
3. The AppStream metainfo ``<release version="...">`` shipped with the
   packaging files.

A future release bump that updates one site but misses another FAILS
here.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

import modules.version

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
VERSION_PY = REPO_ROOT / "modules" / "version.py"
METAINFO = (
    REPO_ROOT
    / "packaging"
    / "io.github.wolfgangwarehaus.jellytoast.metainfo.xml"
)


def _pyproject_version() -> str:
    """Read ``[project].version`` from pyproject. Prefer a real TOML
    parser (tomllib on 3.11+, tomli as a backport); fall back to a regex
    scoped to the ``[project]`` table if neither is importable so the
    test stays robust on a bare interpreter."""
    text = PYPROJECT.read_text(encoding="utf-8")
    try:
        try:
            import tomllib as _toml
        except ModuleNotFoundError:
            import tomli as _toml  # type: ignore[no-redef]
        data = _toml.loads(text)
        return data["project"]["version"]
    except Exception:
        # Regex fallback: scope to the [project] table so we don't pick up
        # a `version =` line from some other table.
        project = re.search(
            r"^\[project\]\s*$(.*?)^\[", text, re.MULTILINE | re.DOTALL
        )
        scope = project.group(1) if project else text
        m = re.search(r'^version\s*=\s*"([^"]+)"', scope, re.MULTILINE)
        assert m, "could not locate [project].version in pyproject.toml"
        return m.group(1)


def _version_py_fallback() -> str:
    """The hardcoded fallback literal in modules/version.py — read from
    source, independent of whatever the package metadata resolves to."""
    text = VERSION_PY.read_text(encoding="utf-8")
    matches = re.findall(r'__version__\s*=\s*"([^"]+)"', text)
    assert matches, "could not find a hardcoded __version__ fallback in version.py"
    # Every hardcoded fallback literal must be identical.
    assert len(set(matches)) == 1, f"version.py fallbacks disagree: {matches}"
    return matches[0]


def _metainfo_version() -> str:
    """The newest ``<release version>`` in the AppStream metainfo."""
    root = ET.parse(METAINFO).getroot()
    releases = root.find("releases")
    assert releases is not None, "no <releases> block in metainfo.xml"
    versions = [r.get("version") for r in releases.findall("release")]
    versions = [v for v in versions if v]
    assert versions, "no <release version> attribute in metainfo.xml"
    return versions[0]


def test_version_py_fallback_matches_pyproject():
    assert _version_py_fallback() == _pyproject_version()


def test_metainfo_matches_pyproject():
    assert _metainfo_version() == _pyproject_version()


def test_runtime_version_matches_pyproject():
    # In a source checkout (CI / dev) the package isn't installed, so
    # __version__ resolves to the fallback, which must equal pyproject.
    # If a stale install IS present, this would surface the drift — which
    # is exactly the failure we want.
    assert modules.version.__version__ == _pyproject_version()

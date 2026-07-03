"""Enforce a single source of truth for the application version.

The version lives in ``pyproject.toml`` ``[project].version`` (the build
source of truth). Three derived copies must never drift from it:

1. ``jellytoast.version.__version__`` — at runtime this is read from the
   installed package metadata, but in a source checkout (no install) it
   falls back to a hardcoded literal. That fallback is what every Python
   site (User-Agent, MPRIS, scrobble client, About dialog) gets, so it
   MUST equal the pyproject version.
2. The hardcoded fallback literal in ``jellytoast/version.py`` — asserted
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

import jellytoast.version

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
VERSION_PY = REPO_ROOT / "jellytoast" / "version.py"
METAINFO = (
    REPO_ROOT
    / "packaging"
    / "io.github.wolfgangwarehaus.jellytoast.metainfo.xml"
)
WINGET_DIR = REPO_ROOT / "packaging" / "winget"
AUR_PKGBUILD = REPO_ROOT / "packaging" / "aur" / "PKGBUILD"


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
    """The hardcoded fallback literal in jellytoast/version.py — read from
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
    assert jellytoast.version.__version__ == _pyproject_version()


# ── Per-channel manifests (the unified-release single-source gate) ───────────
# dev/cut_release.sh stamps these alongside pyproject; gating them here means a
# release can never ship a channel pointing at a stale version (the class that
# made v0.1.1 silently miss PyPI + AUR). The InstallerSha256 / AUR digest are
# NOT gated — they depend on the built artifact and are filled at publish time
# (winget-releaser computes the SHA; `updpkgsums` refreshes the AUR digest).


def _winget_package_versions() -> dict[str, str]:
    """Every ``PackageVersion:`` across the three winget manifests, keyed by
    filename (no YAML parser needed — the field is a simple top-level scalar)."""
    out: dict[str, str] = {}
    for path in sorted(WINGET_DIR.glob("*.yaml")):
        m = re.search(r"(?m)^PackageVersion:\s*(\S+)\s*$", path.read_text("utf-8"))
        assert m, f"no PackageVersion in {path.name}"
        out[path.name] = m.group(1)
    return out


def _winget_installer_url_versions() -> list[str]:
    """The version tokens in the winget InstallerUrl — it must point at the
    release whose version we're shipping (the URL carries it twice: the
    ``/vX.Y.Z/`` path segment and the ``-X.Y.Z-`` filename segment)."""
    text = (WINGET_DIR / "wolfgangwarehaus.jellytoast.installer.yaml").read_text("utf-8")
    m = re.search(r"^\s*InstallerUrl:\s*(\S+)\s*$", text, re.MULTILINE)
    assert m, "no InstallerUrl in the winget installer manifest"
    url = m.group(1)
    return re.findall(r"/v([0-9][^/]*?)/|jellytoast-([0-9][^-]*?)-windows", url)


def _aur_pkgver() -> str:
    m = re.search(r"(?m)^pkgver=(\S+)\s*$", AUR_PKGBUILD.read_text("utf-8"))
    assert m, "no pkgver in the AUR PKGBUILD"
    return m.group(1)


def test_winget_package_versions_match_pyproject():
    expected = _pyproject_version()
    versions = _winget_package_versions()
    assert len(versions) == 3, f"expected 3 winget manifests, found {sorted(versions)}"
    assert set(versions.values()) == {expected}, f"winget drift: {versions} != {expected}"


def test_winget_installer_url_points_at_pyproject_version():
    expected = _pyproject_version()
    found = {v for pair in _winget_installer_url_versions() for v in pair if v}
    assert found == {expected}, f"winget InstallerUrl version(s) {found} != {expected}"


def test_aur_pkgver_matches_pyproject():
    assert _aur_pkgver() == _pyproject_version()


def test_workflow_referenced_packaging_scripts_exist():
    """Every packaging/ script a workflow invokes must exist in the repo.

    Regression guard: #189 ("slim the public repo") deleted the
    packaging/macos/mas/ build scripts but left release.yml's build-mas job
    calling them, so the MAS build failed `exit 127 (No such file)` on every
    run — silently, for a month. A workflow that references a deleted script
    must fail HERE (in seconds, locally) rather than only in a CI build no one
    watches. Catches the whole class, every workflow, both .sh and .ps1.
    """
    wf_dir = REPO_ROOT / ".github" / "workflows"
    ref_re = re.compile(r"packaging/[A-Za-z0-9_./-]+\.(?:sh|ps1)")
    missing = []
    for wf in sorted(wf_dir.glob("*.yml")):
        # encoding= matters: dough-sync-nudge.yml has a 🍞 that cp1252 (the
        # Windows locale default) can't decode.
        for ref in sorted(set(ref_re.findall(wf.read_text(encoding="utf-8")))):
            if not (REPO_ROOT / ref).is_file():
                missing.append(f"{wf.name} → {ref}")
    assert not missing, "workflow(s) reference missing packaging scripts:\n" + "\n".join(missing)

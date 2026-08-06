"""Single source of truth for the application version.

When jellytoast is installed (wheel / AUR), the version is read
straight from the package metadata that setuptools wrote from
``pyproject.toml`` ``[project].version`` — so a release bump in that one
place flows everywhere automatically. When running from a source checkout
(``python -m jellytoast`` with no install), the package metadata is absent,
so we fall back to a hardcoded string that MUST be kept in sync with
``pyproject.toml``. ``tests/test_version_consistency.py`` enforces that the
fallback, pyproject, and the AppStream metainfo release all agree.

Every other site (User-Agent strings, the MPRIS / Qt application version,
the scrobble submission_client_version, the About dialog) imports
``__version__`` from here instead of re-literalising the number.
"""

from __future__ import annotations

from pathlib import Path

# A source checkout has pyproject.toml one level up from this package
# (jellytoast/version.py → repo root). There, the hardcoded literal below is
# authoritative AND always fresh — cut_release.sh stamps it and
# test_version_consistency enforces it — whereas an editable install's
# recorded metadata goes STALE between a version bump and the next
# `pip install -e` (which is why the dev shortcut kept reporting the old
# version + a phantom update chip). So in a checkout, trust the literal and
# skip metadata entirely. Real installs (wheel / AUR / Flatpak) have no
# adjacent pyproject.toml and read package metadata exactly as before.
_IN_SOURCE_CHECKOUT = (Path(__file__).resolve().parent.parent / "pyproject.toml").is_file()

if _IN_SOURCE_CHECKOUT:
    __version__ = "0.2.2"  # keep in sync with pyproject.toml [project].version
else:
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _v

        try:
            __version__ = _v("jellytoast")
        except PackageNotFoundError:
            __version__ = "0.2.2"  # keep in sync with pyproject.toml [project].version
    except Exception:
        __version__ = "0.2.2"  # keep in sync with pyproject.toml [project].version

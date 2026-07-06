"""Coverage for dev/store_whats_new.py + dev/store_patch_release_notes.py —
the CHANGELOG → Microsoft Store "What's new" pipeline (msstore.yml, ops#3).

The Store submission carries these notes verbatim to users, so the
contract is pinned: version-block extraction, the Windows-audience
curation rule, plain-text rendering, the bullet-boundary length cap, and
casing-tolerant stamping into the submission JSON.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_DEV = Path(__file__).resolve().parent.parent / "dev"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _DEV / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wn = _load("store_whats_new")
pn = _load("store_patch_release_notes")

_CHANGELOG = """\
# Changelog

## [Unreleased]

- **Future thing.** Not this release.

## [9.9.9] - 2099-01-01

- **Faster everything.** The app is now faster
  on big libraries.
- **AppImage self-updates actually work now.** Linux packaging fix.
- **Better on Windows and macOS.** Cross-platform but Store-relevant.
- **A `code` fix** with a [link](https://example.com) inside.

## [9.9.8] - 2098-01-01

- Old release bullet.
"""


def test_extracts_only_the_requested_block():
    text = wn.whats_new(_CHANGELOG, "9.9.9")
    assert "Faster everything" in text
    assert "Future thing" not in text
    assert "Old release bullet" not in text


def test_drops_non_windows_bullets_keeps_mixed_ones():
    text = wn.whats_new(_CHANGELOG, "9.9.9")
    assert "AppImage" not in text  # Linux packaging — not for the Store listing
    assert "Better on Windows and macOS" in text  # mentions Windows → kept


def test_renders_plain_text_bullets():
    text = wn.whats_new(_CHANGELOG, "9.9.9")
    assert "**" not in text and "`" not in text and "](" not in text
    assert text.startswith("• ")
    # Continuation lines are joined into one bullet.
    assert "faster on big libraries" in text


def test_caps_on_bullet_boundary():
    long_changelog = "## [1.0.0]\n\n" + "\n".join(
        f"- **Item {i}.** " + "x" * 400 for i in range(6)
    )
    text = wn.whats_new(long_changelog, "1.0.0")
    assert len(text) <= wn.STORE_LIMIT
    assert text.endswith("x")  # a whole bullet, not a mid-sentence cut


def test_missing_version_returns_none():
    assert wn.whats_new(_CHANGELOG, "0.0.1") is None


def test_patch_stamps_camel_and_pascal_casings():
    camel = {"listings": {"en-us": {"baseListing": {"releaseNotes": "old"}}}}
    assert pn.patch(camel, "new") == 1
    assert camel["listings"]["en-us"]["baseListing"]["releaseNotes"] == "new"

    pascal = {"Listings": {"en-us": {"BaseListing": {"ReleaseNotes": "old"}}}}
    assert pn.patch(pascal, "new") == 1
    listing = pascal["Listings"]["en-us"]["BaseListing"]
    assert listing["ReleaseNotes"] == "new"
    assert "releaseNotes" not in listing  # writes back the document's casing


def test_patch_refuses_unknown_shapes():
    assert pn.patch({"nothing": "here"}, "new") == 0

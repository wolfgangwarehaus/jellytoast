#!/usr/bin/env python3
"""Stamp "What's new" text into a Microsoft Store submission JSON.

Usage: python dev/store_patch_release_notes.py submission.json whats_new.txt out.json

Reads the submission returned by `msstore submission get`, sets the
release-notes field on every language listing, writes the patched JSON
for `msstore submission updateMetadata`. Companion to store_whats_new.py
(see msstore.yml).

The Partner Center submission resource nests notes at
listings.<lang>.baseListing.releaseNotes (camelCase), but the msstore
CLI has round-tripped PascalCase in places — so the walk matches keys
case-insensitively and writes back whatever casing the document already
uses. Exits non-zero if no listings structure is found: the workflow
catches that and falls back to publishing with carried-forward notes
rather than guessing at a shape Partner Center might reject.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _key(d: dict, name: str) -> str | None:
    """The dict's actual key matching `name` case-insensitively."""
    for k in d:
        if k.lower() == name.lower():
            return k
    return None


def patch(submission: dict, notes: str) -> int:
    """Set release notes on every listing; returns how many were stamped."""
    listings_key = _key(submission, "listings")
    if listings_key is None or not isinstance(submission[listings_key], dict):
        return 0
    stamped = 0
    for listing in submission[listings_key].values():
        if not isinstance(listing, dict):
            continue
        base_key = _key(listing, "baseListing")
        target = listing[base_key] if base_key and isinstance(listing[base_key], dict) else listing
        notes_key = _key(target, "releaseNotes") or "releaseNotes"
        target[notes_key] = notes
        stamped += 1
    return stamped


def main() -> int:
    sub_path, notes_path, out_path = (Path(p) for p in sys.argv[1:4])
    submission = json.loads(sub_path.read_text(encoding="utf-8"))
    notes = notes_path.read_text(encoding="utf-8").strip()
    if not notes:
        print("error: empty what's-new text", file=sys.stderr)
        return 1
    stamped = patch(submission, notes)
    if not stamped:
        print(
            "error: no listings structure in the submission JSON — "
            "dump it in the workflow log and adjust this script.",
            file=sys.stderr,
        )
        return 1
    out_path.write_text(json.dumps(submission, indent=2), encoding="utf-8")
    print(f"stamped releaseNotes on {stamped} listing(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

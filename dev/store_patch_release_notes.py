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
import re
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


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f]")


def extract_json(raw: str) -> dict:
    """The submission object from `msstore submission get` output.

    The CLI prints the JSON via AnsiConsole with a status spinner on the
    same stream — chatter lines land before the payload (0.1.8 run) and
    spinner ANSI escapes can be woven straight through it (0.1.9 run:
    'Invalid control character' mid-JSON). The workflow sets NO_COLOR
    to suppress them at the source; this strips any survivors and
    parses the outermost brace span with strict=False so a stray
    control char inside a string can't sink the stamping.
    """
    raw = _ANSI.sub("", raw)
    try:
        return json.loads(raw, strict=False)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in the CLI output") from None
        return json.loads(raw[start : end + 1], strict=False)


def main() -> int:
    sub_path, notes_path, out_path = (Path(p) for p in sys.argv[1:4])
    submission = extract_json(sub_path.read_text(encoding="utf-8"))
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

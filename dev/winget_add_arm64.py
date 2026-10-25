#!/usr/bin/env python3
"""Give the open winget PR native arm64 entries.

jellytoast ships ONE Inno setup.exe that installs x64 or ARM64 natively, but
komac (winget-releaser) reads the PE header and emits only x64 installer
entries (user + machine scope). 0.2.1 listed arm64 too; 0.2.3's PR had to be
patched by hand. This does that patch: on the PR branch in our winget-pkgs
fork it duplicates every x64 installer entry as arm64 (same URL, hash,
scope, switches) and commits the manifest. No-op when arm64 is already there.

    GH_TOKEN=<WINGET_TOKEN> python3 dev/winget_add_arm64.py 0.2.3

Needs the gh CLI. Run right after winget-releaser (winget.yml does).
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys

FORK = "wolfgangwarehaus/winget-pkgs"
PKG = "wolfgangwarehaus.jellytoast"
MANIFEST = "manifests/w/wolfgangwarehaus/jellytoast/{v}/wolfgangwarehaus.jellytoast.installer.yaml"


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", "api", *args], check=True, capture_output=True, text=True
    ).stdout


def add_arm64(text: str) -> str | None:
    """The manifest with each x64 installer entry duplicated as arm64, or
    None when it already has arm64 (or has no x64 entry to copy). Keeps the
    file's line endings: komac 2.17 writes CRLF, which hid the
    ``Installers:\n`` block from the 0.2.4 run."""
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    new = _add_arm64_lf(text)
    if new is not None and crlf:
        new = new.replace("\n", "\r\n")
    return new


def _add_arm64_lf(text: str) -> str | None:
    if re.search(r"^- Architecture: arm64$", text, re.M):
        return None
    head, sep, rest = text.partition("Installers:\n")
    if not sep:
        raise ValueError("no Installers: block")
    # The installer list runs until the next top-level key.
    m = re.search(r"^[A-Za-z]", rest, re.M)
    body, tail = (rest[: m.start()], rest[m.start():]) if m else (rest, "")
    entries = re.split(r"(?m)^(?=- Architecture: )", body)
    x64 = [e for e in entries if e.startswith("- Architecture: x64")]
    if not x64:
        return None
    arm = [e.replace("- Architecture: x64", "- Architecture: arm64", 1) for e in x64]
    return head + sep + body + "".join(arm) + tail


def main() -> int:
    version = sys.argv[1].lstrip("v") if len(sys.argv) > 1 else ""
    if not version:
        print("usage: winget_add_arm64.py <version>", file=sys.stderr)
        return 2
    prefix = f"{PKG}-{version}-"
    branches = gh(f"repos/{FORK}/branches", "--paginate", "--jq", ".[].name").split()
    candidates = [b for b in branches if b.startswith(prefix)]
    if not candidates:
        print(f"no {prefix}* branch in {FORK} — nothing to patch", file=sys.stderr)
        return 1
    path = MANIFEST.format(v=version)
    for branch in candidates:  # usually one; a re-run may leave a second
        meta = json.loads(gh(f"repos/{FORK}/contents/{path}?ref={branch}"))
        text = base64.b64decode(meta["content"]).decode()
        new = add_arm64(text)
        if new is None:
            print(f"{branch}: arm64 already present (or no x64 to copy) — skipped")
            continue
        gh(
            "-X", "PUT", f"repos/{FORK}/contents/{path}",
            "-f", "message=Add arm64 installers (one universal x64+arm64 Inno setup)",
            "-f", "content=" + base64.b64encode(new.encode()).decode(),
            "-f", f"sha={meta['sha']}",
            "-f", f"branch={branch}",
        )
        print(f"{branch}: added arm64 entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())

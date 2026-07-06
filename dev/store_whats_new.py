#!/usr/bin/env python3
"""Extract a release's CHANGELOG block as Microsoft Store "What's new" text.

Usage: python dev/store_whats_new.py 0.1.8 [--changelog CHANGELOG.md]

Prints plain text to stdout: one SHORT "• " line per changelog entry —
just each bullet's bold lead ("• Tab works on the sign-in form"), not
its prose; the Store field wants a glance-able list, the changelog keeps
the detail. Curated for the STORE audience:

- Bullets about platforms the Store build isn't (macOS / Linux packaging)
  are dropped, unless the bullet also says Windows. The MSIX user never
  sees an AppImage.
- Markdown emphasis is stripped (the Store field is plain text).
- Output is capped to STORE_LIMIT chars on a bullet boundary — Partner
  Center rejects over-length notes, and a truncated mid-sentence note
  reads broken.

Used by .github/workflows/msstore.yml to push fresh notes with each
auto-submission (ops#3 — `msstore publish` alone carries forward the
previous submission's stale notes). Exit 1 if the version block is
missing so CI fails loudly instead of shipping empty notes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Partner Center's "What's new in this version" hard cap.
STORE_LIMIT = 1500

# A bullet mentioning another platform's packaging/desktop is not for this
# store's listing — unless it also mentions the store's own platform
# (e.g. "on Windows and macOS" stays everywhere). One profile per store;
# the MAS auto-submit (ops#2) reuses this with --platform mac.
_PROFILES = {
    "windows": {
        "drop": re.compile(
            r"\b(macos|mac app store|\.dmg\b|appimage|aur\b|\.deb\b|flatpak|"
            r"linux|kde|plasma|wayland|pipx|homebrew)\b",
            re.IGNORECASE,
        ),
        "keep": re.compile(r"\bwindows|microsoft store|msix|winget\b", re.IGNORECASE),
    },
    "mac": {
        "drop": re.compile(
            r"\b(windows|microsoft store|msix|winget|\.exe\b|wasapi|"
            r"appimage|aur\b|\.deb\b|flatpak|linux|kde|plasma|wayland|pipx)\b",
            re.IGNORECASE,
        ),
        "keep": re.compile(r"\bmacos|mac app store|\.dmg\b|airplay\b", re.IGNORECASE),
    },
}


def version_block(changelog: str, version: str) -> str | None:
    """The body of the `## [version]` section, or None if absent."""
    m = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else None


def bullets(block: str) -> list[str]:
    """Top-level `- ` bullets with their continuation lines joined."""
    items: list[str] = []
    for line in block.splitlines():
        if re.match(r"^- ", line):
            items.append(line[2:].strip())
        elif items and re.match(r"^\s+\S", line) and not line.lstrip().startswith("- "):
            items[-1] += " " + line.strip()
    return items


def plain(text: str) -> str:
    """Strip markdown emphasis/links; the Store field is plain text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def relevant(bullet: str, platform: str) -> bool:
    prof = _PROFILES[platform]
    return bool(prof["keep"].search(bullet)) or not prof["drop"].search(bullet)


# Catch-all bullet titles (grab-bags of unrelated fixes) — their first
# sentence covers only one item, which misleads. Skip; the changelog keeps them.
_GENERIC_TITLES = re.compile(r"^(small stuff|small polish|fixes|misc\w*)\W*$", re.IGNORECASE)

# Per-line budget for title + one prose sentence. A line that would blow
# past this stays title-only — "a sentence or two, never a paragraph".
MAX_LINE = 300


def title_line(bullet: str) -> str | None:
    """The bullet's Store line: bold title + first prose sentence.

    Changelog entries open with a `**Title.**` lead followed by prose.
    The Store's "What's new" wants that title plus at most one sentence
    of the slightly-more-technical detail (august, 2026-07-06 — full
    bullets read like paragraphs there, bare titles were too thin).
    Bullets without a bold lead fall back to their first sentence;
    generic catch-alls are skipped entirely.
    """
    m = re.match(r"\*\*(.+?)\*\*\s*(.*)", bullet, re.DOTALL)
    if m:
        title, rest = plain(m.group(1)), plain(m.group(2))
    else:
        parts = re.split(r"(?<=[.!?]) ", plain(bullet), maxsplit=1)
        title, rest = parts[0], parts[1] if len(parts) > 1 else ""
    title = title.strip()
    if not title or _GENERIC_TITLES.match(title.rstrip(".!")):
        return None
    if not title.endswith((".", "!", "?")):
        title += "."
    detail = re.split(r"(?<=[.!?]) ", rest, maxsplit=1)[0].strip() if rest else ""
    if detail and len(title) + len(detail) + 1 <= MAX_LINE:
        return f"{title} {detail}"
    return title


def whats_new(changelog: str, version: str, platform: str = "windows") -> str | None:
    block = version_block(changelog, version)
    if block is None:
        return None
    titles = (title_line(b) for b in bullets(block) if relevant(plain(b), platform))
    lines = [f"• {t}" for t in titles if t]
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + (1 if out else 0) > STORE_LIMIT:
            break  # cap on a bullet boundary, never mid-sentence
        out.append(line)
        used += len(line) + (1 if len(out) > 1 else 0)
    return "\n".join(out) if out else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("version", help="release version, e.g. 0.1.8 (no leading v)")
    ap.add_argument("--changelog", default="CHANGELOG.md", type=Path)
    ap.add_argument("--platform", default="windows", choices=sorted(_PROFILES))
    # Write the file ourselves: piping stdout through PowerShell's
    # Set-Content re-encodes via the console codepage and mangles •/— on
    # the Windows runners (seen live on the 0.1.8 run).
    ap.add_argument("--out", type=Path, help="write UTF-8 to this file instead of stdout")
    args = ap.parse_args()

    text = whats_new(
        args.changelog.read_text(encoding="utf-8"), args.version, args.platform
    )
    if not text:
        print(
            f"error: no usable [{args.version}] block in {args.changelog}",
            file=sys.stderr,
        )
        return 1
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

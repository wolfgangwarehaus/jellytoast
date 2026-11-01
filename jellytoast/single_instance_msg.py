"""The single-instance launch message — Qt-free, so the light launcher
(``jellytoast.launch``) can send it without importing PySide6."""

from __future__ import annotations

import os


def launch_message() -> bytes:
    """What a second launch sends: ``raise`` plus, on Wayland, the
    activation token its launcher (the Plasma Mobile / Phosh home screen,
    a taskbar) handed it. Wayland only lets a window come forward with
    such a token — without it the running window stayed behind and the
    launcher's "starting…" spinner ran until it gave up."""
    token = os.environ.get("XDG_ACTIVATION_TOKEN", "").strip()
    return b"raise" + ((" " + token).encode("utf-8", "replace") if token else b"")


def parse_launch_message(data: bytes) -> str:
    """The activation token in a launch message ("" if none)."""
    text = bytes(data or b"").decode("utf-8", "replace").strip()
    if not text.startswith("raise"):
        return ""
    return text[len("raise") :].strip()

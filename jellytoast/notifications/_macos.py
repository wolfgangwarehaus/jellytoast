"""macOS desktop notifications — Notification Center banner via ``osascript``.

Mirrors the Linux backend's shell-out approach (``notify-send``): posts a
banner through ``osascript -e 'display notification …'``. The title/body are
passed as AppleScript *run arguments* — never interpolated into the script
source — so arbitrary track/album text can neither break nor inject into the
script. Best-effort by the module contract (never raises). On a signed
``.app`` the banner is attributed to jellytoast.

``icon`` and ``tag`` aren't expressible through this API (Notification Center
groups by app on its own); they're accepted-and-ignored for signature parity
with the other backends.
"""

from __future__ import annotations

import shutil
import subprocess


def is_supported() -> bool:
    return shutil.which("osascript") is not None


def notify(
    title: str,
    body: str = "",
    icon: str | None = None,
    app_name: str = "jellytoast",
    tag: str | None = None,
) -> None:
    osa = shutil.which("osascript")
    if not osa:
        return
    try:
        subprocess.run(
            [
                osa,
                "-e", "on run {t, b}",
                "-e", "display notification b with title t",
                "-e", "end run",
                str(title or app_name),
                str(body or ""),
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass

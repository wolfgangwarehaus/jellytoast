"""Public demo servers for the "Try a demo" first-run affordance.

A new user with no server of their own can explore jellytoast against a public,
read-only demo. The login screen's demo button fills the normal login form for
the selected server type and submits through the ORDINARY auth path — no
special-case playback code, so Jellyfin and Subsonic behave identically (the
provider-parity rule).

These are THIRD-PARTY servers run by the Navidrome / Jellyfin projects, NOT by
jellytoast. They can go offline, rate-limit, or rotate credentials at any time —
so the target is *data*, correctable in a point release, and an operator can
repoint it WITHOUT a rebuild via the ``JT_DEMO_SERVERS`` override. The Navidrome
demo explicitly invites client testing ("works with all Subsonic clients"); the
Jellyfin demo is the project's public evaluation server (the ``demo`` account is
passwordless).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DemoServer:
    """One public demo target. ``password`` may be empty (the Jellyfin demo's
    ``demo`` account is passwordless)."""

    kind: str  # provider_kind: "jellyfin" | "subsonic"
    label: str  # human label for the source ("Navidrome demo")
    url: str
    username: str
    password: str


# Shipped defaults. One per provider kind so the login screen's existing
# server-type picker doubles as the demo selector. Order within a kind = the
# preferred target.
_DEFAULT_DEMOS: tuple[DemoServer, ...] = (
    DemoServer(
        kind="subsonic",
        label="Navidrome demo",
        url="https://demo.navidrome.org",
        username="demo",
        password="demo",
    ),
    DemoServer(
        kind="jellyfin",
        label="Jellyfin demo",
        url="https://demo.jellyfin.org/stable",
        username="demo",
        password="",  # passwordless public account
    ),
)


def get_demo_servers() -> tuple[DemoServer, ...]:
    """The demo servers to offer.

    Defaults to the shipped list. An operator can repoint the demos WITHOUT a
    rebuild via the ``JT_DEMO_SERVERS`` env var — a JSON array of objects with
    ``kind``/``url`` (required) and optional ``label``/``username``/``password``.
    A malformed or empty override falls back to the defaults: a bad value must
    never break the login screen.
    """
    raw = os.environ.get("JT_DEMO_SERVERS")
    if not raw:
        return _DEFAULT_DEMOS
    try:
        items = json.loads(raw)
        parsed = tuple(
            DemoServer(
                kind=str(d["kind"]).lower(),
                label=str(d.get("label", d["kind"])),
                url=str(d["url"]),
                username=str(d.get("username", "")),
                password=str(d.get("password", "")),
            )
            for d in items
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("Ignoring malformed JT_DEMO_SERVERS override: %s", exc)
        return _DEFAULT_DEMOS
    return parsed or _DEFAULT_DEMOS


def demo_for_kind(kind: str) -> DemoServer | None:
    """The first demo server matching a provider kind, or ``None`` if there is
    no demo for that kind."""
    kind = (kind or "").lower()
    return next((d for d in get_demo_servers() if d.kind == kind), None)

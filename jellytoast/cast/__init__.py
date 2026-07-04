"""Cast backends — one submodule per protocol family.

The legacy entry points (``jellytoast.cast_manager`` and
``jellytoast.cast_proxy``) still live at package-top level; this package
is the home for newer protocol-specific backends so the manager file
doesn't keep growing.

Members:

- ``dlna`` — DLNA / UPnP-AV control point via async-upnp-client (SSDP +
  AVTransport + RenderingControl). Lazy-imports the dep; dormant when
  not installed. See docs/research/casting_dlna.md.
- ``sonos`` — native Sonos via soco (UPnP/SOAP). Fully wired into
  cast_manager + the cast dialog behind an off-by-default Settings
  toggle; still unverified against real hardware (the ⓘ in Settings
  says so). SonosEventBridge ships unit-tested but unwired (see its
  docstring). Design history: docs/research/casting_sonos.md (git
  history).

No re-exports — import ``jellytoast.cast.<protocol>.<symbol>`` directly.
"""

__all__ = ["dlna", "sonos"]

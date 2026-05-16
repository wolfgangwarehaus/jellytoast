"""Cast backends — one submodule per protocol family.

The legacy entry points (``modules.cast_manager`` and
``modules.cast_proxy``) still live at modules-top level; this package
is the home for newer protocol-specific backends so the manager file
doesn't keep growing.

Members:

- ``dlna`` — DLNA / UPnP-AV control point via async-upnp-client (SSDP +
  AVTransport + RenderingControl). Lazy-imports the dep; dormant when
  not installed. See docs/research/casting_dlna.md.
- ``sonos`` — native Sonos via soco (UPnP/SOAP). Backend only;
  cast_manager wiring + cast-dialog UI ship separately. Untested
  against real hardware on first cut; see docs/research/casting_sonos.md.

No re-exports — import ``modules.cast.<protocol>.<symbol>`` directly.
"""

__all__ = ["dlna", "sonos"]

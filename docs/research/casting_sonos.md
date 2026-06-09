# Sonos casting — design research

> **📍 Status — 2026-05-20:** Shipped. The Sonos backend landed
> 2026-05-17 and discovery was wired into the cast dialog on
> 2026-05-20. Still untested against real hardware. This is the
> original design doc, kept for rationale — see `docs/SPEC.md` §4 and
> `CHANGELOG.md` for as-built behavior.

*Original planning context (2026-05-15, pre-build — see the Shipped banner above): slotted post-Phase-5 offline UI, after EQ + smart playlists, before the v2 packaging push.*

## 1. Goal & non-goals

**Goal.** Native Sonos cast support — discover the player(s) on the LAN, render the jellytoast queue through the existing `cast_proxy`, control transport + volume per zone, and **respect the user's Sonos group topology** (no clobbering a kitchen+patio group with a single-room push). Same provider-parity contract as every other cast surface: works identically against Jellyfin and Subsonic, no per-provider branching outside the proxy.

**Non-goals (v1).**

- The Sonos cloud control plane (`api.ws.sonos.com`, OAuth, the SMAPI service-publish flow). soco talks UPnP only — that's the moat we keep.
- Sonos "Favorites" / saved radio stations / queue-edit (re-order on the device side). We push our queue track-by-track; the speaker holds one item at a time.
- Reading the user's Sonos library or any third-party music service the speaker is logged into.
- Building our own UPnP/DLNA generic-renderer client (separate epic — `modules/cast/dlna.py`, P2). Sonos's UPnP layer ships proprietary `urn:schemas-rinconnetworks-com:*` services jellytoast wires straight into; a generic-renderer client would handle no-Sonos UPnP speakers (WiiM, Bluesound, Naim).
- iOS / mobile. Desktop-first; the architecture ports cleanly when a Mac shows up.
- Bit-perfect anything. Sonos resamples internally; see §7.

## 2. Why ship this when AirPlay 2 already exists

jellytoast already speaks AirPlay 2 (pyatv 0.17, see `modules/airplay2.py`). Newer Sonos hardware (post-2018 S2 lineup — One Gen 2, Beam, Arc, Era 100/300, Move, Roam, Five, Sub Gen 3) accepts AirPlay 2, so why bother with native?

| Reason | Sonos native | AirPlay 2 to Sonos |
|---|---|---|
| Older speakers (Play:1 Gen 1, Play:5 Gen 1, Connect, ZP100, ZP120, Connect:Amp pre-2015, S1-locked installs) | works | unsupported on device |
| Latency | LAN UPnP push; ~150 ms start-of-play | RAOP encrypted relay; ~1.5–3 s on cold start |
| Group control | native zone topology — group/ungroup at protocol layer | the Sonos app owns grouping; AirPlay sees individuals only |
| Volume per speaker in a group | yes (`RenderingControl` per UUID) | only group-master volume |
| Connection cost while idle | none (push when needed) | encrypted RTSP session, drains laptop battery if held open |
| Crossfade across the queue | native (`Sonos.cross_fade = True`) | not exposed via AirPlay |
| Multi-room across mixed S1/S2 | works on SonosNet | AirPlay 2 hops are individual |

Net: native Sonos is the **only** path for S1 households (Play:1 Gen 1 + Play:5 Gen 1 are still in active use — Sonos disabled S1 cloud TTS in 2024 but the local UPnP API still works), the **better** path on responsiveness and battery for everyone else, and the **right** path for users who actually care about Sonos's group model.

**Recommendation: ship it.** The library is mature, the protocol is stable, the user value is concrete (S1 owners + responsiveness), and it slots cleanly behind the existing cast-device abstraction. AirPlay 2 stays as the fallback when discovery turns up zero Sonos zones.

## 3. Sonos protocol primer

Sonos speakers are UPnP/SOAP under the hood with proprietary service extensions:

- **Discovery.** Standard SSDP M-SEARCH on `239.255.255.250:1900`:

  ```
  M-SEARCH * HTTP/1.1
  HOST: 239.255.255.250:1900
  MAN: "ssdp:discover"
  MX: 1
  ST: urn:schemas-upnp-org:device:ZonePlayer:1
  ```

  Same multicast firewall concern as every other LAN-discovery surface — the user's host firewall must let UDP 1900 through. soco's `discover()` does the M-SEARCH and parses the `LOCATION:` header to find each `device_description.xml`.

- **Zone topology.** Each player serves `/status/topology` and `/xml/ZoneGroupTopology1.xml`. Hitting *any one* player gives you the group graph for the entire household (coordinator UUID per group, all members, satellite/bond relationships, "invisible" entries for bonded subs and surrounds). One responding IP → full picture.

- **Coordinator vs member.** A "group" is one *coordinator* + zero-or-more *members*. The coordinator owns transport (play/pause/seek/queue). Members mirror its audio + offer per-speaker volume. Sending `play_uri` to a member silently fails — you must resolve to the coordinator. soco's `Group.coordinator` gives you the right `SoCo` instance.

- **Bonded sets.** A soundbar + sub + two surrounds present as one zone with one IP. Internally the bond binding is over SonosNet (Sonos's mesh on 5 GHz). We treat the bonded set as a single endpoint; `discover()` skips the satellites.

- **S1 vs S2.** Two parallel firmware lines since 2020. S1 ships on the original 2005–2014 hardware (ZP100/120, Play:5 Gen 1, Connect Gen 1, Bridge). S2 ships on 2015+. The **UPnP control surface is identical** — both run a ZonePlayer:1 service, both speak the same `MediaRenderer:1` SOAP. soco transparently handles both. The only divergence we care about: S1 and S2 households can coexist on a LAN but cannot **mutually group** — soco's topology cleanly separates them, but our zone picker must label them so a user doesn't expect to bridge.

- **The cloud.** Sonos has an OAuth-protected cloud control plane (sonos.com → SMAPI), used by the Sonos app for non-local services, voice assistants, and remote control over the internet. soco ignores it entirely. Sonos has periodically threatened to deprecate parts of the local SOAP API since the S2 split (the [2024 S1 cloud-services cut](https://en.community.sonos.com/controllers-and-music-services-228995/s1-vs-s2-latest-situation-6932174) was the most visible move). As of 2026-05, the local UPnP/SOAP surface is **still required** — Home Assistant's Sonos integration uses it, soco's monthly release cadence depends on it ([v0.31.0 in April 2025](https://github.com/SoCo/SoCo/releases)). Risk is real but not imminent.

## 4. Library survey

| Library | Maintained? | License | Last release | Notes |
|---|---|---|---|---|
| **soco** | yes — monthly cadence | MIT | [v0.31.0 (Apr 2025)](https://github.com/SoCo/SoCo/releases) | The canonical pure-Python Sonos library. Used by Home Assistant indirectly, by soco-cli, and by a stack of hobby projects. Sync API; one short-lived requests session per call. |
| pysonos | archived (2022) | MIT | 0.0.54 | Old Home Assistant fork from 2019. Upstream README now points to soco. Skip. |
| node-sonos, sonos-ts | active, JS only | MIT | — | Reference for protocol details; not a Python option. |
| soco-cli | active | MIT | [Apr 2026](https://pypi.org/project/soco-cli/) | Built on top of soco; useful for manual testing during development. Not a library dep. |

**Pick: soco.** No serious competition. Sync + blocking, which fits jellytoast's `modules.async_io.run_async` worker-thread model perfectly — same pattern as `pychromecast` (sync) and the `pyatv` `*_sync` wrappers in `modules/airplay2.py`. We do **not** use soco's `events_asyncio` mode; sticking to the sync events module lets us run the listener on the existing worker thread.

The one async story worth flagging is `soco.events_asyncio` — an asyncio-event subscription module. It exists, it's the path Home Assistant takes, and it's where the long-running connection bugs live ([SoCo issue #822](https://github.com/SoCo/SoCo/issues/822)). We avoid it by using the sync events module with renewal driven by a Qt timer (§9).

## 5. Architecture

```
+--------------------+
|  jellytoast UI     |
|  cast dialog,      |
|  now-playing bar   |
+----------+---------+
           |
+----------v----------+        +-----------------------+
| modules/cast_manager|<-------|  PlayerBus            |
+----+-------+--------+        |  cast_started,        |
     |       |                 |  cast_stopped,        |
     |       |                 |  cast_devices_updated |
     |       |                 +-----------------------+
     |       |
+----v---+ +-v-------+ +------------+ +-------------------+
| chrome | | airplay | | sonos      | | dlna (future)     |
| cast   | | 2 (pyatv| | (soco)     | |                   |
| (pych) | | _sync)  | | NEW v1     | |                   |
+----+---+ +----+----+ +------+-----+ +---------+---------+
     |          |              |                |
     +----------+--------------+----------------+
                          |
                          v
                +------------------+
                | modules/         |
                | cast_proxy.py    |  (token registry, Range, /file://)
                +------------------+
                          ^
                          |  HTTP GET /s/<token>
                          |
                 +--------+--------+
                 |  Sonos device   |
                 |  on LAN         |
                 +-----------------+
```

New module: **`modules/cast/sonos.py`** (creates the `modules/cast/` package — first occupant; existing `cast_manager.py` and `cast_proxy.py` stay at module-top for now, mirror the `modules/scrobble/` layout from the scrobbling subsystem). Files in v1:

- `modules/cast/__init__.py` — empty package marker.
- `modules/cast/sonos.py` — discovery, transport, zone helpers.
- `modules/cast/_sonos_events.py` — Qt-friendly event-subscription wrapper (renewal timer + auto-resubscribe).
- `tests/test_sonos_cast.py` — unit tests with mocked SOAP.

`cast_manager.py` grows a `_discover_sonos()` branch parallel to its existing `_discover_chromecasts()` and `_discover_airplay()` paths, emits `cast_devices_updated` with the merged list, and routes "start cast" calls through `cast/sonos.py:cast_to_sonos(zone, now_playing)`.

## 6. Control surface

The minimum jellytoast needs (all sync, all soco):

```python
# Discovery — blocking, ~1 s for the multicast wait
from soco.discovery import discover
zones = discover(timeout=1.0)  # set[SoCo] — one entry per group coordinator

# Topology — expand into all groups, including members
hh = next(iter(zones)).all_zones  # set[SoCo] — every visible player
groups = next(iter(zones)).all_groups  # set[ZoneGroup]
for g in groups:
    coord = g.coordinator           # SoCo
    members = g.members             # set[SoCo]
    label = g.label                 # "Kitchen + Patio"

# Push our queue (one URL at a time — the cast_proxy URL)
coord.play_uri(proxy_url, meta=didl_xml, title=track.title)

# Transport
coord.pause();   coord.play();   coord.stop()
coord.seek("0:01:23")            # HH:MM:SS string
coord.next();    coord.previous()

# Volume (group-volume preferred, per-member also available)
g.volume = 35                    # group master fader
member.volume = 50               # individual speaker

# Grouping (additive — keep the user's existing group intact)
target.join(coord)               # add target to coord's group
target.unjoin()                  # remove target from any group

# Crossfade
coord.cross_fade = True

# Events — RenderingControl + AVTransport carry transport state + volume
from soco.events import event_listener
sub = coord.avTransport.subscribe(auto_renew=True)
while True:
    evt = sub.events.get(timeout=0.5)  # blocking-with-timeout
    # evt.variables: {"transport_state": "PLAYING", "current_track_uri": "...", ...}
```

Three things we wrap, not pass through:

1. **Queueing.** soco's `add_uri_to_queue` works but Sonos's queue is opaque-to-us state we'd then need to reconcile with jellytoast's `QueueManager`. v1 stays single-track-push: on `track_changed` we call `play_uri` with the next track's proxy URL. Latency between tracks is ~300 ms — observable but acceptable for v1. Gapless across the Sonos handoff lands in v2 when we use `add_uri_to_queue` + `play_from_queue(index)`.
2. **DIDL-Lite metadata.** soco builds a default envelope from `(title, creator, album_art_uri)` but Sonos cares deeply about XML namespace correctness — bad XML = silent failure, no exception. Reuse soco's `DidlMusicTrack` helper rather than hand-rolling:

   ```xml
   <DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
              xmlns:dc="http://purl.org/dc/elements/1.1/"
              xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
     <item id="-1" parentID="-1" restricted="true">
       <dc:title>Track</dc:title>
       <dc:creator>Artist</dc:creator>
       <upnp:album>Album</upnp:album>
       <upnp:albumArtURI>http://lanip:8943/s/&lt;tok&gt;</upnp:albumArtURI>
       <upnp:class>object.item.audioItem.musicTrack</upnp:class>
     </item>
   </DIDL-Lite>
   ```

   Cover-art URI **must** be reachable from the Sonos device — same cast-proxy hop the stream goes through (§7). Sonos fetches the cover; the client never pushes the bytes.

3. **Events.** soco's sync events module spawns its own HTTP listener on a random port to receive UPnP NOTIFY callbacks. That port must be reachable from the speaker → another LAN-only assumption (same as the proxy). The `auto_renew=True` flag uses a 30-minute subscription that re-up's at ~25 minutes. **Renewal failures are the #1 reported soco bug** — wrap subscribe in a try/except in `_sonos_events.py`, drop and re-subscribe on `SoCoException`. See §10.

## 7. Cast-proxy interaction

Every Sonos cast goes through `modules/cast_proxy.py` (`architecture_cast_proxy.md` memory). Same flow as Chromecast and AirPlay:

```python
proxy_url = cast_proxy.resolve_cast_url(server_stream_url)  # already exists
sonos_coord.play_uri(proxy_url, meta=didl_for(track), title=track.title)
```

Three things to verify, none of which require new proxy code:

- **Range requests.** Sonos seeks via HTTP `Range: bytes=N-`; cast_proxy already forwards Range both ways (`_PASS_RESPONSE_HEADERS` includes `Content-Range` and `Accept-Ranges`). Confirmed working with Chromecast; should be identical here.
- **Cover-art URL.** Build a separate proxy token for the cover image (`/Items/{id}/Images/Primary` on Jellyfin, `getCoverArt` on Subsonic) and put it in the DIDL `albumArtURI`. The speaker hits the proxy for the JPG; the proxy hits the server; no different from how the speaker pulls the stream.
- **`file://` downloaded tracks.** The `_serve_local_file` branch in `cast_proxy.py:191` already serves Range over disk blobs — Sonos can play downloaded files when offline / on flaky upstream, same as Chromecast.

UFW rule (`8943/tcp`) is unchanged. If Sonos is on a different VLAN with mDNS reflector but no inter-VLAN routing, the speaker can't reach the proxy and casting silently 404s — log this conspicuously when the speaker's `RenderingControl` event never arrives within 5 s of `play_uri`.

## 8. Settings

| Key | Type | Default | Notes |
|---|---|---|---|
| `cast/sonos_enabled` | bool | `True` | Master toggle. Off = skip the M-SEARCH at startup. Settings → Playback → "Discover Sonos speakers". |
| `cast/sonos_preferred_zone` | str (UUID) | `""` | Last-used coordinator. If set + present, cast dialog opens with it pre-selected. |
| `cast/sonos_group_with_master` | bool | `True` | When casting to zone B and zone A is already playing jellytoast, *join* B to A instead of fragmenting. Off = treat each cast as a fresh zone. |
| `cast/sonos_event_port` | int | `0` | UPnP NOTIFY listener port; `0` = ephemeral. Setting non-zero matters if user has a tight egress firewall — same one-line ufw rule as the proxy. |
| `cast/sonos_volume_floor` | int (0–50) | `15` | First push to a zone sets volume to `max(current, floor)` so a user who hit "Cast" doesn't get blasted by 3 AM Sonos defaults. |

Follows the `playback/cast_stream_routing` pattern in `modules/settings.py`. No new auth secret — Sonos local API is unauthenticated by design (same LAN = trust model).

## 9. Signals

The existing PlayerBus signals are sufficient — Sonos is one more cast backend, the bus already has:

```python
cast_started = Signal(str)              # device_name; used by player_backend
cast_stopped = Signal()                 # used by player_backend, now_playing_bar
cast_devices_updated = Signal(list)     # used by cast dialog
```

Two additive signals we *should* add for Sonos-specific UX, but only at the UI slice (§12), not v1:

- `cast_group_changed = Signal(str, list)` — `(coordinator_uuid, member_uuids)`. Drives the "Kitchen + Patio" label in now-playing bar when the user re-groups from the Sonos app mid-listen.
- `cast_volume_per_member = Signal(str, int)` — `(member_uuid, 0–100)`. Drives the per-speaker volume sliders in the Sonos-specific cast dialog tab.

v1 keeps `cast_started`/`cast_stopped` exclusively; group-label changes show up at the next discovery tick (5 s default, see `cast_manager._refresh_devices`).

## 10. Edge cases / gotchas

- **Coordinator goes offline mid-playback.** Group elects a new coordinator (Sonos handles this internally — typically within 3 s). Our `avTransport` subscription on the old coordinator dies; reconnect logic must re-discover and re-subscribe to the new coordinator UUID. soco's `Subscription.unsubscribe()` raises if the device is gone; swallow that exception path.
- **Subscription renewal fails after laptop sleep / network change.** [SoCo #822](https://github.com/SoCo/SoCo/issues/822) is the canonical bug. Wrap the subscribe in `_sonos_events.py` with a watchdog Qt timer: if no event in 45 minutes (sub is 30 min × 1.5), force-unsubscribe + re-subscribe.
- **`play_uri` on a member.** Silently 404s. Always resolve to `group.coordinator` first. Add a `cast_to_sonos(player_or_group)` helper that does the resolution.
- **Bonded sub volume.** The sub is a member of the soundbar's bond, not a separate volume target. Don't expose it as a slider; user adjusts in Sonos app.
- **Mixed S1/S2 households.** discover() returns both; treat them as separate sets in the picker, labeled "S1 (older)" / "S2". A future v2 might warn before grouping cross-firmware (which silently degrades).
- **Sonos `favorites` and `getQueue`.** Out of scope — we drive the queue from jellytoast, not from the speaker's saved state. Document this in the cast dialog ("Sonos forgets your jellytoast queue when you cast from the Sonos app").
- **AirPlay-2-capable Sonos.** When discovery returns the *same* device on both protocols (the speaker advertises both), default to native — better latency, group control, grouping. Settings toggle "Prefer AirPlay 2 for Sonos" for the user who likes their AirPlay multi-room habits.
- **DSD / 24-192 hi-res.** Sonos resamples everything above 24/48 — and silently *skips* 24/192 files on the music library scanner. For our stream-push case the server transcodes upstream (default flow), so the speaker sees 16-bit 44.1 FLAC and is happy. Surface a "casting to Sonos = no bit-perfect, no 24/96+" caption in the cast dialog (same style as the EQ disclosure).
- **Crossfade across queue.** `coord.cross_fade = True` lasts the session. Persist user preference per zone? v2. v1 honors the global `playback/crossfade` setting.
- **Local re-import scoping.** soco imports inside the cast module top-level only; never nested inside a function (per the `feedback_local_reimport_scoping.md` rule).
- **Provider singleton refresh.** Cast adapters cache a provider reference for the stream URL. On sign-out / kind-switch, `cast_manager.reset()` must drop any cached Sonos instances so the next cast resolves URLs against the new provider.
- **The "Sonos Connect:Amp at 100% volume" footgun.** Defer to `cast/sonos_volume_floor` (§8); first push sets `max(current, floor)`.

## 11. Test plan

soco does not ship a `MockController`. The strategy is to mock the SOAP transport:

- **Unit:** patch `soco.SoCo._send_command` / `soco.services.Service.send_command` to return canned XML envelopes captured from a real speaker via `soco_cli --debug` (we have a Sonos One Gen 2 in august's setup — confirmed via the hardware roadmap memory; if false, the Windows laptop pairing test stays the only avenue). Verify: discover-parse → coordinator selection → `play_uri` call shape → DIDL XML namespaces → event-variable unpack.
- **Integration (manual, on real hardware):**
  1. Cast a single track from Jellyfin → plays, cover art shows.
  2. Cast a single track from Subsonic → plays, cover art shows.
  3. Pause / resume / seek from jellytoast — speaker reflects.
  4. Pause from Sonos app — `avTransport` event fires `paused` → `PlayerBus.playback_paused` emits → jellytoast UI updates.
  5. Group K+L from Sonos app; cast to L; verify K joins automatically (volume controls work for both).
  6. Cast downloaded track (offline) → `file://` path through proxy works.
  7. Network blip: unplug LAN cable 10 s, replug → event subscription reconnects.
  8. Long-session: leave casting for ≥1 h → subscription renews (no missed events after the 30-minute mark).
  9. S1-only zone (Play:1 Gen 1, if available) → discover, cast, transport.
  10. AirPlay-2-capable Sonos: confirm native is the default when both protocols discover the same device.

## 12. The autonomous-task slice

What a single autonomous agent can ship without august's hands on the wheel (file paths + the exact PlayerBus interactions):

**Branch:** `auto/sonos-cast-discovery`

**Files added:**

- `modules/cast/__init__.py`
- `modules/cast/sonos.py` — `discover_sonos(timeout)`, `expand_topology(player)`, `cast_to_sonos(zone, np, proxy)`, `stop_sonos(zone)`, `set_volume(zone_or_member, v)`, `join(member, coord)`, `unjoin(member)`. All sync; called from `run_async` workers.
- `modules/cast/_sonos_events.py` — `SonosEventBridge` (QObject) that subscribes to `avTransport` + `renderingControl` on a coordinator and emits PySide signals back to `cast_manager`. Watchdog QTimer for renewal failures.
- `tests/test_sonos_cast.py` — discovery parse, DIDL builder, coordinator resolution, group-join logic, volume floor logic. Mocked SOAP throughout.

**Files modified:**

- `modules/cast_manager.py` — add `_discover_sonos()` parallel to existing discovery branches; merge into `cast_devices_updated`.
- `modules/settings.py` — five new keys (§8) with the standard property+setter pattern.
- `pyproject.toml` — `soco>=0.31,<1` added under `[project.optional-dependencies].cast`.

**Signals touched:** `cast_started`, `cast_stopped`, `cast_devices_updated`. Nothing new on the bus.

**Acceptance:** unit tests green; `pytest -k sonos` passes; `python -c "from modules.cast.sonos import discover_sonos; print(discover_sonos(2))"` returns a non-empty set on a Sonos LAN, an empty set on a Sonos-free LAN, and never raises.

**Launch code (per the launch-code feedback memory):**

```
python jellytoast.py
```

## 13. The UI-with-august slice

What needs august in the loop:

- **Cast dialog Sonos tab.** Existing dialog lists devices flat; Sonos wants a two-level "Zones → Players" tree so the user sees group structure. Tile-style rows with the per-zone volume slider + a "Group with…" button. Match the existing modal chrome (`opaque_menu`, `_OpaqueComboBox`, design tokens).
- **Now-playing bar group label.** When casting to a group, show "Kitchen + Patio" not just "Kitchen". Hook on the v2 `cast_group_changed` signal (§9).
- **Per-member volume.** When the cast dialog is open on a multi-member zone, show one slider per speaker plus the group master. Hook on `cast_volume_per_member`.
- **Settings → Playback "Sonos" section.** Discovery toggle, group-with-master toggle, volume floor slider, "Prefer AirPlay 2 for Sonos" toggle. Same chassis as the existing cast section.
- **Error-state polish.** "Sonos lost the coordinator" toast, "Sonos can't reach proxy" diagnostic toast, "Sonos resamples — bit-perfect off" caption. Wording matters here; august should pass on copy.

## 14. Competitive positioning

Who else in the music-client space has *native* Sonos (not via AirPlay or generic DLNA)?

| Client | Sonos native? | How |
|---|---|---|
| **Symfonium** (Android) | yes | Built-in, group-aware. Reported as the gold standard outside Sonos's own app. ([symfonium.app](https://symfonium.app/android-music-player-sonos-chromecast-dlna/)) |
| **BubbleUPnP** (Android) | yes (via OpenHome) | Renders Sonos as a UPnP endpoint plus their server-side OpenHome bridge. ([bubblesoftapps.com](https://bubblesoftapps.com/bubbleupnp/)) |
| **Plexamp** | no | AirPlay 2 and Chromecast only. |
| **Supersonic** | no | UPnP/DLNA renderer support exists but no Sonos-specific code. |
| **Strawberry** | no | Local + Subsonic + Tidal only; cast is AirPlay/Chromecast on macOS, none on Linux. |
| **Tauon** | no | No cast surface at all (mpv-local only). |
| **Sublime Music** | no | Project archived end-of-maintenance. |
| **Foobar2000-mobile** | no | UPnP renderer support, not Sonos-specific. |
| **Feishin** | no | Web-only; no cast. |
| **Sonos app** | yes (definitionally) | Reference. |

**Positioning gist:** native Sonos puts jellytoast in the **two-app tier on Linux desktop** (BubbleUPnP doesn't ship there). On the broader market, it's the differentiator that Plexamp / Supersonic / Strawberry users have asked for and not gotten. Combined with offline downloads + cast-proxy reachability for VPN'd servers, jellytoast becomes the only Linux-first Subsonic/Jellyfin client that can cast to *every* speaker in a typical household — Chromecast, AirPlay 2, **and** native Sonos. That's the AppStream screenshot.

## 15. Cost register

- **Older Sonos hardware (S1)** has undocumented quirks — ZP100 in particular needs `Service.send_command` retries on cold start, soco issue trackers full of one-offs. Mitigation: log on first-attempt failure, retry once with 500 ms backoff, surface a "Sonos device unreachable" toast on second failure.
- **Group state desync on flaky networks** — soco's topology cache can drift when SonosNet reshuffles. Cheap mitigation: re-query topology on every cast (sub-second cost; one HTTP GET to the coordinator's `/status/topology`).
- **Sonos firmware breakage** — Sonos has historically pushed firmware that broke local-API features without warning (2020 reauth, 2023 SOAP-header strictness). Pin soco to a range, not a floor; surface a "Sonos firmware may be incompatible — update jellytoast" toast on persistent SOAP 500s.
- **The `events_asyncio` temptation.** Don't switch from sync events to the asyncio module to "be modern" — that's where Home Assistant lives, and it's where the renewal bugs cluster. Sync + Qt watchdog timer is the cleaner long-run.
- **UFW double-rule confusion.** The proxy listener is `8943/tcp`; the soco event listener is *another* port (ephemeral by default). Users with strict firewalls will need both holes punched. Document; do not auto-open.
- **Multi-VLAN setups.** If Sonos lives on an IoT VLAN and the laptop on a trusted VLAN, mDNS reflectors handle discovery but proxy + event reach-back fail. Out of scope for v1; surface the diagnostic toast in v2.
- **Hardware availability.** august has confirmed Sonos hardware (one zone). The S1-specific bugs require an S1 speaker we don't have — accept that S1 support is "should work, untested" until a community report.

## 16. Effort + sequencing

| Piece | Size | Notes |
|---|---|---|
| `modules/cast/sonos.py` (discovery, transport, group helpers) | M | soco does most lifting; ~250 LoC of glue. |
| `modules/cast/_sonos_events.py` (sync events + Qt watchdog) | M | Renewal + reconnect is the load-bearing logic. |
| Settings keys + property bindings | S | Five keys, copy-paste pattern. |
| `cast_manager.py` integration | S | One new discovery branch, one new cast-to switch. |
| Unit tests (mocked SOAP) | M | Discovery parse, DIDL build, coordinator resolution, watchdog. |
| Manual-test pass (real hardware) | M | Ten scenarios in §11. |
| **Total v1 (autonomous slice)** | **M-to-L** | ~one to one-and-a-half work-days. |
| UI: zone-picker tree | M | New widget, design-token compliant. |
| UI: group label in now-playing bar | S | Bus signal hook. |
| UI: per-member volume | S | List of sliders in cast dialog. |
| Settings UI panel | S | New "Sonos" section. |
| **Total UI slice** | **M** | ~half a day. |

**Slot in `docs/TODO.md`.** Move "🔉 Sonos / DLNA casting" out of P3 ("Skip unless requested") into P2, paired with a note pointing here.

## 17. Open questions

- Does august have an S1-only speaker accessible for testing? (Memory says one Sonos zone, model unspecified.) If S2-only, document the S1 path as "should-work, community-untested".
- Is the cast dialog tree-vs-flat redesign part of this slice or a separate UI epic? My read: tree is necessary to *render* Sonos correctly; ship a minimal tree in the UI slice rather than waiting on a broader cast-dialog redesign.
- Do we want a "Sonos at startup" discovery toggle, or just discover-on-cast-dialog-open? Cheaper to do on-demand; saves the M-SEARCH cost at launch.

## 18. Sources

- [SoCo on GitHub](https://github.com/SoCo/SoCo) — main repository.
- [SoCo releases](https://github.com/SoCo/SoCo/releases) — v0.31.0 April 2025; satellite-speaker support added.
- [SoCo on PyPI](https://pypi.org/project/soco/) — ~10k weekly downloads (May 2026).
- [soco.discovery source](https://docs.python-soco.com/en/latest/_modules/soco/discovery.html) — M-SEARCH on `urn:schemas-upnp-org:device:ZonePlayer:1`.
- [SoCo events module](https://soco.readthedocs.io/en/latest/api/soco.events.html) — sync event subscription, `auto_renew=True`.
- [SoCo issue #822](https://github.com/SoCo/SoCo/issues/822) — `events_asyncio` long-running subscription renewal failures.
- [soco-cli](https://github.com/avantrec/soco-cli) — useful debugging tool; explicitly local-only ("no support for the Sonos cloud API and no intention to change this").
- [Home Assistant Sonos integration](https://www.home-assistant.io/integrations/sonos/) — UPnP-based, local-API still required (2025).
- [Sonos Music API (SMAPI)](https://docs.sonos.com/docs/smapi) — cloud-side service-publish API; not what we're using.
- [Sonos supported audio formats](https://docs.sonos.com/docs/supported-audio-formats) — FLAC/ALAC up to 24/48, no DSD, 24/192 silently skipped.
- [Sonos community: S1 vs S2 latest situation](https://en.community.sonos.com/controllers-and-music-services-228995/s1-vs-s2-latest-situation-6932174) — 2024 cloud-services cut for S1; local API survives.
- [Sonos AirPlay 2 grouping caveats](https://en.community.sonos.com/components-and-architectural-228996/airplay-2-volume-control-and-grouping-with-older-devices-6814181) — AirPlay 2 doesn't display Sonos groups.
- [Symfonium Sonos/Chromecast/DLNA](https://symfonium.app/android-music-player-sonos-chromecast-dlna/) — competitive reference for the group-aware UI.
- [BubbleUPnP](https://bubblesoftapps.com/bubbleupnp/) — competitive reference for the OpenHome bridge approach.
- Internal: `modules/cast_proxy.py`, `modules/cast_manager.py`, `modules/airplay2.py`, `modules/player_state.py`, `modules/settings.py`.

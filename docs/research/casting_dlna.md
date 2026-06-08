# DLNA / UPnP casting — design research

> **📍 Status — shipped + live-verified (updated 2026-06-08):** the
> DLNA backend landed 2026-05-17 and discovery was wired into the cast
> dialog 2026-05-20; it was then **live-verified against a real LG TV
> 2026-05-28** (`CHANGELOG.md` 2026-05-28; `docs/manual_test_plan.md`
> §5). This is the original design doc, kept for rationale — see
> `docs/SPEC.md` §4 and `CHANGELOG.md` for as-built behavior.

Status: research / pre-build. Last updated 2026-05-15. Slot in `docs/TODO.md`:
parity feature (P2), lands after the offline-UI merge and the EQ/Smart-Playlists
slice. Pairs with an autonomous backend slice plus a UI follow-up.

## 1. Goal & non-goals

**Goal.** Add a third cast target alongside Chromecast (`pychromecast`) and
AirPlay 2 (`pyatv`): a DLNA/UPnP-AV control point that discovers renderers,
pushes a stream URL, drives transport (play/pause/stop/seek/volume), and
reports state back into `PlayerBus`. Same surface, same favourites, same
proxy plumbing — DLNA is just another device row in the cast popup. Closes
the one casting hole reviews ding us for; gives jellytoast Chromecast +
AirPlay + DLNA in one desktop client (nobody in the audit table has all
three — see §14).

**v1.** SSDP M-SEARCH discovery; per-renderer `SetAVTransportURI` → `Play`,
plus `Pause`/`Stop`/`Seek`/`SetVolume`; DIDL-Lite metadata with cover art;
mandatory routing through `modules/cast_proxy.py`; queue advancement on
renderer "stopped"; fake-renderer SOAP test pass.

**Non-goals.** DLNA *server* mode (we cast in, never out); ContentDirectory
browsing; OpenHome / Sonos-group sync (v2 with real spec audit);
DLNA-advertisement listening (M-SEARCH is enough for v1); IPv6 SSDP (most
consumer renderers ignore `FF02::C`); DLNA-side EQ (impossible — renderer
decodes, same caveat as Chromecast in `docs/research/eq_dsp.md` §8).

---

## 2. Protocol primer — what we actually need

DLNA is UPnP-AV with extra interop rules (codec profiles, ProtocolInfo
strings, a few mandatory headers). For a control-point client the spec
surface is identical to plain UPnP-AV:

| Layer | Spec | What jellytoast uses |
|---|---|---|
| Discovery | SSDP over UDP multicast `239.255.255.250:1900` | M-SEARCH; ignore advertisements in v1 |
| Description | HTTP GET on the SSDP `LOCATION` URL | parse device + service XML once per device |
| Control | SOAP over HTTP on each service's controlURL | `AVTransport` + `RenderingControl` |
| Events | GENA — `SUBSCRIBE` + inbound `NOTIFY` callbacks | poll instead in v1 (§4.4) |
| Presentation | HTML page on the device | ignored |

Three UPnP-AV roles: **control point** (us), **renderer** (DMR — the TV /
receiver / speaker), **media server** (DMS — Jellyfin/Subsonic, but we
don't expose its catalogue; we push individual URLs). The two services we
touch are `AVTransport:1` (`SetAVTransportURI`, `Play`, `Pause`, `Stop`,
`Seek`, `GetPositionInfo`, `GetTransportInfo`) and `RenderingControl:1`
(`SetVolume`, `SetMute`). Everything else (`ConnectionManager`, OpenHome
`Playlist`, `RenderingControl:3` presets) is out of scope.

### SSDP M-SEARCH on the wire

```
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
MX: 3
ST: urn:schemas-upnp-org:device:MediaRenderer:1
USER-AGENT: jellytoast/0.x UPnP/1.0 DLNADOC/1.50
```

Send three times at 1 s intervals (renderers drop M-SEARCH on busy Wi-Fi),
collect unicast responses, dedupe by `USN`, fetch each `LOCATION` once.
ST `…MediaRenderer:1` catches everything; `ssdp:all` brings in routers /
printers / NAS shares — too noisy.

---

## 3. Python library — pick `async-upnp-client`

| Library | Status (2026) | Async? | Verdict |
|---|---|---|---|
| **`async-upnp-client`** | Active, Home Assistant's DLNA-DMR backbone. v0.47.0 Apr 2026, ~monthly releases. Apache-2.0. | asyncio | **Pick.** Mature parser (HA's bug tracker is the corpus), high-level `profiles.dlna.DmrDevice` wraps AVTransport + RenderingControl, ships SSDP + GENA. |
| `pyupnpclient` | Last release 2019, last commit 2021 | Sync | Skip. Dead, no DLNA helpers. |
| `nano-dlna` | Active but a *CLI for casting one video*. | Sync | Skip. No state, no volume — fine for proof of concept, useless as a backend. |
| `Cohen3` | Active-ish, primarily a *server* framework | Twisted | Skip. Twisted in a PySide6 app is two reactors. |
| PyPI `dlna` | ~250 weekly downloads, stale | Sync | Skip. Toy. |
| `libupnpp` (upmpdcli) | Active C++ binding | GIL-blocking | Skip. Native dep, packaging headache, no Windows. |

**Justify.** `async-upnp-client` is the only library where (a) the SSDP /
SOAP / GENA parsers have survived a million Home Assistant boxes,
(b) `DmrDevice` takes the SOAP grunt work off our plate, (c) it's still on
roughly-monthly releases in 2026. Add as a soft dep, lazy-import on first
cast-popup open — same pattern as `pychromecast` at
`modules/cast_manager.py:23-32`. Cold import pulls aiohttp + defusedxml +
voluptuous (~150 ms warm), tolerable on the lazy path.

### 3.1 Asyncio in a PySide6 app

jellytoast uses no asyncio today; `modules/async_io.run_async()` (Qt thread
pool) is the convention. Two options to host an asyncio library:

1. **qasync bridge** — install a `QEventLoop` in main, mark cast handlers
   `@asyncSlot()`. Native fit, but rewires the *whole* app's event-loop
   story for one feature.
2. **Owned asyncio loop in `modules/cast/dlna.py`** — `DlnaController`
   singleton owns a `threading.Thread` running an `asyncio` loop, schedules
   coroutines via `asyncio.run_coroutine_threadsafe`, surfaces results via
   `PlayerBus`. Same `run_async`-shaped call site as the rest of the app.

**Pick option 2.** One feature, one private thread, no app-wide rewrite.
Future asyncio features can claim the same loop, or migrate to qasync once
there's more than one customer.

---

## 4. Control surface

### 4.1 Discovery + push

`async_upnp_client.search.async_search(target=ST_MEDIA_RENDERER, timeout=5)`
runs the M-SEARCH, yields each response. `UpnpFactory.async_create_device(location_url)`
fetches description and binds services. Wrap each into our existing
`CastDevice` dataclass at `modules/cast_manager.py:48-59` with
`device_type="dlna"`, drop into `self.dlna_devices`, fire `_notify()`.

```python
async def play(dev: DmrDevice, url: str, didl_xml: str):
    await dev.async_set_transport_uri(url, didl_xml)
    await dev.async_play()
```

The URL is **always** a cast-proxy token URL, never a raw Jellyfin/Subsonic
URL (§5). `DmrDevice` also exposes `async_pause`, `async_stop`,
`async_seek_rel_time`, `async_set_volume_level(0..1)`, `async_set_mute`.
Mirror these to existing `MpvController` cast routes. Volume re-normalised
to our 0..100 internal scale.

### 4.2 State updates + queue advance

**v1 polls** `GetTransportInfo` + `GetPositionInfo` every 1 s — ~2 HTTP/sec,
works on every renderer. GENA event subscriptions need an inbound listener
port, fail under default KDE Wayland firewall configs + Flatpak sandboxes,
and many cheap renderers silently drop subscriptions; libupnp-based devices
hang on disconnect. Defer to v2.

Queue advances when poll sees `CurrentTransportState=STOPPED` *and* we're
inside the last 2 s of `TrackDuration` → `PlayerBus.playback_ended` →
queue picks next → push new URL. The 2 s window guards against the brief
STOPPED reading between `SetAVTransportURI` and `Play` confirm.

---

## 5. Cast-proxy interaction

Non-negotiable. DLNA renderers fetch only URLs they can route to: a TV on
`192.168.1.0/24` will not reach Jellyfin behind `100.64.0.1` (Tailscale);
Bose / Yamaha firmwares with hard-coded TLS roots will not load
self-signed certs. `modules/cast_proxy.py` already solves both. Every
DLNA `SetAVTransportURI` URL is a token URL on `localhost:8943`;
`cast_stream_routing="auto"` carries the same Tailscale / public-domain /
TLS logic Chromecast uses today.

Three pre-existing touchpoints: `CastProxy.publish(url) -> token_url`
(stream), `CastProxy.publish_file(path)` (downloaded blob, cast survives
offline), `CastProxy.revoke(token)` after `Stop`. The proxy's `do_HEAD`
already passes through — verify upstream returns `Content-Type` +
`Content-Length`. Some Samsung firmware does an unauthenticated cover-art
GET first, then Range GET for playback; stateless proxy handles fine,
flag in tests.

**`protocolInfo` mapping.** `<res protocolInfo="http-get:*:<mime>:<DLNA.ORG_PN>;DLNA.ORG_OP=01">`.
The profile name (`MP3`, `LPCM`, `FLAC`, `WAVE`, `AAC_ISO`) is what strict
renderers key on; MIME alone isn't enough. v1 ships those five and omits
`DLNA.ORG_PN` for anything else — modern renderers play happily without it,
the ones that don't trigger the §7 transcode fallback.

---

## 6. DIDL-Lite metadata

The DIDL-Lite XML document is `SetAVTransportURI`'s second argument,
XML-escaped (yes, XML-inside-XML — SOAP re-escapes the DIDL). Renderers
pull title / artist / album / cover from this; skip it and most TVs show
"Unknown", some Sony / Bose models refuse the URI entirely. Minimal
portable shape:

```xml
<DIDL-Lite
    xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"
    xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">
  <item id="jt-1" parentID="0" restricted="1">
    <dc:title>Idioteque</dc:title>
    <dc:creator>Radiohead</dc:creator>
    <upnp:artist>Radiohead</upnp:artist>
    <upnp:album>Kid A</upnp:album>
    <upnp:originalTrackNumber>8</upnp:originalTrackNumber>
    <upnp:albumArtURI dlna:profileID="JPEG_TN">
      http://127.0.0.1:8943/t/COVERTOKEN/cover.jpg
    </upnp:albumArtURI>
    <upnp:class>object.item.audioItem.musicTrack</upnp:class>
    <res protocolInfo="http-get:*:audio/flac:DLNA.ORG_PN=FLAC;DLNA.ORG_OP=01"
         duration="0:05:09.000"
         size="48217600">
      http://127.0.0.1:8943/t/STREAMTOKEN/track.flac
    </res>
  </item>
</DIDL-Lite>
```

### Renderer quirks worth coding for v1

| Quirk | Renderer | Fix |
|---|---|---|
| `upnp:class` mandatory; missing → 714 | Sony Bravia, some Yamaha | Always emit |
| DIDL doc > 4 kB rejected | Bose SoundTouch | Cover URL ≤ 200 chars, one `<res>`, skip `<desc>` |
| Demands `DLNA.ORG_PN` in `protocolInfo` | Older Samsung TVs (≤2018) | Ship the five-MIME mapping; transcode to MP3 if unknown |
| Needs `Content-Length`, not chunked | Older Pioneer / Onkyo | Proxy serves numeric length |
| Crashes on cover URL redirect | Various 2015-era TVs | Resolve final URL before embedding |
| Ignores `Pause`, only `Stop` | Some Yamaha MusicCast | Document; UI pause spinner |
| Drops cover fetch without `DLNADOC` UA | Samsung firmware revs | `User-Agent: jellytoast/<ver> UPnP/1.0 DLNADOC/1.50` |
| Progress stalls mid-track | Yamaha MusicCast (Symfonium #2225) | Fall back to local-clock estimate after >5 s of flat `GetPositionInfo` |

Cover art: Jellyfin's
`<server>/Items/{AlbumId}/Images/Primary?maxWidth=512&quality=90` and
Subsonic's `<server>/rest/getCoverArt?id=...` both go through
`CastProxy.publish` with a short-TTL token — same pattern Chromecast uses.

---

## 7. Audio format compatibility

DLNA renderers are aggressively heterogeneous. Mainstream: **MP3, AAC
(in MP4)**. Adds-most-things: **FLAC, WAV/LPCM, Vorbis**. High-end gear
plays ALAC, Opus, DSD; cheap TVs choke past MP3. jellytoast streams
bit-perfect today, but the renderer decodes — so bit-perfection is moot
once the URI is pushed; the renderer's codec list is what matters.

**v1: trust the file, transcode on `714` retry.**

1. First push: `SetAVTransportURI` with the upstream's native MIME. ~90 %
   of post-2018 renderers cope with MP3/AAC/FLAC.
2. On error 714 (Illegal MIME) or 701 (Transition Not Available) →
   re-publish via the proxy with a server-side transcoded URL (Jellyfin:
   `MaxStreamingBitrate=320000&Container=mp3`; Subsonic:
   `bitrate=320&format=mp3`). Cache the renderer's transcode-required flag
   for the session.
3. Per-renderer pin via `cast/dlna_force_transcode` (§9) if step 2 keeps
   biting.

Don't `GetProtocolInfo` feature-detect — half the firmwares lie and the
responses are often megabytes of comma-separated strings. Heuristic + 714
fallback is what every working DLNA controller does in the wild.

---

## 8. Architecture

```
                       ┌──────────────────────────┐
                       │  CastManager              │
                       │  - chromecast_devices     │
                       │  - airplay_devices        │
                       │  - dlna_devices    ◀── new│
                       └──────────┬───────────────┘
                                  │
       ┌──────────────────────────┼──────────────────────────┐
       ▼                          ▼                          ▼
┌──────────────┐         ┌──────────────┐         ┌────────────────────┐
│ pychromecast │         │ pyatv         │         │ DlnaController ◀── new
│ (threads)    │         │ (AirPlay 2)   │         │ modules/cast/dlna.py
└──────┬───────┘         └──────┬───────┘         │  owns asyncio loop │
       │                        │                  │  wraps async-upnp  │
       │                        │                  └─────────┬──────────┘
       └────────────┬───────────┴─────────────┬──────────────┘
                    ▼                         ▼
          ┌──────────────────┐      ┌──────────────────────┐
          │ MpvController    │─────▶│ modules/cast_proxy.py │
          │  route_track/    │ pub  │  token URL on :8943   │
          │  pause/stop/seek │      └──────────────────────┘
          └──────────────────┘
                    │
                    ▼
            PlayerBus signals — cast_started / cast_stopped /
            cast_devices_updated  (no new signals for v1)
```

**New** `modules/cast/__init__.py` + `modules/cast/dlna.py` — first time
the `cast` sub-package exists; existing cast files stay put, the package
only houses DLNA's small surface for now.

**Touched** `modules/cast_manager.py` (add `dlna_devices` + `discover_dlna()`
into the existing `_notify()` path), `modules/player_backend.py` (new
`device_type == "dlna"` cast-router branches), `modules/settings.py` (three
keys, §9). `modules/cast_proxy.py` itself needs no code change — it's
provider-agnostic.

---

## 9. Settings

| Key | Type | Default | Notes |
|---|---|---|---|
| `cast/dlna_enabled` | bool | `True` | Master toggle. Off skips SSDP entirely (silences multicast on KDE Wayland firewall warnings). |
| `cast/dlna_force_transcode` | str JSON `{uuid: bool}` | `"{}"` | Per-renderer pin for §7's transcode fallback. Persisted on first successful auto-fallback. |
| `cast/dlna_user_agent_overrides` | str JSON `{uuid: ua}` | `"{}"` | Power-user escape hatch for the Samsung-UA quirk. No UI until a user files a bug. |

`cast_stream_routing` (existing, `"auto" | "always" | "never"`) governs proxy
routing for *all* cast targets — DLNA inherits its semantics, no new key needed.

---

## 10. Signals

Existing `PlayerBus.cast_started(str)`, `cast_stopped()`,
`cast_devices_updated(list)` are sufficient — DLNA is just another
`device_type`. A future `cast_transport_state = Signal(str, str)` (device_type,
state) for buffering spinners is worth considering in v2, but the existing
`playback_position` cadence covers the v1 UI need.

---

## 11. Test plan

### Without a real renderer (CI)

A fake DMR speaking just enough SOAP — `aiohttp` server + canned SOAP
bodies + an internal transport-state machine, recording every received
URL + DIDL for assertions. Files:

- `tests/dlna/fake_renderer.py` — the test double (description XML, SCPDs,
  SOAP handler, state machine).
- `tests/dlna/test_discovery.py` — `CastDevice` materialises from mocked
  M-SEARCH response.
- `tests/dlna/test_play_flow.py` — push / pause / resume / stop round-trip;
  asserts proxy registration, DIDL contents, bus signals.
- `tests/dlna/test_didl_quirks.py` — table-driven: Sony class present,
  Bose 4 kB cap, Samsung `DLNA.ORG_PN` mapping, cover URL never redirecting.
- `tests/dlna/test_714_fallback.py` — fake returns 714, retry with
  transcode params, second push succeeds.
- `tests/dlna/test_queue_advance.py` — STOPPED-at-duration → queue picks
  next → URI pushed.

Target ~40–60 tests. pytest + `pytest-asyncio` + `aiohttp.test_utils.TestServer`,
matching Home Assistant's own async-upnp test shape.

### With a real renderer (august runs)

Cover art on a real TV (DIDL escaping is the most error-prone path),
position-bar accuracy at ≥30 min, pause-then-skip on a Yamaha (known-broken
sequence), 714 fallback on a real FLAC-blind renderer.

---

## 12. The autonomous slice

Agent ships §1's v1 scope **minus UI**.

**New files**
- `modules/cast/__init__.py`
- `modules/cast/dlna.py` — `DlnaController`, `_DlnaLoopThread`, DIDL
  builder, 714-retry, transport-state poll
- `tests/dlna/` per §11

**Modified files**
- `modules/cast_manager.py` — `dlna_devices`, `discover_dlna()`, threaded
  into `get_all_devices()` and `_notify()`
- `modules/player_backend.py` — `device_type == "dlna"` branches in
  `route_track` / `route_pause` / `route_resume` / `route_stop` /
  `route_seek` / `route_volume`, mirroring the Chromecast branch shape
- `modules/settings.py` — three new keys per §9
- `pyproject.toml` — `async-upnp-client>=0.47,<1.0`

**Signals.** Existing `cast_started`, `cast_stopped`,
`cast_devices_updated` are sufficient. No new signals.

**Branch.** `auto/cast-dlna-backend`. Three commits:
1. `dlna: controller + SSDP discovery + async loop thread`
2. `dlna: AVTransport push + DIDL builder + 714 fallback`
3. `dlna: tests (fake renderer + ~40 cases)`

**Done = all tests pass.** No UI changes; the device row appears for free
because `CastManager.get_all_devices` already feeds the existing cast-popup
model. august smoke-tests with a real renderer afterwards.

---

## 13. The UI-with-august slice

Queued after the backend slice lands:

- **Device-row badge.** Distinguish a DLNA row (small "DLNA" chip) from
  Chromecast / AirPlay so two devices sharing a display name don't collide
  in the picker. Pill shape + colour decided on a real LAN with at least
  one device of each type visible.
- **Per-renderer transcode toast.** First 714 fallback fires for a device:
  "Transcoding to MP3 for <device>". Wording + dismiss + "don't transcode
  again" button want a 5-minute pairing.
- **Error-state surface.** DLNA failures are weirder than Chromecast's
  (SSDP came back, description fetched, `SetAVTransportURI` returns 500).
  Probably: grey the device in the picker + toast on first failed pick.
- **Settings → Playback → Casting expander.** New "DLNA" section, master
  toggle. Per-renderer overrides hidden until someone files a bug. Match
  the EQ-section visual rhythm from `docs/research/eq_dsp.md` §4.

---

## 14. Competitive audit gist

| Client | DLNA? | Notes |
|---|---|---|
| **Supersonic** | Yes — uPnP/DLNA only, no AirPlay, no Chromecast | The reason DLNA matters |
| **Feishin** | No, no cast layer at all | We're already ahead; DLNA widens the lead |
| **Sonixd** | No, abandoned | n/a |
| **Sublime Music** | No, Chromecast only, end-of-maintenance | n/a |
| **Strawberry** | No, long-running forum request (`forum.strawberrymusicplayer.org/topic/1371`) | DLNA is its most-asked missing feature |
| **Tauon** | No, Chromecast only | n/a |
| **Symfonium** (mobile) | Yes, well-documented quirks | `support.symfonium.app` is the best public DLNA-quirk corpus |
| **Finamp** | No (Jellyfin-only, no cast layer) | n/a |
| **BubbleUPnP** (Android) | Yes — the canonical controller | Gold-standard reference UX, not a peer |
| **Astiga** | No, Chromecast only | n/a |

**Takeaway.** Among desktop self-hosted-music clients, **only Supersonic
ships DLNA**, and they ship only DLNA. jellytoast adding DLNA → the only
desktop client with Chromecast + AirPlay + DLNA. Clean differentiator;
buys us the Strawberry-deserter cohort that's been waiting since 2020.

---

## 15. Risk register

1. **SSDP under KDE Wayland firewalls.** Multicast send works; inbound
   unicast response gets dropped by `firewalld` defaults on Fedora-shapes.
   Discovery times out gracefully; settings shows "no DLNA devices found —
   check firewall multicast rules" after 5 s of silence. No auto-disable
   prompt; link to a docs page (TBW).
2. **NIC binding on multi-interface boxes.** Tailscale + Wi-Fi + Docker =
   three interfaces; M-SEARCH source-binds to whichever the kernel picks.
   `async-upnp-client` supports a `source` argument; v1 lets the OS pick,
   v1.1 adds a dropdown if bugs roll in.
3. **Linux bridge `multicast_snooping`.** Bites `br0`-from-libvirt/Docker
   users (ArchWiki ReadyMedia). v1 logs a hint; no auto-fix.
4. **Renderer firmware regressions.** Samsung pushes an update, our DIDL
   breaks, we get blamed. Mitigation: per-renderer overrides (§9) +
   Symfonium's support forum as bug corpus.
5. **Cover-art rendering on cheap TVs.** Some 2015-era models ignore the
   URL or render a single 90×90 forever. Can't fix; document as known.
6. **GENA reverse-firewall (v2).** Subscription listener blocked by
   Flatpak sandbox without portal permission → fall back to polling.
7. **"DLNA" brand vs UPnP protocol.** UI strings say "Speakers and TVs" /
   "DLNA renderers", never just "DLNA".
8. **`async-upnp-client` bus factor.** One maintainer; mitigated by Home
   Assistant's effective sponsorship. Pin a tested upper bound.

---

## 16. Sources

- `async-upnp-client` — `github.com/StevenLooman/async_upnp_client`
  (v0.47.0, Apr 2026); `pypi.org/project/async-upnp-client/`; DLNA profile
  at `async_upnp_client/profiles/dlna.py`
- UPnP Device Architecture v1.1 (SSDP, SOAP, GENA) —
  `upnp.org/specs/arch/UPnP-arch-DeviceArchitecture-v1.1.pdf`
- AVTransport:1 / :3 Service Templates —
  `upnp.org/specs/av/UPnP-av-AVTransport-v1-Service.pdf` /
  `…-v3-Service-20101231.pdf`
- DLNA Guidelines (codec profiles, `DLNA.ORG_PN`) —
  `spirespark.com/dlna/guidelines`
- Music Assistant DLNA provider — `music-assistant.io/player-support/dlna/`
- Symfonium support forum — Yamaha MusicCast progress bug
  (`support.symfonium.app/t/track-progress-not-updated-if-using-upnp-dlna-with-yamaha-musiccast-devices/2225`)
  and UPnP-cast issue thread (`/t/upnp-cast-issue/9219`)
- Sony DLNA AVTransport example —
  `github.com/sonydevworld/audio_control_api_examples/blob/master/DLNA/AVTransport/play_file.adoc`
- Sony Bravia error 714 corpus —
  `github.com/UniversalMediaServer/UniversalMediaServer/issues/929`,
  `github.com/gotwalt/sonos/issues/61`
- Samsung TV `upnp:class` requirements (the canonical gist) —
  `gist.github.com/probonopd/9893084d982893c4c7b7`
- Home Assistant DLNA-DMR issues corpus — `home-assistant/core` tagged
  `integration: dlna_dmr`
- ArchWiki ReadyMedia (Linux bridge `multicast_snooping`) —
  `wiki.archlinux.org/title/ReadyMedia`
- BubbleUPnP — `bubblesoftapps.com/bubbleupnp/` (reference UX)
- Supersonic — `github.com/dweymouth/supersonic`
- qasync (deferred option) — `github.com/CabbageDevelopment/qasync`
- Internal: `modules/cast_manager.py`, `modules/cast_proxy.py`,
  `modules/player_backend.py`, `modules/player_state.py`,
  `modules/settings.py`, `docs/research/eq_dsp.md` §8,
  `docs/research/radio_and_seeded_queues.md` §2.5

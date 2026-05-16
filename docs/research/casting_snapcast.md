# Snapcast casting — design research

Status: research / pre-build. No code yet. Last updated 2026-05-15.
Slot: post-Phase-5 (offline UI), alongside the wider casting parity push
(DLNA, Sonos). Lower priority than EQ + smart-playlists; higher cultural
fit than either with the self-hosted audience.

## 1. Goal & non-goals

**Goal.** Make jellytoast a first-class controller and (optionally) source
for [Snapcast](https://github.com/snapcast/snapcast) — the de-facto
multi-room synchronized-audio system in the self-hosted world. A jellytoast
user with a `snapserver` on their network should see snapserver groups +
clients in the cast popup, retarget audio at them in one click, set
per-client volumes, regroup speakers, and switch which stream a group is
playing. On Linux they should optionally be able to *pipe jellytoast's own
audio* into snapserver as a stream source.

**v1.** Control surface only (Option B in §2): JSON-RPC client + mDNS
discovery + clients/groups/streams + per-client volume + group reassignment
+ stream switching + event subscription. Snapserver appears as a new row
type in the cast popup; the existing HTTP-URL-push surfaces (Chromecast,
AirPlay 2, DLNA-eventually, Sonos-eventually) are unaffected.

**Non-goals (v1).** Bit-perfect transport (Snapcast resamples; same
disclosure as EQ/Sonos). Audio routing (Option A) — pushed to v1.5,
Linux-experimental. Running `snapserver` ourselves. Bundling
`snapclient`. iOS/Android control. v0.32+ password auth (most home
deployments are unauthenticated; add when someone files an issue).

## 2. Snapcast integration shapes

Snapcast is fundamentally not the HTTP-URL-push casting model. There's no
"renderer" you point at a media URL — there's a **server** (`snapserver`)
that ingests audio from a *source* (typed: `pipe`, `librespot`, `airplay`,
`tcp`, `meta`) and fans synchronized PCM chunks out to **clients**
(`snapclient`) over TCP, buffering ~1 s to align playback.

For a music client like jellytoast, two integration shapes exist and
they're orthogonal:

| Option | What it controls | Where the audio comes from | OS-portable? |
|---|---|---|---|
| **A — Audio source** | jellytoast's mpv output is routed into a `pipe` stream that snapserver reads. | jellytoast's own playback. | Linux: yes (FIFO via mpv or PipeWire tunnel). macOS: possible via BlackHole / Soundflower-class loopbacks. Windows: hard (no native named-pipe-as-audio-sink). |
| **B — Control surface** | jellytoast discovers snapservers and talks to their JSON-RPC API: list/regroup clients, per-client volume, switch a group's source stream, subscribe to events. | Whatever the user already configured snapserver against (their `librespot`, their `airplay`, their `pipe`, etc.). | Yes — pure TCP + JSON, no audio plumbing. |

Almost every existing controller (Snap.Net, Home Assistant, Music
Assistant) is Option B only. Option A is the niche play that turns
jellytoast from "yet another snapcast remote" into "the music client that
*can also be* a snap source".

**Pick: v1 is B; v1.5 adds A behind a settings toggle, Linux-first.**

## 3. Architecture fit with existing cast subsystem

`modules/cast_manager.py` + `modules/cast_proxy.py` are built around the
HTTP-URL-push family — pychromecast and pyatv want a media URL, the proxy
makes that URL reachable across Tailscale / remote / self-signed setups
and serves downloaded `file://` blobs over Range HTTP. Snapcast doesn't
consume a URL; the proxy is irrelevant to it. Snapcast slots **alongside**
cast_manager as a parallel subsystem:

```
    jellytoast
       │
       ├─── cast_manager ──── cast_proxy ───► Chromecast / AirPlay 2
       │   (URL-push)        (HTTP, Range)
       │
       ├─── snapcast ──────── JSON-RPC :1705 ───► snapserver ───► snapclient × N
       │   (control + optional audio src)            │            (synchronized,
       │                                             │             ~1 s buffer)
       │                                         streams/groups/clients
       │
       └─── player_backend (mpv) ───► local ao (PipeWire)
                          └── v1.5: also writes /tmp/snapfifo or pipewire/snapcast-sink
```

The cast popup grows a new row family ("Snapcast: <hostname>") with
indented group rows, each exposing its clients. It becomes a switcher
between three destination classes: local (mpv here), HTTP-URL renderer
(cast_manager family), Snapcast group.

New module: `modules/snapcast/` (`_rpc.py` + `_discovery.py` +
`_controller.py` + `_models.py`, ~600–900 lines total).

## 4. Python library survey

| Option | Status (May 2026) | Asyncio? | Zeroconf? | Notes |
|---|---|---|---|---|
| `pip install snapcast` (happyleavesaoc/python-snapcast) | **Healthy.** v2.3.8 on PyPI, released 2026-05-08. 20 contributors. Backs the Home Assistant integration. | Yes — `asyncio` end-to-end, JSON-RPC over raw TCP (port 1705). | No — caller supplies host:port. | Stale-looking Snyk reports cite a 12-month gap; that data is from before the May 2026 release. Real story: a typical maintenance cadence with one large maintainer + occasional drive-bys. |
| `pip install python-snapcast` | Same package re-published name — same author. Pin to `snapcast>=2.3.8`. | — | — | Don't confuse with the namespace; the import is `import snapcast.control`. |
| Hand-rolled JSON-RPC over `QTcpSocket` / `QWebSocket` | ~300 lines if we want it. The protocol is ndjson over TCP, or POST + WebSocket over HTTP (port 1780). | Trivial to do either Qt-native or with `asyncio` via `run_async`. | We add via our existing `zeroconf` dep. | A clean fit with the existing async pattern (`modules/async_io.run_async`) and avoids a transitive dep. Costs us the maintenance subsidy we get from HA + Domoticz keeping the upstream alive. |

**Pick: `snapcast>=2.3.8` for v1.** Maintenance subsidy from the HA
integration, asyncio surface bridges cleanly via `modules/async_io.run_async`,
zero transitive PyPI deps. Plan B if it staleens: ~300 lines of hand-rolled
JSON-RPC behind the same `SnapcastController` wrapper — keep that wrapper
thin so the swap is a 1-day job.

**What it gives us.** TCP JSON-RPC with auto-reconnect; `Server` /
`Snapclient` / `Snapgroup` / `Snapstream` object model; setter coroutines
(`set_volume`, `set_clients`, `set_stream`, `set_mute`, `set_name`);
per-object change callbacks.

**What we add ourselves.** mDNS discovery via our existing `zeroconf`
dep (§5). Qt-signal bridging — wrap the async callbacks into
`PlayerBus.emit(...)` on the GUI thread (see
`modules/scrobble/listenbrainz.py` for the pattern). HTTP/WebSocket
transport is unused; raw TCP is enough.

## 5. Discovery

Snapcast advertises `_snapcast._tcp.local.` (audio :1704) and
`_snapcast-jsonrpc._tcp.local.` (control :1705) via Avahi. The latter is
one byte over RFC 6763's 15-byte limit (upstream #243); strict discovery
libs reject it. Our pinned `zeroconf>=0.80` handles it fine — Home
Assistant has been doing this for years.

Implementation mirrors `modules/cast_manager.py`: long-lived
`AsyncZeroconfServiceBrowser` for the JSON-RPC service type, resolves to
`(host, port, hostname, version)`, caches, emits
`snapcast_servers_changed`. Manual "add by IP" affordance for
mDNS-firewalled networks, same as the existing cast-device picker.
Windows uses zeroconf's bundled mDNS responder — no Bonjour install
needed, same code path that works for our DLNA/AirPlay flows.

## 6. Control surface

v1 surface, mapped to JSON-RPC methods (python-snapcast wrappers exist
for all of them):

- **Read:** `Server.GetStatus` — full snapshot of groups, clients,
  streams.
- **Client:** `Client.SetVolume`, `Client.SetName`, `Client.SetLatency`.
- **Group:** `Group.SetClients` (add/remove), `Group.SetStream`,
  `Group.SetMute`, `Group.SetName`.
- **Stream:** `Stream.Control` (play/pause/next/seek), `Stream.AddStream`
  (v2 — register jellytoast as a source), `Stream.RemoveStream`.

Notifications we subscribe to (fan onto `PlayerBus` via §10):
`Client.On{Connect,Disconnect,VolumeChanged,NameChanged}`,
`Group.On{Mute,StreamChanged,NameChanged}`,
`Stream.On{Update,Properties}`, and `Server.OnUpdate` for full resync.

Multiple controllers (HA, Snap.Net, us) can be attached at once, so
robustness to "someone else moved a client while my popup was open"
matters — `Server.OnUpdate` covers it.

## 7. Audio path (Option A, v1.5)

The harder, optional half. Three Linux routes + a quasi-route on macOS +
a "don't bother" on Windows.

### 7.1 mpv → FIFO directly

```
[stream]
source = pipe:///tmp/snapfifo?name=jellytoast&sampleformat=48000:16:2
```

```
mpv --ao=pcm --ao-pcm-waveheader=no --ao-pcm-file=/tmp/snapfifo \
    --audio-samplerate=48000 --audio-format=s16 --audio-channels=stereo
```

Deterministic; mpv writes raw PCM to a FIFO snapserver tails. Cons:
format is **fixed at config time** (mpv resamples — bit-perfect gone);
FIFO needs `fs.protected_fifos=0` on newer kernels; pause starves the
FIFO unless `--audio-stream-silence=yes`; un-pairing requires an mpv
`ao` re-init (one-track gap at the swap).

### 7.2 PipeWire `module-pipe-tunnel` → FIFO

```
context.modules = [
  { name = libpipewire-module-pipe-tunnel
    args = {
      tunnel.mode = sink
      pipe.filename = "/tmp/snapfifo"
      audio.format = "S16LE"
      audio.rate = 48000
      audio.channels = 2
      node.name = "snapcast-sink"
      node.description = "Snapcast"
    }
  }
]
```

mpv then points at the sink: `--audio-device=pipewire/snapcast-sink`.
System-level (any app can route here), and jellytoast's role shrinks to
"set `--audio-device` from a settings string". Cost: we're shipping a
PipeWire config snippet — editing the user's audio system is a real
support-burden escalation.

### 7.3 PipeWire's `snapcast-discover` module

PipeWire 1.2+ ships `libpipewire-module-snapcast-discover` which
auto-creates a sink per discovered snapserver. If loaded, every PipeWire
app sees the snapserver as another sink. We detect the sink and offer a
one-click "use system-managed Snapcast sink" toggle. Cost: not enabled
by default in most distros yet; can't rely on it.

### 7.4 macOS / Windows

macOS — BlackHole / Soundflower can route mpv into snapserver's pipe
over the network. Obscure; defer. Windows — no native named-pipe-as-sink;
third-party kernel-level audio drivers are too much. Document as
unsupported.

### 7.5 Format compatibility, resampling, bit-perfect

Snapcast: source → optional encoder (PCM/FLAC/OGG/Opus) → network →
snapclient decode → soundcard. The source is **fixed-rate PCM**. mpv
resamples to whatever the FIFO is configured at. High-quality survives
if the user picked 24/96 or 24/192 in the snapserver conf and the
network has headroom; **bit-perfect is gone**. Caption it like the EQ
disclosure: *"Snapcast resampling means audio is no longer bit-identical
to the source. High quality is preserved; bit-perfect is not."*

### 7.6 Recommendation

For v1.5, ship **PipeWire `module-pipe-tunnel` routing** as the default
Linux path with a one-shot helper that writes
`~/.config/pipewire/pipewire.conf.d/jellytoast-snapcast.conf` and
reloads. If `snapcast-discover` is already loaded, detect its sink and
skip the helper. Settings → Playback → "Snapcast audio route" combo:

- Off (default) — local audio device.
- Pipe sink (auto) — use `snapcast-discover` if found, else our
  helper-installed `snapcast-sink`.
- Direct FIFO (advanced) — mpv writes `/tmp/snapfifo` directly.

Hide all three behind an "Experimental" disclosure on macOS / Windows.

## 8. Latency reality

Snapcast's default end-to-end buffer is **1000 ms** (the typical value
across the docs and the upstream Discussion #743). Floor is 400 ms — the
server ignores anything lower. Chunk size is 26 ms.

UX consequences: Next/Prev/Pause feel laggy — UI flips immediately,
audio lags ~1 s. Same for Seek. Volume *doesn't* go through the buffer
(per-client RPCs, or local pre-pipe gain) so volume feels snappy.
Latency is fundamental, not a bug — surface once in the Settings
caption, never apologize again.

## 9. Settings keys (v1)

Follow the `playback/*` pattern in `modules/settings.py`. All snapcast
keys live under `cast/snapcast_*` so they're co-located with the existing
`playback/favorite_cast_devices`, `playback/cast_member_volumes`, etc.

| Key | Type | Default | Notes |
|---|---|---|---|
| `cast/snapcast_enabled` | bool | `False` | Master kill switch. Off = no mDNS browse, no popup row. |
| `cast/snapcast_servers` | str (JSON list) | `"[]"` | Known servers as `[{host, port, hostname, last_seen}]`. Same pattern as `favorite_cast_devices`. |
| `cast/snapcast_manual_hosts` | str (JSON list) | `"[]"` | User-typed `host:port` entries for mDNS-firewalled networks. |
| `cast/snapcast_active_group` | str | `""` | UUID of the snapcast group the user has paired with. Empty = local playback. |
| `cast/snapcast_audio_route` | str | `"off"` | `"off"`, `"pipe_sink"`, `"fifo_direct"`. v1.5+. |
| `cast/snapcast_pipe_path` | str | `"/tmp/snapfifo"` | FIFO path. Only used by `fifo_direct`. |
| `cast/snapcast_pipe_rate` | int | `48000` | Sample rate. Tradeoff: 44100 preserves CD masters better; 48000 fits modern content. |
| `cast/snapcast_show_inactive_clients` | bool | `False` | Many homes leave snapclients running on Raspberry Pis 24/7. Default hides the disconnected ones. |

Migration: none — all keys are new.

## 10. PlayerBus signals

New signals on `modules/player_state.PlayerBus`. Naming follows the
existing `cast_*` family (compare `cast_started`, `cast_stopped`,
`cast_devices_updated`).

| Signal | Payload | When |
|---|---|---|
| `snapcast_servers_changed` | `list[dict]` of `{host, port, hostname, version}` | mDNS browse adds/removes, or `Server.OnUpdate` arrives |
| `snapcast_state_changed` | `dict` snapshot of `{groups, clients, streams}` for the active server | After `Server.GetStatus`, or any notification that mutates state |
| `snapcast_client_volume_changed` | `(client_id: str, percent: int, muted: bool)` | `Client.OnVolumeChanged` from snapserver |
| `snapcast_group_changed` | `group_id: str` | `Group.On{Mute,StreamChanged,NameChanged}` |
| `snapcast_active_group_changed` | `group_id: str` (empty = unpaired) | The user paired or unpaired |
| `snapcast_audio_route_changed` | `route: str` | The user changed `cast/snapcast_audio_route` (v1.5) |
| `snapcast_error` | `str` (human-readable) | Connection lost, mDNS failed, RPC error |

Connection state intentionally folds into `snapcast_error` rather than
its own signal — most consumers only care "are we live?", and an empty
`snapcast_state_changed` payload + the most recent error covers it.

## 11. Cast popup integration

The cast popup grows a Snapcast section. Sketch:

```
Snapcast — snapserver.local
  ○ Living Room   [Spotify ▾]   5 clients
     ◌ couch-pi      ▮▮▮▮▮▮▯▯▯▯   72%
     ◌ kitchen-pi    ▮▮▮▮▮▯▯▯▯▯   58%
     ◌ office-pi     ▮▮▮▮▮▮▮▮▯▯   91% mute
  ○ Bedroom       [jellytoast ▾]  1 client
     ◌ bedroom-pi    ▮▮▮▮▯▯▯▯▯▯   44%
  + Add server by IP
```

Server rows are non-selectable headers. **Group rows are the cast
target** — picking one routes jellytoast through that group; the combo
is the group's current source stream (switching it calls
`Group.SetStream`). Client rows expose per-client volume + mute. v1
ships a context-menu "Move to group..." (drag-drop deferred to v1.5).

Performance note: ~20 clients max in a typical home, so QFrame-per-row
is fine. If we ever cross "lots of rows" — QListView + delegate, not
QFrame-per-row (see `feedback_model_view_for_big_lists`).

## 12. Provider abstraction

Zero impact. Snapcast is downstream of provider — Jellyfin and Subsonic
both deliver bytes to mpv; mpv either plays locally (default) or pipes
into snapserver (Option A). The provider abstraction never knows.

## 13. Edge cases

- **Multiple snapservers on the LAN.** One row each; pair with one at a
  time. Switching servers unpairs the previous.
- **Snapserver restart.** TCP drops; python-snapcast auto-reconnects;
  `snapcast_state_changed` re-fires. If the active group still exists,
  stay paired; if gone, unpair + toast.
- **Concurrent controllers.** Another HA/Snap.Net controller mutates
  state — we receive `Server.OnUpdate` and repaint. Snapcast's data
  model is the source of truth.
- **Network partition.** Snapclients drain their ~1 s buffer then go
  silent. jellytoast flips to unreachable state with retry. No data loss.
- **mpv pause + FIFO route.** Pipe starves, snapclients lose sync.
  `--audio-stream-silence=yes` keeps the FIFO fed.
- **Two sources fighting one pipe.** Behavior is undefined (last opener
  wins). Document, don't prevent.
- **EQ + Snapcast.** EQ filter is in mpv, applies before `ao` write to
  FIFO — Snapcast clients hear the EQ'd audio. (Contrast Chromecast,
  where EQ does *not* apply.) Surface in the EQ caption: "EQ applies to
  local + pipe audio; not to Chromecast/AirPlay/Sonos."
- **ReplayGain + Snapcast.** Same path — RG applies pre-FIFO.

## 14. Competitive positioning

| Client | Snapcast support |
|---|---|
| Supersonic, Strawberry, Tauon, Symfonium, Plexamp, Feishin, BubbleUPnP | None. |
| Snap.Net | Yes — dedicated controller. |
| Home Assistant | Yes — control only. |
| Music Assistant | Yes — control + source (Snapcast player provider). |

Snapcast is the **least mainstream** of the multi-room options jellytoast
could integrate (DLNA + Sonos + Chromecast all have larger audiences) but
the **highest cultural fit**: every snapserver user is, by definition, a
self-hosted-music user. That's our audience. Native Snapcast is a moat
against Supersonic / Feishin / Strawberry — none of them have it — and a
strong word-of-mouth surface on r/selfhosted / HN. Prevalence is real,
though: maybe 5% of Jellyfin users run snapserver. Position it as "we
support your weird, beautiful audio setup" — same vibe as the cast proxy
— not as a hero feature.

## 15. Test plan

- **Mocked JSON-RPC server fixture.** ~150-line `asyncio` TCP fixture
  speaking ndjson `{"jsonrpc":"2.0","id":N,"method":...}`. Cover:
  `Server.GetStatus` → state snapshot; `Client.SetVolume` → callback;
  `Group.SetStream` → group update; notification roundtrip (server
  emits → bus signal fires). Error paths: connection drop + reconnect,
  unknown method, malformed JSON.
- **Discovery tests.** Patch `zeroconf.AsyncServiceBrowser`; inject a
  fake `_snapcast-jsonrpc._tcp.local.` record; assert
  `snapcast_servers_changed` fires.
- **Audio-route helpers (v1.5).** Subprocess-inspect the mpv argv for
  `fifo_direct` vs `pipe_sink`; assert flags. Don't actually pipe audio
  in CI.
- **Manual smoke.** Real LAN: snapserver + two snapclients on Pis.
  Step list in `docs/manual_test_plan.md`: discover, group/regroup,
  per-client volume, switch streams, graceful disconnect, (v1.5) FIFO
  route playing on the Pis with the expected ~1 s latency.

## 16. Effort + sequencing

### v1 — Control surface (M, autonomous-task slice)

Lands without UI; surfaces via tests + an ad-hoc
`python -m modules.snapcast.demo` script that prints state.

1. `modules/snapcast/` package: `_rpc.py` (wraps
   `snapcast.control.Server`), `_discovery.py` (zeroconf browser),
   `_controller.py` (pair, set_volume, set_stream, ...), `_models.py`
   (dataclasses). **M.**
2. PlayerBus signals (§10). **S.**
3. Settings keys + properties (§9). **S.**
4. Tests (§15). **M.**

Pre-reqs: add `snapcast>=2.3.8` to `requirements.txt` + `pyproject.toml`.

### v1.5 — Cast popup UI + audio route (L, august-eyes)

5. Cast popup Snapcast section (server header + group rows + client
   rows). **M.**
6. Pair/unpair, per-client volume sliders, mute toggles, "Move to
   group..." context menu. **M.**
7. Settings → Playback → "Snapcast" (enable, audio route combo,
   bit-perfect caption, manual hosts). **S.**
8. mpv `--audio-device` for `pipe_sink`; mpv FIFO flags for
   `fifo_direct`. **S.**
9. One-shot PipeWire conf installer + reload. **S — fragile, hence
   august-eyes.**
10. Manual test on the real LAN Pi rig. **S, recurring.**

### v2 — Niceties (deferred)

- Drag-drop client→group in the popup.
- Multi-snapserver popup ergonomics.
- jellytoast registers itself as a Snapcast stream via `Stream.AddStream`
  so a fresh snapserver auto-creates the jellytoast source — skips
  `snapserver.conf` editing.
- Push track metadata via `Stream.SetProperty` so other controllers see
  what jellytoast is playing.

## 17. Cost register

- **Library risk.** `snapcast>=2.3.8` healthy May 2026, carried by one
  maintainer + the HA bug-report flow. Plan B: hand-rolled JSON-RPC
  behind the same wrapper, ~300 lines.
- **Option A fragility.** FIFO permissions, PipeWire conf reload, mpv
  ao re-init, distro-specific FIFO protection. Ship behind experimental
  toggle; default off.
- **Latency.** Users will flag ~1 s as a bug. Caption + one-time "first
  cast" explainer toast.
- **Niche audience.** Lean on strategic narrative (only-music-client-
  that-does-this), not raw numbers.
- **No bit-perfect.** Off by default; caption.
- **Windows audio-route gap.** Document, don't fight. Control surface
  works fine on Windows.

## 18. Headline recommendation

**Ship native Snapcast — control surface (Option B) only in v1, audio
routing (Option A) as v1.5 Linux-experimental.** Option B is cheap,
clean, competitively distinctive, and has zero downside for the ~95% of
users without snapserver (the popup row simply doesn't appear). Option A
is the flourish — turns "yet another remote" into "the music client that
*also* feeds Snapcast" — but it's per-OS, hard to QA, and not worth
gating v1 on. Decouple them.

## 19. Open questions for august

1. **Build artifact for the Pi rig.** Do you already have a `snapserver`
   running for the cast-proxy testing, or do we need to stand one up
   before v1.5 manual QA?
2. **Settings placement.** Snapcast section under Settings → Playback,
   alongside Normalization / Gapless / EQ? Or new "Multi-room" tab once
   DLNA + Sonos arrive and we have three things to group?
3. **Bus signal scope.** Worth folding `snapcast_*` into the existing
   `cast_*` family (broader signal, payload discriminator) versus keeping
   them separate? I lean separate — the popup needs to render Snapcast
   differently from URL-push casts — but if you'd rather have one signal
   family I can collapse them.
4. **AppStream / Flathub copy.** When this lands, Snapcast support is a
   marketable bullet. Add to the AppStream description, or save for the
   blog post when v1.5 lands?
5. **Auth (post-v1).** Snapcast 0.32 added a `Server.Authenticate` flow.
   Worth shipping in v1 (the few snapservers that enforce it) or wait
   until someone files an issue?

## 20. Sources

- [snapcast/snapcast — README](https://github.com/snapcast/snapcast)
- [JSON-RPC API: control.md](https://github.com/snapcast/snapcast/blob/develop/doc/json_rpc_api/control.md)
- [JSON-RPC API: stream_plugin.md](https://github.com/snapcast/snapcast/blob/develop/doc/json_rpc_api/stream_plugin.md)
- [Player setup — pipe/librespot/airplay/PulseAudio/PipeWire](https://github.com/snapcast/snapcast/blob/develop/doc/player_setup.md)
- [happyleavesaoc/python-snapcast](https://github.com/happyleavesaoc/python-snapcast)
- [snapcast on PyPI — v2.3.8 (2026-05-08)](https://pypi.org/project/snapcast/)
- [Latency and Buffers explained — Discussion #743](https://github.com/snapcast/snapcast/discussions/743)
- [snapserver buffer below 400ms — Issue #329](https://github.com/snapcast/snapcast/issues/329)
- [MDNS service name length — Issue #243](https://github.com/snapcast/snapcast/issues/243)
- [PipeWire `libpipewire-module-snapcast-discover` man page](https://man.archlinux.org/man/extra/pipewire-zeroconf/libpipewire-module-snapcast-discover.7.en)
- [PipeWire docs — module-snapcast-discover](https://docs.pipewire.org/page_module_snapcast_discover.html)
- [PipeWire docs — module-pipe-tunnel](https://docs.pipewire.org/page_module_pipe_tunnel.html)
- [PipeWire 1.2 ships Snapcast support — Phoronix](https://www.phoronix.com/news/PipeWire-1.2-RC2)
- [Snapcast — NixOS Wiki](https://nixos.wiki/wiki/Snapcast)
- [How to make it work with Pipewire? — Discussion #1108](https://github.com/snapcast/snapcast/discussions/1108)
- [Snap.Net — cross-platform Snapcast controller (comp reference)](https://github.com/stijnvdb88/Snap.Net)
- [Music Assistant Snapcast Player Provider](https://www.music-assistant.io/player-support/snapcast/)
- [CVE-2023-52261 — JSON-RPC RCE (informs auth-stance §1 non-goals)](https://cavefxa.com/posts/snapcast-json-rpc-to-rce/)

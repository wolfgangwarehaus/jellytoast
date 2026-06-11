# Research / design notes — index

These are design + research notes. Most describe features that have since
**shipped** — they are kept for the rationale and the as-built history, not
as live work items. Source-of-truth for current behaviour lives in
`docs/SPEC.md`, `docs/TODO.md`, `CHANGELOG.md`, and the code. Several docs
are cited by code comments (e.g. `artist_page.py` → `dpr_cache_keys.md`,
`pipewire_setup.py` → `bit_perfect_playback.md`), so **do not rename or move
them** — fix banners in place.

Status key: **SHIPPED** = built + in the code; **LIVE-VERIFIED** = SHIPPED
+ confirmed on real hardware; **OPEN** = design only, not yet built;
**SUPERSEDED** = the as-built reversed part of the design (see the note);
**REFERENCE** = methodology / tooling note, not a feature spec.

| Doc | Status | Notes |
|---|---|---|
| `bit_perfect_playback.md` | SHIPPED | Bit-perfect T1-T4 (`9abed14`); `settings.bit_perfect_mode` + `player_backend._compute_bit_perfect_active`; user guide `docs/bit_perfect.md`. |
| `audio_output_routing.md` | SHIPPED | Output-device picker (Pulse/PipeWire/WASAPI sinks + raw ALSA `hw:` direct path); `settings.audio_output_device`, ALSA crossfade guardrail, visualizer caption. |
| `eq_dsp.md` | SHIPPED | 10-band EQ + pre-amp; `modules/eq_presets.py` emits one `anequalizer` filter. |
| `eq_dsp_v2.md` | SHIPPED | Tiered path landed T1-T3c incl. AutoEQ `ParametricEQ.txt` import (`9abed14`); the `anequalizer` wart resolved. |
| `theme_live_apply.md` | SHIPPED | Theme-mode live-apply via `ui_helpers.refresh_theme()` → `theme_changed`. |
| `theming.md` | SHIPPED | Token layer + light family (`frosted_light`/`light`) + Auto follow-OS (`b9c90ef`, #72). Transparent variants dropped (#60). |
| `visualizers.md` | SHIPPED | FFT backend + paint widget; real Linux tap (`MonitorAudioTap`). Per-OS taps beyond Linux still OPEN (P4). |
| `visualizer_rendering.md` | SHIPPED | Spectrum-bar paint widget (later Bezier wave). |
| `crossfade.md` | SHIPPED | Engine + Settings controls + equal-power easing; gated on `crossfade_enabled`. |
| `tag_editing.md` | SHIPPED | `modules/tag_editor.py`, Jellyfin-only; live cover-upload verify is the open hand-check. |
| `smart_playlists.md` | SHIPPED | Editor + library tab + live preview. |
| `radio_and_seeded_queues.md` | SHIPPED | Internet-radio UI + seeded-radio feeder. |
| `downloads_progress_ui.md` | SHIPPED | Aggregate in-flight progress cluster + Downloads page. |
| `dpr_cache_keys.md` | SHIPPED | Fixed `* 3` worst-case source size baked at every cover-art fetch site ("DPR invariant-4 complete", `b97ab98`). |
| `provider_abstraction_cleanup.md` | SHIPPED | `cast_manager.py` (Chromecast+AirPlay) + `cast/dlna` split done. |
| `parity_small_items.md` | SHIPPED | Sleep timer, smart shuffle (now always-on), hotkey rebinding, live theme modes, crossfade UI. |
| `casting_dlna.md` | LIVE-VERIFIED | Backend shipped 2026-05-17; verified on a real LG TV 2026-05-28. |
| `casting_sonos.md` | SHIPPED (HW-gated) | `modules/cast/sonos.py`; not yet verified on real hardware. |
| `casting_snapcast.md` | SHIPPED (HW-gated) | `modules/cast/snapcast.py`; not yet verified on real hardware. |
| `cross_platform_dispatch.md` | PARTIAL | Backend-package pattern + Linux backends live; Windows/macOS still stubs (P4). |
| `portable_blur.md` | SHIPPED / §5 SUPERSEDED | KDE/Linux blur status model live. **§5 SUPERSEDED**: Windows ships real **Acrylic** by default (`modules/blur/_dwm.py apply_acrylic`), not the Mica the section recommended; Mica is only the `JT_NO_WIN_BLUR` fallback. |
| `flatpak_packaging.md` | OPEN | No flatpak manifest yet — `packaging/` has only `.desktop`/`.metainfo.xml`/AUR. |
| `testing_tooling_2026-06-02.md` | REFERENCE | Testing-stack research; the `JT_TEST_BRIDGE` socket + `dev/jt_ctl.py`/`jt_drive.py` it recommends shipped. Pairs with `docs/live_shakedown_report.md`. |

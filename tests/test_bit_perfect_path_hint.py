"""The color-keyed BIT-PERFECT legend (settings → Playback).

One rich-text caption maps the picker's family dot colors to what each
output family needs for bit-perfect: green PipeWire (config + Exclusive,
sharing degrades) and purple ALSA (exclusive by nature). The active
family's line renders bold/full-strength; the other's body text dims.
Pure-function table tests — the widget wiring calls this with live state.
"""

from __future__ import annotations

from jellytoast.settings_dialog import (
    AUDIO_FAMILY_GREEN,
    AUDIO_FAMILY_PURPLE,
    audio_family_dot_color,
    bit_perfect_path_hint,
)


def _hint(**kw):
    base = dict(
        bp_on=True,
        device="auto",
        has_alsa_direct=True,
        pw_conf_installed=False,
        exclusive_on=False,
        platform="linux",
        faint_color="#717171",
    )
    base.update(kw)
    return bit_perfect_path_hint(**base)


def test_hidden_when_bit_perfect_off():
    assert _hint(bp_on=False) == ""
    assert _hint(bp_on=False, platform="windows") == ""


def test_linux_legend_has_both_family_lines_in_their_colors():
    h = _hint()
    assert "PipeWire" in h and AUDIO_FAMILY_GREEN in h
    assert "ALSA" in h and AUDIO_FAMILY_PURPLE in h
    assert "<br>" in h


def test_linux_pipewire_active_bolds_pipewire_and_dims_alsa():
    h = _hint(device="pipewire/sink1")
    assert h.index("<b>") < h.index("PipeWire")
    # ALSA body text carries the faint color (inactive)
    alsa_part = h.split("<br>")[1]
    assert "#717171" in alsa_part


def test_linux_alsa_active_bolds_alsa_and_dims_pipewire():
    h = _hint(device="alsa/front:CARD=A2,DEV=0")
    pw_part, alsa_part = h.split("<br>")
    assert "<b>" in alsa_part
    assert "#717171" in pw_part


def test_linux_pipewire_line_adapts_to_config_and_exclusive():
    todo = _hint()
    assert "install the sample-rate config" in todo
    assert "turn on Exclusive output" in todo
    done = _hint(pw_conf_installed=True, exclusive_on=True)
    assert "config installed" in done
    assert "Exclusive output on" in done
    assert "degrades" in done  # the sharing caveat always present


def test_linux_no_alsa_devices_says_so():
    h = _hint(has_alsa_direct=False)
    assert "no direct device detected" in h


def test_windows_suggests_then_confirms_exclusive():
    off = _hint(platform="windows", exclusive_on=False)
    assert "WASAPI" in off and "turn on Exclusive output" in off
    on = _hint(platform="windows", exclusive_on=True)
    assert "end to end" in on


def test_unknown_platform_stays_silent():
    assert _hint(platform="") == ""
    assert _hint(platform="darwin") == ""


def test_dot_colors_by_family():
    assert audio_family_dot_color("pipewire") == AUDIO_FAMILY_GREEN
    assert audio_family_dot_color("pipewire/sink1") == AUDIO_FAMILY_GREEN
    assert audio_family_dot_color("pulse/sink1") == AUDIO_FAMILY_GREEN
    assert audio_family_dot_color("wasapi/{guid}") == AUDIO_FAMILY_GREEN
    assert audio_family_dot_color("alsa/front:CARD=A2,DEV=0") == AUDIO_FAMILY_PURPLE
    assert audio_family_dot_color("alsa/hdmi:CARD=NVidia,DEV=1") == AUDIO_FAMILY_PURPLE
    # bare alsa default + auto carry no tag
    assert audio_family_dot_color("alsa") == ""
    assert audio_family_dot_color("auto") == ""

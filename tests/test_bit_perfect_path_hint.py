"""The adaptive BIT-PERFECT path hint (settings → Playback).

One caption assembles the bit-perfect puzzle for the user: app layer is
the toggle's own gating; the OS layer guidance adapts to platform,
selected output device, available ALSA-direct devices, the PipeWire
rate config, and the exclusive toggle. Pure-function table tests — the
widget wiring just calls this with live state."""

from __future__ import annotations

from jellytoast.settings_dialog import bit_perfect_path_hint


def _hint(**kw):
    base = dict(
        bp_on=True,
        device="auto",
        has_alsa_direct=True,
        pw_conf_installed=False,
        exclusive_on=False,
        platform="linux",
    )
    base.update(kw)
    return bit_perfect_path_hint(**base)


def test_hidden_when_bit_perfect_off():
    assert _hint(bp_on=False) == ""
    assert _hint(bp_on=False, platform="windows") == ""


def test_linux_alsa_direct_is_terminal_state():
    h = _hint(device="alsa/front:CARD=DAC,DEV=0")
    assert "end to end" in h
    assert "isn't needed" in h


def test_linux_suggests_alsa_when_available():
    h = _hint(device="auto")
    assert "ALSA" in h
    assert "sample-rate config" in h  # the stay-on-PipeWire alternative


def test_linux_pw_installed_still_offers_direct_path():
    h = _hint(device="pipewire/sink1", pw_conf_installed=True)
    assert "config" in h.lower() and "installed" in h
    assert "ALSA" in h


def test_linux_no_alsa_points_at_config():
    h = _hint(has_alsa_direct=False)
    assert "Install" in h
    assert "44.1" in h


def test_linux_no_alsa_config_installed_confirms():
    h = _hint(has_alsa_direct=False, pw_conf_installed=True)
    assert "installed" in h
    assert "Install " not in h


def test_windows_suggests_exclusive():
    h = _hint(platform="windows", exclusive_on=False)
    assert "Exclusive" in h


def test_windows_exclusive_on_confirms():
    h = _hint(platform="windows", exclusive_on=True)
    assert "end to end" in h


def test_unknown_platform_stays_silent():
    assert _hint(platform="") == ""
    assert _hint(platform="darwin") == ""

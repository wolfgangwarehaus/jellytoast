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


def _rows(h: str) -> list[str]:
    return [r for r in h.split("<tr>") if "</td>" in r]


def test_linux_legend_has_both_family_rows_in_their_colors():
    h = _hint()
    assert "PipeWire" in h and AUDIO_FAMILY_GREEN in h
    assert "ALSA" in h and AUDIO_FAMILY_PURPLE in h
    assert len(_rows(h)) == 2


def test_linux_pipewire_active_bolds_pipewire_and_dims_alsa():
    pw_row, alsa_row = _rows(_hint(device="pipewire/sink1"))
    assert "<b>" in pw_row
    # ALSA body text carries the faint color (inactive)
    assert "#717171" in alsa_row


def test_linux_alsa_active_bolds_alsa_and_dims_pipewire():
    pw_row, alsa_row = _rows(_hint(device="alsa/front:CARD=A2,DEV=0"))
    assert "<b>" in alsa_row
    assert "#717171" in pw_row


def test_linux_pipewire_line_adapts_to_config_state():
    todo = _hint()
    assert "install the sample-rate config" in todo
    done = _hint(pw_conf_installed=True)
    assert "config installed" in done
    assert "degrade the bit-perfect path" in done  # sharing caveat always present


def test_linux_alsa_line_spells_out_the_exclusivity_consequence():
    h = _hint()
    assert "claims it exclusively" in h
    assert "other audio sources won't play" in h


def test_linux_legend_never_recommends_exclusive():
    """mpv's PipeWire exclusive mode failed every open on a real box
    (2026-06-11 zombie bug) — the Linux legend must not steer users
    into it; ALSA-direct is the Linux exclusivity story."""
    for kw in ({}, {"exclusive_on": True}, {"pw_conf_installed": True}):
        assert "Exclusive" not in _hint(**kw)


def test_linux_shared_device_caveat_is_its_own_line():
    """The caveat breaks onto its own line INSIDE the description column
    (a <br> within the body td), so it starts under the description
    text, not under the family word."""
    pw_row, _alsa_row = _rows(_hint())
    body = pw_row.split("</td>", 1)[1]  # second column
    assert "<br>Sharing the playback device" in body


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


def test_auto_names_the_resolved_family():
    """On Auto, the legend names what Auto actually resolved to
    (mpv current-ao) so 'where is my sound going' has an answer."""
    h = _hint(device="auto", resolved_family="pipewire")
    assert "Auto → PipeWire" in h
    h = _hint(device="auto", resolved_family="alsa")
    assert "Auto → ALSA" in h
    # ...and the resolved family is the bold/active row
    pw_row, alsa_row = _rows(_hint(device="auto", resolved_family="alsa"))
    assert "<b>" in alsa_row and "#717171" in pw_row


def test_auto_unresolved_keeps_plain_words():
    h = _hint(device="auto", resolved_family="")
    assert "Auto →" not in h
    assert "PipeWire" in h and "ALSA" in h


def test_explicit_device_ignores_resolution_tag():
    h = _hint(device="pipewire/sink1", resolved_family="pipewire")
    assert "Auto →" not in h


def test_exclusive_checkbox_hidden_on_linux_and_armed_setting_cleared(
    qapp, isolated_settings, monkeypatch
):
    """mpv's PipeWire exclusive mode has no working Linux backend (every
    AO open fails with it on; the alsa AO ignores it) — the checkbox is
    not built on Linux, and a previously-armed persisted value is
    force-cleared at page build so it can't keep poisoning opens
    invisibly."""
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)  # dialog reads the flag lazily
    isolated_settings.audio_exclusive = True
    from jellytoast.settings_dialog import SettingsDialog

    dlg = SettingsDialog()
    try:
        dlg.nav.setCurrentRow(1)  # build the Playback page
        assert not hasattr(dlg, "_audio_exclusive_check")
        assert isolated_settings.audio_exclusive is False
    finally:
        dlg.deleteLater()

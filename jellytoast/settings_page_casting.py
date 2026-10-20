"""Settings dialog — the Casting page.

Split out of ``settings_dialog.py``: a mixin of ``SettingsDialog`` (it uses
the dialog's widgets, settings handle and shared helpers via ``self``).
Strings use ``QCoreApplication.translate("SettingsDialog", …)`` rather than
``self.tr`` so they keep the SettingsDialog translation context — lupdate
files a mixin's ``self.tr`` under the MIXIN's name, which never matches at
runtime.
"""

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from jellytoast.design_tokens import (
    TYPE_CAPTION,
    type_qss,
)
from jellytoast.settings_widgets import (
    CAST_ROUTING_MODES,
)
from jellytoast.ui_helpers import (
    TEXT_FAINT,
)


class _CastingPageMixin:
    # ── Page: Casting ──────────────────────────────────────────────────
    def _build_casting(self) -> QWidget:
        # Dedicated page (moved out of Playback 2026-05-16). Covers:
        # per-protocol discovery toggles, when discovery runs, and the
        # stream-routing escape hatch a cast device behind a private
        # network needs to reach the server. Cast protocols are OFF by
        # default (opt-in) — nothing scans the network until the user
        # turns a type on here; the cast button routes to this page when
        # none are enabled.
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # ── Device types ───────────────────────────────────────────────
        # ⓘ holds firewall guidance: on a firewalled host the discovery query
        # leaves fine but the device's reply is dropped, so AirPlay/DLNA never
        # appear (Chromecast usually survives). The hint hands the user a
        # copy-paste allow rule pre-filled with their own LAN subnet.
        dt_row = QHBoxLayout()
        dt_row.setContentsMargins(0, 0, 0, 0)
        dt_row.setSpacing(10)
        dt_row.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "Device types"))
        )
        dt_row.addWidget(
            self._info_button(
                QCoreApplication.translate("SettingsDialog", "Cast devices not showing?"),
                self._firewall_help_text(),
                tooltip=QCoreApplication.translate(
                    "SettingsDialog", "Firewall help — why AirPlay/DLNA might not appear"
                ),
            )
        )
        dt_row.addStretch(1)
        v.addLayout(dt_row)

        dt_caption = QLabel(
            QCoreApplication.translate(
                "SettingsDialog",
                "Casting is off until you turn it on — nothing scans your "
                "network until you enable a protocol below.",
            )
        )
        dt_caption.setWordWrap(True)
        dt_caption.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(dt_caption)

        # Per-protocol toggle rows. All four backends ship: Chromecast,
        # AirPlay, and DLNA are hardware-verified; Sonos is implemented
        # (deps bundled) and just awaits a final hardware verify.
        # (visible label, attribute name on Settings)
        cast_types = [
            ("Chromecast", "cast_chromecast_enabled"),
            ("AirPlay", "cast_airplay_enabled"),
            ("DLNA / UPnP", "cast_dlna_enabled"),
            ("Sonos", "cast_sonos_enabled"),
        ]
        # One grid for all four rows: every checkbox sits in column 0 so they
        # share a single left edge with even row spacing, and the Sonos ⓘ rides
        # column 1. A grid (rather than per-row addWidget / an HBox just for
        # Sonos) keeps the column flush — Qt offsets a bare addWidget'd control
        # ~2px from a layout-nested one, which made Sonos read 2px right of its
        # neighbours and the rows space unevenly.
        dt_grid = QGridLayout()
        dt_grid.setContentsMargins(0, 0, 0, 0)
        dt_grid.setHorizontalSpacing(10)  # gap before the ⓘ
        dt_grid.setVerticalSpacing(0)
        dt_grid.setColumnStretch(2, 1)
        self._cast_type_checks: dict = {}
        for r, (label, attr) in enumerate(cast_types):
            cb = QCheckBox(label)
            cb.setChecked(bool(getattr(self.s, attr)))
            cb.toggled.connect(lambda val, a=attr: setattr(self.s, a, val))
            self._cast_type_checks[attr] = cb
            # Uniform row height so the ⓘ row doesn't run taller than the rest
            # (even spacing) while the checkbox stays comfortably un-cramped.
            dt_grid.setRowMinimumHeight(r, 26)
            if attr == "cast_sonos_enabled":
                # Sonos carries the transport caveat in an ⓘ right next to it.
                _sonos_info = self._info_button(
                    QCoreApplication.translate("SettingsDialog", "Sonos transport"),
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "Mid-cast play/pause, volume, and seek (including the "
                        "skip ± buttons, both directions) are implemented for "
                        "Sonos — but Sonos transport hasn't been tested against "
                        "real hardware yet.",
                    ),
                    tooltip=QCoreApplication.translate(
                        "SettingsDialog", "Sonos transport: implemented, not yet hardware-tested"
                    ),
                )
                # ⓘ in column 1, tight to "Sonos": every OTHER row spans all
                # three columns (below) so it doesn't widen column 0 — that keeps
                # column 0 sized to the short "Sonos" label, so its ⓘ sits right
                # after it instead of after the widest checkbox. The checkbox is
                # still an addWidget in column 0, so it holds the column-0 edge.
                dt_grid.addWidget(cb, r, 0)
                dt_grid.addWidget(_sonos_info, r, 1, Qt.AlignmentFlag.AlignVCenter)
            else:
                dt_grid.addWidget(cb, r, 0, 1, 3)
        v.addLayout(dt_grid)

        v.addSpacing(8)

        # ── Discovery timing ───────────────────────────────────────────
        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "Discovery timing"))
        )

        self._discover_at_startup_radio = QRadioButton(
            QCoreApplication.translate("SettingsDialog", "Discover at startup")
        )
        self._discover_on_demand_radio = QRadioButton(
            QCoreApplication.translate("SettingsDialog", "Discover on cast")
        )
        timing_group = QButtonGroup(self)
        timing_group.addButton(self._discover_at_startup_radio)
        timing_group.addButton(self._discover_on_demand_radio)
        # Keep the group alive on self so it doesn't get GC'd; without
        # it the radios behave as independent checkboxes.
        self._discover_timing_group = timing_group
        current_timing = self.s.cast_discovery_timing
        self._discover_at_startup_radio.setChecked(current_timing == "startup")
        self._discover_on_demand_radio.setChecked(current_timing != "startup")

        def _on_timing_changed(_checked: bool):
            v_ = "startup" if self._discover_at_startup_radio.isChecked() else "on_demand"
            self.s.cast_discovery_timing = v_

        self._discover_at_startup_radio.toggled.connect(_on_timing_changed)
        # Grid (column 0) so the two radios share the device-types column edge
        # and the same even row spacing.
        dtim_grid = QGridLayout()
        dtim_grid.setContentsMargins(0, 0, 0, 0)
        dtim_grid.setVerticalSpacing(0)
        dtim_grid.setColumnStretch(1, 1)
        dtim_grid.addWidget(self._discover_at_startup_radio, 0, 0)
        dtim_grid.addWidget(self._discover_on_demand_radio, 1, 0)
        dtim_grid.setRowMinimumHeight(0, 26)
        dtim_grid.setRowMinimumHeight(1, 26)
        v.addLayout(dtim_grid)

        v.addSpacing(8)

        # ── Stream routing ─────────────────────────────────────────────
        # A cast device can only fetch a URL it can route to. "Network
        # cast" lets the device fetch from the server directly (normal
        # mode); "Route locally" relays the stream through a small
        # local HTTP server on this machine, the escape hatch for
        # offline / Tailscale / self-signed-cert / cross-VLAN setups
        # where the cast device can't reach the server itself. "Auto"
        # picks per-server based on URL reachability. Three-radio form
        # matches the Discovery timing section above so the page reads
        # as a consistent stack of single-pick groups.
        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "Stream routing"))
        )

        self._cast_routing_radios: dict = {}
        routing_group = QButtonGroup(self)
        current_routing = self.s.cast_stream_routing or "auto"
        # Grid: radios flush in column 0 (in line under each other + the device /
        # discovery columns), faint hint suffixes in column 1 so "(normal mode)"
        # / "(offline, special case)" align in their own column. The grid also
        # gives even row spacing — the old per-row HBoxes ran the hinted rows
        # taller, so Route locally sat further from Network than Network from Auto.
        routing_grid = QGridLayout()
        routing_grid.setContentsMargins(0, 0, 0, 0)
        routing_grid.setHorizontalSpacing(16)
        routing_grid.setVerticalSpacing(0)
        routing_grid.setColumnStretch(2, 1)
        for r, (label, hint, key) in enumerate(CAST_ROUTING_MODES):
            rb = QRadioButton(label)
            rb.setChecked(key == current_routing)
            routing_group.addButton(rb)
            routing_grid.addWidget(rb, r, 0)
            routing_grid.setRowMinimumHeight(r, 26)  # even rows incl. the hinted ones
            if hint:
                # Faint parenthetical as its own QLabel (QRadioButton's label
                # doesn't render rich text) so it reads dim grey.
                hint_lbl = QLabel(hint)
                hint_lbl.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
                routing_grid.addWidget(hint_lbl, r, 1, Qt.AlignmentFlag.AlignVCenter)
            self._cast_routing_radios[key] = rb
        v.addLayout(routing_grid)
        # Keep the group alive on self so it doesn't get GC'd — see
        # the timing_group note above for the same trap.
        self._cast_routing_group = routing_group

        def _on_routing_changed(_checked: bool):
            for key, rb in self._cast_routing_radios.items():
                if rb.isChecked():
                    self.s.cast_stream_routing = key
                    return

        for rb in self._cast_routing_radios.values():
            rb.toggled.connect(_on_routing_changed)

        v.addStretch(1)
        return page

    def _firewall_help_text(self) -> str:
        """Body for the Casting-page firewall ⓘ. A firewall can let the
        discovery query out but block the device's reply, so AirPlay/DLNA never
        appear (Chromecast usually still works — the tell). Pre-fills the host's
        real LAN subnet so the allow rule is copy-paste ready, and tailors the
        steps to the running OS."""
        from PySide6.QtCore import QCoreApplication

        from jellytoast.cast_manager import _lan_cidrs
        from jellytoast.platform_compat import IS_MACOS, IS_WINDOWS

        try:
            from jellytoast.cast_proxy import _PROXY_PORT
        except Exception:
            _PROXY_PORT = 8943

        cidrs = _lan_cidrs()
        subnet = (
            cidrs[0] if cidrs else QCoreApplication.translate("SettingsDialog", "your local subnet")
        )

        intro = QCoreApplication.translate(
            "SettingsDialog",
            "AirPlay and DLNA devices are found by sending discovery requests "
            "on your local network. A firewall can allow the request out but "
            "block the reply, so those devices never appear. If Chromecast "
            "works but AirPlay/DLNA don't, a firewall is the likely cause.\n\n",
        )
        if IS_WINDOWS:
            body = QCoreApplication.translate(
                "SettingsDialog",
                "Windows\n"
                "When Windows asks on first launch, click “Allow access” "
                "for private networks. To change it later: Windows Security → "
                "Firewall & network protection → Allow an app through firewall "
                "→ enable jellytoast on Private networks.",
            )
        elif IS_MACOS:
            body = QCoreApplication.translate(
                "SettingsDialog",
                "macOS\n"
                "System Settings → Network → Firewall → Options → "
                "allow incoming connections for jellytoast (or click "
                "“Allow” if prompted on launch).",
            )
        else:
            body = QCoreApplication.translate(
                "SettingsDialog",
                "Linux — ufw\n    sudo ufw allow from {0}\n\n"
                "Linux — firewalld\n"
                "    sudo firewall-cmd --permanent --add-rich-rule="
                "'rule family=ipv4 source address={0} accept'\n"
                "    sudo firewall-cmd --reload",
            ).format(subnet)
        ports = QCoreApplication.translate(
            "SettingsDialog",
            "\n\nPorts used: UDP 5353 (mDNS / AirPlay), UDP 1900 (SSDP / DLNA), "
            "TCP {0} (cast proxy).",
        ).format(_PROXY_PORT)
        return intro + body + ports

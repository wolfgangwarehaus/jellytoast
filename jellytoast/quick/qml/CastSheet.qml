// "Play on…" — pick a speaker / TV to cast to, or come back to this phone.
// While casting, a card up top shows where it's playing, the speaker's own
// volume (each speaker's, for a group) and Stop casting.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Drawer {
    id: sheet
    objectName: "castSheet"
    signal openSettings()
    parent: Overlay.overlay
    edge: Qt.BottomEdge
    width: parent ? parent.width : 400
    // Fixed height: devices are discovered while the sheet is open, and a
    // sheet that grew with them would move rows under the finger.
    height: parent ? Math.round(parent.height * 0.78) : 600
    modal: true
    dim: true
    Overlay.modal: Rectangle { color: "#80000000" }
    onOpened: {
        if (cast.enabled)
            cast.scan()
        if (cast.activeKind === "group")
            cast.loadMembers()
    }
    onClosed: { cast.stopScan(); membersOpen = false }

    property bool membersOpen: false

    function glyphFor(kind, type) {
        if (type === "airplay")
            return "airplay"
        return kind === "group" ? "speaker_group" : kind === "display" ? "tv" : "speaker"
    }
    function typeLabel(type) {
        return ({ chromecast: "Chromecast", dlna: "DLNA", sonos: "Sonos", airplay: "AirPlay" })[type] || ""
    }

    background: Rectangle {
        color: theme.surface
        radius: 18
        Rectangle {  // square off the bottom corners
            anchors.bottom: parent.bottom
            width: parent.width
            height: 18
            color: theme.surface
        }
    }

    Rectangle {  // grab handle
        anchors.top: parent.top
        anchors.topMargin: 8
        anchors.horizontalCenter: parent.horizontalCenter
        width: 40; height: 4; radius: 2
        color: theme.textFaint
    }

    // ── header ──
    RowLayout {
        id: header
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.topMargin: 22
        anchors.leftMargin: 20
        anchors.rightMargin: 8
        height: 44
        Label {
            Layout.fillWidth: true
            text: qsTr("Play on")
            color: theme.text
            font.pixelSize: 20
            font.weight: Font.DemiBold
        }
        BusyIndicator {
            Layout.preferredWidth: 26; Layout.preferredHeight: 26
            running: cast.scanning
            visible: running
        }
        IconButton {
            objectName: "castRescan"
            visible: cast.enabled && !cast.scanning
            iconName: "refresh"
            onClicked: cast.scan()
        }
    }

    Flickable {
        id: body
        anchors.top: header.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.topMargin: 6
        contentHeight: col.implicitHeight + 24
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100
        clip: true

        ColumnLayout {
            id: col
            x: 16
            width: body.width - 32
            spacing: 2

            // Nothing enabled → nothing can be found.
            ColumnLayout {
                visible: !cast.enabled
                Layout.fillWidth: true
                Layout.topMargin: 8
                spacing: 12
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    color: theme.textDim
                    text: qsTr("Turn on the kinds of speakers and TVs you use in Settings → Casting.")
                }
                PillButton {
                    objectName: "castOpenSettings"
                    text: qsTr("Open Settings")
                    onClicked: { sheet.close(); sheet.openSettings() }
                }
            }

            // ── now casting ──
            Rectangle {
                id: card
                objectName: "castCard"
                visible: cast.active !== ""
                Layout.fillWidth: true
                Layout.preferredHeight: cardCol.implicitHeight + 32
                Layout.bottomMargin: 14
                radius: 18
                color: theme.raised

                ColumnLayout {
                    id: cardCol
                    anchors.fill: parent
                    anchors.margins: 16
                    spacing: 12

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 14
                        Rectangle {
                            Layout.preferredWidth: 52; Layout.preferredHeight: 52
                            radius: 26
                            color: theme.accent
                            Icon {
                                anchors.centerIn: parent
                                name: sheet.glyphFor(cast.activeKind, "")
                                tint: theme.onAccent
                                size: 26
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1
                            Label {
                                text: qsTr("PLAYING ON")
                                color: theme.accent
                                font.pixelSize: 11
                                font.letterSpacing: 1.2
                                font.weight: Font.DemiBold
                            }
                            Label {
                                objectName: "castCardName"
                                Layout.fillWidth: true
                                text: cast.active
                                color: theme.text
                                elide: Text.ElideRight
                                font.pixelSize: 18
                                font.weight: Font.DemiBold
                            }
                        }
                    }

                    // The speaker's own volume (never the phone's).
                    RowLayout {
                        Layout.fillWidth: true
                        visible: cast.volumeControl
                        spacing: 10
                        Icon {
                            name: volSlider.value < 1 ? "volume_muted" : "volume"
                            tint: theme.textDim
                            size: 20
                        }
                        AppSlider {
                            id: volSlider
                            objectName: "castVolume"
                            Layout.fillWidth: true
                            from: 0; to: 100; stepSize: 1
                            value: cast.volume
                            live: true
                            // A drag sends a level per tick; the device gets
                            // at most a few a second.
                            onMoved: volPush.restart()
                            onPressedChanged: if (!pressed) { volPush.stop(); cast.setVolume(value) }
                            Timer { id: volPush; interval: 150; onTriggered: cast.setVolume(volSlider.value) }
                        }
                        Label {
                            Layout.preferredWidth: 34
                            horizontalAlignment: Text.AlignRight
                            text: Math.round(volSlider.value)
                            color: theme.textDim
                            font.pixelSize: 13
                            font.features: { "tnum": 1 }
                        }
                    }

                    // A group: each speaker's own level.
                    ItemDelegate {
                        objectName: "castMembersToggle"
                        visible: cast.activeKind === "group"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 40
                        leftPadding: 0; rightPadding: 0
                        focusPolicy: Qt.NoFocus
                        hoverEnabled: false
                        background: null
                        onClicked: {
                            sheet.membersOpen = !sheet.membersOpen
                            if (sheet.membersOpen && cast.members.length === 0)
                                cast.loadMembers()
                        }
                        contentItem: RowLayout {
                            spacing: 10
                            Icon { name: "speaker"; tint: theme.textDim; size: 20 }
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("Speakers in this group")
                                color: theme.text
                                font.pixelSize: 14
                            }
                            BusyIndicator {
                                Layout.preferredWidth: 20; Layout.preferredHeight: 20
                                running: cast.membersLoading && sheet.membersOpen
                                visible: running
                            }
                            Icon {
                                name: "chevron_down"
                                tint: theme.textDim
                                size: 18
                                rotation: sheet.membersOpen ? 180 : 0
                                Behavior on rotation { NumberAnimation { duration: 150 } }
                            }
                        }
                    }
                    Repeater {
                        model: cast.members
                        ColumnLayout {
                            required property var modelData
                            visible: sheet.membersOpen && cast.activeKind === "group"
                            Layout.fillWidth: true
                            Layout.leftMargin: 30
                            spacing: 0
                            Label {
                                Layout.fillWidth: true
                                text: modelData.name
                                color: modelData.available ? theme.text : theme.textFaint
                                elide: Text.ElideRight
                                font.pixelSize: 13
                            }
                            AppSlider {
                                id: memberSlider
                                Layout.fillWidth: true
                                enabled: modelData.available
                                from: 0; to: 100; stepSize: 1
                                value: modelData.volume
                                onMoved: memberPush.restart()
                                onPressedChanged: if (!pressed) { memberPush.stop(); cast.setMemberVolume(modelData.uuid, value) }
                                Timer {
                                    id: memberPush
                                    interval: 150
                                    onTriggered: cast.setMemberVolume(modelData.uuid, memberSlider.value)
                                }
                            }
                        }
                    }

                    PillButton {
                        objectName: "castStop"
                        Layout.alignment: Qt.AlignHCenter
                        Layout.topMargin: 2
                        iconName: "stop"
                        text: qsTr("Stop casting")
                        onClicked: cast.disconnect()
                    }
                }
            }

            component SectionLabel: Label {
                Layout.fillWidth: true
                Layout.topMargin: 12
                Layout.bottomMargin: 2
                leftPadding: 4
                color: theme.textDim
                font.pixelSize: 12
                font.letterSpacing: 1.0
                font.weight: Font.DemiBold
            }

            component DeviceRow: ItemDelegate {
                id: drow
                property string label
                property string detail
                property string glyph: "cast"
                property bool current: false
                property bool busy: false
                Layout.fillWidth: true
                Layout.preferredHeight: 60
                focusPolicy: Qt.NoFocus
                hoverEnabled: false
                // Same geometry as SheetRow (the other sheets' rows): 22 px
                // glyph, 16 px gap, 10 px corners — plus a detail line.
                leftPadding: 4
                background: Rectangle {
                    radius: 10
                    color: drow.current ? theme.accent : theme.text
                    opacity: drow.current ? 0.12 : (drow.pressed ? 0.08 : 0)
                }
                contentItem: RowLayout {
                    spacing: 16
                    Icon {
                        name: drow.glyph
                        tint: drow.current ? theme.accent : theme.textDim
                        size: 22
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        Label {
                            Layout.fillWidth: true
                            text: drow.label
                            color: drow.current ? theme.accent : theme.text
                            elide: Text.ElideRight
                            font.pixelSize: 16
                            font.weight: drow.current ? Font.DemiBold : Font.Normal
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: text !== ""
                            text: drow.busy ? qsTr("Connecting…") : drow.detail
                            color: theme.textDim
                            elide: Text.ElideRight
                            font.pixelSize: 12
                        }
                    }
                    BusyIndicator {
                        Layout.preferredWidth: 24; Layout.preferredHeight: 24
                        running: drow.busy
                        visible: running
                    }
                    Icon { visible: drow.current; name: "check"; tint: theme.accent; size: 20 }
                }
            }

            DeviceRow {
                objectName: "castThisDevice"
                visible: cast.enabled
                label: qsTr("This phone")
                glyph: "phone"
                current: cast.active === ""
                onClicked: { if (cast.active !== "") cast.disconnect(); sheet.close() }
            }

            SectionLabel {
                visible: cast.enabled && speakerRows.count > 0
                text: qsTr("SPEAKERS & TVS")
            }
            Repeater {
                id: speakerRows
                // Only `devices` here: any other dependency (enabled,
                // scanning…) would re-run the binding and rebuild every row
                // under the finger.
                model: cast.devices.filter(d => d.kind !== "group")
                DeviceRow {
                    required property var modelData
                    objectName: "castDevice_" + modelData.name
                    visible: cast.enabled
                    label: modelData.name
                    detail: sheet.typeLabel(modelData.type)
                    glyph: sheet.glyphFor(modelData.kind, modelData.type)
                    current: cast.active === modelData.name
                    busy: cast.connecting === modelData.uuid
                    onClicked: cast.castTo(modelData.uuid)
                }
            }

            SectionLabel {
                visible: cast.enabled && groupRows.count > 0
                text: qsTr("GROUPS")
            }
            Repeater {
                id: groupRows
                model: cast.devices.filter(d => d.kind === "group")
                DeviceRow {
                    required property var modelData
                    objectName: "castDevice_" + modelData.name
                    visible: cast.enabled
                    label: modelData.name
                    detail: qsTr("Speaker group")
                    glyph: "speaker_group"
                    current: cast.active === modelData.name
                    busy: cast.connecting === modelData.uuid
                    onClicked: cast.castTo(modelData.uuid)
                }
            }

            ColumnLayout {
                visible: cast.enabled && !cast.scanning && cast.devices.length === 0
                Layout.fillWidth: true
                Layout.topMargin: 14
                spacing: 12
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    color: theme.textDim
                    text: qsTr("No speakers or TVs found on this network.")
                }
                PillButton {
                    objectName: "castSearchAgain"
                    text: qsTr("Search again")
                    onClicked: cast.scan()
                }
            }
            Label {
                visible: cast.enabled && cast.scanning && cast.devices.length === 0
                Layout.fillWidth: true
                Layout.topMargin: 14
                color: theme.textDim
                text: qsTr("Looking for speakers and TVs…")
            }
        }
    }
}

// "Play on…" — pick a speaker / TV to cast to, or come back to this device.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Drawer {
    id: sheet
    objectName: "castSheet"
    signal openSettings()
    edge: Qt.BottomEdge
    width: parent ? parent.width : 400
    // Fixed height: devices are discovered while the sheet is open, and a
    // sheet that grew with them would move rows under the finger.
    height: parent ? Math.round(parent.height * 0.6) : 480
    modal: true
    onOpened: if (cast.enabled) cast.scan()
    onClosed: cast.stopScan()

    background: Rectangle {
        color: theme.surface
        radius: 16
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 16; color: theme.surface }
    }

    ColumnLayout {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 16
        spacing: 4

        Rectangle {  // grab handle
            Layout.alignment: Qt.AlignHCenter
            width: 36; height: 4; radius: 2
            color: theme.textFaint
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.topMargin: 8
            Label {
                Layout.fillWidth: true
                text: qsTr("Play on")
                color: theme.text
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }
            BusyIndicator {
                Layout.preferredWidth: 28; Layout.preferredHeight: 28
                running: cast.scanning
                visible: running
            }
        }

        // Nothing enabled → nothing can be found.
        ColumnLayout {
            visible: !cast.enabled
            Layout.fillWidth: true
            spacing: 10
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

        component DeviceRow: ItemDelegate {
            id: drow
            property string label
            property string glyph: "cast"
            property bool current: false
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            focusPolicy: Qt.NoFocus
            hoverEnabled: false
            background: Rectangle { radius: 10; color: theme.text; opacity: drow.pressed ? 0.08 : 0 }
            contentItem: RowLayout {
                spacing: 14
                Icon { name: drow.glyph; tint: drow.current ? theme.accent : theme.textDim; size: 22 }
                Label {
                    Layout.fillWidth: true
                    text: drow.label
                    color: drow.current ? theme.accent : theme.text
                    elide: Text.ElideRight
                    font.pixelSize: 16
                    font.weight: drow.current ? Font.DemiBold : Font.Normal
                }
                Icon { visible: drow.current; name: "check"; tint: theme.accent; size: 20 }
            }
        }

        DeviceRow {
            objectName: "castThisDevice"
            visible: cast.enabled
            label: qsTr("This device")
            glyph: "volume"
            current: cast.active === ""
            onClicked: { if (cast.active !== "") cast.disconnect(); sheet.close() }
        }
        Repeater {
            model: cast.enabled ? cast.devices : []
            DeviceRow {
                required property var modelData
                label: modelData.name
                glyph: modelData.type === "airplay" ? "airplay" : "cast"
                current: cast.active === modelData.name
                onClicked: { cast.castTo(modelData.uuid); sheet.close() }
            }
        }
        Label {
            visible: cast.enabled && !cast.scanning && cast.devices.length === 0
            Layout.fillWidth: true
            Layout.topMargin: 6
            wrapMode: Text.WordWrap
            color: theme.textDim
            text: qsTr("No speakers or TVs found on this network.")
        }
    }
}

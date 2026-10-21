// Phone settings: account, playback, about.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    objectName: "settingsPage"
    property string title: qsTr("Settings")
    readonly property bool loading: false

    component Heading: Label {
        Layout.fillWidth: true
        Layout.topMargin: 18
        leftPadding: 16
        color: theme.textDim
        font.pixelSize: 12
        font.weight: Font.DemiBold
        font.letterSpacing: 1
    }

    // A row showing a choice; tap to pick from `options` ({label, value}).
    component ChoiceRow: ItemDelegate {
        id: choice
        property string label
        property var options: []
        property string value
        signal picked(string value)
        Layout.fillWidth: true
        height: 56
        focusPolicy: Qt.NoFocus
        hoverEnabled: false
        background: Rectangle { color: theme.text; opacity: choice.pressed ? 0.08 : 0 }
        contentItem: RowLayout {
            Label { text: choice.label; color: theme.text; font.pixelSize: 16; Layout.fillWidth: true; leftPadding: 4 }
            Label {
                text: (choice.options.find(o => o.value === choice.value) || { label: choice.value }).label
                color: theme.textDim
                font.pixelSize: 15
            }
            Icon { name: "chevron_down"; tint: theme.textDim; size: 16 }
        }
        onClicked: menu.popup(choice, choice.width - menu.width, choice.height)
        Menu {
            id: menu
            Repeater {
                model: choice.options
                MenuItem {
                    required property var modelData
                    text: modelData.label
                    checkable: true
                    checked: modelData.value === choice.value
                    onTriggered: choice.picked(modelData.value)
                }
            }
        }
    }

    Flickable {
        anchors.fill: parent
        contentHeight: col.implicitHeight + 32
        boundsBehavior: Flickable.DragOverBounds
        clip: true

        ColumnLayout {
            id: col
            width: parent.width
            spacing: 0

            Heading { text: qsTr("ACCOUNT") }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; rightPadding: 20; topPadding: 8
                text: session.username
                color: theme.text
                font.pixelSize: 17
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; rightPadding: 20; bottomPadding: 12
                text: (session.serverKind === "subsonic" ? "Navidrome / Subsonic" : "Jellyfin") + " · " + session.serverUrl
                color: theme.textDim
                font.pixelSize: 13
                elide: Text.ElideMiddle
            }
            PillButton {
                objectName: "signOutButton"
                Layout.leftMargin: 16
                text: qsTr("Sign out")
                onClicked: confirm.open()
            }

            Heading { text: qsTr("PLAYBACK") }
            ChoiceRow {
                objectName: "qualityRow"
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Streaming quality")
                options: prefs.qualities
                value: prefs.quality
                onPicked: v => prefs.setQuality(v)
            }
            ChoiceRow {
                objectName: "downloadQualityRow"
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Download quality")
                options: prefs.qualities
                value: prefs.downloadQuality
                onPicked: v => prefs.setDownloadQuality(v)
            }
            ChoiceRow {
                objectName: "normalizationRow"
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Normalization")
                options: prefs.normalizations
                value: prefs.normalization
                onPicked: v => prefs.setNormalization(v)
            }
            SwitchRow {
                objectName: "mediaKeysSwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                text: qsTr("Lock screen & media controls")
                checked: prefs.mediaKeys
                onToggled: prefs.setMediaKeys(checked)
            }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; rightPadding: 20
                text: qsTr("Takes effect next time jellytoast starts.")
                color: theme.textDim
                font.pixelSize: 12
            }

            Heading { text: qsTr("OFFLINE") }
            SwitchRow {
                objectName: "offlineModeSwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                text: qsTr("Offline mode — only play downloads")
                checked: prefs.offlineMode
                onToggled: prefs.setOfflineMode(checked)
            }
            SwitchRow {
                objectName: "wifiOnlySwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                text: qsTr("Download on Wi-Fi only")
                checked: prefs.wifiOnly
                onToggled: prefs.setWifiOnly(checked)
            }

            Heading { text: qsTr("CASTING") }
            Repeater {
                model: prefs.castTypes
                SwitchRow {
                    required property var modelData
                    objectName: "cast_" + modelData.key
                    Layout.fillWidth: true
                    Layout.leftMargin: 12; Layout.rightMargin: 12
                    text: modelData.label
                    checked: modelData.on
                    onToggled: prefs.setCastType(modelData.key, checked)
                }
            }

            Heading { text: qsTr("ABOUT") }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; topPadding: 8
                text: "jellytoast " + prefs.version()
                color: theme.textDim
                font.pixelSize: 14
            }
        }
    }

    Dialog {
        id: confirm
        objectName: "signOutDialog"
        anchors.centerIn: parent
        width: Math.min(parent.width - 48, 360)
        modal: true
        title: qsTr("Sign out?")
        standardButtons: Dialog.Cancel | Dialog.Ok
        Label {
            width: confirm.availableWidth
            wrapMode: Text.WordWrap
            color: theme.text
            text: qsTr("Playback stops, the queue is cleared and pending downloads are cancelled.")
        }
        onAccepted: session.signOut()
    }
}

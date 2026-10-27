// Internet radio: your stations (tap to play), Browse popular, Add station.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Jellytoast

Item {
    id: page
    objectName: "radioPage"
    property string title: qsTr("Radio")
    readonly property bool loading: stations.loading

    RadioModel {
        id: stations
        onFailed: msg => toast.show(msg)
    }
    Component.onCompleted: stations.reload()

    Row {
        id: actionsRow
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.topMargin: 4
        spacing: 10
        // Both pills at full size ran off both edges of a 360 px phone:
        // drop their icons and trim padding when they wouldn't fit.
        readonly property bool tight: browsePill.fullWidth + addPill.fullWidth + spacing > parent.width - 32
        PillButton {
            id: browsePill
            compact: actionsRow.tight
            objectName: "browsePopular"
            iconName: "search"
            text: qsTr("Browse popular")
            onClicked: popularSheet.open()
        }
        PillButton {
            id: addPill
            compact: actionsRow.tight
            objectName: "addStation"
            iconName: "music_note"
            text: qsTr("Add station")
            onClicked: addDialog.open()
        }
    }

    Label {
        anchors.centerIn: parent
        width: parent.width - 48
        visible: !stations.loading && stations.count === 0
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        color: theme.textDim
        text: qsTr("No stations yet. Browse popular ones or add a stream address.")
    }

    ListView {
        id: list
        objectName: "stationList"
        anchors.top: actionsRow.bottom
        anchors.topMargin: 10
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        clip: true
        model: stations
        boundsBehavior: Flickable.DragAndOvershootBounds
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100
        maximumFlickVelocity: theme.scrollMaxVelocity
        delegate: ItemDelegate {
            id: srow
            required property int index
            required property string name
            required property string subtitle
            required property string cover
            readonly property bool playing: player.live && player.station === name
            width: list.width
            height: 68
            focusPolicy: Qt.NoFocus
            hoverEnabled: false
            leftPadding: 12
            rightPadding: 16
            background: Rectangle { color: theme.text; opacity: srow.pressed ? 0.08 : 0 }
            onClicked: stations.play(index)
            onPressAndHold: {
                stationSheet.row = index
                stationSheet.name = name
                stationSheet.open()
            }
            contentItem: RowLayout {
                spacing: 12
                Item {
                    Layout.preferredWidth: 48
                    Layout.preferredHeight: 48
                    Rectangle { anchors.fill: parent; radius: 8; color: theme.surface }
                    Icon { anchors.centerIn: parent; name: "volume"; size: 22; tint: theme.textDim; visible: srow.cover === "" }
                    Cover { anchors.fill: parent; radius: 8; pixels: 144; glyph: "volume"; source: srow.cover; visible: srow.cover !== "" }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    Label {
                        Layout.fillWidth: true
                        text: srow.name
                        color: srow.playing ? theme.accent : theme.text
                        elide: Text.ElideRight
                        font.pixelSize: 15
                        font.weight: Font.Medium
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: srow.subtitle
                        color: theme.textDim
                        elide: Text.ElideRight
                        font.pixelSize: 12
                    }
                }
                Label {
                    visible: srow.playing
                    text: "● " + qsTr("LIVE")
                    color: theme.accent
                    font.pixelSize: 11
                    font.weight: Font.Bold
                }
            }
        }
    }

    // Long-press: play / remove one station.
    Sheet {
        id: stationSheet
        objectName: "stationSheet"
        property int row: -1
        property string name: ""
        Label {
            width: parent.width
            text: stationSheet.name
            color: theme.text
            elide: Text.ElideRight
            font.pixelSize: 17
            font.weight: Font.DemiBold
            bottomPadding: 4
        }
        SheetRow {
            iconName: "play"
            text: qsTr("Play")
            onClicked: { stations.play(stationSheet.row); stationSheet.close() }
        }
        SheetRow {
            objectName: "removeStation"
            iconName: "win_close"
            tint: "#e57373"
            text: qsTr("Remove station")
            onClicked: { stations.remove(stationSheet.row); stationSheet.close() }
        }
    }

    // The curated list, by category; tap one to add it to your stations.
    Sheet {
        id: popularSheet
        objectName: "popularSheet"
        property var items: []
        onAboutToShow: items = stations.popular()
        SheetHeading { text: qsTr("POPULAR STATIONS") }
        Repeater {
            model: popularSheet.items
            Column {
                required property var modelData
                required property int index
                width: parent ? parent.width : 300
                SheetHeading {
                    visible: index === 0 || popularSheet.items[index - 1].category !== modelData.category
                    text: modelData.category.toUpperCase()
                    font.pixelSize: 11
                }
                SheetRow {
                    iconName: modelData.added ? "check" : "volume"
                    tint: modelData.added ? theme.accent : theme.text
                    text: modelData.name
                    enabled: !modelData.added
                    onClicked: {
                        stations.add(modelData.name, modelData.streamUrl, modelData.homePageUrl)
                        toast.show(qsTr("Adding %1").arg(modelData.name))
                        popularSheet.close()
                    }
                }
            }
        }
    }

    Dialog {
        id: addDialog
        objectName: "addStationDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 380)
        modal: true
        title: qsTr("Add station")
        standardButtons: Dialog.Cancel | Dialog.Save
        onOpened: { nameField.text = ""; urlField.text = ""; nameField.forceActiveFocus() }
        Column {
            width: addDialog.availableWidth
            spacing: 10
            TextField {
                id: nameField
                objectName: "stationName"
                width: parent.width
                placeholderText: qsTr("Name")
                color: theme.text
            }
            TextField {
                id: urlField
                objectName: "stationUrl"
                width: parent.width
                placeholderText: qsTr("Stream address (http…)")
                color: theme.text
                inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
            }
        }
        onAccepted: stations.add(nameField.text, urlField.text, "")
    }
}

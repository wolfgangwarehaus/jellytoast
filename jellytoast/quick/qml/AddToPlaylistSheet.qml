// "Add to playlist": a new playlist (name it right here) or one of yours.
// show(item) where item = { kind, id, name, … } (a song, album, playlist
// or artist — all of its songs are added).
import QtQuick
import QtQuick.Controls

Sheet {
    id: sheet
    objectName: "addToPlaylistSheet"
    property var item: ({})
    property bool naming: false

    function show(it) {
        item = it
        naming = false
        nameField.text = ""
        playlists.refresh()
        open()
    }

    SheetHeading { text: qsTr("ADD TO PLAYLIST") }
    Label {
        width: parent.width
        text: sheet.item.name || ""
        color: theme.text
        elide: Text.ElideRight
        font.pixelSize: 17
        font.weight: Font.DemiBold
        bottomPadding: 4
    }

    SheetRow {
        objectName: "newPlaylistRow"
        visible: !sheet.naming
        iconName: "music_note"
        text: qsTr("New playlist…")
        onClicked: {
            sheet.naming = true
            nameField.forceActiveFocus()
        }
    }
    Row {
        width: parent.width
        spacing: 8
        visible: sheet.naming
        TextField {
            id: nameField
            objectName: "newPlaylistName"
            width: parent.width - createButton.width - 8
            height: 44
            placeholderText: qsTr("Playlist name")
            placeholderTextColor: theme.textFaint
            color: theme.text
            font.pixelSize: 16
            leftPadding: 14
            background: Rectangle { radius: 12; color: theme.background }
            onAccepted: createButton.clicked()
        }
        PillButton {
            id: createButton
            objectName: "createPlaylist"
            filled: true
            text: qsTr("Create")
            enabled: nameField.text.trim() !== ""
            onClicked: {
                playlists.createWith(nameField.text, sheet.item.kind, sheet.item.id)
                sheet.close()
            }
        }
    }

    BusyIndicator {
        anchors.horizontalCenter: parent.horizontalCenter
        running: playlists.loading && playlists.items.length === 0
        visible: running
    }
    Repeater {
        model: playlists.items
        SheetRow {
            required property var modelData
            iconName: "list"
            text: modelData.name
            trailing: 44
            onClicked: {
                playlists.addTo(modelData.id, modelData.name, sheet.item.kind, sheet.item.id)
                sheet.close()
            }
            Label {
                anchors.right: parent.right
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: modelData.count
                color: theme.textDim
                font.pixelSize: 13
            }
        }
    }
}

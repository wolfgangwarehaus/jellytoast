// Rename / delete a playlist — shared dialogs (Main owns one instance).
import QtQuick
import QtQuick.Controls

Item {
    id: dialogs
    signal deleted(string playlistId)

    function rename(it) {
        renameDialog.item = it
        renameField.text = it.name || ""
        renameDialog.open()
        renameField.forceActiveFocus()
        renameField.selectAll()
    }
    function confirmDelete(it) {
        deleteDialog.item = it
        deleteDialog.open()
    }

    Dialog {
        id: renameDialog
        objectName: "renameDialog"
        property var item: ({})
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 380)
        modal: true
        title: qsTr("Rename playlist")
        standardButtons: Dialog.Cancel | Dialog.Save
        TextField {
            id: renameField
            objectName: "renameField"
            width: renameDialog.availableWidth
            color: theme.text
            font.pixelSize: 16
            onAccepted: renameDialog.accept()
        }
        onAccepted: playlists.rename(item.id, renameField.text)
    }

    Dialog {
        id: deleteDialog
        objectName: "deletePlaylistDialog"
        property var item: ({})
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 380)
        modal: true
        title: qsTr("Delete playlist?")
        standardButtons: Dialog.Cancel | Dialog.Ok
        Label {
            objectName: "deletePlaylistText"
            width: deleteDialog.availableWidth
            wrapMode: Text.WordWrap
            color: theme.text
            // Only while open: wrapping a long message in a dialog that isn't
            // laid out yet was a binding loop on its height at start.
            text: deleteDialog.visible
                  ? playlists.deleteMessage(deleteDialog.item.name || "",
                                            deleteDialog.item.sourceFile || "")
                  : ""
        }
        onAccepted: {
            playlists.remove(item.id, item.name)
            dialogs.deleted(item.id)
        }
    }
}

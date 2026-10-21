// Genres, A–Z. Tap one for its albums.
import QtQuick
import QtQuick.Controls
import Jellytoast

Item {
    id: page
    property string title: qsTr("Genres")
    readonly property bool loading: lib.loading
    signal openItem(string kind, var item)
    LibraryModel { id: lib; kind: "genre" }
    Component.onCompleted: if (session.signedIn) lib.reload()

    ListView {
        id: list
        objectName: "genreList"
        anchors.fill: parent
        clip: true
        model: session.signedIn ? lib : null
        boundsBehavior: Flickable.DragOverBounds
        maximumFlickVelocity: 8000
        flickDeceleration: 2500
        pixelAligned: true
        delegate: ItemDelegate {
            id: gRow
            required property string itemId
            required property string name
            width: list.width
            height: 56
            focusPolicy: Qt.NoFocus
            hoverEnabled: false
            background: Rectangle { color: theme.text; opacity: gRow.pressed ? 0.08 : 0 }
            contentItem: Label {
                leftPadding: 8
                text: gRow.name
                color: theme.text
                font.pixelSize: 16
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            onClicked: page.openItem("genre", { kind: "genre", id: gRow.itemId, name: gRow.name })
        }
        footer: Item {
            width: list.width
            height: lib.loading ? 56 : 0
            BusyIndicator { anchors.centerIn: parent; running: lib.loading; visible: running }
        }
    }
}

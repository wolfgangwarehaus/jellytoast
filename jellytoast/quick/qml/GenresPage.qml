// Genres, A–Z. Tap one for its albums.
import QtQuick
import QtQuick.Controls
import Jellytoast

Item {
    // The top bar and mini player float over the page as frosted glass, so
    // content scrolls under them; these keep it clear at rest (see Main).
    readonly property real insetTop: StackView.view ? StackView.view.insetTop : 0
    readonly property real insetBottom: StackView.view ? StackView.view.insetBottom : 0
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
        topMargin: page.insetTop
        bottomMargin: page.insetBottom
        model: session.signedIn ? lib : null
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
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
            onPressAndHold: menu.show({ kind: "genre", id: gRow.itemId, name: gRow.name }, gRow, width / 2, height / 2)
        }
        footer: Item {
            width: list.width
            height: lib.loading ? 56 : 0
            BusyIndicator { anchors.centerIn: parent; running: lib.loading; visible: running }
        }
    }
    ItemMenu { id: menu }
}

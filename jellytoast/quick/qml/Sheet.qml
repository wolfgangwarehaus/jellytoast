// A bottom sheet: slides up over a dimmed screen, drag down (or tap the
// dim) to close. Content goes in its column; it scrolls if it's taller than
// most of the screen. The mobile stand-in for desktop menus.
import QtQuick
import QtQuick.Controls

Drawer {
    id: sheet
    default property alias content: col.data
    property int sidePadding: 16

    parent: Overlay.overlay
    edge: Qt.BottomEdge
    modal: true
    width: parent ? parent.width : 400
    height: Math.min(col.implicitHeight + 36, (parent ? parent.height : 800) * 0.85)
    dim: true
    Overlay.modal: Rectangle { color: "#80000000" }

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
        id: handle
        anchors.top: parent.top
        anchors.topMargin: 8
        anchors.horizontalCenter: parent.horizontalCenter
        width: 40
        height: 4
        radius: 2
        color: theme.textFaint
    }

    Flickable {
        anchors.fill: parent
        anchors.topMargin: 22
        contentHeight: col.implicitHeight + 12
        boundsBehavior: Flickable.StopAtBounds
        clip: true
        interactive: contentHeight > height
        Column {
            id: col
            x: sheet.sidePadding
            width: parent.width - 2 * sheet.sidePadding
            spacing: 6
        }
    }
}

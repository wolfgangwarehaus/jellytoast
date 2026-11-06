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

    // Frosted glass over the app (the page stack, or Now Playing while it's
    // up — both layers, so this is one quad per frame; the sheet lives in
    // the overlay, outside them). Runs past the bottom edge so only the top
    // corners are rounded; `track` follows the slide-in.
    background: Item {
        FrostedPane {
            anchors.fill: parent
            anchors.bottomMargin: -18
            // Only while open: a sheet keeping the window's content as its
            // texture source crashed window teardown.
            // (The window is null until the sheet is parented into the overlay.)
            behind: sheet.visible && ApplicationWindow.window ? ApplicationWindow.window.frostBehind : null
            track: sheet.position
            radius: 18
            tint: theme.surface
            tintOpacity: 0.78
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

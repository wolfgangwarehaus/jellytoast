// One of the app's SVG glyphs (jellytoast/icons.py), tinted.
//
// An Item around the Image, not a bare Image: layouts size children by
// their implicit size, and an Image's (read-only) implicit size is its pixel
// size — size × the screen scale. On a 2.5× phone a 24 px icon laid out at
// 60 px and spilled over its row (the cast sheet, 2026-10-27).
import QtQuick

Item {
    id: root
    property string name
    property color tint: theme.text
    property int size: 24

    function hex(c) {
        return [c.r, c.g, c.b].map(v => ("0" + Math.round(v * 255).toString(16)).slice(-2)).join("")
    }

    width: size
    height: size
    implicitWidth: size
    implicitHeight: size

    Image {
        anchors.fill: parent
        sourceSize.width: root.size * Screen.devicePixelRatio
        sourceSize.height: root.size * Screen.devicePixelRatio
        source: root.name ? "image://icon/" + root.name + "/" + root.hex(root.tint) : ""
        smooth: true
    }
}

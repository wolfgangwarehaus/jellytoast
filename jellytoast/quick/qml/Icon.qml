// One of the app's SVG glyphs (jellytoast/icons.py), tinted.
import QtQuick

Image {
    id: root
    property string name
    property color tint: theme.text
    property int size: 24

    function hex(c) {
        return [c.r, c.g, c.b].map(v => ("0" + Math.round(v * 255).toString(16)).slice(-2)).join("")
    }

    width: size
    height: size
    sourceSize.width: size * Screen.devicePixelRatio
    sourceSize.height: size * Screen.devicePixelRatio
    source: name ? "image://icon/" + name + "/" + hex(tint) : ""
    smooth: true
}

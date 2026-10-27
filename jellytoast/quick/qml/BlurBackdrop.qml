// A blurred-art backdrop: the given cover blown up and heavily blurred under
// a theme-coloured wash, so a page takes its mood from its album / artist.
// Shared by Now Playing (full screen) and the album / artist page headers
// (which fade into the page at the bottom). Off with Settings → Frosted glass.
import QtQuick
import QtQuick.Effects

Item {
    id: root
    property string source: ""
    // How much page colour sits over the blur (higher = calmer).
    property real wash: 0.6
    // Fade into the page background along the bottom (page headers).
    property bool fadeBottom: false

    clip: true
    visible: source !== "" && prefs.frosted

    Cover {
        id: art
        anchors.centerIn: parent
        width: Math.max(parent.width, parent.height) * 1.25
        height: width
        radius: 0
        pixels: 160  // plenty once blurred
        glyph: ""
        pulse: false  // the blurred backdrop shouldn't breathe while loading
        source: root.source
        fallback: root.source
        layer.enabled: true
        visible: false
    }
    MultiEffect {
        anchors.fill: art
        source: art
        blurEnabled: true
        blur: 1.0
        blurMax: 64
        saturation: 0.15
    }
    Rectangle {
        anchors.fill: parent
        color: theme.background
        opacity: root.wash
    }
    Rectangle {
        visible: root.fadeBottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: parent.height * 0.5
        gradient: Gradient {
            GradientStop { position: 0.0; color: "transparent" }
            GradientStop { position: 1.0; color: theme.background }
        }
    }
}

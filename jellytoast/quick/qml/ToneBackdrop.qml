// A colour picked from a cover, washing down from the top into the page
// colour — Now Playing's backdrop and the album / artist page headers
// (august: "a colour from the album art"). Softened for the theme: deep and
// muted on dark, pale on light; crossfades when it changes. Off with
// Settings → Frosted glass.
//
// Give it either `source` (a cover; its colour comes from `covers.tone`) or
// `baseTone` + `hasTone` (Now Playing passes the player's).
import QtQuick

Rectangle {
    id: root
    property string source: ""
    property color baseTone: "transparent"
    property bool hasTone: false

    property string picked: ""
    function lookup() { picked = source !== "" ? covers.tone(source) : "" }
    onSourceChanged: lookup()
    Component.onCompleted: lookup()
    Connections {
        target: covers
        function onToneReady(src, name) { if (src === root.source) root.picked = name }
    }

    readonly property bool known: source !== "" ? picked !== "" : hasTone
    readonly property color base: source !== "" && picked !== "" ? picked : baseTone
    readonly property bool lightTheme: theme.background.hslLightness > 0.5
    property color tone: {
        var c = root.base
        var h = c.hslHue < 0 ? 0 : c.hslHue
        var s = Math.min(c.hslSaturation, lightTheme ? 0.45 : 0.55)
        return Qt.hsla(h, s, lightTheme ? 0.84 : 0.26, 1)
    }
    Behavior on tone { ColorAnimation { duration: 600 } }

    visible: prefs.frosted && known
    gradient: Gradient {
        GradientStop { position: 0.0; color: root.tone }
        GradientStop {
            position: 0.55
            color: Qt.tint(root.tone, Qt.rgba(theme.background.r, theme.background.g, theme.background.b, 0.55))
        }
        GradientStop { position: 1.0; color: theme.background }
    }
}

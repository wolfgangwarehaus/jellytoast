// Album art with rounded corners. `fallback` (an already-loaded smaller
// image) shows underneath until `source` is ready, so art never blinks.
// Sources of the form image://cover/<id>/<px> load through `covers` (the
// app's image cache — see jellytoast/quick/covers.py); anything else
// (a radio logo URL, a file) loads directly.
import QtQuick
import QtQuick.Effects

Item {
    id: root
    property url source
    property url fallback
    property int radius: 8
    property int pixels: 512
    // Rounded corners need shaders; the software renderer (no GPU) gets
    // square corners instead of no art at all.
    readonly property bool rounded: GraphicsInfo.api !== GraphicsInfo.Software

    component Art: Image {
        id: img
        property url want
        readonly property string key: String(want).startsWith("image://cover/") ? String(want).substring(14) : ""
        property bool ready: false
        function refresh() { ready = key !== "" && covers.request(key) }
        onKeyChanged: refresh()
        Component.onCompleted: refresh()
        Connections {
            target: covers
            enabled: img.key !== "" && !img.ready
            function onReady(k) { if (k === img.key) img.ready = true }
        }
        source: key === "" ? want : (ready ? "image://coverpx/" + key : "")
        fillMode: Image.PreserveAspectCrop
        asynchronous: key === ""
        cache: false
    }

    Item {
        id: art
        anchors.fill: parent
        visible: !root.rounded
        layer.enabled: root.rounded

        Rectangle { anchors.fill: parent; color: theme.surface }
        Art {
            anchors.fill: parent
            want: root.fallback
            sourceSize.width: 160
            sourceSize.height: 160
            visible: String(root.fallback) !== "" && main.status !== Image.Ready
        }
        Art {
            id: main
            anchors.fill: parent
            want: root.source
            sourceSize.width: root.pixels
            sourceSize.height: root.pixels
            opacity: status === Image.Ready ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 140 } }
        }
    }
    Rectangle {
        id: mask
        anchors.fill: parent
        radius: root.radius
        visible: false
        layer.enabled: root.rounded
    }
    MultiEffect {
        anchors.fill: parent
        visible: root.rounded
        source: art
        maskEnabled: true
        maskSource: mask
        maskThresholdMin: 0.5
        maskSpreadAtMin: 1.0
    }
}

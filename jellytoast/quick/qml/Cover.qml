// Album art with rounded corners. `fallback` (an already-cached smaller
// image) shows underneath until `source` is ready, so art never blinks.
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

    Item {
        id: art
        anchors.fill: parent
        visible: !root.rounded
        layer.enabled: root.rounded

        Rectangle { anchors.fill: parent; color: theme.surface }
        Image {
            anchors.fill: parent
            source: root.fallback
            sourceSize.width: 160
            sourceSize.height: 160
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            visible: root.fallback != "" && main.status !== Image.Ready
        }
        Image {
            id: main
            anchors.fill: parent
            source: root.source
            sourceSize.width: root.pixels
            sourceSize.height: root.pixels
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
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

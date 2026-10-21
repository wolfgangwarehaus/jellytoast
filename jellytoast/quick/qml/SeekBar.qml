// Seek bar + elapsed / total. Dragging previews locally and seeks on release.
import QtQuick

Item {
    id: root
    property int position: 0
    property int duration: 0
    signal seek(int ms)

    readonly property bool dragging: area.pressed
    property int dragMs: 0
    readonly property int shown: dragging ? dragMs : position
    readonly property real frac: duration > 0 ? Math.min(1, Math.max(0, shown / duration)) : 0

    function fmt(ms) {
        var s = Math.max(0, Math.floor(ms / 1000))
        var m = Math.floor(s / 60)
        if (m >= 60)
            return Math.floor(m / 60) + ":" + ("0" + (m % 60)).slice(-2) + ":" + ("0" + s % 60).slice(-2)
        return m + ":" + ("0" + s % 60).slice(-2)
    }

    implicitHeight: 48

    Item {
        id: track
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 24

        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width
            height: root.dragging ? 6 : 4
            radius: height / 2
            color: theme.groove
            Rectangle {
                width: parent.width * root.frac
                height: parent.height
                radius: height / 2
                color: theme.text
            }
        }
        Rectangle {
            x: parent.width * root.frac - width / 2
            anchors.verticalCenter: parent.verticalCenter
            width: root.dragging ? 18 : 12
            height: width
            radius: width / 2
            color: theme.text
            Behavior on width { NumberAnimation { duration: 90 } }
        }
        MouseArea {
            id: area
            anchors.fill: parent
            anchors.topMargin: -12
            anchors.bottomMargin: -12
            enabled: root.duration > 0
            preventStealing: true
            function msAt(x) { return Math.round(Math.min(1, Math.max(0, x / track.width)) * root.duration) }
            onPressed: mouse => root.dragMs = msAt(mouse.x)
            onPositionChanged: mouse => root.dragMs = msAt(mouse.x)
            onReleased: root.seek(root.dragMs)
        }
    }
    Text {
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        text: root.fmt(root.shown)
        color: theme.textDim
        font.pixelSize: 12
        font.features: { "tnum": 1 }
    }
    Text {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        text: root.fmt(root.duration)
        color: theme.textDim
        font.pixelSize: 12
        font.features: { "tnum": 1 }
    }
}

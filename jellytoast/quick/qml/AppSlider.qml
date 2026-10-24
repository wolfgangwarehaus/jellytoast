// The app's slider: an accent fill up to a round white handle on a quiet
// groove (the Basic style's default draws the fill the other way round).
import QtQuick
import QtQuick.Controls

Slider {
    id: s
    focusPolicy: Qt.NoFocus
    hoverEnabled: false
    implicitWidth: vertical ? 36 : 200
    implicitHeight: vertical ? 160 : 36

    background: Item {
        x: s.leftPadding
        y: s.topPadding
        width: s.availableWidth
        height: s.availableHeight
        Rectangle {  // groove
            x: s.vertical ? (parent.width - width) / 2 : 0
            y: s.vertical ? 0 : (parent.height - height) / 2
            width: s.vertical ? 4 : parent.width
            height: s.vertical ? parent.height : 4
            radius: 2
            color: theme.groove
            Rectangle {  // fill
                x: 0
                y: s.vertical ? parent.height - height : 0
                width: s.vertical ? parent.width : s.visualPosition * parent.width
                height: s.vertical ? (1 - s.visualPosition) * parent.height : parent.height
                radius: 2
                color: s.enabled ? theme.accent : theme.textFaint
            }
        }
    }
    handle: Rectangle {
        readonly property real d: s.pressed ? 20 : 16
        x: s.leftPadding + (s.vertical ? (s.availableWidth - d) / 2 : s.visualPosition * (s.availableWidth - d))
        y: s.topPadding + (s.vertical ? s.visualPosition * (s.availableHeight - d) : (s.availableHeight - d) / 2)
        width: d
        height: d
        radius: d / 2
        color: s.enabled ? "white" : theme.textFaint
        Behavior on width { NumberAnimation { duration: 90 } }
    }
}

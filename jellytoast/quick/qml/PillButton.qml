// Rounded text + icon button (album Play / Shuffle).
import QtQuick
import QtQuick.Controls

AbstractButton {
    id: root
    property string iconName
    property bool filled: false
    // Compact: no icon and tighter padding, for a row of pills that
    // wouldn't otherwise fit a narrow phone (Radio at 360 px).
    property bool compact: false
    // Width with the icon and full padding, whatever `compact` is — so a
    // caller can decide on compact without a binding loop.
    readonly property real fullWidth: Math.max(96, label.implicitWidth + (iconName !== "" ? 26 : 0) + 40)
    focusPolicy: Qt.NoFocus
    hoverEnabled: false
    implicitWidth: compact ? Math.max(80, label.implicitWidth + 28) : fullWidth
    implicitHeight: 44

    background: Rectangle {
        radius: height / 2
        color: root.filled ? theme.accent : "transparent"
        border.width: root.filled ? 0 : 1
        border.color: theme.textFaint
        opacity: root.enabled ? (root.pressed ? 0.75 : 1) : 0.4
    }
    contentItem: Item {
        Row {
            id: content
            anchors.centerIn: parent
            spacing: 8
            Icon {
                anchors.verticalCenter: parent.verticalCenter
                visible: root.iconName !== "" && !root.compact
                name: root.iconName
                size: 18
                tint: root.filled ? theme.onAccent : theme.text
            }
            Label {
                id: label
                anchors.verticalCenter: parent.verticalCenter
                text: root.text
                color: root.filled ? theme.onAccent : theme.text
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }
        }
    }
}

// Rounded text + icon button (album Play / Shuffle).
import QtQuick
import QtQuick.Controls

AbstractButton {
    id: root
    property string iconName
    property bool filled: false
    focusPolicy: Qt.NoFocus
    hoverEnabled: false
    implicitWidth: Math.max(120, content.implicitWidth + 40)
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
                name: root.iconName
                size: 18
                tint: root.filled ? theme.onAccent : theme.text
            }
            Label {
                anchors.verticalCenter: parent.verticalCenter
                text: root.text
                color: root.filled ? theme.onAccent : theme.text
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }
        }
    }
}

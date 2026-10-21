// Flat, focusless touch button around an Icon. Never shows a focus ring.
import QtQuick
import QtQuick.Controls

AbstractButton {
    id: root
    property string iconName
    property color tint: theme.text
    property int iconSize: 24

    implicitWidth: 48
    implicitHeight: 48
    focusPolicy: Qt.NoFocus
    hoverEnabled: false

    background: Rectangle {
        radius: width / 2
        color: theme.text
        opacity: root.pressed ? 0.12 : 0
        Behavior on opacity { NumberAnimation { duration: 90 } }
    }
    contentItem: Item {
        Icon {
            anchors.centerIn: parent
            name: root.iconName
            tint: root.tint
            size: root.iconSize
            opacity: root.enabled ? 1 : 0.35
        }
    }
}

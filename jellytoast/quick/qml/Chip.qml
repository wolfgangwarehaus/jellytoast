// A selectable chip (filter / choice). `selected` fills it with the accent.
import QtQuick
import QtQuick.Controls

AbstractButton {
    id: chip
    property bool selected: false
    property string iconName: ""
    focusPolicy: Qt.NoFocus
    hoverEnabled: false
    implicitHeight: 38
    implicitWidth: row.implicitWidth + 28
    background: Rectangle {
        radius: height / 2
        color: chip.selected ? theme.accent : "transparent"
        border.width: chip.selected ? 0 : 1
        border.color: theme.textFaint
        opacity: chip.pressed ? 0.75 : 1
    }
    contentItem: Item {
        Row {
            id: row
            anchors.centerIn: parent
            spacing: 6
            Icon {
                anchors.verticalCenter: parent.verticalCenter
                visible: chip.selected || chip.iconName !== ""
                name: chip.iconName !== "" ? chip.iconName : "check"
                size: 16
                tint: chip.selected ? theme.onAccent : theme.textDim
            }
            Label {
                anchors.verticalCenter: parent.verticalCenter
                text: chip.text
                color: chip.selected ? theme.onAccent : theme.text
                font.pixelSize: 14
                font.weight: chip.selected ? Font.DemiBold : Font.Normal
            }
        }
    }
}

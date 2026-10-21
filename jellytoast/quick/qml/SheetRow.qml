// An action row in a bottom sheet: icon + label, full width, 52 px tall.
import QtQuick
import QtQuick.Controls

ItemDelegate {
    id: row
    property string iconName
    property color tint: theme.text
    // Room kept free on the right (for a count or check the caller adds).
    property int trailing: 0
    width: parent ? parent.width : 300
    height: visible ? 52 : 0
    focusPolicy: Qt.NoFocus
    hoverEnabled: false
    leftPadding: 4
    background: Rectangle { radius: 10; color: theme.text; opacity: row.pressed ? 0.08 : 0 }
    contentItem: Row {
        spacing: 16
        Icon {
            anchors.verticalCenter: parent.verticalCenter
            name: row.iconName
            size: 22
            tint: row.tint
        }
        Label {
            anchors.verticalCenter: parent.verticalCenter
            width: row.availableWidth - 38 - row.trailing
            text: row.text
            color: row.tint
            elide: Text.ElideRight
            font.pixelSize: 16
        }
    }
}

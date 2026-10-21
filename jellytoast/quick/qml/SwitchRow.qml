// A settings row with an on / off switch (accent track when on).
import QtQuick
import QtQuick.Controls

SwitchDelegate {
    id: row
    focusPolicy: Qt.NoFocus
    hoverEnabled: false
    background: Rectangle { color: theme.text; opacity: row.pressed ? 0.06 : 0 }
    contentItem: Label {
        leftPadding: 4
        rightPadding: row.indicator.width + 8
        text: row.text
        color: theme.text
        font.pixelSize: 16
        wrapMode: Text.WordWrap
        verticalAlignment: Text.AlignVCenter
    }
    indicator: Rectangle {
        x: row.width - width - row.rightPadding
        y: (row.height - height) / 2
        implicitWidth: 48
        implicitHeight: 28
        radius: height / 2
        color: row.checked ? theme.accent : theme.groove
        Behavior on color { ColorAnimation { duration: 120 } }
        Rectangle {
            x: row.checked ? parent.width - width - 3 : 3
            y: 3
            width: parent.height - 6
            height: width
            radius: width / 2
            color: row.checked ? theme.onAccent : theme.text
            Behavior on x { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
        }
    }
}

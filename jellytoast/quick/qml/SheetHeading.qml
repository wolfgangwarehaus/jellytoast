// A small caps section heading inside a bottom sheet.
import QtQuick
import QtQuick.Controls

Label {
    width: parent ? parent.width : 300
    topPadding: 10
    bottomPadding: 2
    color: theme.textDim
    font.pixelSize: 12
    font.weight: Font.DemiBold
    font.letterSpacing: 1
}

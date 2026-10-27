// What's kept offline: progress while downloading, size when done.
// Swipe left to remove; tap to open.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Jellytoast

Item {
    // The top bar and mini player float over the page as frosted glass, so
    // content scrolls under them; these keep it clear at rest (see Main).
    readonly property real insetTop: StackView.view ? StackView.view.insetTop : 0
    readonly property real insetBottom: StackView.view ? StackView.view.insetBottom : 0
    id: page
    objectName: "downloadsPage"
    property string title: qsTr("Downloads")
    readonly property bool loading: false
    signal openItem(string kind, var item)

    DownloadsModel { id: dl }
    Component.onCompleted: dl.reload()

    RowLayout {
        id: bar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.topMargin: page.insetTop
        anchors.leftMargin: 16
        anchors.rightMargin: 8
        height: 48
        Label {
            Layout.fillWidth: true
            text: dl.count > 0 && dl.storageUsed ? qsTr("%1 on this device").arg(dl.storageUsed) : ""
            color: theme.textDim
            font.pixelSize: 13
        }
        PillButton {
            objectName: "pauseDownloads"
            visible: dl.count > 0
            iconName: dl.paused ? "play" : "pause"
            text: dl.paused ? qsTr("Resume") : qsTr("Pause")
            onClicked: dl.togglePause()
        }
    }

    Label {
        anchors.centerIn: parent
        width: parent.width - 48
        visible: dl.count === 0
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        color: theme.textDim
        text: qsTr("Nothing downloaded yet. Long-press an album or playlist and choose Download.")
    }

    ListView {
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        maximumFlickVelocity: theme.scrollMaxVelocity
        id: list
        objectName: "downloadList"
        anchors.top: bar.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        bottomMargin: page.insetBottom
        clip: true
        model: dl
        boundsBehavior: Flickable.DragAndOvershootBounds
        delegate: SwipeDelegate {
            id: drow
            required property int index
            required property string name
            required property string subtitle
            required property string kind
            required property string state
            required property real progress
            required property string size
            required property string cover
            width: list.width
            height: 68
            padding: 0
            leftPadding: 12
            rightPadding: 16
            focusPolicy: Qt.NoFocus
            hoverEnabled: false
            background: Rectangle {
                color: theme.background
                Rectangle { anchors.fill: parent; color: theme.text; opacity: drow.pressed ? 0.08 : 0 }
            }
            onClicked: if (drow.kind !== "track") page.openItem(drow.kind, dl.get(drow.index))
            swipe.right: Rectangle {
                width: parent.width; height: parent.height
                color: "#c0392b"
                Label {
                    anchors.right: parent.right; anchors.rightMargin: 20
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Remove download"); color: "white"; font.pixelSize: 14; font.weight: Font.DemiBold
                }
            }
            swipe.onCompleted: dl.remove(drow.index)
            contentItem: RowLayout {
                spacing: 12
                Cover {
                    Layout.preferredWidth: 48; Layout.preferredHeight: 48
                    radius: drow.kind === "artist" ? 24 : 6
                    glyph: drow.kind === "artist" ? "user" : "music_note"
                    pixels: 144
                    source: drow.cover
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Label {
                        Layout.fillWidth: true
                        text: drow.name
                        color: theme.text
                        elide: Text.ElideRight
                        font.pixelSize: 15
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: drow.state === "complete" || drow.state === "failed"
                        text: drow.state === "failed" ? qsTr("Download failed — will retry")
                                                      : [drow.subtitle, drow.size].filter(s => s).join(" · ")
                        color: drow.state === "failed" ? "#e57373" : theme.textDim
                        elide: Text.ElideRight
                        font.pixelSize: 12
                    }
                    Rectangle {  // progress while it downloads
                        Layout.fillWidth: true
                        Layout.preferredHeight: 4
                        visible: drow.state === "pending" || drow.state === "downloading"
                        radius: 2
                        color: theme.groove
                        Rectangle {
                            width: parent.width * Math.max(0.02, drow.progress)
                            height: parent.height
                            radius: 2
                            color: theme.accent
                            Behavior on width { NumberAnimation { duration: 200 } }
                        }
                    }
                }
                Icon {
                    visible: drow.state === "complete"
                    name: "download"
                    tint: theme.accent
                    size: 18
                }
            }
        }
    }
}

// Sign in to Jellyfin or Navidrome / Subsonic.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    // The top bar and mini player float over the page as frosted glass, so
    // content scrolls under them; these keep it clear at rest (see Main).
    readonly property real insetTop: StackView.view ? StackView.view.insetTop : 0
    readonly property real insetBottom: StackView.view ? StackView.view.insetBottom : 0
    id: page
    objectName: "signInPage"
    property string title: qsTr("Sign in")
    readonly property bool loading: session.busy

    property string kind: session.serverKind === "subsonic" ? "subsonic" : "jellyfin"

    // Prefill once — a binding would re-apply on every session change and
    // wipe what was typed (e.g. after a wrong password).
    Component.onCompleted: {
        server.text = session.serverUrl
        user.text = session.username
    }

    function submit() {
        session.signIn(page.kind, server.text, user.text, pass.text)
    }

    Flickable {
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        maximumFlickVelocity: theme.scrollMaxVelocity
        anchors.fill: parent
        contentHeight: page.insetTop + form.implicitHeight + 48 + page.insetBottom
        boundsBehavior: Flickable.DragAndOvershootBounds
        clip: true

        ColumnLayout {
            id: form
            x: 24
            y: 24 + page.insetTop
            width: parent.width - 48
            spacing: 14

            Label {
                Layout.fillWidth: true
                text: qsTr("Connect to your music server")
                color: theme.text
                wrapMode: Text.WordWrap
                font.pixelSize: 22
                font.weight: Font.DemiBold
            }

            // Server type: two pills.
            Row {
                spacing: 8
                Repeater {
                    model: [{ k: "jellyfin", label: "Jellyfin" }, { k: "subsonic", label: qsTr("Navidrome / Subsonic") }]
                    PillButton {
                        required property var modelData
                        objectName: "kind_" + modelData.k
                        text: modelData.label
                        filled: page.kind === modelData.k
                        onClicked: page.kind = modelData.k
                    }
                }
            }

            component Field: TextField {
                Layout.fillWidth: true
                Layout.preferredHeight: 48
                color: theme.text
                placeholderTextColor: theme.textFaint
                font.pixelSize: 16
                leftPadding: 16
                background: Rectangle { radius: 12; color: theme.surface }
            }

            Field {
                id: server
                objectName: "serverField"
                placeholderText: qsTr("Server address, e.g. music.example.com")
                inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
                onAccepted: user.forceActiveFocus()
            }
            Field {
                id: user
                objectName: "userField"
                placeholderText: qsTr("Username")
                inputMethodHints: Qt.ImhNoAutoUppercase | Qt.ImhNoPredictiveText
                onAccepted: pass.forceActiveFocus()
            }
            Field {
                id: pass
                objectName: "passwordField"
                placeholderText: qsTr("Password")
                echoMode: TextInput.Password
                onAccepted: page.submit()
            }

            Label {
                objectName: "signInError"
                Layout.fillWidth: true
                visible: session.error !== ""
                text: session.error
                color: "#e57373"
                wrapMode: Text.WordWrap
                font.pixelSize: 14
            }

            PillButton {
                objectName: "signInButton"
                Layout.fillWidth: true
                filled: true
                enabled: !session.busy
                text: session.busy ? qsTr("Signing in…") : qsTr("Sign in")
                onClicked: page.submit()
            }
            PillButton {
                objectName: "quickConnectButton"
                Layout.fillWidth: true
                visible: page.kind === "jellyfin" && session.quickConnectCode === ""
                enabled: !session.busy
                text: qsTr("Use Quick Connect")
                onClicked: session.quickConnect(server.text)
            }
            // Quick Connect: the code to approve elsewhere.
            ColumnLayout {
                Layout.fillWidth: true
                visible: session.quickConnectCode !== ""
                spacing: 8
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    color: theme.textDim
                    text: qsTr("On a device that's already signed in to Jellyfin, open Quick Connect and enter:")
                }
                Label {
                    objectName: "quickConnectCode"
                    Layout.alignment: Qt.AlignHCenter
                    text: session.quickConnectCode
                    color: theme.text
                    font.pixelSize: 40
                    font.weight: Font.Bold
                    font.letterSpacing: 6
                }
                Label {
                    Layout.alignment: Qt.AlignHCenter
                    color: theme.textDim
                    font.pixelSize: 13
                    text: qsTr("Waiting for approval…")
                }
                PillButton {
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("Cancel")
                    onClicked: session.cancelQuickConnect()
                }
            }
            PillButton {
                objectName: "demoButton"
                Layout.fillWidth: true
                enabled: !session.busy
                text: qsTr("Try a demo server")
                onClicked: {
                    var d = session.demo(page.kind)
                    if (!d.url)
                        return
                    server.text = d.url
                    user.text = d.username
                    pass.text = d.password
                    page.submit()
                }
            }
            Label {
                Layout.fillWidth: true
                text: qsTr("The demo is a public server run by the Jellyfin / Navidrome projects.")
                color: theme.textDim
                wrapMode: Text.WordWrap
                font.pixelSize: 12
            }
        }
    }
}

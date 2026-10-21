// Sign in to Jellyfin or Navidrome / Subsonic.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
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
        anchors.fill: parent
        contentHeight: form.implicitHeight + 48
        boundsBehavior: Flickable.DragOverBounds
        clip: true

        ColumnLayout {
            id: form
            x: 24
            y: 24
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

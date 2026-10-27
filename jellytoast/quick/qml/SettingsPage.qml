// Phone settings: account, playback, about.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    // The top bar and mini player float over the page as frosted glass, so
    // content scrolls under them; these keep it clear at rest (see Main).
    readonly property real insetTop: StackView.view ? StackView.view.insetTop : 0
    readonly property real insetBottom: StackView.view ? StackView.view.insetBottom : 0
    id: page
    objectName: "settingsPage"
    property string title: qsTr("Settings")
    readonly property bool loading: false

    component Heading: Label {
        Layout.fillWidth: true
        Layout.topMargin: 18
        leftPadding: 16
        color: theme.textDim
        font.pixelSize: 12
        font.weight: Font.DemiBold
        font.letterSpacing: 1
    }

    // A row showing a choice; tap to pick from `options` ({label, value}).
    component ChoiceRow: ItemDelegate {
        id: choice
        property string label
        property var options: []
        property string value
        signal picked(string value)
        Layout.fillWidth: true
        Layout.preferredHeight: 56
        focusPolicy: Qt.NoFocus
        hoverEnabled: false
        background: Rectangle { color: theme.text; opacity: choice.pressed ? 0.08 : 0 }
        contentItem: RowLayout {
            Label { text: choice.label; color: theme.text; font.pixelSize: 16; Layout.fillWidth: true; leftPadding: 4 }
            Label {
                text: (choice.options.find(o => o.value === choice.value) || { label: choice.value }).label
                color: theme.textDim
                font.pixelSize: 15
            }
            Icon { name: "chevron_down"; tint: theme.textDim; size: 16 }
        }
        onClicked: menu.popup(choice, choice.width - menu.width, choice.height)
        Menu {
            id: menu
            Repeater {
                model: choice.options
                MenuItem {
                    required property var modelData
                    text: modelData.label
                    checkable: true
                    checked: modelData.value === choice.value
                    onTriggered: choice.picked(modelData.value)
                }
            }
        }
    }

    Flickable {
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        maximumFlickVelocity: theme.scrollMaxVelocity
        anchors.fill: parent
        contentHeight: page.insetTop + col.implicitHeight + 32 + page.insetBottom
        boundsBehavior: Flickable.DragAndOvershootBounds
        clip: true

        ColumnLayout {
            id: col
            y: page.insetTop
            width: parent.width
            spacing: 0

            Heading { text: qsTr("ACCOUNT") }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; rightPadding: 20; topPadding: 8
                text: session.username
                color: theme.text
                font.pixelSize: 17
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; rightPadding: 20; bottomPadding: 12
                text: (session.serverKind === "subsonic" ? "Navidrome / Subsonic" : "Jellyfin") + " · " + session.serverUrl
                color: theme.textDim
                font.pixelSize: 13
                elide: Text.ElideMiddle
            }
            PillButton {
                objectName: "signOutButton"
                Layout.leftMargin: 16
                text: qsTr("Sign out")
                onClicked: confirm.open()
            }

            Heading { text: qsTr("LOOK") }
            ChoiceRow {
                objectName: "themeRow"
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Theme")
                options: prefs.themes
                value: prefs.theme
                onPicked: v => prefs.setTheme(v)
            }
            ChoiceRow {
                objectName: "themeModeRow"
                visible: prefs.themeHasModes
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Appearance")
                options: prefs.themeModes
                value: prefs.themeMode
                onPicked: v => prefs.setThemeMode(v)
            }
            SwitchRow {
                objectName: "frostedSwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                text: qsTr("Frosted glass")
                checked: prefs.frosted
                onToggled: prefs.setFrosted(checked)
            }

            Heading { text: qsTr("PLAYBACK") }
            ChoiceRow {
                objectName: "qualityRow"
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Streaming quality")
                options: prefs.qualities
                value: prefs.quality
                onPicked: v => prefs.setQuality(v)
            }
            ChoiceRow {
                objectName: "downloadQualityRow"
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Download quality")
                options: prefs.qualities
                value: prefs.downloadQuality
                onPicked: v => prefs.setDownloadQuality(v)
            }
            ChoiceRow {
                objectName: "normalizationRow"
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Normalization")
                options: prefs.normalizations
                value: prefs.normalization
                onPicked: v => prefs.setNormalization(v)
            }
            SwitchRow {
                objectName: "mediaKeysSwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                text: qsTr("Lock screen & media controls")
                checked: prefs.mediaKeys
                onToggled: prefs.setMediaKeys(checked)
            }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; rightPadding: 20
                text: qsTr("Takes effect next time jellytoast starts.")
                color: theme.textDim
                font.pixelSize: 12
            }

            Heading { text: qsTr("SOUND") }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; rightPadding: 20
                visible: prefs.bitPerfect
                wrapMode: Text.WordWrap
                text: qsTr("Bit-perfect playback is on, so the equalizer and crossfade are off.")
                color: theme.textDim
                font.pixelSize: 12
            }
            SwitchRow {
                objectName: "eqSwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                enabled: !prefs.bitPerfect
                text: qsTr("Equalizer")
                checked: prefs.eqEnabled
                onToggled: prefs.setEqEnabled(checked)
            }
            ChoiceRow {
                objectName: "eqPresetRow"
                visible: prefs.eqEnabled
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Preset")
                options: prefs.eqPresets.map(n => ({ label: n === "Custom" ? qsTr("Custom") : n, value: n }))
                value: prefs.eqPreset
                onPicked: v => prefs.setEqPreset(v)
            }
            // Ten bands, ±12 dB. Release a slider to apply it.
            Row {
                objectName: "eqBands"
                Layout.alignment: Qt.AlignHCenter
                Layout.topMargin: 4
                visible: prefs.eqEnabled
                spacing: 2
                Repeater {
                    model: 10
                    Column {
                        required property int index
                        spacing: 2
                        AppSlider {
                            id: band
                            objectName: "eqBand" + index
                            orientation: Qt.Vertical
                            width: 34
                            height: 150
                            from: -12; to: 12; stepSize: 0.5
                            value: prefs.eqBands[index] || 0
                            focusPolicy: Qt.NoFocus
                            onPressedChanged: if (!pressed) prefs.setEqBand(index, value)
                        }
                        Label {
                            width: 34
                            horizontalAlignment: Text.AlignHCenter
                            text: {
                                var f = prefs.eqFrequencies[index]
                                return f >= 1000 ? (f / 1000) + "k" : String(f)
                            }
                            color: theme.textDim
                            font.pixelSize: 10
                        }
                    }
                }
            }
            SwitchRow {
                objectName: "crossfadeSwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                enabled: !prefs.bitPerfect
                text: qsTr("Crossfade between songs")
                checked: prefs.crossfade
                onToggled: prefs.setCrossfade(checked)
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 20; Layout.rightMargin: 20
                visible: prefs.crossfade
                AppSlider {
                    id: xf
                    objectName: "crossfadeSlider"
                    Layout.fillWidth: true
                    from: 1; to: 10; stepSize: 0.5
                    value: prefs.crossfadeSeconds
                    focusPolicy: Qt.NoFocus
                    onPressedChanged: if (!pressed) prefs.setCrossfadeSeconds(value)
                }
                Label {
                    text: qsTr("%1 s").arg(xf.value.toFixed(1))
                    color: theme.textDim
                    font.pixelSize: 14
                    font.features: { "tnum": 1 }
                }
            }
            ChoiceRow {
                objectName: "lyricsSizeRow"
                Layout.leftMargin: 12; Layout.rightMargin: 12
                label: qsTr("Lyrics size")
                options: prefs.lyricsSizes
                value: prefs.lyricsSize
                onPicked: v => prefs.setLyricsSize(v)
            }

            Heading { text: qsTr("SCROBBLING") }
            // Last.fm
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 20; Layout.rightMargin: 12
                Layout.preferredHeight: 52
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Label { text: "Last.fm"; color: theme.text; font.pixelSize: 16 }
                    Label {
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: scrobbling.lastfmConnected ? qsTr("Connected as %1").arg(scrobbling.lastfmUser)
                                                         : scrobbling.lastfmStatus
                        color: theme.textDim
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                    }
                }
                PillButton {
                    objectName: "lastfmButton"
                    text: scrobbling.lastfmConnected ? qsTr("Disconnect")
                          : scrobbling.lastfmWaiting ? qsTr("Cancel") : qsTr("Connect")
                    onClicked: scrobbling.lastfmConnected ? scrobbling.disconnectLastfm()
                               : scrobbling.lastfmWaiting ? scrobbling.cancelLastfm() : scrobbling.connectLastfm()
                }
            }
            // ListenBrainz
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 20; Layout.rightMargin: 12
                Layout.preferredHeight: 52
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Label { text: "ListenBrainz"; color: theme.text; font.pixelSize: 16 }
                    Label {
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: scrobbling.listenbrainzConnected
                              ? qsTr("Connected as %1").arg(scrobbling.listenbrainzUser)
                              : scrobbling.listenbrainzStatus
                        color: theme.textDim
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                    }
                }
                PillButton {
                    objectName: "listenbrainzDisconnect"
                    visible: scrobbling.listenbrainzConnected
                    text: qsTr("Disconnect")
                    onClicked: scrobbling.disconnectListenbrainz()
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 20; Layout.rightMargin: 12
                visible: !scrobbling.listenbrainzConnected
                spacing: 8
                TextField {
                    id: lbToken
                    objectName: "listenbrainzToken"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 44
                    placeholderText: qsTr("User token (listenbrainz.org/settings)")
                    placeholderTextColor: theme.textFaint
                    color: theme.text
                    font.pixelSize: 14
                    leftPadding: 12
                    echoMode: TextInput.PasswordEchoOnEdit
                    inputMethodHints: Qt.ImhNoAutoUppercase | Qt.ImhNoPredictiveText
                    background: Rectangle { radius: 10; color: theme.surface }
                }
                PillButton {
                    objectName: "listenbrainzConnect"
                    text: qsTr("Connect")
                    enabled: lbToken.text.trim() !== ""
                    onClicked: scrobbling.connectListenbrainz(lbToken.text)
                }
            }

            Heading { text: qsTr("OFFLINE") }
            SwitchRow {
                objectName: "offlineModeSwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                text: qsTr("Offline mode — only play downloads")
                checked: prefs.offlineMode
                onToggled: prefs.setOfflineMode(checked)
            }
            SwitchRow {
                objectName: "wifiOnlySwitch"
                Layout.fillWidth: true
                Layout.leftMargin: 12; Layout.rightMargin: 12
                text: qsTr("Download on Wi-Fi only")
                checked: prefs.wifiOnly
                onToggled: prefs.setWifiOnly(checked)
            }

            Heading { text: qsTr("CASTING") }
            Repeater {
                model: prefs.castTypes
                SwitchRow {
                    required property var modelData
                    objectName: "cast_" + modelData.key
                    Layout.fillWidth: true
                    Layout.leftMargin: 12; Layout.rightMargin: 12
                    text: modelData.label
                    checked: modelData.on
                    onToggled: prefs.setCastType(modelData.key, checked)
                }
            }

            Heading { text: qsTr("INTERFACE") }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; rightPadding: 20; topPadding: 6
                wrapMode: Text.WordWrap
                text: qsTr("This is the phone interface. You can go back to the classic one any time.")
                color: theme.textDim
                font.pixelSize: 13
            }
            PillButton {
                objectName: "classicUiButton"
                Layout.leftMargin: 16
                Layout.topMargin: 8
                text: qsTr("Use the classic interface")
                onClicked: {
                    prefs.useClassicUi()
                    toast.show(qsTr("Restart jellytoast to switch"))
                }
            }

            Heading { text: qsTr("ABOUT") }
            Label {
                Layout.fillWidth: true
                leftPadding: 20; topPadding: 8
                text: "jellytoast " + prefs.version()
                color: theme.textDim
                font.pixelSize: 14
            }
        }
    }

    Dialog {
        id: confirm
        objectName: "signOutDialog"
        anchors.centerIn: parent
        width: Math.min(parent.width - 48, 360)
        modal: true
        title: qsTr("Sign out?")
        standardButtons: Dialog.Cancel | Dialog.Ok
        Label {
            width: confirm.availableWidth
            wrapMode: Text.WordWrap
            color: theme.text
            text: qsTr("Playback stops, the queue is cleared and pending downloads are cancelled.")
        }
        onAccepted: session.signOut()
    }
}

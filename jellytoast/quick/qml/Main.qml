// jellytoast — Qt Quick phone front end (see dev/QT_QUICK_PLAN.md).
// Context properties from jellytoast/quick: `albums` (LibraryModel),
// `player` (PlayerController), `queue` (QueueModel), `lyrics` (LyricsModel),
// `actions` (Actions), `theme` (Theme), `session` (Session).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: win
    width: 402
    height: 740
    visible: true
    title: "jellytoast"
    color: theme.background
    palette.buttonText: theme.text
    palette.windowText: theme.text
    palette.text: theme.text
    palette.window: theme.surface
    palette.base: theme.surface
    palette.light: theme.surface
    palette.mid: theme.textFaint
    palette.highlight: theme.accent
    palette.highlightedText: theme.onAccent

    // ── sections ──
    readonly property var sections: [
        { key: "home", label: qsTr("Home") },
        { key: "album", label: qsTr("Albums") },
        { key: "artist", label: qsTr("Artists") },
        { key: "playlist", label: qsTr("Playlists") },
        { key: "song", label: qsTr("Songs") },
        { key: "genre", label: qsTr("Genres") },
        { key: "favorite", label: qsTr("Favorites") },
        { key: "download", label: qsTr("Downloads") }
    ]
    property string section: "album"

    function showSection(key) {
        nowPlaying.close()
        if (!session.signedIn) {
            stack.replace(null, signInPage, {}, StackView.Immediate)
            return
        }
        section = key
        player.section = key
        var page
        if (key === "home")
            page = homePage
        else if (key === "download")
            page = downloadsPage
        else if (key === "song")
            page = songsPage
        else if (key === "genre")
            page = genresPage
        else
            page = gridPage
        var props = ["song", "genre", "download", "home"].indexOf(key) >= 0 ? {}
            : { kind: key === "favorite" ? "favAlbums" : key, title: sections.find(s => s.key === key).label,
                source: key === "album" ? albums : null }
        stack.replace(null, page, props, StackView.Immediate)
    }

    // Where tapping an item goes: albums / playlists → track list,
    // artists → their albums, genres → their albums.
    function openItem(kind, item) {
        if (kind === "album" || kind === "playlist")
            stack.push(albumPage, { album: Object.assign({ kind: kind }, item) })
        else if (kind === "artist")
            stack.push(gridPage, { kind: "artistAlbums", parentId: item.id, title: item.name })
        else if (kind === "genre")
            stack.push(gridPage, { kind: "genreAlbums", parentId: item.id, title: item.name })
        else if (kind === "likedSongs")
            stack.push(songsPage, { kind: "favSongs", title: qsTr("Liked songs") })
    }

    function openSearch() {
        if (!(stack.currentItem && stack.currentItem.objectName === "searchPage"))
            stack.push(searchPage)
    }

    // Back: Now Playing first, then the page stack. (Esc / Back key / button / edge swipe.)
    function back() {
        if (nowPlaying.shown) {
            if (nowPlaying.surface !== "cover")
                nowPlaying.surface = "cover"
            else
                nowPlaying.close()
            return true
        }
        if (stack.depth > 1) {
            stack.pop()
            return true
        }
        return false
    }

    Shortcut {
        sequences: [StandardKey.Back, "Esc", "Back"]
        onActivated: win.back()
    }
    Connections {
        target: session
        function onSignedInNow() {
            libraries.refresh()
            albums.reload()
            win.showSection(player.section)
        }
        function onSignedOutNow() { win.showSection(win.section) }
    }

    Shortcut {
        sequences: [StandardKey.Find]
        onActivated: win.openSearch()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            id: topBar
            objectName: "topBar"
            Layout.fillWidth: true
            Layout.preferredHeight: 52
            readonly property bool root: stack.depth <= 1

            IconButton {
                objectName: "backButton"
                anchors.left: parent.left
                anchors.leftMargin: 4
                anchors.verticalCenter: parent.verticalCenter
                iconName: "back"
                visible: !topBar.root
                onClicked: win.back()
            }
            // Root: the section title is the section picker.
            AbstractButton {
                id: sectionButton
                objectName: "sectionButton"
                anchors.centerIn: parent
                visible: topBar.root && session.signedIn
                focusPolicy: Qt.NoFocus
                hoverEnabled: false
                implicitWidth: sectionRow.implicitWidth + 24
                implicitHeight: 44
                onClicked: sectionMenu.popup(this, (width - sectionMenu.width) / 2, height)
                background: Rectangle { radius: 22; color: theme.text; opacity: sectionButton.pressed ? 0.1 : 0 }
                contentItem: Item {
                    Row {
                        id: sectionRow
                        anchors.centerIn: parent
                        spacing: 6
                        Label {
                            text: stack.currentItem ? stack.currentItem.title : ""
                            color: theme.text
                            font.pixelSize: 18
                            font.weight: Font.DemiBold
                        }
                        Icon {
                            anchors.verticalCenter: parent.verticalCenter
                            name: "chevron_down"
                            tint: theme.textDim
                            size: 18
                        }
                    }
                }
                Menu {
                    id: sectionMenu
                    objectName: "sectionMenu"
                    Repeater {
                        model: win.sections
                        MenuItem {
                            required property var modelData
                            text: modelData.label
                            checkable: true
                            checked: win.section === modelData.key
                            onTriggered: win.showSection(modelData.key)
                        }
                    }
                }
            }
            Label {
                anchors.centerIn: parent
                width: parent.width - 120
                visible: !topBar.root || !session.signedIn
                horizontalAlignment: Text.AlignHCenter
                text: stack.currentItem ? stack.currentItem.title : ""
                color: theme.text
                elide: Text.ElideRight
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }
            IconButton {
                id: moreButton
                objectName: "moreButton"
                // The page on screen's library model, if it has one.
                readonly property var lib: stack.currentItem && stack.currentItem.lib ? stack.currentItem.lib : null
                readonly property bool albumGrid: lib !== null && lib.sortable
                anchors.right: searchButton.left
                anchors.verticalCenter: parent.verticalCenter
                iconName: "more_vert"
                visible: session.signedIn
                onClicked: moreMenu.popup(moreButton, moreButton.width - moreMenu.width, moreButton.height)
                Menu {
                    id: moreMenu
                    objectName: "moreMenu"
                    Menu {
                        id: librariesMenu
                        title: qsTr("Libraries")
                        enabled: libraries.multiple
                        MenuItem {
                            text: qsTr("All libraries")
                            checkable: true
                            checked: libraries.allSelected
                            onTriggered: libraries.selectAll()
                        }
                        MenuSeparator { }
                        Repeater {
                            model: libraries.items
                            MenuItem {
                                required property var modelData
                                text: modelData.name
                                checkable: true
                                checked: modelData.checked
                                onTriggered: libraries.toggle(modelData.id)
                            }
                        }
                    }
                    Menu {
                        id: sortMenu
                        objectName: "sortMenu"
                        title: qsTr("Sort")
                        enabled: moreButton.albumGrid
                        Repeater {
                            model: [
                                { key: "SortName", label: qsTr("Name") },
                                { key: "AlbumArtist,SortName", label: qsTr("Album artist") },
                                { key: "PremiereDate,SortName", label: qsTr("Release date") },
                                { key: "DateCreated,SortName", label: qsTr("Date added") },
                                { key: "DatePlayed,SortName", label: qsTr("Recently played") }
                            ]
                            MenuItem {
                                required property var modelData
                                text: modelData.label
                                checkable: true
                                checked: moreButton.albumGrid && moreButton.lib.sortBy === modelData.key
                                onTriggered: moreButton.lib.setSort(modelData.key, moreButton.lib.sortDescending)
                            }
                        }
                        MenuSeparator { }
                        MenuItem {
                            text: qsTr("Ascending")
                            checkable: true
                            checked: moreButton.albumGrid && !moreButton.lib.sortDescending
                            onTriggered: moreButton.lib.setSort(moreButton.lib.sortBy, false)
                        }
                        MenuItem {
                            text: qsTr("Descending")
                            checkable: true
                            checked: moreButton.albumGrid && moreButton.lib.sortDescending
                            onTriggered: moreButton.lib.setSort(moreButton.lib.sortBy, true)
                        }
                    }
                    MenuItem {
                        objectName: "viewToggle"
                        text: prefs.viewMode === "list" ? qsTr("Show as grid") : qsTr("Show as list")
                        enabled: moreButton.albumGrid
                        onTriggered: prefs.setViewMode(prefs.viewMode === "list" ? "grid" : "list")
                    }
                    MenuItem {
                        text: qsTr("Shuffle all")
                        onTriggered: actions.shuffleLibrary()
                    }
                    MenuSeparator { }
                    MenuItem {
                        objectName: "settingsItem"
                        text: qsTr("Settings")
                        onTriggered: stack.push(settingsPage)
                    }
                }
            }
            IconButton {
                id: searchButton
                objectName: "searchButton"
                anchors.right: parent.right
                anchors.rightMargin: 4
                anchors.verticalCenter: parent.verticalCenter
                iconName: "search"
                visible: session.signedIn && !(stack.currentItem
                         && ["searchPage", "settingsPage"].indexOf(stack.currentItem.objectName) >= 0)
                onClicked: win.openSearch()
            }
        }

        Rectangle {
            objectName: "offlineBanner"
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 30 : 0
            visible: prefs.offlineMode && session.signedIn
            color: theme.surface
            Label {
                anchors.centerIn: parent
                text: qsTr("Offline — showing your downloads")
                color: theme.textDim
                font.pixelSize: 12
            }
        }

        StackView {
            id: stack
            objectName: "stack"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            Component.onCompleted: {
                if (session.signedIn)
                    libraries.refresh()
                win.showSection(player.section)
            }

            pushEnter: Transition {
                ParallelAnimation {
                    NumberAnimation { property: "x"; from: stack.width * 0.3; to: 0; duration: 220; easing.type: Easing.OutCubic }
                    NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 160 }
                }
            }
            pushExit: Transition {
                NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 160 }
            }
            popEnter: Transition {
                NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 160 }
            }
            popExit: Transition {
                ParallelAnimation {
                    NumberAnimation { property: "x"; from: 0; to: stack.width * 0.3; duration: 200; easing.type: Easing.InCubic }
                    NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 200 }
                }
            }

            // Back gesture: drag in from the left edge.
            Item {
                id: edge
                objectName: "edgeSwipe"
                z: 100
                width: 16
                height: parent.height
                visible: stack.depth > 1
                DragHandler {
                    target: null
                    yAxis.enabled: false
                    onActiveChanged: if (!active && centroid.position.x - centroid.pressPosition.x > 80) win.back()
                }
            }
        }

        MiniPlayer {
            Layout.fillWidth: true
            Layout.preferredHeight: height
            onOpen: nowPlaying.open()
        }
    }

    Component {
        id: gridPage
        LibraryGridPage { onOpenItem: (kind, item) => win.openItem(kind, item) }
    }
    Component {
        id: songsPage
        SongsPage { }
    }
    Component {
        id: genresPage
        GenresPage { onOpenItem: (kind, item) => win.openItem(kind, item) }
    }
    Component {
        id: albumPage
        AlbumPage { }
    }
    Component {
        id: homePage
        HomePage { onOpenItem: (kind, item) => win.openItem(kind, item) }
    }
    Component {
        id: downloadsPage
        DownloadsPage { onOpenItem: (kind, item) => win.openItem(kind, item) }
    }
    Component {
        id: settingsPage
        SettingsPage { }
    }
    Component {
        id: signInPage
        SignInPage { }
    }
    Component {
        id: searchPage
        SearchPage {
            objectName: "searchPage"
            onOpenItem: (kind, item) => win.openItem(kind, item)
        }
    }

    NowPlaying {
        id: nowPlaying
        width: win.width
        height: win.height
        z: 10
        onOpenCast: castSheet.open()
        onGoToArtist: artist => {
            var cur = stack.currentItem
            if (!(cur && cur.kind === "artistAlbums" && cur.parentId === artist.id))
                win.openItem("artist", artist)
        }
        onGoToAlbum: album => {
            var cur = stack.currentItem
            if (!(cur && cur.album && cur.album.id === album.id))
                win.openItem("album", album)
        }
    }

    CastSheet {
        id: castSheet
        onOpenSettings: { nowPlaying.close(); stack.push(settingsPage) }
    }
    Dialog {
        id: castFailed
        objectName: "castFailed"
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 420)
        modal: true
        title: qsTr("Cast failed")
        standardButtons: Dialog.Ok
        property alias text: castFailedText.text
        Label {
            id: castFailedText
            width: castFailed.availableWidth
            wrapMode: Text.WordWrap
            color: theme.text
            textFormat: Text.PlainText
        }
    }
    Connections {
        target: cast
        function onFailed(msg) { castFailed.text = msg; castFailed.open() }
    }

    // Short confirmations ("Added to queue").
    Rectangle {
        id: toast
        objectName: "toast"
        property alias text: toastLabel.text
        function show(msg) {
            text = msg
            opacity = 1
            toastTimer.restart()
        }
        z: 20
        anchors.horizontalCenter: parent.horizontalCenter
        y: parent.height - 76 - 64 - (nowPlaying.shown ? 0 : 0)
        width: toastLabel.implicitWidth + 32
        height: 40
        radius: 20
        color: theme.text
        opacity: 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: 180 } }
        Label {
            id: toastLabel
            anchors.centerIn: parent
            color: theme.background
            font.pixelSize: 14
            font.weight: Font.Medium
        }
        Timer { id: toastTimer; interval: 1800; onTriggered: toast.opacity = 0 }
    }
    Connections {
        target: actions
        function onToast(msg) { toast.show(msg) }
    }
}

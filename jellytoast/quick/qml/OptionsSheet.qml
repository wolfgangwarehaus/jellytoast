// The ⋮ sheet, as a short settings-style list: each row shows its current
// value; Library and Sort by expand in place (one at a time) to pick, View
// is a two-icon toggle, then Shuffle all / Settings.
import QtQuick
import QtQuick.Controls

Sheet {
    id: sheet
    objectName: "optionsSheet"
    property var lib: null  // the page's LibraryModel (null when not a library page)
    readonly property bool albumGrid: lib !== null && lib.sortable
    property string expanded: ""  // "" | "libraries" | "sort"
    signal openSettings()

    // An artist's own page sorts its albums by name or year; the library
    // grids have the full set.
    readonly property bool artistPage: lib !== null && lib.kind === "artistAlbums"
    readonly property var sorts: artistPage ? [
        { key: "SortName", label: qsTr("Name") },
        { key: "PremiereDate", label: qsTr("Release year") }
    ] : [
        { key: "SortName", label: qsTr("Name") },
        { key: "AlbumArtist,SortName", label: qsTr("Album artist") },
        { key: "PremiereDate,SortName", label: qsTr("Release date") },
        { key: "DateCreated,SortName", label: qsTr("Date added") },
        { key: "DatePlayed,SortName", label: qsTr("Recently played") }
    ]

    function show(model) {
        lib = model
        expanded = ""
        open()
    }
    function toggle(section) { expanded = expanded === section ? "" : section }

    function libraryValue() {
        if (libraries.allSelected)
            return qsTr("All")
        var on = libraries.items.filter(l => l.checked).map(l => l.name)
        return on.length <= 2 ? on.join(", ") : qsTr("%1 libraries").arg(on.length)
    }
    function sortValue() {
        if (!albumGrid)
            return ""
        var s = sorts.find(o => o.key === lib.sortBy)
        return s ? s.label : qsTr("Name")
    }

    // A row: label on the left, current value + chevron on the right.
    component ValueRow: ItemDelegate {
        id: vrow
        property string label
        property string value
        property bool open: false
        width: parent ? parent.width : 300
        height: visible ? 52 : 0
        focusPolicy: Qt.NoFocus
        hoverEnabled: false
        leftPadding: 4
        rightPadding: 4
        background: Rectangle { radius: 10; color: theme.text; opacity: vrow.pressed ? 0.08 : 0 }
        contentItem: Item {
            Label {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: vrow.label
                color: theme.text
                font.pixelSize: 16
            }
            Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: 6
                Label {
                    anchors.verticalCenter: parent.verticalCenter
                    text: vrow.value
                    color: theme.textDim
                    font.pixelSize: 15
                    elide: Text.ElideRight
                    width: Math.min(implicitWidth, vrow.width * 0.5)
                }
                Icon {
                    anchors.verticalCenter: parent.verticalCenter
                    name: "chevron_down"
                    size: 18
                    tint: theme.textDim
                    rotation: vrow.open ? 180 : 0
                    Behavior on rotation { NumberAnimation { duration: 150 } }
                }
            }
        }
    }

    // A choice inside an expanded row: indented, check on the right.
    component ChoiceRow: ItemDelegate {
        id: crow
        property bool selected: false
        width: parent ? parent.width : 300
        height: 46
        focusPolicy: Qt.NoFocus
        hoverEnabled: false
        leftPadding: 20
        rightPadding: 8
        background: Rectangle { radius: 10; color: theme.text; opacity: crow.pressed ? 0.08 : 0 }
        contentItem: Item {
            Label {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: crow.text
                color: crow.selected ? theme.accent : theme.text
                font.pixelSize: 15
                font.weight: crow.selected ? Font.DemiBold : Font.Normal
            }
            Icon {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                visible: crow.selected
                name: "check"
                size: 18
                tint: theme.accent
            }
        }
    }

    // ── Library ──
    ValueRow {
        objectName: "libraryRow"
        visible: libraries.multiple
        label: qsTr("Library")
        value: sheet.libraryValue()
        open: sheet.expanded === "libraries"
        onClicked: sheet.toggle("libraries")
    }
    Column {
        width: parent.width
        visible: libraries.multiple && sheet.expanded === "libraries"
        ChoiceRow {
            objectName: "libAll"
            text: qsTr("All libraries")
            selected: libraries.allSelected
            onClicked: libraries.selectAll()
        }
        Repeater {
            model: libraries.items
            ChoiceRow {
                required property var modelData
                text: modelData.name
                selected: !libraries.allSelected && modelData.checked
                onClicked: libraries.toggle(modelData.id)
            }
        }
    }

    // ── Sort by (+ direction) ──
    Item {
        width: parent.width
        height: sheet.albumGrid ? 52 : 0
        visible: sheet.albumGrid
        ValueRow {
            objectName: "sortRow"
            anchors.left: parent.left
            anchors.right: dirButton.left
            label: qsTr("Sort by")
            value: sheet.sortValue()
            open: sheet.expanded === "sort"
            onClicked: sheet.toggle("sort")
        }
        IconButton {
            id: dirButton
            objectName: "sortDirection"
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconName: "sort"
            tint: theme.accent
            // Flip the glyph for descending — A↓Z vs Z↓A at a glance.
            rotation: sheet.albumGrid && sheet.lib.sortDescending ? 180 : 0
            Behavior on rotation { NumberAnimation { duration: 150 } }
            onClicked: sheet.lib.setSort(sheet.lib.sortBy, !sheet.lib.sortDescending)
        }
    }
    Column {
        width: parent.width
        visible: sheet.albumGrid && sheet.expanded === "sort"
        Repeater {
            model: sheet.sorts
            ChoiceRow {
                required property var modelData
                text: modelData.label
                selected: sheet.albumGrid && sheet.lib.sortBy === modelData.key
                onClicked: {
                    sheet.lib.setSort(modelData.key, sheet.lib.sortDescending)
                    sheet.expanded = ""
                }
            }
        }
    }

    // ── View: grid / list ──
    Item {
        width: parent.width
        height: sheet.albumGrid ? 52 : 0
        visible: sheet.albumGrid
        Label {
            anchors.left: parent.left
            anchors.leftMargin: 4
            anchors.verticalCenter: parent.verticalCenter
            text: qsTr("View")
            color: theme.text
            font.pixelSize: 16
        }
        Rectangle {  // a two-icon segmented toggle
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: 92
            height: 38
            radius: 19
            color: "transparent"
            border.width: 1
            border.color: theme.textFaint
            Row {
                anchors.centerIn: parent
                Repeater {
                    model: [{ mode: "grid", icon: "grid" }, { mode: "list", icon: "list" }]
                    AbstractButton {
                        id: seg
                        required property var modelData
                        objectName: "view_" + modelData.mode
                        readonly property bool on: (prefs.viewMode === "list") === (modelData.mode === "list")
                        width: 44
                        height: 34
                        focusPolicy: Qt.NoFocus
                        hoverEnabled: false
                        onClicked: prefs.setViewMode(modelData.mode)
                        background: Rectangle { radius: 17; color: seg.on ? theme.accent : "transparent" }
                        contentItem: Item {
                            Icon {
                                anchors.centerIn: parent
                                name: seg.modelData.icon
                                size: 18
                                tint: seg.on ? theme.onAccent : theme.textDim
                            }
                        }
                    }
                }
            }
        }
    }

    Item { width: 1; height: 4 }
    Rectangle { width: parent.width; height: 1; color: theme.textFaint; opacity: 0.4 }

    SheetRow {
        objectName: "shuffleAllRow"
        iconName: "shuffle"
        text: qsTr("Shuffle all")
        onClicked: { actions.shuffleLibrary(); sheet.close() }
    }
    SheetRow {
        objectName: "settingsItem"
        iconName: "settings"
        text: qsTr("Settings")
        onClicked: { sheet.close(); sheet.openSettings() }
    }
}

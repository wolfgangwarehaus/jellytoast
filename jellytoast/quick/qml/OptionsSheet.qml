// The ⋮ sheet: which libraries, how albums are sorted and shown, plus
// Shuffle all / Settings. Chips stay put when tapped — pick several
// libraries, change the sort, then swipe the sheet away.
import QtQuick
import QtQuick.Controls

Sheet {
    id: sheet
    objectName: "optionsSheet"
    property var lib: null  // the page's LibraryModel (null when not a library page)
    readonly property bool albumGrid: lib !== null && lib.sortable
    signal openSettings()

    function show(model) {
        lib = model
        open()
    }

    SheetHeading {
        text: qsTr("LIBRARIES")
        visible: libraries.multiple
    }
    Flow {
        width: parent.width
        spacing: 8
        visible: libraries.multiple
        Chip {
            objectName: "libAll"
            text: qsTr("All")
            selected: libraries.allSelected
            onClicked: libraries.selectAll()
        }
        Repeater {
            model: libraries.items
            Chip {
                required property var modelData
                text: modelData.name
                selected: !libraries.allSelected && modelData.checked
                onClicked: libraries.toggle(modelData.id)
            }
        }
    }

    SheetHeading {
        text: qsTr("SORT BY")
        visible: sheet.albumGrid
    }
    Flow {
        width: parent.width
        spacing: 8
        visible: sheet.albumGrid
        Repeater {
            model: [
                { key: "SortName", label: qsTr("Name") },
                { key: "AlbumArtist,SortName", label: qsTr("Album artist") },
                { key: "PremiereDate,SortName", label: qsTr("Release date") },
                { key: "DateCreated,SortName", label: qsTr("Date added") },
                { key: "DatePlayed,SortName", label: qsTr("Recently played") }
            ]
            Chip {
                required property var modelData
                text: modelData.label
                selected: sheet.albumGrid && sheet.lib.sortBy === modelData.key
                onClicked: sheet.lib.setSort(modelData.key, sheet.lib.sortDescending)
            }
        }
    }
    Row {
        spacing: 8
        visible: sheet.albumGrid
        Chip {
            objectName: "sortAscending"
            text: qsTr("Ascending")
            iconName: "sort"
            selected: sheet.albumGrid && !sheet.lib.sortDescending
            onClicked: sheet.lib.setSort(sheet.lib.sortBy, false)
        }
        Chip {
            objectName: "sortDescending"
            text: qsTr("Descending")
            iconName: "sort"
            selected: sheet.albumGrid && sheet.lib.sortDescending
            onClicked: sheet.lib.setSort(sheet.lib.sortBy, true)
        }
    }

    SheetHeading {
        text: qsTr("VIEW")
        visible: sheet.albumGrid
    }
    Row {
        spacing: 8
        visible: sheet.albumGrid
        Chip {
            objectName: "viewGrid"
            text: qsTr("Grid")
            iconName: "grid"
            selected: prefs.viewMode !== "list"
            onClicked: prefs.setViewMode("grid")
        }
        Chip {
            objectName: "viewList"
            text: qsTr("List")
            iconName: "list"
            selected: prefs.viewMode === "list"
            onClicked: prefs.setViewMode("list")
        }
    }

    Item { width: 1; height: 6 }
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

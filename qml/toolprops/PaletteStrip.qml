import QtQuick 2.15

// Full-QML palette strip: recessed well + circular swatch grid + add tile.
// The shared preset collection lives in ToolShelfStore; `bridge`
// (PresetBridge) delegates every mutation there. Selection is per-strip
// (stroke vs fill). Context props: `bridge`, `theme`.
//
// M7.1.4.3:
// - Fill strips prepend ONE permanent "No Fill" slot. It is not part of
//   `bridge.colors` (the persisted preset collection), so it can never be
//   removed, reordered or overwritten; every swatch keeps its store index
//   via `leading`.
// - Right-click no longer opens the default-styled QML Menu: the host
//   (Python) shows its themed InkfallMenu at the clicked swatch, which
//   also guarantees on-screen clamping.
Rectangle {
    id: well
    color: bridge.wellBg
    border.color: bridge.wellEdge
    border.width: 1
    radius: 10
    implicitWidth: 280
    // Grid metrics (must match _swatch_center in tool_popup.py):
    // 6 columns, 26px cells, 5px spacing, 8px padding.
    implicitHeight: grid.y + grid.height + 8

    // 1 for fill strips (permanent No Fill slot), 0 for stroke strips.
    property int leading: bridge.noFillAvailable ? 1 : 0

    Grid {
        id: grid
        // 6 columns * 26px + 5 gaps * 5px = 181px content, centered.
        x: Math.max(8, (parent.width - 181) / 2); y: 8
        width: 181
        columns: 6
        columnSpacing: 5
        rowSpacing: 5

        Repeater {
            id: rep
            // No Fill slot (fill only) + presets + trailing add tile.
            model: well.leading + bridge.colors.length + 1

            Item {
                id: cell
                width: 26; height: 26
                activeFocusOnTab: true
                property bool isNoFill: well.leading > 0 && index === 0
                property bool isAdd: index >= bridge.colors.length + well.leading
                property int swatchIndex: index - well.leading
                property string swatchColor: (isAdd || isNoFill)
                    ? "" : bridge.colors[swatchIndex]
                property bool checked: isNoFill ? bridge.noFillSelected
                    : (!isAdd && swatchColor.toLowerCase() ===
                       bridge.selectedColor.toLowerCase())

                // Keyboard focus frame.
                Rectangle {
                    anchors.centerIn: parent
                    width: 26; height: 26; radius: 13
                    color: "transparent"
                    border.color: theme.inputFocus
                    border.width: 1
                    visible: cell.activeFocus
                }

                // Selected accent ring (matches _SwatchButton: 2px, 24px).
                Rectangle {
                    anchors.centerIn: parent
                    width: 26; height: 26; radius: 13
                    color: "transparent"
                    border.color: cell.checked ? theme.accent : "transparent"
                    border.width: 2
                    visible: cell.checked
                }
                // Fill circle (22px) + low-contrast outline from Python.
                Rectangle {
                    id: fill
                    anchors.centerIn: parent
                    width: 22; height: 22; radius: 11
                    color: cell.isAdd ? "transparent" : cell.swatchColor
                    border.color: cell.isAdd ? theme.textMuted
                                             : bridge.swatchEdge(cell.swatchColor)
                    border.width: 1
                    visible: !cell.isNoFill
                }
                // Selected white center dot (4px).
                Rectangle {
                    anchors.centerIn: parent
                    width: 4; height: 4; radius: 2
                    color: "white"
                    visible: cell.checked && !cell.isNoFill
                }
                // Permanent No Fill slot: empty swatch + cross-out. It is
                // never a color and never carries a remove badge.
                Rectangle {
                    anchors.centerIn: parent
                    width: 22; height: 22; radius: 11
                    color: "transparent"
                    border.color: theme.textMuted
                    border.width: 1
                    visible: cell.isNoFill
                }
                Rectangle {
                    anchors.centerIn: parent
                    width: 22; height: 1.5
                    color: theme.textMuted
                    rotation: -45
                    visible: cell.isNoFill
                }
                // Dashed add ring + plus (add tile only).
                Rectangle {
                    anchors.centerIn: parent
                    width: 22; height: 22; radius: 11
                    color: "transparent"
                    border.color: theme.textMuted
                    border.width: 1
                    visible: cell.isAdd
                }
                Rectangle {
                    anchors.centerIn: parent
                    width: 10; height: 2; radius: 1
                    color: theme.textMuted
                    visible: cell.isAdd
                }
                Rectangle {
                    anchors.centerIn: parent
                    width: 2; height: 10; radius: 1
                    color: theme.textMuted
                    visible: cell.isAdd
                }
                // Hover remove badge (top-right X). Colors only: the No
                // Fill slot and the add tile are never removable.
                Rectangle {
                    id: removeBadge
                    x: 14; y: 0
                    width: 12; height: 12; radius: 6
                    color: "#14161ce6"
                    border.color: "white"; border.width: 1
                    visible: !cell.isAdd && !cell.isNoFill
                             && cellMouse.containsMouse
                             && bridge.colors.length > 1
                    Text {
                        anchors.centerIn: parent
                        text: "×"
                        color: "white"
                        font.pixelSize: 9
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: bridge.removeAt(cell.swatchIndex)
                    }
                }

                MouseArea {
                    id: cellMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    onClicked: function(mouse) {
                        if (cell.isAdd) { bridge.clickAdd(); return }
                        if (cell.isNoFill) {
                            // Right-click on the permanent slot is inert:
                            // it has no color, no remove, no reorder.
                            if (mouse.button === Qt.LeftButton)
                                bridge.selectNoFill()
                            return
                        }
                        if (mouse.button === Qt.RightButton) {
                            // Scene coordinates; the host maps them to
                            // global and shows the themed InkfallMenu.
                            var p = cell.mapToItem(null, 13, 13)
                            bridge.showContextMenu(
                                cell.swatchIndex, p.x, p.y)
                            return
                        }
                        // Remove badge handles its own clicks.
                        bridge.select(cell.swatchIndex)
                    }
                    onReleased: function(mouse) {
                        if (pressedMoved && !cell.isAdd && !cell.isNoFill)
                            cell.dropReorder(mouse.x, mouse.y)
                    }
                    property bool pressedMoved: false
                    onPositionChanged: function(mouse) {
                        if (pressed &&
                            (Math.abs(mouse.x - 13) > 8 ||
                             Math.abs(mouse.y - 13) > 8))
                            pressedMoved = true
                    }
                }

                function dropReorder(px, py) {
                    // Map release point to a grid index (same metrics).
                    var gx = cell.x + px - 8, gy = cell.y + py - 8
                    var col = Math.floor(gx / 31), row = Math.floor(gy / 31)
                    if (col < 0 || col > 5 || row < 0) return
                    var dst = row * 6 + col - well.leading
                    if (dst < 0) dst = 0
                    if (dst >= bridge.colors.length)
                        dst = bridge.colors.length - 1
                    if (dst !== cell.swatchIndex)
                        bridge.movePreset(cell.swatchIndex, dst)
                }

                Keys.onReturnPressed: {
                    if (cell.isAdd) bridge.clickAdd()
                    else if (cell.isNoFill) bridge.selectNoFill()
                    else bridge.select(cell.swatchIndex)
                }
            }
        }
    }

    Component.onCompleted: { }
}

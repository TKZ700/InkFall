import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

// Reusable inline numeric editor for tool properties (Size / Opacity).
// Context properties: `bridge` (NumericBridge), `theme` (ThemeBridge).
// Python ToolInstance stays authoritative: Enter commits via
// bridge.commit(text), Escape restores via bridge.cancel().
Rectangle {
    id: root
    color: theme.inputBg
    border.color: field.activeFocus ? theme.inputFocus
                  : (field.hovered ? theme.textMuted : theme.inputBorder)
    border.width: field.activeFocus ? 2 : 1
    radius: 8
    implicitWidth: 280
    // Children anchor top/left/right (never fill) so implicitHeight
    // always reflects content; the host syncs its fixed height to it.
    implicitHeight: field.y + field.implicitHeight + 8

    Text {
        id: caption
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.topMargin: 7
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        text: bridge.label
        color: theme.textSecondary
        // Inherits the application font: Inter + bundled Vazirmatn.
        font.pixelSize: 11
    }

        TextField {
        id: field
        objectName: "numericField"
        anchors.top: caption.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.topMargin: 1
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        anchors.bottomMargin: 8
        text: bridge.displayText
        placeholderTextColor: theme.textMuted
        // Inherits the application font: Inter + bundled Vazirmatn.
        font.pixelSize: 13
        selectByMouse: true
        hoverEnabled: true
        background: null
        padding: 0
        // Invalid text gets a visible cue without committing.
        color: bridge.isValid(field.text) ? theme.textPrimary : theme.danger
        onAccepted: bridge.commit(field.text)
        onActiveFocusChanged: {
            if (activeFocus) {
                bridge.beginEdit()
                field.selectAll()
            } else {
                bridge.commit(field.text)
            }
        }
        Keys.onEscapePressed: function(event) {
            bridge.cancel()
            event.accepted = true
        }
    }

    Connections {
        target: bridge
        function onDisplayTextChanged() {
            if (!field.activeFocus && field.text !== bridge.displayText)
                field.text = bridge.displayText
        }
        function onFocusRequested() {
            field.forceActiveFocus()
        }
    }

    Component.onCompleted: field.forceActiveFocus()
}

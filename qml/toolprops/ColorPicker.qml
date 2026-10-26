import QtQuick 2.15
import QtQuick.Controls 2.15

// Square HSV picker card: SV square + hue slider + preview + hex.
// Python (SquarePickerBridge) owns HSV state and math; this file owns
// presentation only. The square/slider are declarative Qt Quick
// gradients (GPU-composited): no per-pixel JS, no image provider, no
// tree rebuilds during drags. Context props: `bridge`, `theme`.
Rectangle {
    id: card
    color: theme.surfaceOverlay
    border.color: theme.inputBorder
    border.width: 1
    radius: 14
    implicitWidth: 240
    // Content stacks top-anchored (never fill) so implicitHeight is exact.
    implicitHeight: hexRow.y + hexRow.height + 12

    // M5-A2.2.2: QML-side reveal/hide opacity (same ~160/140ms OutCubic
    // as the QWidget fx helpers). QWidget opacity effects must never
    // cover this card's QQuickWidget ancestor (effect-source pixmap
    // would repaint the Quick framebuffer reentrantly), so Python
    // drives these instead of fx.fade_in/fade_out on the wrap.
    function playReveal(ms) {
        revealAnim.stop()
        card.opacity = 0.0
        revealAnim.duration = Math.max(1, ms || 160)
        revealAnim.from = 0.0
        revealAnim.to = 1.0
        revealAnim.start()
    }
    function playHide(ms) {
        revealAnim.stop()
        revealAnim.duration = Math.max(1, ms || 140)
        revealAnim.from = card.opacity
        revealAnim.to = 0.0
        revealAnim.start()
    }
    NumberAnimation {
        id: revealAnim
        target: card
        property: "opacity"
        duration: 160
        easing.type: Easing.OutCubic
    }

    // -- SV square (200x200): hue base + white overlay (sat) +
    // black overlay (val). Top-left = white, top-right = pure hue.
    Rectangle {
        id: svBox
        x: (parent.width - 200) / 2
        y: 12
        width: 200
        height: 200
        radius: 8
        color: bridge.hueColor
        // Saturation overlay: opaque white at left, transparent at right.
        Rectangle {
            anchors.fill: parent
            radius: 8
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: "#FFFFFFFF" }
                GradientStop { position: 1.0; color: "#00FFFFFF" }
            }
        }
        // Value overlay: transparent at top, opaque black at bottom.
        Rectangle {
            anchors.fill: parent
            radius: 8
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 0.0; color: "#00000000" }
                GradientStop { position: 1.0; color: "#FF000000" }
            }
        }
        // SV marker ring.
        Rectangle {
            id: svMarkOuter
            width: 14; height: 14; radius: 7
            color: "transparent"
            border.color: "black"; border.width: 2
            x: bridge.svMarkX * 200 - 7
            y: bridge.svMarkY * 200 - 7
        }
        Rectangle {
            id: svMarkInner
            width: 12; height: 12; radius: 6
            color: "transparent"
            border.color: "white"; border.width: 2
            x: bridge.svMarkX * 200 - 6
            y: bridge.svMarkY * 200 - 6
        }
        MouseArea {
            id: svPad
            anchors.fill: parent
            preventStealing: true
            onPressed: function(mouse) {
                bridge.pressSV(mouse.x, mouse.y, 200, 200)
            }
            onPositionChanged: function(mouse) {
                if (pressed) bridge.moveSV(mouse.x, mouse.y, 200, 200)
            }
        }
    }

    // -- Hue slider: full range, compact, below the square.
    Rectangle {
        id: hueBar
        x: (parent.width - 200) / 2
        y: svBox.y + svBox.height + 10
        width: 200
        height: 14
        radius: 7
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.00; color: "#FF0000" }
            GradientStop { position: 1 / 6; color: "#FFFF00" }
            GradientStop { position: 2 / 6; color: "#00FF00" }
            GradientStop { position: 3 / 6; color: "#00FFFF" }
            GradientStop { position: 4 / 6; color: "#0000FF" }
            GradientStop { position: 5 / 6; color: "#FF00FF" }
            GradientStop { position: 1.00; color: "#FF0000" }
        }
        Rectangle {
            id: hueMark
            width: 6; height: 18; radius: 3
            color: "transparent"
            border.color: "white"; border.width: 2
            x: bridge.hueMarkX * 200 - 3
            y: -2
        }
        // Contrast edge so the marker reads on bright stops.
        Rectangle {
            width: 8; height: 20; radius: 4
            color: "transparent"
            border.color: "black"; border.width: 1
            x: hueMark.x - 1
            y: -3
        }
        MouseArea {
            anchors.fill: parent
            preventStealing: true
            onPressed: function(mouse) { bridge.pressHue(mouse.x, 200) }
            onPositionChanged: function(mouse) {
                if (pressed) bridge.moveHue(mouse.x, 200)
            }
        }
    }

    // -- Preview dot + editable hex.
    Row {
        id: hexRow
        x: (parent.width - 200) / 2
        y: hueBar.y + hueBar.height + 10
        width: 200
        height: 26
        spacing: 8
        Rectangle {
            id: previewDot
            width: 24; height: 24; radius: 12
            anchors.verticalCenter: parent.verticalCenter
            color: bridge.previewColor
            border.color: theme.inputBorder
            border.width: 1
        }
        TextField {
            id: hexField
            objectName: "hexField"
            width: 110; height: 26
            anchors.verticalCenter: parent.verticalCenter
            text: bridge.hexText
            color: theme.textPrimary
            placeholderTextColor: theme.textMuted
            // Inherits the application font: Inter + bundled Vazirmatn.
            font.pixelSize: 13
            selectByMouse: true
            background: Rectangle {
                color: theme.inputBg
                border.color: hexField.activeFocus ? theme.inputFocus
                                                   : theme.inputBorder
                border.width: hexField.activeFocus ? 2 : 1
                radius: 6
            }
            onAccepted: bridge.commitHex(hexField.text)
            onActiveFocusChanged: {
                if (!activeFocus) bridge.commitHex(hexField.text)
            }
            Keys.onEscapePressed: function(event) {
                hexField.text = bridge.hexText
                hexField.focus = false
                event.accepted = true
            }
        }
    }

    Connections {
        target: bridge
        function onHexChanged() {
            if (!hexField.activeFocus && hexField.text !== bridge.hexText)
                hexField.text = bridge.hexText
        }
    }
}

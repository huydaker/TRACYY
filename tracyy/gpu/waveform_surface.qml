import QtQuick

Item {
    id: root
    property var amplitudes: []
    property var cueLines: []
    property real playedRatio: 0.0
    property real playheadRatio: -1.0

    clip: true

    Rectangle {
        anchors.fill: parent
        color: "#070A05"
        z: 0
    }

    // V1.2.4 draws the complete static waveform into one scene-graph texture
    // instead of creating one QQuickItem per peak. The texture is rebuilt only
    // when the track/view/size changes; playhead movement does not repaint it.
    Canvas {
        id: waveformCanvas
        anchors.fill: parent
        z: 1

        renderStrategy: Canvas.Cooperative

        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)

            var count = root.amplitudes ? root.amplitudes.length : 0
            if (count <= 0 || width <= 0 || height <= 0)
                return

            var centerY = height * 0.5
            var amplitudePixels = height * 0.46
            var step = width / Math.max(1, count)

            ctx.fillStyle = "#70756F"

            // Fill the horizontal footprint of every peak bin instead of
            // stroking a fixed 1 px line.  The old Canvas path made sparse
            // vertical sticks whenever count < widget width.
            var barWidth = Math.max(1.0, step + 0.35)
            for (var i = 0; i < count; ++i) {
                var amp = Math.max(0.0, Math.min(1.0, Number(root.amplitudes[i])))
                if (amp <= 0.0005)
                    continue

                var x = i * step
                var h = Math.max(0.75, amp * amplitudePixels)
                ctx.fillRect(x, centerY - h, barWidth, h * 2.0)
            }
        }

        Component.onCompleted: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
    }

    onAmplitudesChanged: waveformCanvas.requestPaint()

    Rectangle {
        x: 0
        y: 0
        width: Math.max(0.0, Math.min(root.width, root.playedRatio * root.width))
        height: root.height
        color: "#B2000000"
        visible: root.playedRatio > 0.0
        z: 2
    }

    Repeater {
        model: root.cueLines
        delegate: Rectangle {
            property real ratio: Number(modelData.ratio)
            x: ratio * root.width - width * 0.5
            y: 12
            width: Math.max(2.0, Number(modelData.lineWidth))
            height: Math.max(0.0, root.height - 12)
            color: String(modelData.color)
            visible: ratio >= 0.0 && ratio <= 1.0
            antialiasing: false
            z: 3
        }
    }

    Rectangle {
        x: root.playheadRatio * root.width - width * 0.5
        y: 0
        width: 2.2
        height: root.height
        color: "#D9FFE1"
        visible: root.playheadRatio >= 0.0 && root.playheadRatio <= 1.0
        antialiasing: false
        z: 4
    }
}

//@ pragma ShellId chronix-widget
import Quickshell
import Quickshell.Io
import QtQuick

PanelWindow {
    id: root

    readonly property int innerRadius: 30
    readonly property int ringSpacing: 12
    readonly property int ringStrokeWidth: 8
    readonly property int ringCount: activeTasks.length
    readonly property int outerRadius: innerRadius + Math.max(0, ringCount - 1) * ringSpacing
    readonly property int ringVisualSize: Math.max(
        128,
        Math.ceil((outerRadius + ringStrokeWidth / 2 + 8) * 2)
    )

    property var tasks: []
    property var blocks: []
    property double serverOffsetMs: 0
    property double nowMs: Date.now()
    property int pollIntervalMs: 10000
    property bool requestInFlight: false
    property string apiStatus: "connecting"
    property int widgetTopMargin: 18
    property int widgetRightMargin: 18
    property int dragOriginTop: 18
    property int dragOriginRight: 18
    property real dragStartX: 0
    property real dragStartY: 0
    property bool dragHandleHovered: false
    readonly property real visualOpacity: dragHandleHovered ? 1 : 0.55
    property double lastSuccessfulSyncMs: 0
    property string defaultApiBaseUrl: "http://127.0.0.1:8766"
    property string apiBaseUrl: {
        const environmentUrl = Quickshell.env("CHRONIX_API_BASE_URL");
        const configuredUrl = apiBase.text().trim();
        return environmentUrl || configuredUrl || defaultApiBaseUrl;
    }

    readonly property var activeTasks: tasks.filter(function(task) {
        const start = Date.parse(task.startAt);
        const end = Date.parse(task.endAt);
        return Number.isFinite(start) &&
            Number.isFinite(end) &&
            end > start &&
            start <= nowMs &&
            nowMs < end;
    })

    readonly property var activeBlocks: blocks.filter(function(block) {
        const start = Date.parse(block.startAt);
        const end = Date.parse(block.endAt);
        return Number.isFinite(start) && Number.isFinite(end) &&
            end > start && start <= nowMs && nowMs < end;
    })

    readonly property var upcomingTasks: tasks.filter(function(task) {
        const start = Date.parse(task.startAt);
        const end = Date.parse(task.endAt);
        return Number.isFinite(start) && Number.isFinite(end) &&
            end > start && start > nowMs;
    }).sort(function(a, b) {
        const startDifference = Date.parse(a.startAt) - Date.parse(b.startAt);
        if (startDifference !== 0) return startDifference;
        if (a.track === "primary" && b.track !== "primary") return -1;
        if (b.track === "primary" && a.track !== "primary") return 1;
        return String(a.id).localeCompare(String(b.id));
    })

    readonly property var nextTask: upcomingTasks.length > 0 ? upcomingTasks[0] : null

    readonly property bool hasUpcomingBlocks: blocks.some(function(block) {
        const start = Date.parse(block.startAt);
        const end = Date.parse(block.endAt);
        return Number.isFinite(start) && Number.isFinite(end) &&
            end > start && start > nowMs;
    })

    readonly property var displayBlocks: activeBlocks.slice().sort(function(a, b) {
        return Date.parse(a.endAt) - Date.parse(b.endAt);
    })

    readonly property var displayTasks: activeTasks.slice().sort(function(a, b) {
        const endDifference = Date.parse(a.endAt) - Date.parse(b.endAt);
        return endDifference || String(a.id).localeCompare(String(b.id));
    })

    implicitWidth: ringVisualSize + 310
    implicitHeight: Math.max(ringVisualSize, Math.max(90, displayTasks.length * 38 + displayBlocks.length * 34 + (nextTask !== null ? 53 : 0) + (apiStatus === "offline" && lastSuccessfulSyncMs > 0 ? 16 : 0) + 66)) + 24
    color: "transparent"
    mask: Region { item: dragHandleArea }
    aboveWindows: true
    focusable: false
    exclusionMode: ExclusionMode.Ignore

    anchors {
        top: true
        right: true
    }

    margins {
        top: root.widgetTopMargin
        right: root.widgetRightMargin
    }

    FileView {
        id: apiBase
        path: Quickshell.env("HOME") + "/.config/chronix-widget/api-base-url"
        blockLoading: true
        printErrors: false
        watchChanges: true
        onFileChanged: reload()
    }

    function colorForTask(task) {
        const value = String(task.id);
        let hash = 2166136261;
        for (let index = 0; index < value.length; index++) {
            hash ^= value.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        const hue = ((hash >>> 0) % 360) / 360;
        return Qt.hsla(hue, 0.72, 0.62, 1);
    }

    function shortLabel(task) {
        const value = String(task.title || task.id || "TASK").trim();
        return value.length > 7 ? value.slice(0, 6) + "…" : value;
    }

    function shortBlockLabel(block) {
        const value = String(block.title || block.kind || "BLOCK").trim();
        return value.length > 10 ? value.slice(0, 9) + "..." : value;
    }

    function colorForBlock(block) {
        const label = String(block.title || "").toLowerCase();
        if (label.includes("sleep") || block.kind === "sleep") return "#8ba8ff";
        if (block.kind === "meeting") return "#c49bff";
        if (block.kind === "break") return "#62c9bb";
        return "#e0ad68";
    }

    function formatRemaining(task) {
        const remainingMs = Math.max(0, Date.parse(task.endAt) - nowMs);
        if (remainingMs < 60000) {
            return Math.max(1, Math.ceil(remainingMs / 1000)) + "s left";
        }

        const totalMinutes = Math.ceil(remainingMs / 60000);
        const hours = Math.floor(totalMinutes / 60);
        const minutes = totalMinutes % 60;
        return hours > 0
            ? hours + "h " + String(minutes).padStart(2, "0") + "m left"
            : totalMinutes + "m left";
    }

    function formatStartTime(block) {
        const start = new Date(Date.parse(block.startAt));
        return String(start.getHours()).padStart(2, "0") + ":" +
            String(start.getMinutes()).padStart(2, "0");
    }

    function formatBlockStatus(block) {
        const start = Date.parse(block.startAt);
        if (nowMs < start) {
            const minutes = Math.ceil((start - nowMs) / 60000);
            return "starts in " + minutes + "m | at " + formatStartTime(block);
        }
        return formatRemaining(block) + " | ends " + formatEndTime(block);
    }

    function formatBlockCenter(block) {
        const start = Date.parse(block.startAt);
        if (nowMs < start) {
            return "NEXT IN " + Math.ceil((start - nowMs) / 60000) + "m";
        }
        return formatRemaining(block);
    }

    function formatEndTime(task) {
        const end = new Date(Date.parse(task.endAt));
        return String(end.getHours()).padStart(2, "0") + ":" +
            String(end.getMinutes()).padStart(2, "0");
    }

    function scheduleRetry() {
        pollIntervalMs = Math.min(60000, Math.max(10000, Math.round(pollIntervalMs * 1.5)));
        pollTimer.interval = pollIntervalMs;
        pollTimer.restart();
    }

    function handleRequestFailure() {
        if (!requestInFlight) {
            return;
        }
        requestInFlight = false;
        apiStatus = "offline";
        scheduleRetry();
    }

    function refreshTasks() {
        if (requestInFlight) {
            return;
        }

        requestInFlight = true;
        const request = new XMLHttpRequest();
        request.timeout = 8000;
        request.onreadystatechange = function() {
            if (request.readyState !== XMLHttpRequest.DONE || !root.requestInFlight) {
                return;
            }

            root.requestInFlight = false;
            if (request.status !== 200) {
                root.apiStatus = "offline";
                root.scheduleRetry();
                return;
            }

            try {
                const payload = JSON.parse(request.responseText);
                if (payload.ok !== true ||
                    !Array.isArray(payload.tasks) ||
                    !Array.isArray(payload.blocks) ||
                    !payload.tasks.every(function(task) {
                        return task &&
                            typeof task.id === "string" &&
                            typeof task.title === "string" &&
                            typeof task.startAt === "string" &&
                            typeof task.endAt === "string";
                    }) ||
                    !payload.blocks.every(function(block) {
                        return block &&
                            typeof block.id === "string" &&
                            typeof block.title === "string" &&
                            typeof block.kind === "string" &&
                            typeof block.startAt === "string" &&
                            typeof block.endAt === "string";
                    })) {
                    throw new Error("Invalid Chronix widget response");
                }

                root.tasks = payload.tasks;
                root.blocks = payload.blocks;
                const serverTime = Date.parse(payload.serverTime);
                if (Number.isFinite(serverTime)) {
                    root.serverOffsetMs = serverTime - Date.now();
                    root.nowMs = Date.now() + root.serverOffsetMs;
                }

                if (typeof payload.pollAfterMs === "number" &&
                    Number.isFinite(payload.pollAfterMs) &&
                    payload.pollAfterMs >= 1000) {
                    root.pollIntervalMs = payload.pollAfterMs;
                }

                root.lastSuccessfulSyncMs = Date.now();
                root.apiStatus = "online";
                pollTimer.interval = root.pollIntervalMs;
                pollTimer.restart();
            } catch (error) {
                console.warn("Chronix widget received invalid API data", error);
                root.apiStatus = "offline";
                root.scheduleRetry();
            }
        };

        request.onerror = function() {
            root.handleRequestFailure();
        };
        request.ontimeout = function() {
            root.handleRequestFailure();
        };

        request.open(
            "GET",
            apiBaseUrl.replace(/\/+$/, "") + "/api/v1/active-tasks",
            true
        );
        request.setRequestHeader("Accept", "application/json");
        request.send();
    }

    Rectangle {
        anchors.fill: parent
        opacity: root.visualOpacity
        Behavior on opacity { NumberAnimation { duration: 160 } }
        radius: 16
        color: "#e6101722"
        border.width: 1
        border.color: "#50394a5d"
    }

    Item {
        id: dragHandleArea
        width: root.width
        height: 14
        opacity: root.visualOpacity
        Behavior on opacity { NumberAnimation { duration: 160 } }
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        z: 10

        Rectangle {
            anchors.centerIn: parent
            width: 34
            height: 4
            radius: 2
            color: "#70a7ff"
        }

        HoverHandler {
            cursorShape: Qt.SizeAllCursor
            onHoveredChanged: root.dragHandleHovered = hovered
        }

        DragHandler {
            target: null
            acceptedButtons: Qt.LeftButton
            dragThreshold: 0
            onActiveChanged: {
                if (!active) return
                const pointerPosition = dragHandleArea.mapToGlobal(centroid.position)
                root.dragStartX = pointerPosition.x
                root.dragStartY = pointerPosition.y
                root.dragOriginTop = root.widgetTopMargin
                root.dragOriginRight = root.widgetRightMargin
            }
            onCentroidChanged: {
                if (!active) return
                const pointerPosition = dragHandleArea.mapToGlobal(centroid.position)
                root.widgetTopMargin = Math.max(0,
                    root.dragOriginTop + Math.round(pointerPosition.y - root.dragStartY))
                root.widgetRightMargin = Math.max(0,
                    root.dragOriginRight - Math.round(pointerPosition.x - root.dragStartX))
            }
        }
    }

    Row {
        anchors.fill: parent
        opacity: root.visualOpacity
        Behavior on opacity { NumberAnimation { duration: 160 } }
        anchors.margins: 12
        spacing: 12

        Item {
            id: ringArea
            width: root.ringVisualSize
            height: root.ringVisualSize
            anchors.verticalCenter: parent.verticalCenter

            Canvas {
                id: rings
                anchors.fill: parent

                onPaint: {
                    const context = getContext("2d");
                    context.clearRect(0, 0, width, height);
                    context.lineWidth = root.ringStrokeWidth;
                    context.lineCap = "round";

                    const center = width / 2;
                    const count = root.activeTasks.length;

                    for (let index = 0; index < count; index++) {
                        const task = root.activeTasks[index];
                        const radius = root.innerRadius + (count - index - 1) * root.ringSpacing;
                        const start = Date.parse(task.startAt);
                        const end = Date.parse(task.endAt);
                        const progress = Math.max(
                            0,
                            Math.min(1, (end - root.nowMs) / (end - start))
                        );

                        context.beginPath();
                        context.strokeStyle = "rgba(190,205,218,0.18)";
                        context.arc(center, center, radius, 0, Math.PI * 2, false);
                        context.stroke();

                        if (progress > 0) {
                            context.beginPath();
                            context.strokeStyle = root.colorForTask(task).toString();
                            context.arc(
                                center,
                                center,
                                radius,
                                -Math.PI / 2,
                                -Math.PI / 2 + Math.PI * 2 * progress,
                                false
                            );
                            context.stroke();
                        }
                    }
                }
            }

            Repeater {
                model: root.activeTasks

                delegate: Rectangle {
                    required property var modelData
                    required property int index

                    readonly property real ringRadius:
                        root.innerRadius + (root.activeTasks.length - index - 1) * root.ringSpacing
                    readonly property string ringLabel: root.shortLabel(modelData)
                    readonly property real labelWidth: Math.max(28, ringLabel.length * 5.8 + 10)

                    width: labelWidth
                    height: 12
                    x: (ringArea.width - width) / 2
                    y: ringArea.height / 2 - ringRadius - height / 2
                    radius: 4
                    color: "#f20d1520"
                    border.width: 1
                    border.color: root.colorForTask(modelData)

                    Text {
                        anchors.centerIn: parent
                        text: parent.ringLabel
                        color: "#f4f7fa"
                        font.pixelSize: 8
                        font.weight: Font.DemiBold
                        renderType: Text.NativeRendering
                    }
                }
            }

            Column {
                anchors.centerIn: parent
                spacing: 1

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: root.activeTasks.length > 0
                        ? String(root.activeTasks.length)
                        : (root.activeBlocks.length > 0
                            ? root.shortBlockLabel(root.activeBlocks[0])
                            : "FREE")
                    color: "#f4f7fa"
                    font.pixelSize: root.activeTasks.length > 0 ? 23 : 11
                    font.weight: Font.DemiBold
                }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: root.activeTasks.length > 0
                        ? "TASKS"
                        : (root.displayBlocks.length > 0
                            ? root.formatBlockCenter(root.displayBlocks[0])
                            : "TIME")
                    color: "#9eacbb"
                    font.pixelSize: 7
                    font.weight: Font.Bold
                    font.letterSpacing: 1
                }
            }
        }

        Column {
            width: parent.width - ringArea.width - parent.spacing
            anchors.verticalCenter: parent.verticalCenter
            spacing: 8

            Item {
                width: parent.width
                height: 14
            }

            Row {
                width: parent.width
                height: 14
                spacing: 6

                Text {
                    text: "NOW"
                    color: "#aab8c7"
                    font.pixelSize: 9
                    font.weight: Font.Bold
                    font.letterSpacing: 1
                }

                Item { width: 1; height: 1 }

                Rectangle {
                    width: 6
                    height: 6
                    radius: 3
                    anchors.verticalCenter: parent.verticalCenter
                    color: root.apiStatus === "online" ? "#48c78e" : "#e0a34a"
                }

                Text {
                    text: root.apiStatus === "online" ? "LIVE" :
                        (root.apiStatus === "connecting" ? "CONNECTING" : "OFFLINE")
                    color: root.apiStatus === "online" ? "#7bd8a9" : "#e0b36d"
                    font.pixelSize: 8
                    font.weight: Font.DemiBold
                }
            }

            Text {
                visible: root.displayBlocks.length > 0
                text: "CURRENT TIME BLOCKS"
                color: "#aab8c7"
                font.pixelSize: 9
                font.weight: Font.Bold
                font.letterSpacing: 1
            }

            Repeater {
                model: root.displayBlocks
                delegate: Row {
                    required property var modelData
                    width: parent.width
                    height: 26
                    spacing: 8
                    Rectangle {
                        width: 4
                        height: 22
                        radius: 2
                        anchors.verticalCenter: parent.verticalCenter
                        color: root.colorForBlock(modelData)
                    }
                    Column {
                        width: parent.width - 12
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 1
                        Text {
                            width: parent.width
                            text: modelData.title
                            color: "#f1f5f9"
                            font.pixelSize: 10
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                        Text {
                            width: parent.width
                            text: root.formatBlockStatus(modelData)
                            color: "#aebdcb"
                            font.pixelSize: 9
                            font.weight: Font.Medium
                        }
                    }
                }
            }

            Text {
                visible: root.nextTask !== null
                text: "UP NEXT"
                color: "#aab8c7"
                font.pixelSize: 9
                font.weight: Font.Bold
                font.letterSpacing: 1
            }

            Row {
                visible: root.nextTask !== null
                width: parent.width
                height: 26
                spacing: 8

                Rectangle {
                    width: 4
                    height: 22
                    radius: 2
                    anchors.verticalCenter: parent.verticalCenter
                    color: root.nextTask ? root.colorForTask(root.nextTask) : "transparent"
                }

                Column {
                    width: parent.width - 12
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 1

                    Text {
                        width: parent.width
                        text: root.nextTask ? (root.nextTask.title || root.nextTask.id) : ""
                        color: "#f1f5f9"
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }

                    Text {
                        width: parent.width
                        text: root.nextTask ? root.formatBlockStatus(root.nextTask) : ""
                        color: "#aebdcb"
                        font.pixelSize: 9
                        font.weight: Font.Medium
                    }
                }
            }

            Text {
                visible: root.displayTasks.length > 0
                text: "ACTIVE TASKS"
                color: "#aab8c7"
                font.pixelSize: 9
                font.weight: Font.Bold
                font.letterSpacing: 1
            }

            Repeater {
                model: root.displayTasks

                delegate: Row {
                    required property var modelData
                    width: parent.width
                    height: 30
                    spacing: 8

                    Rectangle {
                        width: 4
                        height: 25
                        radius: 2
                        anchors.verticalCenter: parent.verticalCenter
                        color: root.colorForTask(modelData)
                    }

                    Column {
                        width: parent.width - 12
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 2

                        Text {
                            width: parent.width
                            text: modelData.title || modelData.id
                            color: "#f1f5f9"
                            font.pixelSize: 10
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }

                        Text {
                            width: parent.width
                            text: root.formatRemaining(modelData) +
                                "  ·  ends " + root.formatEndTime(modelData)
                            color: "#aebdcb"
                            font.pixelSize: 9
                            font.weight: Font.Medium
                        }
                    }
                }
            }

            Text {
                visible: root.displayTasks.length === 0 && root.displayBlocks.length === 0 && root.nextTask === null
                width: parent.width
                text: root.apiStatus === "offline"
                    ? "Waiting for Chronix to reconnect"
                    : (root.hasUpcomingBlocks
                        ? "No current block or task"
                        : "Free time - no scheduled block or task")
                color: "#aebdcb"
                font.pixelSize: 10
                wrapMode: Text.WordWrap
            }

            Text {
                visible: root.apiStatus === "offline" && root.lastSuccessfulSyncMs > 0
                text: "Last sync " + Math.max(
                    1,
                    Math.floor((Date.now() - root.lastSuccessfulSyncMs) / 60000)
                ) + "m ago"
                color: "#e0b36d"
                font.pixelSize: 8
            }
        }
    }

    Timer {
        interval: 1000
        running: true
        repeat: true
        onTriggered: {
            root.nowMs = Date.now() + root.serverOffsetMs;
            rings.requestPaint();
        }
    }

    Timer {
        id: pollTimer
        interval: root.pollIntervalMs
        running: false
        repeat: false
        onTriggered: root.refreshTasks()
    }

    onTasksChanged: rings.requestPaint()
    onBlocksChanged: rings.requestPaint()
    onNowMsChanged: rings.requestPaint()
    Component.onCompleted: refreshTasks()
}

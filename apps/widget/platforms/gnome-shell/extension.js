import Gio from "gi://Gio";
import GLib from "gi://GLib";
import Soup from "gi://Soup?version=3.0";
import St from "gi://St";
import Cairo from "cairo";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8766";
const POLL_INTERVAL_SECONDS = 10;
const TICK_INTERVAL_MS = 1000;
const INNER_RADIUS = 30;
const RING_SPACING = 12;
const STROKE_WIDTH = 8;
const EDGE_PADDING = 5;
const WINDOW_MINIMUM_SIZE = 128;
const SCREEN_MARGIN = 18;

function readApiBaseUrl() {
    const environmentUrl = GLib.getenv("CHRONIX_API_BASE_URL");
    if (environmentUrl)
        return environmentUrl.replace(/\/+$/, "");

    const configPath = GLib.build_filenamev([
        GLib.get_user_config_dir(),
        "chronix-widget",
        "api-base-url",
    ]);

    try {
        const [success, contents] = GLib.file_get_contents(configPath);
        if (success) {
            const configuredUrl = new TextDecoder().decode(contents).trim();
            if (configuredUrl)
                return configuredUrl.replace(/\/+$/, "");
        }
    } catch (error) {
        console.debug("Chronix widget API URL config is not available", error);
    }

    return DEFAULT_API_BASE_URL;
}

function isValidTask(task) {
    return task !== null &&
        typeof task === "object" &&
        typeof task.id === "string" &&
        typeof task.title === "string" &&
        typeof task.startAt === "string" &&
        typeof task.endAt === "string";
}

function isValidBlock(block) {
    return block !== null &&
        typeof block === "object" &&
        typeof block.id === "string" &&
        typeof block.title === "string" &&
        typeof block.kind === "string" &&
        typeof block.startAt === "string" &&
        typeof block.endAt === "string";
}

function taskColor(task) {
    let hash = 2166136261;
    for (const character of task.id)
        hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
    const hue = ((hash >>> 0) % 360) / 60;
    const chroma = 0.72;
    const secondary = chroma * (1 - Math.abs(hue % 2 - 1));
    const channels = hue < 1 ? [chroma, secondary, 0] :
        hue < 2 ? [secondary, chroma, 0] :
        hue < 3 ? [0, chroma, secondary] :
        hue < 4 ? [0, secondary, chroma] :
        hue < 5 ? [secondary, 0, chroma] : [chroma, 0, secondary];
    const offset = 0.62 - chroma / 2;
    return channels.map(channel => channel + offset);
}

function remainingLabel(endAt, now) {
    const remainingMs = Math.max(0, Date.parse(endAt) - now);
    const minutes = Math.ceil(remainingMs / 60000);
    if (minutes < 1)
        return "<1m";
    if (minutes < 60)
        return minutes + "m left";
    return Math.floor(minutes / 60) + "h " +
        String(minutes % 60).padStart(2, "0") + "m left";
}

export default class ChronixWidgetExtension extends Extension {
    enable() {
        this._enabled = true;
        this._tasks = [];
        this._blocks = [];
        this._activeTasks = [];
        this._activeBlocks = [];
        this._serverOffsetMs = 0;
        this._apiBaseUrl = readApiBaseUrl();
        this._requestInFlight = false;
        this._size = 0;
        this._position = null;
        this._session = new Soup.Session();
        this._cancellable = new Gio.Cancellable();

        this._area = new St.DrawingArea({
            name: "chronix-task-rings",
            reactive: false,
            can_focus: false,
        });
        this._repaintId = this._area.connect("repaint", area => {
            this._draw(area);
        });

        Main.layoutManager.addChrome(this._area, {
            affectsStruts: false,
            trackFullscreen: true,
        });
        this._monitorChangedId = Main.layoutManager.connect(
            "monitors-changed",
            () => this._updateGeometry(),
        );

        this._pollSourceId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT,
            POLL_INTERVAL_SECONDS,
            () => {
                this._refreshTasks();
                return GLib.SOURCE_CONTINUE;
            },
        );
        this._tickSourceId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT,
            TICK_INTERVAL_MS,
            () => {
                this._updateActiveTasks();
                return GLib.SOURCE_CONTINUE;
            },
        );

        this._updateActiveTasks();
        this._refreshTasks();
    }

    disable() {
        this._enabled = false;

        if (this._pollSourceId) {
            GLib.source_remove(this._pollSourceId);
            this._pollSourceId = 0;
        }
        if (this._tickSourceId) {
            GLib.source_remove(this._tickSourceId);
            this._tickSourceId = 0;
        }
        if (this._monitorChangedId) {
            Main.layoutManager.disconnect(this._monitorChangedId);
            this._monitorChangedId = 0;
        }

        this._cancellable?.cancel();
        this._session?.abort();

        if (this._area) {
            Main.layoutManager.removeChrome(this._area);
            this._area.disconnect(this._repaintId);
            this._area.destroy();
            this._area = null;
        }

        this._session = null;
        this._cancellable = null;
        this._tasks = [];
        this._blocks = [];
        this._activeTasks = [];
        this._activeBlocks = [];
    }

    _refreshTasks() {
        if (!this._enabled || this._requestInFlight)
            return;

        const message = Soup.Message.new(
            "GET",
            this._apiBaseUrl + "/api/v1/active-tasks",
        );
        if (!message)
            return;

        message.get_request_headers().append("Accept", "application/json");
        this._requestInFlight = true;

        this._session.send_and_read_async(
            message,
            GLib.PRIORITY_DEFAULT,
            this._cancellable,
            (session, result) => {
                this._requestInFlight = false;
                if (!this._enabled)
                    return;

                try {
                    const bytes = session.send_and_read_finish(result);
                    if (message.get_status() !== Soup.Status.OK)
                        return;

                    const payload = JSON.parse(
                        new TextDecoder().decode(bytes.get_data()),
                    );
                    if (payload.ok !== true ||
                        !Array.isArray(payload.tasks) ||
                        !Array.isArray(payload.blocks))
                        return;

                    this._tasks = payload.tasks.filter(isValidTask);
                    this._blocks = payload.blocks.filter(isValidBlock);
                    const serverTime = Date.parse(payload.serverTime);
                    if (Number.isFinite(serverTime))
                        this._serverOffsetMs = serverTime - Date.now();

                    this._updateActiveTasks();
                } catch (error) {
                    console.debug("Chronix widget API request failed", error);
                }
            },
        );
    }

    _updateActiveTasks() {
        if (!this._enabled)
            return;

        const now = Date.now() + this._serverOffsetMs;
        this._activeTasks = this._tasks.filter(task => {
            const start = Date.parse(task.startAt);
            const end = Date.parse(task.endAt);
            return Number.isFinite(start) &&
                Number.isFinite(end) &&
                end > start &&
                start <= now &&
                now < end;
        });
        this._activeBlocks = this._blocks.filter(block => {
            const start = Date.parse(block.startAt);
            const end = Date.parse(block.endAt);
            return Number.isFinite(start) &&
                Number.isFinite(end) &&
                end > start &&
                start <= now &&
                now < end;
        });

        this._updateGeometry();
    }

    _updateGeometry() {
        if (!this._area)
            return;

        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor)
            return;

        const count = this._activeTasks.length;
        const size = Math.max(
            WINDOW_MINIMUM_SIZE,
            (INNER_RADIUS + Math.max(0, count - 1) * RING_SPACING + EDGE_PADDING) * 2,
        );
        const x = monitor.x + monitor.width - size - SCREEN_MARGIN;
        const y = monitor.y + SCREEN_MARGIN;

        if (size !== this._size) {
            this._area.set_size(size, size);
            this._size = size;
        }
        if (!this._position || this._position.x !== x || this._position.y !== y) {
            this._area.set_position(x, y);
            this._position = { x, y };
        }
        this._area.queue_repaint();
    }

    _draw(area) {
        const cr = area.get_context();
        try {
            const size = Math.max(area.width, area.height);
            const center = size / 2;
            const count = this._activeTasks.length;
            const now = Date.now() + this._serverOffsetMs;

            cr.setOperator(Cairo.Operator.CLEAR);
            cr.paint();
            cr.setOperator(Cairo.Operator.OVER);
            cr.setLineWidth(STROKE_WIDTH);
            cr.setLineCap(Cairo.LineCap.ROUND);

            for (let index = 0; index < count; index++) {
                const task = this._activeTasks[index];
                const radius = INNER_RADIUS + (count - index - 1) * RING_SPACING;
                const start = Date.parse(task.startAt);
                const end = Date.parse(task.endAt);
                const progress = Math.max(
                    0,
                    Math.min(1, (end - now) / (end - start)),
                );

                cr.setSourceRGBA(190 / 255, 205 / 255, 218 / 255, 0.24);
                cr.arc(center, center, radius, 0, Math.PI * 2);
                cr.stroke();

                if (progress > 0) {
                    const [red, green, blue] = taskColor(task);
                    cr.setSourceRGBA(red, green, blue, 1);
                    cr.arc(
                        center,
                        center,
                        radius,
                        -Math.PI / 2,
                        -Math.PI / 2 + Math.PI * 2 * progress,
                    );
                    cr.stroke();
                }
            }

            cr.setSourceRGBA(244 / 255, 247 / 255, 250 / 255, 1);
            cr.selectFontFace("Sans", Cairo.FontSlant.NORMAL, Cairo.FontWeight.BOLD);
            if (count > 0) {
                const text = String(count);
                cr.setFontSize(22);
                const extents = cr.textExtents(text);
                cr.moveTo(
                    center - extents.width / 2 - extents.xBearing,
                    center - extents.height / 2 - extents.yBearing,
                );
                cr.showText(text);
            } else if (this._activeBlocks.length > 0) {
                const block = this._activeBlocks[0];
                const title = block.title.length > 11
                    ? block.title.slice(0, 10) + "..."
                    : block.title;
                cr.setFontSize(10);
                let extents = cr.textExtents(title);
                cr.moveTo(center - extents.width / 2 - extents.xBearing, center - 3);
                cr.showText(title);
                const remaining = remainingLabel(block.endAt, now);
                cr.setFontSize(9);
                extents = cr.textExtents(remaining);
                cr.moveTo(center - extents.width / 2 - extents.xBearing, center + 12);
                cr.showText(remaining);
            } else {
                const text = "FREE";
                cr.setFontSize(12);
                const extents = cr.textExtents(text);
                cr.moveTo(
                    center - extents.width / 2 - extents.xBearing,
                    center - extents.height / 2 - extents.yBearing,
                );
                cr.showText(text);
            }
        } finally {
            cr.$dispose();
        }
    }
}

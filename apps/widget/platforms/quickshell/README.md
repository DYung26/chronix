# Quickshell overlay

This is the native Hyprland/Wayland overlay for the Chronix task rings. It uses a top-layer PanelWindow, does not reserve desktop space or take keyboard focus, and sizes itself for active time blocks, active tasks, and the next scheduled task.

## Install

Run apps/widget/platforms/quickshell/install.sh from the Chronix repository root. The installer creates a named Quickshell config link and enables the user service.

The API base URL is read from ~/.config/chronix-widget/api-base-url. The default is http://127.0.0.1:8766. For a remote API, set this file to your private Tailscale Serve URL.

Quickshell 0.3.2 is the tested target. The config uses Quickshell PanelWindow, FileView, and QML XMLHttpRequest APIs. The top-center blue handle is a 34×4 visual strip over a full-width 14px drag area across the complete widget, including the task-ring area. Only that top strip area accepts pointer input; the rest of the display is click-through. Drag the strip to reposition the overlay. Dragging uses screen-space pointer movement. The widget dims when idle and returns to full opacity while hovering over the drag strip. UP NEXT shows the next scheduled task, not the next time block.

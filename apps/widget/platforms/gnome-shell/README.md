# GNOME Shell overlay

This is the native GNOME Wayland overlay for Chronix task rings. It uses GNOME Shell chrome rather than a normal desktop window, remains visible above fullscreen windows, does not reserve workspace space, and is non-reactive and non-focusable.

## Install

Run apps/widget/platforms/gnome-shell/install.sh from the Chronix repository root while logged into the GNOME session. If the current Shell session has not indexed the newly installed extension, log out and back in once; the installer reports this case.

The API base URL is read from ~/.config/chronix-widget/api-base-url. The default is http://127.0.0.1:8766. For a remote API, set this file to your private Tailscale Serve URL.

The extension targets GNOME Shell 51 and uses St.DrawingArea with Cairo for rendering and libsoup 3 for asynchronous HTTP polling. Task rings use stable per-task colours; when no task is active, the centre shows the current time-block label and remaining time (or FREE).

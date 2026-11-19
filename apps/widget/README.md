# Chronix Widget

A small Tauri desktop widget that displays one concentric countdown ring for every currently active Chronix scheduled task. The centre number is the actual active-task count; the widget does not cap or throttle the task list.

## Chronix API

Set VITE_CHRONIX_API_BASE_URL to the base URL of the private Chronix API. The default is http://127.0.0.1:8766. For a remote API, configure the private Tailscale Serve URL in your local environment; do not commit that URL.

The widget requests GET /api/v1/active-tasks immediately on startup and then uses the server-provided polling interval (10 seconds by default). Ring progress is animated locally once per second using the server clock offset, so the display does not need a WebSocket connection.

Copy .env.example to .env.local for local development and set the base URL to the Chronix API server. For deployed devices, use the private Tailscale Serve HTTPS URL.

## Development

Run these commands from the widget directory (apps/widget):

```bash
npm ci
npm test
npm run build
npm run tauri dev
```

Tauri requires the platform's native build toolchain. Windows builds require the Rust MSVC toolchain, Visual Studio C++ Build Tools, and WebView2 Runtime. Linux builds require Rust, GTK/WebKitGTK, and the platform's native overlay integration.

## Display behavior

The widget renders every task returned by Chronix. It does not enforce a concurrency limit. Task intervals are interpreted as half-open ranges: the start is active, and the end is inactive.

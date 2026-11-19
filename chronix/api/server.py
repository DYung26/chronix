"""Loopback-only HTTP server for desktop widgets and local integrations."""

from __future__ import annotations

import argparse
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from chronix.api.service import ActiveTaskProvider, create_payload

logger = logging.getLogger("chronix.api")
ALLOWED_ORIGINS = frozenset(
    {
        "http://tauri.localhost",
        "tauri://localhost",
        "http://localhost:1420",
        "http://127.0.0.1:1420",
    }
)


def make_handler(provider: ActiveTaskProvider):
    class ChronixAPIHandler(BaseHTTPRequestHandler):
        def _send_cors_headers(self) -> bool:
            origin = self.headers.get("Origin")
            if origin not in ALLOWED_ORIGINS:
                return False

            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            return True

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in ALLOWED_ORIGINS:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            self.send_response(204)
            self._send_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send_json(200, {"ok": True, "service": "chronix-api"})
                return
            if path != "/api/v1/active-tasks":
                self._send_json(404, {"ok": False, "error": "not found"})
                return

            try:
                self._send_json(200, create_payload(provider))
            except Exception:
                logger.exception("Unable to resolve scheduled task intervals")
                self._send_json(503, {"ok": False, "error": "task data unavailable"})

        def log_message(self, format: str, *args) -> None:
            logger.info("%s - %s", self.address_string(), format % args)

    return ChronixAPIHandler


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve Chronix's read-only desktop widget API."
    )
    parser.add_argument(
        "--host", default=os.environ.get("CHRONIX_API_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("CHRONIX_API_PORT", "8766"))
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("CHRONIX_API_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(ActiveTaskProvider())
    )
    server.daemon_threads = True
    logger.info("Chronix API listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Chronix API")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

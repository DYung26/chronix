import json
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.request import Request, urlopen

from chronix.api.server import make_handler


def test_health_and_active_task_routes_return_json_and_cors_headers():
    provider = SimpleNamespace(
        get_active_tasks=lambda now: [
            {
                "id": "task-1",
                "title": "Write API tests",
                "startAt": "2026-11-16T10:00:00+00:00",
                "endAt": "2026-11-16T11:00:00+00:00",
                "track": "primary",
            }
        ]
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(provider))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with urlopen(f"{base_url}/healthz") as response:
            health = json.loads(response.read())
            assert response.status == 200
            assert health == {"ok": True, "service": "chronix-api"}

        with urlopen(f"{base_url}/api/v1/active-tasks") as response:
            payload = json.loads(response.read())
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert payload["ok"] is True
            assert payload["pollAfterMs"] == 10000
            assert payload["tasks"][0]["id"] == "task-1"

        request = Request(f"{base_url}/api/v1/active-tasks", method="OPTIONS")
        with urlopen(request) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from chronix.api.server import make_handler


@contextmanager
def _api_server():
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
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_and_active_task_routes_return_json_without_browser_origin():
    with _api_server() as base_url:
        with urlopen(f"{base_url}/healthz") as response:
            health = json.loads(response.read())
            assert response.status == 200
            assert health == {"ok": True, "service": "chronix-api"}
            assert "Access-Control-Allow-Origin" not in response.headers

        with urlopen(f"{base_url}/api/v1/active-tasks") as response:
            payload = json.loads(response.read())
            assert response.status == 200
            assert "Access-Control-Allow-Origin" not in response.headers
            assert payload["ok"] is True
            assert payload["pollAfterMs"] == 10000
            assert payload["tasks"][0]["id"] == "task-1"


def test_active_task_route_allows_only_tauri_and_local_dev_origins():
    allowed_origins = (
        "http://tauri.localhost",
        "tauri://localhost",
        "http://localhost:1420",
        "http://127.0.0.1:1420",
    )
    with _api_server() as base_url:
        for origin in allowed_origins:
            request = Request(
                f"{base_url}/api/v1/active-tasks",
                headers={"Origin": origin},
            )
            with urlopen(request) as response:
                assert response.status == 200
                assert response.headers["Access-Control-Allow-Origin"] == origin
                assert response.headers["Vary"] == "Origin"

        request = Request(
            f"{base_url}/api/v1/active-tasks",
            headers={"Origin": "https://attacker.example"},
        )
        with urlopen(request) as response:
            assert response.status == 200
            assert "Access-Control-Allow-Origin" not in response.headers


def test_options_preflight_rejects_untrusted_origins():
    with _api_server() as base_url:
        request = Request(
            f"{base_url}/api/v1/active-tasks",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
            },
            method="OPTIONS",
        )
        with urlopen(request) as response:
            assert response.status == 204
            assert (
                response.headers["Access-Control-Allow-Origin"]
                == "http://tauri.localhost"
            )
            assert response.headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"
            assert (
                response.headers["Access-Control-Allow-Headers"]
                == "Accept, Content-Type"
            )

        request = Request(
            f"{base_url}/api/v1/active-tasks",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "GET",
            },
            method="OPTIONS",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 403

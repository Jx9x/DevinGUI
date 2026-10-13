from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest

from server.chatgpt_adapter import LocalAPIError
from server.chatgpt_routes import get_chatgpt_client
from server.main import app

SESSION = "2026-09-10-abcd"
INPUT = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def backend():
    fake = MagicMock()
    fake.status.return_value = {"apiVersion": 1, "build": "devin-gui.1", "connectionState": "connected"}
    fake.call.return_value = {"receipt": "accepted"}
    app.dependency_overrides[get_chatgpt_client] = lambda: fake
    try:
        with TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)) as client:
            yield client, fake
    finally:
        app.dependency_overrides.pop(get_chatgpt_client, None)


@pytest.mark.parametrize("method,url,body,operation,payload", [
    ("GET", "/sessions?limit=2&cursorId="+SESSION+"&cursorUpdatedAt=9", None, "sessions.list", {"limit":2,"cursor":{"id":SESSION,"updatedAt":9}}),
    ("GET", f"/sessions/{SESSION}/events?from=3", None, "sessions.events", {"id":SESSION,"limit":80,"from":3}),
    ("GET", f"/sessions/{SESSION}/events?before=7&limit=4", None, "sessions.events", {"id":SESSION,"limit":4,"before":7}),
    ("GET", f"/sessions/{SESSION}/controls", None, "sessions.controls", {"id":SESSION}),
    ("GET", "/models", None, "models.get", None),
    ("POST", "/models/refresh", {}, "models.refresh", None),
    ("GET", "/outbox", None, "sessions.outbox", None),
    ("POST", f"/outbox/{INPUT}/cancel", {}, "sessions.cancelInput", {"id":INPUT}),
    ("POST", f"/sessions/{SESSION}/stop", {"expectedTurnId":"turn-1"}, "sessions.stop", {"id":SESSION,"expectedTurnId":"turn-1"}),
    ("POST", f"/sessions/{SESSION}/open", {}, "sessions.open", {"id":SESSION}),
])
def test_routes_preserve_adapter_contract(backend, method, url, body, operation, payload):
    client, fake = backend
    response = client.request(method, "/api/chatgpt"+url, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    fake.call.assert_called_once_with(operation, payload)


def test_send_preserves_input_uuid_and_reports_ambiguous_delivery(backend):
    client, fake = backend
    payload = {"id":INPUT,"sessionId":SESSION,"text":"hello","model":"observed-model","reasoningEffort":"high"}
    fake.call.side_effect = LocalAPIError("Connection interrupted", uncertain=True)
    response = client.post("/api/chatgpt/send", json=payload)
    assert response.status_code == 503
    assert response.json()["deliveryUncertain"] is True
    fake.call.assert_called_once_with("sessions.send", {**payload,"mode":"auto","dueAt":0,"automation":"off"})


@pytest.mark.parametrize("url,body", [("/send", {"id":INPUT,"sessionId":None,"text":"hello","model":"model","automation":"goal"}),
    ("/send", {"sessionId":None,"text":"hello","model":"model"}),
    ("/send", {"id":INPUT,"sessionId":None,"text":" ","model":"model"}),
    (f"/sessions/{SESSION}/stop", {}), ("/outbox/not-a-uuid/cancel", {})])
def test_invalid_mutation_never_reaches_adapter(backend, url, body):
    client, fake = backend
    assert client.post("/api/chatgpt"+url, json=body).status_code == 422
    fake.call.assert_not_called()


def test_invalid_pagination_never_reaches_adapter(backend):
    client, fake = backend
    for url in ("/sessions?limit=100", "/sessions?cursorUpdatedAt=1", f"/sessions/{SESSION}/events?from=1&before=2", "/sessions/bad!/controls"):
        assert client.get("/api/chatgpt"+url).status_code == 422
    fake.call.assert_not_called()


@pytest.mark.parametrize("headers", [{"Origin":"https://evil.test"}, {"Origin":"null"}, {"Host":"evil.test"},
    {"Origin":"http://localhost:9999"}, {"Sec-Fetch-Site":"cross-site"}, {"Host":"localhost@evil.test"}])
def test_browser_boundary_blocks_reads_and_mutations(backend, headers):
    client, fake = backend
    assert client.get("/api/chatgpt/status", headers=headers).status_code == 403
    assert client.post("/api/chatgpt/models/refresh", headers=headers, json={}).status_code == 403
    fake.status.assert_not_called()
    fake.call.assert_not_called()


@pytest.mark.parametrize("origin", ["http://localhost:8000", "http://localhost:5173", "http://127.0.0.1:5174"])
def test_local_production_and_vite_origins_work(backend, origin):
    client, _ = backend
    assert client.get("/api/chatgpt/status", headers={"Origin":origin}).status_code == 200


def test_offline_status_does_not_claim_live_connection(backend):
    client, fake = backend
    fake.status.side_effect = LocalAPIError("Start the companion.")
    body = client.get("/api/chatgpt/status").json()
    assert body["available"] is False
    assert body["error"] == "adapter_unavailable"
    assert "connectionState" not in body


def test_request_boundary_rejects_forms_and_oversized_chunked_json(backend):
    client, fake = backend
    assert client.post("/api/chatgpt/send", data={"text":"hello"}).status_code == 415
    response = client.post("/api/chatgpt/send", content=iter([b"x"*65536]*3), headers={"Content-Type":"application/json"})
    assert response.status_code == 413
    fake.call.assert_not_called()


def test_waiting_adapter_does_not_block_devin_health(backend):
    client, fake = backend
    started, release = threading.Event(), threading.Event()
    def wait(*args):
        started.set()
        assert release.wait(5)
        return {}
    fake.call.side_effect = wait
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, "/api/chatgpt/models")
        try:
            assert started.wait(3)
            assert client.get("/api/health").status_code == 200
        finally:
            release.set()
        assert pending.result().status_code == 200

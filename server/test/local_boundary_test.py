"""The shared loopback boundary must gate every route before any service runs.

All requests go through the real app so the tests cover the actual middleware
wiring, not a test-only copy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.main import app


def client(**kwargs):
    kwargs.setdefault("base_url", "http://localhost:8000")
    kwargs.setdefault("client", ("127.0.0.1", 50000))
    return TestClient(app, **kwargs)


async def raw_http(headers, path="/api/health", client=("127.0.0.1", 50000)):
    """Drive the real app with exact raw header bytes, bypassing client checks."""
    messages = []
    received = []
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode("latin-1"), "query_string": b"", "root_path": "",
        "headers": headers, "client": client, "server": ("localhost", 8000),
    }

    async def receive():
        received.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages, received


async def raw_ws(headers, path="/ws", scheme="ws", client=("127.0.0.1", 50000)):
    """Open a websocket scope with exact raw headers; returns sent messages."""
    messages = []
    received = []
    scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "scheme": scheme, "path": path, "raw_path": path.encode("latin-1"),
        "query_string": b"", "root_path": "", "headers": headers,
        "client": client, "server": ("localhost", 8000),
        "subprotocols": [],
    }

    async def receive():
        received.append(True)
        if len(received) == 1:
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages, received


def test_health_allows_native_clients_without_origin():
    response = client().get("/api/health")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("origin", [
    "http://localhost:8000",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://[::1]:5173",
    "http://[::1]:5174",
])
def test_loopback_and_vite_origins_get_exact_acao(origin):
    response = client().get("/api/health", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("host,origin", [
    ("localhost:9000", "http://localhost:9000"),
    ("127.0.0.1:3000", "http://127.0.0.1:3000"),
    ("[::1]:8000", "http://[::1]:8000"),
    ("[::1]", "http://[::1]"),
    ("LOCALHOST:8000", "http://localhost:8000"),
])
def test_same_origin_on_other_loopback_ports_is_allowed(host, origin):
    response = client().get("/api/health", headers={"Host": host, "Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.test"},
    {"Origin": "null"},
    {"Origin": "http://localhost.evil.test"},
    {"Origin": "http://localhost@evil.test"},
    {"Origin": "http://" "user:pass@localhost:8000"},
    {"Origin": "http://localhost:8000.evil.test"},
    {"Origin": "http://localhost:9999"},
    {"Origin": "http://localhost:0"},
    {"Origin": "http://localhost:65536"},
    {"Origin": "http://localhost:"},
    {"Origin": "ftp://localhost:8000"},
    {"Origin": "http://localhost:8000/"},
    {"Origin": "http://localhost:8000/path"},
    {"Origin": "http://localhost:8000?"},
    {"Origin": "http://localhost:8000#"},
    {"Origin": "https://localhost:8000"},
    {"Origin": "http://[localhost]:8000"},
    {"Origin": "http://[127.0.0.1]:8000"},
    {"Origin": "http://localhost:" + "9" * 6},
    {"Host": "[localhost]:8000"},
    {"Host": "[127.0.0.1]:8000"},
    {"Host": "localhost:" + "9" * 5000},
    {"Host": "evil.test"},
    {"Host": "localhost.evil.test"},
    {"Host": "localhost@evil.test"},
    {"Host": "user:pass@localhost:8000"},
    {"Host": "localhost:0"},
    {"Host": "localhost:65536"},
    {"Host": "localhost:99999"},
    {"Host": "localhost:"},
    {"Host": "::1"},
    {"Host": "[::1"},
    {"Host": "[::2]:8000"},
    {"Host": "localhost :8000"},
    {"Host": "localhost:8000/path"},
    {"Host": "testserver"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "Cross-Site"},
    {"Origin": "http://localhost:8000", "Sec-Fetch-Site": "CROSS-SITE"},
])
def test_unsafe_requests_never_reach_services(headers):
    fake_db = MagicMock()
    fake_archive = MagicMock()
    with (
        patch("server.main.db_reader", fake_db),
        patch("server.main.archive_manager", fake_archive),
    ):
        response = client().get("/api/sessions", headers=headers)
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers
        mutation = client().post("/api/sessions/s-1/archive", headers=headers)
        assert mutation.status_code == 403
    fake_db.list_sessions.assert_not_called()
    fake_db.get_session.assert_not_called()
    fake_archive.archive_session.assert_not_called()


@pytest.mark.parametrize("headers", [
    [("Host", "localhost:8000"), ("Host", "localhost:8000")],
    [("Host", "localhost:8000"), ("Host", "evil.test")],
    [("Origin", "http://localhost:8000"), ("Origin", "http://localhost:8000")],
    [("Origin", "http://localhost:8000"), ("Origin", "https://evil.test")],
    [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")],
])
def test_duplicate_boundary_headers_are_rejected(headers):
    response = client().get("/api/health", headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("fetch_site", ["same-origin", "same-site", "none"])
def test_non_cross_site_fetch_metadata_is_allowed(fetch_site):
    headers = {"Origin": "http://localhost:8000", "Sec-Fetch-Site": fetch_site}
    response = client().get("/api/health", headers=headers)
    assert response.status_code == 200


def test_preflight_from_allowed_dev_origin_gets_exact_acao():
    response = client().options(
        "/api/sessions",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.parametrize("origin", [
    "https://evil.test",
    "null",
    "http://localhost:9999",
    "http://localhost.evil.test",
])
def test_preflight_from_unsafe_origin_is_denied_before_cors(origin):
    response = client().options(
        "/api/sessions",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


async def test_request_without_host_header_is_rejected():
    messages, _ = await raw_http([])
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 403


@pytest.mark.parametrize("headers", [
    [(b"host", b"localhost:8000"), (b"origin", b"http://local\thost:8000")],
    [(b"host", b"localhost:8000"), (b"origin", b"http://local\nhost:8000")],
    [(b"host", b"localhost:8000"), (b"origin", b"\x01http://localhost:8000")],
    [(b"host", b"localhost:8000"), (b"origin", b"http://[localhost]:8000")],
    [(b"host", b"[localhost]:8000")],
    [(b"host", b"[127.0.0.1]:8000")],
    [(b"host", b"localhost:8000 "), (b"origin", b"http://localhost:8000")],
    [(b"host", b"localhost:8000"), (b"origin", b" http://localhost:8000")],
])
async def test_malformed_raw_headers_never_reach_services(headers):
    fake_db = MagicMock()
    with patch("server.main.db_reader", fake_db):
        messages, _ = await raw_http(headers)
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 403
    assert fake_db.mock_calls == []


@pytest.mark.parametrize("host,origin", [
    ("localhost:80", "http://localhost"),
    ("localhost", "http://localhost:80"),
])
def test_default_http_ports_normalize_to_same_origin(host, origin):
    response = client().get("/api/health", headers={"Host": host, "Origin": origin})
    assert response.status_code == 200


@pytest.mark.parametrize("host,origin", [
    ("localhost:443", "https://localhost"),
    ("localhost", "https://localhost:443"),
])
def test_default_https_ports_normalize_to_same_origin(host, origin):
    response = client(base_url="https://localhost:8000").get(
        "/api/health", headers={"Host": host, "Origin": origin}
    )
    assert response.status_code == 200


def _patched_session_manager():
    manager = MagicMock()
    manager.attach_socket.return_value = MagicMock(name="managed-session")
    manager.detach_socket = AsyncMock()
    return manager


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.test"},
    {"Origin": "null"},
    {"Origin": "http://localhost:9999"},
    {"Origin": "http://localhost.evil.test"},
    {"Origin": "https://localhost:8000"},
    {"Host": "evil.test"},
    {"Host": "localhost@evil.test"},
    {"Host": "localhost:0"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "Cross-Site"},
])
def test_websocket_rejected_before_accept_and_session_attach(headers):
    manager = _patched_session_manager()
    with patch("server.main.session_manager", manager):
        with client() as test_client:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with test_client.websocket_connect("ws://localhost:8000/ws", headers=headers):
                    pass
    assert excinfo.value.code == 1008
    manager.attach_socket.assert_not_called()


@pytest.mark.parametrize("url,headers", [
    ("ws://localhost:8000/ws", {}),
    ("ws://localhost:8000/ws", {"Origin": "http://localhost:8000"}),
    ("ws://localhost:8000/ws", {"Origin": "http://localhost:5173"}),
    ("ws://localhost:8000/ws", {"Origin": "http://127.0.0.1:5174"}),
    ("ws://localhost:8000/ws", {"Origin": "http://[::1]:5173"}),
    ("ws://127.0.0.1:9000/ws", {"Origin": "http://127.0.0.1:9000"}),
    ("ws://localhost:8000/ws", {"Host": "[::1]:8000", "Origin": "http://[::1]:8000"}),
    ("wss://localhost:8000/ws", {"Origin": "https://localhost:8000"}),
])
def test_local_websocket_handshakes_attach_a_session(url, headers):
    manager = _patched_session_manager()
    with patch("server.main.session_manager", manager):
        with client() as test_client:
            with test_client.websocket_connect(url, headers=headers):
                pass
    manager.attach_socket.assert_called_once()
    manager.detach_socket.assert_awaited_once()


@pytest.mark.parametrize("headers", [
    [(b"host", b"localhost:8000"), (b"origin", b"http://local\thost:8000")],
    [(b"host", b"localhost:8000"), (b"origin", b"http://local\nhost:8000")],
    [(b"host", b"localhost:8000"), (b"origin", b"http://[localhost]:8000")],
    [(b"host", b"[localhost]:8000")],
    [(b"host", b"[127.0.0.1]:8000")],
])
async def test_raw_websocket_handshakes_close_before_accept(headers):
    manager = _patched_session_manager()
    with patch("server.main.session_manager", manager):
        messages, _ = await raw_ws(headers)
    assert messages[0]["type"] == "websocket.close"
    assert messages[0]["code"] == 1008
    manager.attach_socket.assert_not_called()


_FORGED_LOCAL_HEADERS = [
    (b"host", b"localhost:8000"),
    (b"origin", b"http://localhost:8000"),
    (b"x-forwarded-for", b"127.0.0.1"),
]


@pytest.mark.parametrize("peer", [
    ("203.0.113.10", 50000),
    ("192.168.1.20", 50000),
    ("2001:db8::10", 50000),
    ("::ffff:203.0.113.10", 50000),
])
async def test_remote_peers_rejected_despite_forged_headers(peer):
    fake_db = MagicMock()
    fake_archive = MagicMock()
    with (
        patch("server.main.db_reader", fake_db),
        patch("server.main.archive_manager", fake_archive),
    ):
        messages, received = await raw_http(_FORGED_LOCAL_HEADERS, client=peer)
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 403
    assert received == []
    assert fake_db.mock_calls == []
    assert fake_archive.mock_calls == []


@pytest.mark.parametrize("peer", [
    None,
    ("testclient", 50000),
    ("localhost", 50000),
    ("not-an-ip", 50000),
    ("127.0.0.1",),
    ("127.0.0.1", "50000"),
    ("127.0.0.1", 50000, "extra"),
    "127.0.0.1",
])
async def test_missing_or_malformed_peers_fail_closed(peer):
    messages, received = await raw_http(_FORGED_LOCAL_HEADERS, client=peer)
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 403
    assert received == []


@pytest.mark.parametrize("peer", [
    ("203.0.113.10", 50000),
    ("2001:db8::10", 50000),
    ("::ffff:203.0.113.10", 50000),
    None,
    ("testclient", 50000),
])
async def test_websocket_remote_peers_close_before_accept_and_receive(peer):
    manager = _patched_session_manager()
    with patch("server.main.session_manager", manager):
        messages, received = await raw_ws(_FORGED_LOCAL_HEADERS, client=peer)
    assert [m["type"] for m in messages] == ["websocket.close"]
    assert messages[0]["code"] == 1008
    assert received == []
    manager.attach_socket.assert_not_called()


@pytest.mark.parametrize("peer", [
    ("127.0.0.1", 50000),
    ("::1", 50000),
    ("::ffff:127.0.0.1", 50000),
])
async def test_loopback_peers_reach_http_routes(peer):
    messages, _ = await raw_http([(b"host", b"localhost:8000")], client=peer)
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 200


@pytest.mark.parametrize("peer", [
    ("127.0.0.1", 50000),
    ("::1", 50000),
    ("::ffff:127.0.0.1", 50000),
])
async def test_loopback_websocket_peers_are_accepted(peer):
    manager = _patched_session_manager()
    with patch("server.main.session_manager", manager):
        messages, received = await raw_ws([(b"host", b"localhost:8000")], client=peer)
    assert messages[0]["type"] == "websocket.accept"
    manager.attach_socket.assert_called_once()
    manager.detach_socket.assert_awaited_once()

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from server.chatgpt_adapter import ChatOnSteroidsClient, LocalAPIError


@contextmanager
def adapter(tmp_path, token="a" * 64):
    calls = []
    reply = {"status": 200, "data": {"ok": True, "data": {"sessions": [], "nextCursor": None}}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.path, self.headers.get("Authorization"), json.loads(raw) if raw else None))
            if reply.get("disconnect"):
                self.close_connection = True
                return
            self.send_response(reply["status"])
            self.end_headers()
            self.wfile.write(reply.get("raw", json.dumps(reply["data"]).encode()))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    directory = tmp_path / "devin-gui-api"
    directory.mkdir(exist_ok=True)
    (directory / "connection.json").write_text(json.dumps({"apiVersion": 1, "baseUrl": f"http://127.0.0.1:{server.server_port}", "token": token}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield calls, reply
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_http_authentication_cursor_and_discovery_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    client = ChatOnSteroidsClient(tmp_path, timeout=2)
    payload = {"limit": 3, "cursor": {"updatedAt": 9, "id": "2026-09-10-abcd"}}
    with adapter(tmp_path) as (calls, _):
        assert client.call("sessions.list", payload)["sessions"] == []
        assert calls == [("/v1/sessions.list", "Bearer " + "a" * 64, payload)]
    with adapter(tmp_path, "b" * 64) as (calls, _):
        client.call("models.get")
        assert calls[0][1] == "Bearer " + "b" * 64


@pytest.mark.parametrize("descriptor", [[], None, {"apiVersion": 1, "baseUrl": "http://evil.test:80", "token": "a" * 64},
    {"apiVersion": True, "baseUrl": "http://127.0.0.1:80", "token": "a" * 64},
    {"apiVersion": 1, "baseUrl": "http://127.0.0.1:0", "token": "a" * 64},
    {"apiVersion": 1, "baseUrl": "http://127.0.0.1:80", "token": []}])
def test_invalid_discovery_is_rejected_without_network(tmp_path, descriptor):
    folder = tmp_path / "devin-gui-api"
    folder.mkdir()
    (folder / "connection.json").write_text(json.dumps(descriptor))
    with pytest.raises(LocalAPIError) as error:
        ChatOnSteroidsClient(tmp_path).status()
    assert error.value.code == "invalid_discovery"
    assert "evil.test" not in str(error.value)


def test_missing_profile_and_missing_discovery_do_not_break_server_import(tmp_path, monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("DEVINGUI_CHATGPT_PROFILE", raising=False)
    with pytest.raises(LocalAPIError):
        ChatOnSteroidsClient().status()
    monkeypatch.setenv("DEVINGUI_CHATGPT_PROFILE", str(tmp_path))
    assert ChatOnSteroidsClient().profile == tmp_path
    with pytest.raises(LocalAPIError):
        ChatOnSteroidsClient().status()


def test_lost_send_response_is_uncertain_and_not_retried(tmp_path):
    with adapter(tmp_path) as (calls, reply):
        reply["disconnect"] = True
        with pytest.raises(LocalAPIError) as error:
            ChatOnSteroidsClient(tmp_path, timeout=2).call("sessions.send", {"id": "fixed-input-id", "text": "hello"})
        assert error.value.uncertain
        assert len(calls) == 1
        assert "hello" not in str(error.value)


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"ok":true}', b'{"ok":false,"error":"secret"}'])
def test_malformed_response_is_safe_and_not_retried(tmp_path, raw):
    with adapter(tmp_path) as (calls, reply):
        reply["raw"] = raw
        with pytest.raises(LocalAPIError) as error:
            ChatOnSteroidsClient(tmp_path).call("sessions.send", {})
        assert error.value.code == "invalid_response"
        assert error.value.uncertain
        assert len(calls) == 1
        assert "secret" not in str(error.value)


def test_auth_failure_and_redirect_never_forward_upstream_secret(tmp_path):
    with adapter(tmp_path) as (calls, reply):
        for status in (401, 302, 409):
            reply.update(status=status, data={"error": "secret-token-and-local-path"})
            with pytest.raises(LocalAPIError) as error:
                ChatOnSteroidsClient(tmp_path).call("models.get")
            assert "secret" not in str(error.value)
        assert len(calls) == 3


def test_status_projects_only_public_fields(tmp_path):
    with adapter(tmp_path) as (_, reply):
        reply["data"] = {"ok": True, "data": {"apiVersion": 1, "build": "devin-gui.1", "token": "secret",
            "connectionState": "connected", "bridge": {"present": True, "token": "secret", "port": 123}}}
        status = ChatOnSteroidsClient(tmp_path).status()
        assert status["bridge"]["present"] is True
        assert "secret" not in json.dumps(status)
        assert "port" not in status["bridge"]


def test_request_and_response_limits_and_operation_allowlist(tmp_path, monkeypatch):
    client = ChatOnSteroidsClient(tmp_path)
    with pytest.raises(LocalAPIError, match="Unsupported"):
        client.call("settings.set", {})
    with pytest.raises(LocalAPIError) as error:
        client.call("sessions.send", {"text": "x" * (128 * 1024)})
    assert error.value.status_code == 413
    with adapter(tmp_path):
        monkeypatch.setattr("server.chatgpt_adapter.MAX_RESPONSE", 16)
        with pytest.raises(LocalAPIError) as error:
            client.call("sessions.list")
        assert error.value.code == "response_too_large"

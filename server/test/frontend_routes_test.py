"""The SPA handler must serve the dist tree and nothing outside it."""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.frontend_routes import create_frontend_router

OUTSIDE = b"OUTSIDE-SECRET"
SIBLING = b"SIBLING-SECRET"


@pytest.fixture
def site(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"icon-bytes")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "sentinel.txt").write_bytes(OUTSIDE)
    sibling = tmp_path / "dist-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_bytes(SIBLING)
    app = FastAPI()
    app.include_router(create_frontend_router(dist))
    return app


async def raw_get(app, raw_path):
    """Drive the ASGI app directly so traversal bytes are never normalized away."""
    messages = []
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": unquote(raw_path),
        "raw_path": raw_path.encode("latin-1"), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 50000), "server": ("localhost", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


def test_serves_index_nested_assets_and_root_files(site):
    client = TestClient(site)
    assert client.get("/").text == "<html>spa</html>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/favicon.ico").content == b"icon-bytes"


def test_client_routes_fall_back_to_index(site):
    client = TestClient(site)
    response = client.get("/sessions/some/client/route")
    assert response.status_code == 200
    assert response.text == "<html>spa</html>"


@pytest.mark.parametrize("path", ["/api/sessions", "/api", "/ws", "/api/health"])
def test_api_and_ws_paths_are_404_not_spa_fallback(site, path):
    assert TestClient(site).get(path).status_code == 404


@pytest.mark.parametrize("path", [
    "/../sentinel.txt",
    "/../../sentinel.txt",
    "/assets/../sentinel.txt",
    "/..",
    "/../dist-evil/secret.txt",
    "/..\\sentinel.txt",
    "/assets\\..\\sentinel.txt",
    "/C:/Windows/win.ini",
    "/C:\\Windows\\win.ini",
    "/C:Windows\\win.ini",
    "//server/share/sentinel.txt",
    "/\\\\server\\share\\sentinel.txt",
    "/\\\\.\\C$\\sentinel.txt",
    "/\x00",
    "/assets/\x00app.js",
])
async def test_unsafe_paths_never_serve_outside_bytes(site, path):
    status, body = await raw_get(site, path)
    assert status == 404
    assert OUTSIDE not in body
    assert SIBLING not in body
    assert b"icon-bytes" not in body


@pytest.mark.parametrize("raw_path", [
    "/%2e%2e/sentinel.txt",
    "/..%2Fsentinel.txt",
    "/%2e%2e%2Fsentinel.txt",
    "/%2E%2E%5Csentinel.txt",
    "/%2e%2e/%2e%2e/sentinel.txt",
    "/C%3A%5CWindows%5Cwin.ini",
    "/%5C%5Cserver%5Cshare%5Csentinel.txt",
    "/%2f%2fserver%2fshare",
    "/%00",
])
async def test_percent_encoded_traversal_cannot_escape(site, raw_path):
    status, body = await raw_get(site, raw_path)
    assert status == 404
    assert OUTSIDE not in body
    assert SIBLING not in body


def test_file_symlink_escape_is_rejected(site, tmp_path):
    link = tmp_path / "dist" / "link.txt"
    try:
        link.symlink_to(tmp_path / "sentinel.txt")
    except OSError as exc:
        pytest.skip(f"symlinks require elevated privileges: {exc}")
    client = TestClient(site)
    response = client.get("/link.txt")
    assert response.status_code == 404
    assert OUTSIDE not in response.content


def test_directory_symlink_escape_is_rejected(site, tmp_path):
    link = tmp_path / "dist" / "linkdir"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks require elevated privileges: {exc}")
    client = TestClient(site)
    response = client.get("/linkdir/sentinel.txt")
    assert response.status_code == 404
    assert OUTSIDE not in response.content


def test_index_symlink_escape_returns_404_not_outside_index(site, tmp_path):
    index = tmp_path / "dist" / "index.html"
    index.unlink()
    try:
        index.symlink_to(tmp_path / "sentinel.txt")
    except OSError as exc:
        pytest.skip(f"symlinks require elevated privileges: {exc}")
    response = TestClient(site).get("/some/client/route")
    assert response.status_code == 404
    assert OUTSIDE not in response.content

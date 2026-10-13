import base64
import json
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.db_reader import DBReader
from server.test.db_reader_test import _make_db
from server.main import app, normalize_session_update


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
ENCODED = base64.b64encode(PNG).decode()


@pytest.fixture
def image_db(tmp_path):
    path = tmp_path / "sessions.db"
    _make_db(path)
    conn = sqlite3.connect(path)
    for row_id, role in [(4, "user"), (5, "tool")]:
        conn.execute("INSERT INTO message_nodes VALUES (?, ?, ?, ?, ?, ?, ?)", (
            row_id, "session-1", str(row_id), None,
            json.dumps({"role": role, "content": "", "images": [
                None,
                {"mime_type": "image/png", "base64_data": ENCODED, "width": 1, "height": 1},
                {"mime_type": "image/png", "base64_data": "invalid!", "width": "bad", "height": True},
                {"mime_type": "text/html", "base64_data": ENCODED},
            ]}), "2026-09-21T10:00:00Z", "{}",
        ))
    conn.commit()
    conn.close()
    return DBReader(str(path))


def test_history_includes_lightweight_images_with_original_indices(image_db):
    messages = image_db.get_messages("session-1")
    for message in messages[-2:]:
        assert message["images"] == [
            {"index": 1, "mime_type": "image/png", "width": 1, "height": 1},
            {"index": 2, "mime_type": "image/png"},
        ]
    assert "base64_data" not in json.dumps(messages)
    assert ENCODED not in json.dumps(messages)


def test_image_route_returns_bytes_and_conditional_cache_response(image_db):
    with patch("server.main.db_reader", image_db):
        client = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))
        response = client.get("/api/message-nodes/5/images/1")
        assert response.status_code == 200
        assert response.content == PNG
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        cached = client.get("/api/message-nodes/5/images/1", headers={"If-None-Match": response.headers["etag"]})
        assert cached.status_code == 304
        assert cached.content == b""


@pytest.mark.parametrize("row_id,index", [(999, 0), (5, -1), (5, 99), (5, 0), (5, 2), (5, 3)])
def test_unavailable_or_invalid_images_are_404(image_db, row_id, index):
    with patch("server.main.db_reader", image_db):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get(f"/api/message-nodes/{row_id}/images/{index}")
        assert response.status_code == 404


@pytest.mark.parametrize("kind", ["tool_call", "tool_call_update"])
def test_live_tool_images_survive_normalization_with_text(kind):
    image = {"type": "image", "mimeType": "image/png", "data": ENCODED}
    update = normalize_session_update({"sessionUpdate": kind, "content": [
        {"type": "content", "content": {"type": "text", "text": "Screenshot"}},
        {"type": "content", "content": image}, image,
        {"type": "image", "mimeType": "text/html", "data": ENCODED},
        {"type": "image", "mimeType": "image/png", "data": 42},
    ]})
    assert update["content"] == [{"text": "Screenshot"}, image, image]

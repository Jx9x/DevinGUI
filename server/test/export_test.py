"""The export preview must describe the same Markdown that file export saves."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from scripts import export_conversation_to_md as exporter
from server.main import app


@pytest.fixture
def export_db(tmp_path):
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE sessions (
                id TEXT, title TEXT, working_directory TEXT, model TEXT,
                created_at REAL, last_activity_at REAL, hidden INTEGER
            )"""
        )
        conn.execute(
            "CREATE TABLE message_nodes (row_id INTEGER, session_id TEXT, chat_message TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s-1", "Sample", None, "model", 1, 2, 0),
        )
        conn.execute(
            "INSERT INTO message_nodes VALUES (?, ?, ?)",
            (1, "s-1", json.dumps({"role": "user", "content": "Short DB text"})),
        )
    return db_path


@pytest.mark.parametrize("database_form", ["default", "absolute", "parent", "relative"])
def test_relocated_database_export_keeps_original_transcripts(
    tmp_path, export_db, monkeypatch, capsys, database_form,
):
    sessions_dir = tmp_path / "relocated sessions"
    database = sessions_dir / "cli" / "sessions.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(export_db.read_bytes())
    monkeypatch.setenv("DEVIN_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    transcript_dir = tmp_path / "roaming" / "devin" / "cli" / "transcripts"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "s-1.json").write_text(json.dumps({"steps": [
        {"source": "user", "message": "Original transcript prompt"},
        {"source": "agent", "message": "Original transcript answer"},
    ]}), encoding="utf-8")
    before = database.read_bytes()
    arguments = [exporter.__file__, "s-1", "--exact", "--preview-json"]
    if database_form == "absolute":
        arguments.extend(["--db", str(database)])
    elif database_form == "parent":
        arguments.extend(["--db", str(database.parent / ".." / "cli" / database.name)])
    elif database_form == "relative":
        monkeypatch.chdir(tmp_path)
        arguments.extend(["--db", str(Path("relocated sessions") / "cli" / ".." / "cli" / database.name)])
    monkeypatch.setattr(sys, "argv", arguments)

    assert exporter.main() == 0

    result = json.loads(capsys.readouterr().out)
    assert "Original transcript answer" in result["markdown"]
    assert "Short DB text" not in result["markdown"]
    assert database.read_bytes() == before


def test_export_script_runs_without_an_installed_package(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", "-S", exporter.__file__, "--help"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert "--db" in result.stdout


def test_explicit_database_export_keeps_adjacent_transcripts(tmp_path, export_db, monkeypatch):
    monkeypatch.setenv("DEVIN_SESSIONS_DIR", str(tmp_path / "different sessions"))
    transcript_dir = export_db.parent / "transcripts"
    transcript_dir.mkdir()
    (transcript_dir / "s-1.json").write_text(json.dumps({"steps": [
        {"source": "agent", "message": "Adjacent transcript answer"},
    ]}), encoding="utf-8")

    messages = exporter._load_messages(str(export_db), "s-1", prefer_transcript=True)

    assert messages[0]["content"] == "Adjacent transcript answer"


def test_export_preview_counts_selected_markdown_and_save_matches(tmp_path, export_db):
    db_path = export_db
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    (transcript_dir / "s-1.json").write_text(
        json.dumps({"steps": [
            {"source": "user", "message": "How does this work? 🔎"},
            {"source": "agent", "message": "Here is the full answer.",
             "reasoning_content": "First, check the evidence carefully.",
             "tool_calls": [{"tool_call_id": "call-1", "function_name": "shell",
                             "arguments": {"command": "echo hello"}}],
             "observation": {"results": [
                 {"source_call_id": "call-1", "content": "hello from the tool"}
             ]}},
            {"source": "user", "message": "Conversation to summarize: prior context"},
            {"source": "agent", "message": "<summary>Condensed context</summary>"},
        ]}),
        encoding="utf-8",
    )

    options = {
        "include_reasoning": True,
        "include_tool_calls": True,
        "include_tool_outputs": True,
        "include_summaries": True,
        "db_only": False,
    }
    with patch("server.main.db_reader", MagicMock(db_path=str(db_path))):
        client = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))
        response = client.post("/api/sessions/s-1/export/preview", json=options)
        assert response.status_code == 200
        preview = response.json()
        assert not list(tmp_path.glob("*.md"))

        markdown = preview["markdown"]
        assert "🔎" in markdown
        assert "First, check the evidence carefully." in markdown
        assert "echo hello" in markdown
        assert "hello from the tool" in markdown
        assert "Condensed context" in markdown
        counts = preview["counts"]
        assert all(counts[key] > 0 for key in (
            "base", "include_reasoning", "include_tool_calls",
            "include_tool_outputs", "include_summaries",
        ))
        assert counts["total"] == sum(counts[key] for key in (
            "base", "include_reasoning", "include_tool_calls",
            "include_tool_outputs", "include_summaries",
        ))
        assert counts["db_only"] != counts["total"]

        output = tmp_path / "saved.md"
        saved = client.post("/api/sessions/s-1/export", json={**options, "output": str(output)})
        assert saved.status_code == 200
        assert output.read_bytes() == markdown.encode("utf-8")

        db_preview = client.post(
            "/api/sessions/s-1/export/preview", json={**options, "db_only": True}
        )
        assert db_preview.status_code == 200
        assert db_preview.json()["counts"]["total"] == counts["db_only"]


@pytest.fixture
def client(export_db):
    with patch("server.main.db_reader", MagicMock(db_path=str(export_db))):
        yield TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))


@pytest.mark.parametrize("overwrite", [False, True])
def test_export_cannot_replace_a_non_markdown_file(tmp_path, client, overwrite):
    output = tmp_path / "application.py"
    original = b"print('keep this source file')\n"
    output.write_bytes(original)

    response = client.post(
        "/api/sessions/s-1/export", json={"output": str(output), "overwrite": overwrite}
    )

    assert response.status_code == 400
    assert response.json()["error"] == "export_session"
    assert output.read_bytes() == original


def test_export_requires_confirmation_before_replacing_markdown(tmp_path, client):
    output = tmp_path / "existing.md"
    original = b"Keep this document.\n"
    output.write_bytes(original)
    with patch("asyncio.create_subprocess_exec", wraps=asyncio.create_subprocess_exec) as spawn:
        response = client.post("/api/sessions/s-1/export", json={"output": str(output)})

    assert response.status_code == 409
    assert response.json()["error"] == "export_session"
    assert output.read_bytes() == original
    spawn.assert_not_called()


@pytest.mark.parametrize("output", [
    "", "   ", "saved.py", "saved.txt", "saved.md.exe", "saved.md\x00",
    "\\\\server\\share\\saved.md", "//server/share/saved.md",
    "/\\server/share/saved.md", "\\/server/share/saved.md",
    "\\\\?\\C:\\saved.md", "\\\\.\\C:\\saved.md",
    "file:///tmp/saved.md", "https://exports.invalid/saved.md",
    "saved.py:export.md", "C:relative.md",
])
@pytest.mark.parametrize("overwrite", [False, True])
def test_export_rejects_unsafe_destinations_before_spawning(client, output, overwrite):
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"Wrote export", b""))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
        response = client.post(
            "/api/sessions/s-1/export", json={"output": output, "overwrite": overwrite}
        )

    assert response.status_code == 400
    assert response.json()["error"] == "export_session"
    spawn.assert_not_awaited()


@pytest.mark.parametrize("overwrite", ["true", "false", "yes", 1, 0, None])
def test_overwrite_requires_a_json_boolean(tmp_path, client, overwrite):
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"Wrote export", b""))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
        response = client.post(
            "/api/sessions/s-1/export",
            json={"output": str(tmp_path / "saved.md"), "overwrite": overwrite},
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "overwrite"]
    spawn.assert_not_awaited()


@pytest.mark.parametrize("filename", ["new.md", "new.MD"])
def test_export_creates_markdown_in_missing_local_directories(tmp_path, client, filename):
    output = tmp_path / "new directory" / filename
    response = client.post("/api/sessions/s-1/export", json={"output": str(output)})

    assert response.status_code == 200
    assert response.json()["path"] == str(output)
    assert "Short DB text" in output.read_text(encoding="utf-8")


def test_export_can_explicitly_replace_an_existing_markdown_file(tmp_path, client):
    output = tmp_path / "existing.md"
    output.write_text("Previous document", encoding="utf-8")
    response = client.post(
        "/api/sessions/s-1/export", json={"output": str(output), "overwrite": True}
    )

    assert response.status_code == 200
    assert response.json()["path"] == str(output)
    assert "Short DB text" in output.read_text(encoding="utf-8")
    assert "Previous document" not in output.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("component", ["parent", "destination"])
@pytest.mark.parametrize("mode,attributes", [(stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)])
def test_export_rejects_links_and_reparse_points(tmp_path, client, component, mode, attributes):
    parent = tmp_path / "parent"
    parent.mkdir()
    output = parent / "saved.md"
    output.write_text("Keep this document", encoding="utf-8")
    linked = parent if component == "parent" else output
    real_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == linked:
            return MagicMock(st_mode=mode, st_file_attributes=attributes)
        return real_lstat(path, *args, **kwargs)

    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"Wrote export", b""))
    with (
        patch.object(Path, "lstat", lstat),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn,
    ):
        response = client.post(
            "/api/sessions/s-1/export", json={"output": str(output), "overwrite": True}
        )

    assert response.status_code == 400
    assert output.read_text(encoding="utf-8") == "Keep this document"
    spawn.assert_not_awaited()


def test_export_rejects_a_directory_named_markdown(tmp_path, client):
    output = tmp_path / "directory.md"
    output.mkdir()
    with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
        response = client.post(
            "/api/sessions/s-1/export", json={"output": str(output), "overwrite": True}
        )

    assert response.status_code == 400
    assert output.is_dir()
    spawn.assert_not_awaited()


@pytest.mark.skipif(os.name != "nt", reason="Windows local-drive policy")
def test_export_rejects_a_mapped_network_drive_before_filesystem_access(client):
    with (
        patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=4) as drive_type,
        patch.object(Path, "lstat") as lstat,
        patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
    ):
        response = client.post(
            "/api/sessions/s-1/export", json={"output": "Z:\\exports\\saved.md"}
        )

    assert response.status_code == 400
    drive_type.assert_called_once_with("Z:\\")
    lstat.assert_not_called()
    spawn.assert_not_awaited()


@pytest.mark.skipif(os.name != "nt", reason="Windows device-path policy")
@pytest.mark.parametrize("output", [
    "NUL.md", "CON.md", "AUX.md", "COM1.md", "LPT1.md", "COM¹.md",
    "C:\\NUL\\saved.md", "C:\\CON .txt\\saved.md", "C:\\saved.py:export.md",
])
def test_export_rejects_windows_devices_before_filesystem_access(client, output):
    with (
        patch.object(Path, "lstat") as lstat,
        patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
    ):
        response = client.post(
            "/api/sessions/s-1/export", json={"output": output, "overwrite": True}
        )

    assert response.status_code == 400
    lstat.assert_not_called()
    spawn.assert_not_awaited()


def test_export_preserves_a_file_created_after_endpoint_validation(tmp_path, client):
    output = tmp_path / "racing.md"
    create_subprocess = asyncio.create_subprocess_exec

    async def create_file_then_spawn(*args, **kwargs):
        output.write_text("Created by another writer", encoding="utf-8")
        return await create_subprocess(*args, **kwargs)

    with patch("asyncio.create_subprocess_exec", side_effect=create_file_then_spawn):
        response = client.post("/api/sessions/s-1/export", json={"output": str(output)})

    assert response.status_code == 409
    assert output.read_text(encoding="utf-8") == "Created by another writer"


@pytest.mark.parametrize("filename,exit_code", [("saved.md", 3), ("application.py", 4)])
def test_cli_rejects_existing_destinations_by_default(
    tmp_path, export_db, monkeypatch, filename, exit_code,
):
    output = tmp_path / filename
    output.write_text("Keep this file", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        exporter.__file__, "s-1", "--db", str(export_db), "--exact", "-o", str(output),
    ])

    assert exporter.main() == exit_code
    assert output.read_text(encoding="utf-8") == "Keep this file"


def test_cli_overwrite_still_requires_a_markdown_destination(tmp_path, export_db, monkeypatch):
    output = tmp_path / "application.py"
    output.write_text("Keep this source file", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        exporter.__file__, "s-1", "--db", str(export_db), "--exact",
        "-o", str(output), "--overwrite",
    ])

    assert exporter.main() == 4
    assert output.read_text(encoding="utf-8") == "Keep this source file"


def test_cli_rejects_an_explicitly_empty_destination(tmp_path, export_db, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        exporter.__file__, "s-1", "--db", str(export_db), "--exact", "-o", "",
    ])

    assert exporter.main() == 4
    assert not list(tmp_path.glob("*.md"))


def test_cli_generated_filename_does_not_overwrite(tmp_path, export_db, monkeypatch):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "Sample.md"
    output.write_text("Keep this file", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        exporter.__file__, "s-1", "--db", str(export_db), "--exact",
    ])

    assert exporter.main() == 3
    assert output.read_text(encoding="utf-8") == "Keep this file"


def test_cli_exclusively_creates_the_destination(tmp_path, export_db, monkeypatch):
    output = tmp_path / "racing.md"
    real_open = Path.open

    def open_after_competing_writer(path, mode="r", *args, **kwargs):
        if path == output and mode in ("w", "x"):
            with real_open(output, "x", encoding="utf-8") as competing_file:
                competing_file.write("Created just before open")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_after_competing_writer)
    monkeypatch.setattr(sys, "argv", [
        exporter.__file__, "s-1", "--db", str(export_db), "--exact", "-o", str(output),
    ])

    assert exporter.main() == 3
    assert output.read_text(encoding="utf-8") == "Created just before open"


def test_confirmed_overwrite_does_not_modify_a_hardlinked_source(tmp_path, client):
    source = tmp_path / "application.py"
    original = b"print('keep this source file')\n"
    source.write_bytes(original)
    output = tmp_path / "linked.md"
    try:
        os.link(source, output)
    except OSError as error:
        pytest.skip(f"Hard links unavailable: {error}")

    response = client.post(
        "/api/sessions/s-1/export", json={"output": str(output), "overwrite": True}
    )

    assert response.status_code == 200
    assert "Short DB text" in output.read_text(encoding="utf-8")
    assert source.read_bytes() == original


def test_failed_confirmed_overwrite_preserves_original_and_cleans_temp_file(
    tmp_path, export_db, monkeypatch,
):
    output = tmp_path / "existing.md"
    output.write_text("Keep this document", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        exporter.__file__, "s-1", "--db", str(export_db), "--exact",
        "-o", str(output), "--overwrite",
    ])
    with patch("scripts.export_conversation_to_md.os.replace", side_effect=PermissionError("blocked")):
        assert exporter.main() == 1

    assert output.read_text(encoding="utf-8") == "Keep this document"
    assert not list(tmp_path.glob(".*.tmp"))

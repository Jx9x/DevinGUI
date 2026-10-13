from __future__ import annotations

import json
from pathlib import Path

from server.archive_manager import ArchiveManager


class FakeDBReader:
    db_path = r"C:\Users\Example\AppData\Roaming\devin\cli\sessions.db"

    def get_session(self, session_id: str):
        if session_id != "session-1":
            return None
        return {
            "id": session_id,
            "title": "Fix the GUI",
            "working_directory": r"C:\Code\ProjectA",
            "model": "test-model",
            "created_at": "2026-07-14T10:00:00Z",
            "last_activity_at": "2026-07-14T11:00:00Z",
        }

    def get_messages(self, session_id: str):
        return [
            {
                "role": "user",
                "content": "Please fix it",
                "created_at": "2026-07-14T10:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Done",
                "created_at": "2026-07-14T10:01:00Z",
            },
        ]

    def get_rendered_commits(self, session_id: str):
        return [{"sequence_number": 1, "rendered_html": "<div>Done</div>"}]


class FakeTranscriptWatcher:
    def get_transcript(self, session_id: str):
        return {"session_id": session_id, "steps": []}


def test_archive_exports_session_and_indexes_it_without_deleting(tmp_path: Path):
    manager = ArchiveManager(FakeDBReader(), FakeTranscriptWatcher(), tmp_path)

    result = manager.archive_session("session-1")

    archive_dir = Path(result["archive_path"])
    assert archive_dir == tmp_path / "ProjectA" / "session-1"
    assert (archive_dir / "session.json").exists()
    assert (archive_dir / "messages.json").exists()
    assert (archive_dir / "rendered_commits.json").exists()
    assert (archive_dir / "transcript.json").exists()
    assert "Please fix it" in (archive_dir / "conversation.md").read_text(encoding="utf-8")
    assert manager.archived_session_ids() == {"session-1"}

    index = json.loads((tmp_path / ".archive_index.json").read_text(encoding="utf-8"))
    assert index["sessions"]["session-1"]["project_name"] == "ProjectA"

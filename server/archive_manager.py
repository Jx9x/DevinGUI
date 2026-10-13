"""Non-destructive conversation archiving for Devin GUI.

Devin's database and transcripts remain untouched. Exports use the user's
Documents folder, preserving an existing legacy archive root. Message exports
include image metadata, not image bytes. A local index hides archived sessions
from the sidebar and project counts.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db_reader import DBReader
from .paths import default_archive_root as _default_archive_root
from .transcript_watcher import TranscriptWatcher

_INVALID_WINDOWS_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_folder_name(value: str, fallback: str) -> str:
    cleaned = _INVALID_WINDOWS_NAME_CHARS.sub("_", value).strip().rstrip(".")
    return cleaned or fallback


def _json_default(value: Any) -> str:
    return str(value)


class ArchiveManager:
    """Exports and indexes archived conversations without modifying Devin DB."""

    def __init__(
        self,
        db_reader: DBReader,
        transcript_watcher: TranscriptWatcher,
        archive_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.db_reader = db_reader
        self.transcript_watcher = transcript_watcher
        self.archive_root = Path(archive_root) if archive_root else _default_archive_root()
        self.index_path = self.archive_root / ".archive_index.json"

    def _read_index(self) -> dict[str, Any]:
        try:
            with self.index_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict) and isinstance(value.get("sessions"), dict):
                return value
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"version": 1, "sessions": {}}

    def _write_json_atomic(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, default=_json_default)
                handle.write("\n")
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _write_text_atomic(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(value)
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def archived_session_ids(self) -> set[str]:
        sessions = self._read_index().get("sessions", {})
        return {str(session_id) for session_id in sessions}

    def is_archived(self, session_id: str) -> bool:
        return session_id in self.archived_session_ids()

    @staticmethod
    def _conversation_markdown(session: dict, messages: list[dict]) -> str:
        title = session.get("title") or session.get("id") or "Devin conversation"
        lines = [
            f"# {title}",
            "",
            f"- Session: `{session.get('id', '')}`",
            f"- Project: `{session.get('working_directory') or ''}`",
            f"- Model: `{session.get('model') or ''}`",
            f"- Created: `{session.get('created_at') or ''}`",
            f"- Last activity: `{session.get('last_activity_at') or ''}`",
            "",
        ]
        for message in messages:
            role = str(message.get("role") or "unknown").capitalize()
            timestamp = message.get("created_at")
            heading = f"## {role}"
            if timestamp:
                heading += f" — {timestamp}"
            lines.extend([heading, "", str(message.get("content") or ""), ""])
        return "\n".join(lines).rstrip() + "\n"

    def archive_session(self, session_id: str) -> dict[str, Any]:
        session = self.db_reader.get_session(session_id)
        if not session:
            raise KeyError(session_id)

        working_directory = session.get("working_directory") or ""
        # Accept either Windows or POSIX separators regardless of the host
        # running the tests/server.
        project_basename = (
            re.split(r"[\\/]", working_directory.rstrip("\\/"))[-1]
            if working_directory
            else "Unassigned"
        )
        project_name = _safe_folder_name(project_basename, "Unassigned")
        safe_session_id = _safe_folder_name(session_id, "session")
        archive_dir = self.archive_root / project_name / safe_session_id
        archive_dir.mkdir(parents=True, exist_ok=True)

        messages = self.db_reader.get_messages(session_id)
        rendered = self.db_reader.get_rendered_commits(session_id)
        transcript = self.transcript_watcher.get_transcript(session_id)
        archived_at = datetime.now(timezone.utc).isoformat()

        manifest = {
            "archive_version": 1,
            "archived_at": archived_at,
            "session_id": session_id,
            "project_name": project_name,
            "working_directory": working_directory,
            "source_database": self.db_reader.db_path,
            "files": {
                "session": "session.json",
                "messages": "messages.json",
                "rendered_commits": "rendered_commits.json",
                "transcript": "transcript.json" if transcript is not None else None,
                "conversation": "conversation.md",
            },
        }

        self._write_json_atomic(archive_dir / "session.json", session)
        self._write_json_atomic(archive_dir / "messages.json", messages)
        self._write_json_atomic(archive_dir / "rendered_commits.json", rendered)
        if transcript is not None:
            self._write_json_atomic(archive_dir / "transcript.json", transcript)
        self._write_text_atomic(
            archive_dir / "conversation.md", self._conversation_markdown(session, messages)
        )
        self._write_json_atomic(archive_dir / "archive_manifest.json", manifest)

        # Publish the index last so failed exports don't hide the session.
        index = self._read_index()
        sessions = index.setdefault("sessions", {})
        sessions[session_id] = {
            "archived_at": archived_at,
            "archive_path": str(archive_dir),
            "project_name": project_name,
            "working_directory": working_directory,
            "title": session.get("title"),
        }
        self._write_json_atomic(self.index_path, index)

        return {
            "ok": True,
            "session_id": session_id,
            "archive_path": str(archive_dir),
            "project_name": project_name,
        }

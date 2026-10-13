"""Session history, archiving, image delivery, and Markdown export routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import sys
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, StrictBool

from scripts.export_conversation_to_md import (
    EXPORT_DESTINATION_EXISTS, EXPORT_DESTINATION_INVALID, validate_output_path,
)

from .rest_errors import error_response, history_error_message
from .session_ids import valid_session_id

if TYPE_CHECKING:
    from .archive_manager import ArchiveManager
    from .db_reader import DBReader
    from .transcript_watcher import TranscriptWatcher


class ExportOptions(BaseModel):
    """Content selected for a Markdown export or preview."""

    include_reasoning: bool = False
    include_tool_calls: bool = False
    include_tool_outputs: bool = False
    include_summaries: bool = False
    db_only: bool = False


class ExportRequest(ExportOptions):
    """Options and destination for saving a Markdown export."""

    output: str
    overwrite: StrictBool = False


def visible_sessions(
    db_reader: DBReader, archive_manager: ArchiveManager, cwd: str | None = None,
) -> list[dict]:
    archived = archive_manager.archived_session_ids()
    return [
        session
        for session in db_reader.list_sessions(working_directory=cwd)
        if session.get("id") not in archived
    ]


def projects_from_visible_sessions(sessions: list[dict]) -> list[dict]:
    projects: dict[str, dict] = {}
    for session in sessions:
        raw_path = session.get("working_directory")
        if not raw_path:
            continue
        normalized = os.path.normpath(raw_path)
        key = os.path.normcase(normalized)
        current = projects.get(key)
        activity = session.get("last_activity_at")
        if current is None:
            projects[key] = {
                "path": normalized,
                "name": os.path.basename(normalized) or normalized,
                "session_count": 1,
                "last_activity_at": activity,
            }
        else:
            current["session_count"] += 1
            if activity and (
                current["last_activity_at"] is None
                or activity > current["last_activity_at"]
            ):
                current["last_activity_at"] = activity
    return sorted(
        projects.values(),
        key=lambda project: project.get("last_activity_at") or "",
        reverse=True,
    )


def _export_command(
    session_id: str, options: ExportOptions, db_path: str, output: str | None = None,
    *, overwrite: bool = False,
) -> list[str]:
    script = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "export_conversation_to_md.py")
    )
    cmd = [
        sys.executable,
        script,
        session_id,
        "--db",
        db_path,
        "--exact",
    ]
    if output is None:
        cmd.append("--preview-json")
    else:
        cmd.extend(("-o", output))
        if overwrite:
            cmd.append("--overwrite")
    if options.include_reasoning:
        cmd.append("--include-reasoning")
    if options.include_tool_calls:
        cmd.append("--include-tool-calls")
    if options.include_tool_outputs:
        cmd.append("--include-tool-outputs")
    if options.include_summaries:
        cmd.append("--include-summaries")
    if options.db_only:
        cmd.append("--db-only")
    return cmd


async def _run_export_command(
    cmd: list[str], session_id: str, operation: str, *, logger: logging.Logger,
) -> str | JSONResponse:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as e:
        logger.error("Export %s failed for %s: %s", operation, session_id, e)
        return error_response(500, history_error_message(e), operation)
    if proc.returncode != 0:
        status = 500
        if operation == "export_session":
            status = {EXPORT_DESTINATION_EXISTS: 409, EXPORT_DESTINATION_INVALID: 400}.get(proc.returncode, 500)
        return error_response(
            status,
            stderr.decode("utf-8", errors="replace").strip() or "Export failed",
            operation,
        )
    return stdout.decode("utf-8", errors="replace")


def create_session_router(
    *,
    get_db_reader: Callable[[], DBReader],
    get_transcript_watcher: Callable[[], TranscriptWatcher],
    get_archive_manager: Callable[[], ArchiveManager],
    reasoning_steps: Callable[[str], list[dict]],
    session_cost: Callable[[str], dict],
    logger: logging.Logger,
) -> APIRouter:
    """Resolve shared services through explicit callbacks at request time."""
    router = APIRouter()

    def history_result(operation: str, read: Callable):
        try:
            return read()
        except (OSError, sqlite3.DatabaseError) as error:
            logger.warning("History read failed for %s: %s", operation, error)
            return error_response(500, history_error_message(error), operation)

    @router.get("/api/sessions")
    async def list_sessions(cwd: str | None = None):
        try:
            return visible_sessions(get_db_reader(), get_archive_manager(), cwd)
        except Exception as e:
            logger.error("Failed to list sessions: %s", e)
            return error_response(500, history_error_message(e), "list_sessions")

    @router.get("/api/projects")
    async def list_projects():
        try:
            return projects_from_visible_sessions(
                visible_sessions(get_db_reader(), get_archive_manager())
            )
        except Exception as e:
            logger.error("Failed to list projects: %s", e)
            return error_response(500, history_error_message(e), "list_projects")

    @router.post("/api/sessions/{session_id}/archive")
    async def archive_session(session_id: Annotated[str, Depends(valid_session_id)]):
        try:
            return get_archive_manager().archive_session(session_id)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"detail": "Session not found", "error": "archive_session"},
            )
        except Exception as e:
            logger.error("Failed to archive session %s: %s", session_id, e)
            return error_response(500, history_error_message(e), "archive_session")

    @router.post("/api/sessions/{session_id}/export/preview")
    async def preview_export(
        session_id: Annotated[str, Depends(valid_session_id)], body: ExportOptions
    ):
        result = await _run_export_command(
            _export_command(session_id, body, get_db_reader().db_path),
            session_id, "preview_export", logger=logger,
        )
        if isinstance(result, JSONResponse):
            return result
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            logger.error("Invalid export preview for %s", session_id)
            return error_response(500, "Invalid export preview", "preview_export")

    @router.post("/api/sessions/{session_id}/export")
    async def export_session(
        session_id: Annotated[str, Depends(valid_session_id)], body: ExportRequest
    ):
        try:
            output = validate_output_path(body.output)
            if output.exists() and not body.overwrite:
                return error_response(
                    409, f"Destination already exists: {output}. Confirm overwrite or choose another .md filename.",
                    "export_session",
                )
        except (ValueError, OSError) as error:
            return error_response(400, str(error), "export_session")
        result = await _run_export_command(
            _export_command(
                session_id, body, get_db_reader().db_path, str(output), overwrite=body.overwrite,
            ),
            session_id, "export_session", logger=logger,
        )
        if isinstance(result, JSONResponse):
            return result
        return {
            "ok": True,
            "path": str(output),
            "message": result.strip(),
        }

    @router.get("/api/sessions/{session_id}")
    async def get_session(session_id: Annotated[str, Depends(valid_session_id)]):
        return history_result("get_session", lambda: get_db_reader().get_session(session_id))

    @router.get("/api/sessions/{session_id}/messages")
    async def get_messages(session_id: Annotated[str, Depends(valid_session_id)]):
        return history_result("get_messages", lambda: get_db_reader().get_messages(session_id))

    @router.get("/api/sessions/{session_id}/rendered")
    async def get_rendered(session_id: Annotated[str, Depends(valid_session_id)]):
        return history_result("get_rendered", lambda: get_db_reader().get_rendered_commits(session_id))

    @router.get("/api/message-nodes/{row_id}/images/{index}")
    def get_message_image(row_id: int, index: int, request: Request):
        image = get_db_reader().get_message_image(row_id, index)
        if image is None:
            raise HTTPException(status_code=404, detail="Image unavailable")
        data, mime = image
        etag = '"' + hashlib.sha256(data).hexdigest() + '"'
        headers = {
            "Cache-Control": "private, max-age=0, must-revalidate",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content=data, media_type=mime, headers=headers)

    @router.get("/api/sessions/{session_id}/transcript")
    async def get_transcript(session_id: Annotated[str, Depends(valid_session_id)]):
        return get_transcript_watcher().get_transcript(session_id)

    @router.get("/api/sessions/{session_id}/reasoning")
    async def get_reasoning(session_id: Annotated[str, Depends(valid_session_id)]):
        return reasoning_steps(session_id)

    @router.get("/api/sessions/{session_id}/metrics")
    async def get_metrics(session_id: Annotated[str, Depends(valid_session_id)]):
        return get_transcript_watcher().get_final_metrics(session_id)

    @router.get("/api/sessions/{session_id}/cost")
    async def get_cost(session_id: Annotated[str, Depends(valid_session_id)]):
        return session_cost(session_id)

    @router.get("/api/prompt-history")
    async def get_prompt_history():
        try:
            return get_db_reader().get_prompt_history()
        except Exception as e:
            logger.error("Failed to get prompt history: %s", e)
            return error_response(500, history_error_message(e), "get_prompt_history")

    return router

"""FastAPI backend for the Devin GUI.

Serves REST endpoints for session history / transcript data and a WebSocket
for live interaction (prompt submission, streaming updates, reasoning).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .acp_client import ACPClient, ACPError
# Retain the existing main.py entry points while keeping translation independent.
from .acp_updates import (
    TEXT_CHUNK_UPDATES as _TEXT_CHUNK_UPDATES,
    normalize_session_update,
    permission_detail as _permission_detail,
    select_permission_option as _select_permission_option,
)
from .archive_manager import ArchiveManager
from .browse_routes import create_browse_router
from .chatgpt_routes import ChatGPTBoundaryMiddleware, router as chatgpt_router
from .codex_usage_provider import CodexUsageProvider
from .compaction_settings import compaction_thresholds
from .db_reader import DBReader, DBWatcher
from .diagnostics import frontend_available
from .diagnostics_routes import create_diagnostics_router
from .frontend_routes import create_frontend_router
from .local_boundary import LocalBoundaryMiddleware
from .model_registry import ModelRegistry
# Request models remain importable from main.py for compatibility.
from .model_routes import ModelIdList, ModelUpdate, create_model_router
from .permission_coordinator import PermissionCoordinator
from .pricing_history import PricingHistory
from .session_history import merge_reasoning_steps, session_cost
from .session_ids import session_id_is_valid
from .session_manager import ManagedSession, cancel_task, session_manager
from .session_notifications import SessionNotifications
from .session_transitions import SessionTransitionCoordinator
from .session_routes import (
    ExportOptions, ExportRequest, create_session_router,
    projects_from_visible_sessions, visible_sessions,
)
from .transcript_watcher import TranscriptWatcher
from .turn_commands import TurnCommands
from .turn_coordinator import TurnCoordinator, TurnSettlementTiming
from .usage_provider import UsageProvider, load_api_key
from .usage_routes import create_usage_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("devin_gui")

app = FastAPI(title="Devin GUI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$",
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ChatGPTBoundaryMiddleware)
app.add_middleware(LocalBoundaryMiddleware)
app.include_router(chatgpt_router)

db_reader = DBReader()
db_watcher = DBWatcher(db_reader)
transcript_watcher = TranscriptWatcher()
archive_manager = ArchiveManager(db_reader, transcript_watcher)
model_registry = ModelRegistry(db_path=db_reader.db_path)
pricing_history = PricingHistory(model_registry.registry_dir)


def _stop_watchers_for_session(session_id: str) -> None:
    transcript_watcher.stop_watching(session_id)
    db_watcher.stop_watching(session_id)


# Retain REST/WebSocket history hooks and resolve shared services at call time.
def _session_cost(session_id: str) -> dict:
    return session_cost(
        session_id,
        db_reader=db_reader,
        transcript_watcher=transcript_watcher,
        model_registry=model_registry,
        pricing_history=pricing_history,
    )


def _combined_reasoning_steps(session_id: str) -> list[dict]:
    sources = []
    for read_steps in (transcript_watcher.get_reasoning_steps, db_reader.get_reasoning_steps):
        try:
            sources.extend(read_steps(session_id))
        except (OSError, sqlite3.DatabaseError) as error:
            logger.warning("Reasoning source unavailable for %s: %s", session_id, error)
    return merge_reasoning_steps(sources)


# Configure the session manager with the watcher-cleanup callback so
# ``cancel_orphan`` can stop watchers when a grace-period timer fires.
session_manager.on_session_cleanup = _stop_watchers_for_session

# Default time a browser-facing permission request may remain unanswered before
# the backend rejects the corresponding ACP JSON-RPC request.
_PERMISSION_TIMEOUT_SECONDS = 300

# Turn-settlement fallback. ``session/prompt`` is normally the completion
# signal, but a dropped or never-emitted response leaves ``prompt()``
# pending even though the CLI finished the turn and persisted its final
# message.  A terminal assistant row can also belong to an intermediate
# generation (notably just before conversation compaction), so require a
# longer confirmation window before sending a provisional completion.
_TURN_SETTLE_POLL_SECONDS = 1.0
_TURN_SETTLE_QUIET_SECONDS = 5.0
_TURN_SETTLE_CONFIRM_SECONDS = 30.0


# Compatibility helpers used by existing history consumers.
def _visible_sessions(cwd: str | None = None) -> list[dict]:
    return visible_sessions(db_reader, archive_manager, cwd)


def _projects_from_visible_sessions() -> list[dict]:
    return projects_from_visible_sessions(_visible_sessions())


# Cache a found windsurf_api_key for 60 seconds; retry missing keys on each
# access so sign-in changes are picked up without restarting the server.
_api_key_cache: str | None = None
_api_key_cached_at: float = 0.0
_API_KEY_CACHE_TTL = 60.0
_codex_usage_provider = CodexUsageProvider()


def _get_api_key() -> str | None:
    """Return the cached windsurf_api_key, re-reading credentials.toml if stale."""
    import time

    global _api_key_cache, _api_key_cached_at
    now = time.time()
    if _api_key_cache is not None and (now - _api_key_cached_at) < _API_KEY_CACHE_TTL:
        return _api_key_cache
    key = load_api_key()
    _api_key_cache = key
    _api_key_cached_at = now
    return key


# Resolve shared services at request time, including replaced test services.
# Include feature routers before WebSocket and static frontend registration.
app.include_router(create_session_router(
    get_db_reader=lambda: db_reader,
    get_transcript_watcher=lambda: transcript_watcher,
    get_archive_manager=lambda: archive_manager,
    reasoning_steps=lambda session_id: _combined_reasoning_steps(session_id),
    session_cost=lambda session_id: _session_cost(session_id),
    logger=logger,
))
app.include_router(create_browse_router(logger=logger))
app.include_router(create_model_router(get_registry=lambda: model_registry))
app.include_router(create_usage_router(
    get_api_key=lambda: _get_api_key(),
    create_usage_provider=lambda api_key: UsageProvider(api_key),
    get_codex_usage_provider=lambda: _codex_usage_provider,
))


# -- WebSocket for live interaction -----------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    # Start unregistered; new_session/load_history register the assigned ID.
    # resume_session may replace this holder with a session that survived
    # disconnect, so callbacks must resolve the holder's current session.
    ms_holder: list[ManagedSession] = [session_manager.attach_socket(None, ws)]

    notifications = SessionNotifications(
        current_session=lambda: ms_holder[0],
        resume_live_updates=lambda sid: turns.resume_live_updates(sid),
        normalize_update=lambda update: normalize_session_update(update),
    )

    turns = TurnCoordinator(
        current_session=lambda: ms_holder[0],
        send=notifications.send,
        db_reader=db_reader,
        db_watcher=db_watcher,
        transcript_watcher=transcript_watcher,
        reasoning_steps=lambda session_id: _combined_reasoning_steps(session_id),
        session_cost=lambda session_id: _session_cost(session_id),
        timing=lambda: TurnSettlementTiming(
            poll_seconds=_TURN_SETTLE_POLL_SECONDS,
            quiet_seconds=_TURN_SETTLE_QUIET_SECONDS,
            confirm_seconds=_TURN_SETTLE_CONFIRM_SECONDS,
        ),
        logger=logger,
    )

    permissions = PermissionCoordinator(
        current_session=lambda: ms_holder[0],
        send=notifications.send,
        timeout_seconds=lambda: _PERMISSION_TIMEOUT_SECONDS,
        logger=logger,
    )

    def _replace_session(session: ManagedSession) -> None:
        ms_holder[0] = session

    def _create_acp(cwd: str, model: str | None, permission_mode: str | None) -> ACPClient:
        return ACPClient(
            notifications.handle_notification,
            cwd=cwd,
            request_handler=permissions.handle_request,
            model=model,
            permission_mode=permission_mode,
            on_unavailable=permissions.handle_acp_unavailable,
        )

    transitions = SessionTransitionCoordinator(
        current_session=lambda: ms_holder[0],
        replace_session=_replace_session,
        websocket=ws,
        create_acp=_create_acp,
        send=notifications.send,
        get_db_reader=lambda: db_reader,
        get_db_watcher=lambda: db_watcher,
        get_transcript_watcher=lambda: transcript_watcher,
        get_session_manager=lambda: session_manager,
        get_model_registry=lambda: model_registry,
        reasoning_steps=lambda sid: _combined_reasoning_steps(sid),
        send_cost_update=turns.send_cost_update,
        logger=logger,
    )
    commands = TurnCommands(
        reply=lambda frame: ws.send_json(frame),
        get_db_reader=lambda: db_reader,
        get_model_registry=lambda: model_registry,
        compaction_thresholds=lambda cwd, model: compaction_thresholds(cwd, model),
        transitions=transitions,
        logger=logger,
    )

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")
            ms = ms_holder[0]

            if msg_type == "new_session":
                cwd = data.get("cwd") or os.getcwd()
                model = data.get("model") or model_registry.get_last_used_model()
                permission_mode = data.get("permissionMode") if data.get("permissionMode") == "bypass" else None
                request_id = data.get("requestId")
                # Increment generation before cancelling the previous
                # transition so its late messages are suppressed.
                ms.generation_id += 1
                gen_id = ms.generation_id
                # Transitions and turns are mutually exclusive.
                await cancel_task(ms.active_transition_task, "transition", error_logger=logger)
                await cancel_task(ms.active_turn_task, "turn", error_logger=logger)
                # Run the transition as a background task so the receive
                # loop stays responsive while ACP startup runs.
                ms.active_transition_task = asyncio.create_task(
                    transitions.new_session(cwd, model, permission_mode, request_id, gen_id)
                )

            elif msg_type == "prompt":
                prepared = await commands.prepare_prompt(ms, data)
                if prepared is None:
                    continue
                # Increment generation only when actually starting a turn,
                # so a rejected prompt does not suppress a transition's
                # late messages.
                ms.generation_id += 1
                gen_id = ms.generation_id
                async with ms.transition_lock:
                    await cancel_task(ms.active_turn_task, "turn", error_logger=logger)
                    turns.start_watching(prepared.session_id)
                    model_registry.refresh()
                    pricing_history.record_prompt(prepared.session_id, model_registry.get_model(ms.model))
                    ms.active_turn_task = asyncio.create_task(
                        turns.run_prompt(
                            ms.acp, prepared.session_id, prepared.text, prepared.images, prepared.request_id, gen_id,
                        )
                    )

            elif msg_type == "cancel":
                await commands.cancel(ms, data)

            elif msg_type == "view_history":
                sid = data.get("sessionId", "")
                request_id = data.get("requestId")
                if not sid:
                    continue
                if not session_id_is_valid(sid):
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "Invalid session ID",
                            "requestId": request_id,
                        }
                    )
                    continue
                ms.generation_id += 1
                gen_id = ms.generation_id
                await cancel_task(ms.active_transition_task, "transition", error_logger=logger)
                await cancel_task(ms.active_turn_task, "turn", error_logger=logger)
                ms.active_transition_task = asyncio.create_task(
                    transitions.view_history(sid, request_id, gen_id)
                )

            elif msg_type == "load_history":
                # On success, resume ACP before sending DB history.
                # Use a background task to keep the receive loop responsive.
                sid = data.get("sessionId", "")
                request_id = data.get("requestId")
                requested_model = data.get("model")
                permission_mode = data.get("permissionMode") if data.get("permissionMode") == "bypass" else None
                if not sid:
                    continue
                if not session_id_is_valid(sid):
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "Invalid session ID",
                            "requestId": request_id,
                        }
                    )
                    continue
                ms.generation_id += 1
                gen_id = ms.generation_id
                # Transitions and turns are mutually exclusive.
                await cancel_task(ms.active_transition_task, "transition", error_logger=logger)
                await cancel_task(ms.active_turn_task, "turn", error_logger=logger)
                ms.active_transition_task = asyncio.create_task(
                    transitions.load_history(sid, requested_model, permission_mode, request_id, gen_id)
                )

            elif msg_type == "resume_session":
                # Reconnect without resending history already held by the frontend.
                # Prompts must wait for `session_resumed`; use a background task
                # so ACP startup does not block the receive loop.
                sid = data.get("sessionId", "")
                request_id = data.get("requestId")
                requested_model = data.get("model")
                permission_mode = data.get("permissionMode") if data.get("permissionMode") == "bypass" else None
                if not sid:
                    await ws.send_json(
                        {
                            "type": "resume_failed",
                            "sessionId": sid,
                            "message": "No session ID",
                            "requestId": request_id,
                        }
                    )
                    continue
                if not session_id_is_valid(sid):
                    await ws.send_json(
                        {
                            "type": "resume_failed",
                            "sessionId": sid if isinstance(sid, str) else "",
                            "message": "Invalid session ID",
                            "requestId": request_id,
                        }
                    )
                    continue
                ms.generation_id += 1
                gen_id = ms.generation_id
                await cancel_task(ms.active_transition_task, "transition", error_logger=logger)
                await cancel_task(ms.active_turn_task, "turn", error_logger=logger)
                ms.active_transition_task = asyncio.create_task(
                    transitions.resume_session(sid, requested_model, permission_mode, request_id, gen_id)
                )

            elif msg_type == "permission_response":
                await permissions.handle_response(ms, data, ws.send_json)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
    finally:
        # Registered sessions keep ACP and in-flight turns through the grace
        # window. Anonymous sessions or nonpositive grace tear down immediately;
        # otherwise the timer tears down sessions that do not reconnect.
        await session_manager.detach_socket(ms_holder[0], ws)


# -- Serve frontend ---------------------------------------------------------

_frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")


@app.get("/api/health")
async def health():
    return {"status": "ok", "db_path": db_reader.db_path}


app.include_router(create_diagnostics_router(
    get_db_reader=lambda: db_reader,
    get_transcripts_dir=lambda: transcript_watcher.transcripts_dir,
    frontend_dist=_frontend_dist,
))

# Mount static files last so it doesn't shadow API routes
if frontend_available(_frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="assets")
    app.include_router(create_frontend_router(_frontend_dist))

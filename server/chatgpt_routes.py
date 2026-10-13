"""Independent ChatGPT REST routes; blocking adapter I/O runs in FastAPI's thread pool."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers

from .chatgpt_adapter import ChatOnSteroidsClient, LocalAPIError, MAX_REQUEST


class ChatGPTBoundaryMiddleware:
    """Scope the local adapter boundary separately from legacy Devin CORS routes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api/chatgpt" or path.startswith("/api/chatgpt/")):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        origin = headers.get("origin")
        def loopback(authority):
            try:
                url = urlsplit("http://" + authority)
                return (url.hostname in {"localhost", "127.0.0.1", "::1"} and not url.username
                        and not url.password and not url.path and not url.query and not url.fragment
                        and (url.port is None or 1 <= url.port <= 65535))
            except ValueError:
                return False
        allowed_origins = {f"{scheme}://{host}" for scheme in ("http", "https")}
        allowed_origins.update(f"http://{name}:{port}" for name in ("localhost", "127.0.0.1", "[::1]") for port in (5173, 5174))
        if not loopback(host) or (origin is not None and origin not in allowed_origins) or headers.get("sec-fetch-site") == "cross-site":
            return await JSONResponse({"detail": "ChatGPT routes require a local DevinGUI origin."}, status_code=403)(scope, receive, send)
        # Require JSON so browser forms cannot trigger authenticated adapter calls.
        if scope["method"] == "POST" and headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return await JSONResponse({"detail": "Use application/json."}, status_code=415)(scope, receive, send)
        # Bound even chunked bodies before FastAPI parses them.
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_REQUEST:
                return await JSONResponse({"detail": "Chat request is too large."}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()
        async def no_cache(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
            await send(message)
        await self.app(scope, bounded_receive, no_cache)


router = APIRouter(prefix="/api/chatgpt", tags=["ChatGPT"])
SessionID = Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[0-9a-zA-Z-]+$")]


def get_chatgpt_client() -> ChatOnSteroidsClient:
    return ChatOnSteroidsClient()


Client = Annotated[ChatOnSteroidsClient, Depends(get_chatgpt_client)]


def invoke(client, operation, payload=None):
    try:
        return client.call(operation, payload)
    except LocalAPIError as error:
        return JSONResponse(status_code=error.status_code, content={"detail": str(error), "error": error.code,
                            "operation": operation, "deliveryUncertain": error.uncertain})


@router.get("/status")
def status(client: Client):
    try:
        return {"available": True, **client.status(), "error": None}
    except LocalAPIError as error:
        return {"available": False, "error": error.code, "detail": str(error)}


@router.get("/sessions")
def sessions(client: Client, limit: Annotated[int, Query(ge=1, le=60)] = 60,
             cursor_id: Annotated[SessionID | None, Query(alias="cursorId")] = None,
             cursor_time: Annotated[int | None, Query(alias="cursorUpdatedAt", ge=0, le=9007199254740991)] = None):
    if (cursor_id is None) != (cursor_time is None):
        raise HTTPException(422, "Supply both cursorId and cursorUpdatedAt.")
    payload = {"limit": limit}
    if cursor_id is not None:
        payload["cursor"] = {"id": cursor_id, "updatedAt": cursor_time}
    return invoke(client, "sessions.list", payload)


@router.get("/sessions/{session_id}/events")
def events(session_id: SessionID, client: Client, limit: Annotated[int, Query(ge=1, le=1000)] = 80,
           from_seq: Annotated[int | None, Query(alias="from", ge=0, le=10000000)] = None,
           before: Annotated[int | None, Query(ge=1, le=10000000)] = None):
    if from_seq is not None and before is not None:
        raise HTTPException(422, "Use from or before, not both.")
    payload = {"id": session_id, "limit": limit}
    if from_seq is not None:
        payload["from"] = from_seq
    if before is not None:
        payload["before"] = before
    return invoke(client, "sessions.events", payload)


@router.get("/sessions/{session_id}/controls")
def controls(session_id: SessionID, client: Client):
    return invoke(client, "sessions.controls", {"id": session_id})


@router.get("/models")
def models(client: Client):
    return invoke(client, "models.get")


@router.post("/models/refresh")
def refresh_models(client: Client):
    return invoke(client, "models.refresh")


@router.get("/outbox")
def outbox(client: Client):
    return invoke(client, "sessions.outbox")


class SendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    sessionId: SessionID | None
    text: Annotated[str, Field(min_length=1, max_length=16000, pattern=r"\S")]
    model: Annotated[str, Field(min_length=1, max_length=80)]
    reasoningEffort: Annotated[str | None, Field(max_length=32)] = None
    mode: Literal["auto", "after-turn"] = "auto"
    dueAt: Literal[0] = 0
    automation: Literal["off"] = "off"


@router.post("/send")
def send_input(body: SendInput, client: Client):
    # Retain the supplied UUID. The upstream outbox, not the browser connection,
    # owns the receipt; neither layer may silently invent an ID to retry a send.
    return invoke(client, "sessions.send", body.model_dump(mode="json"))


class StopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectedTurnId: Annotated[str, Field(min_length=1, max_length=256)]


@router.post("/sessions/{session_id}/stop")
def stop_input(session_id: SessionID, body: StopInput, client: Client):
    return invoke(client, "sessions.stop", {"id": session_id, "expectedTurnId": body.expectedTurnId})


@router.post("/outbox/{input_id}/cancel")
def cancel_input(input_id: UUID, client: Client):
    return invoke(client, "sessions.cancelInput", {"id": str(input_id)})


@router.post("/sessions/{session_id}/open")
def open_session(session_id: SessionID, client: Client):
    return invoke(client, "sessions.open", {"id": session_id})

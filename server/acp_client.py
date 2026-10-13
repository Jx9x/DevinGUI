"""ACP (Agent Client Protocol) JSON-RPC client for `devin acp`.

Spawns `devin acp` as a subprocess and communicates via newline-delimited
JSON-RPC 2.0 over stdio.  Notifications from the agent are routed to a
caller-provided async callback; request/response pairs are tracked with
futures so that long-running calls like `session/prompt` can stream
notifications while awaiting the final result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .acp_configuration import (
    apply_model, apply_mode, config_option as _config_option,
    model_controls as _model_controls, option_values as _option_values,
)
from .acp_errors import ACPError, ModelNotAppliedError
from .cli_diagnostics import probe_devin, probe_devin_auth
from .compaction_settings import compaction_thresholds, user_config_for_compaction
from .paths import find_devin_binary as _find_devin_binary

logger = logging.getLogger(__name__)

NotificationHandler = Callable[[dict], Awaitable[None]]
RequestHandler = Callable[[dict], Awaitable[None]]
UnavailableHandler = Callable[[str], Awaitable[None]]

# Control operations have deadlines; a prompt can legitimately run for hours.
# Keep its response future until completion, cancellation or process exit so
# the WebSocket turn task can still notify the browser when the work finishes.
DEFAULT_REQUEST_TIMEOUT = 30.0
LOAD_SESSION_TIMEOUT = 60.0
WRITE_TIMEOUT = 10.0

# Notification methods that are dropped while ``suppress_updates`` is true.
# Config options are still remembered; permission requests use a separate
# dispatch path and remain unsuppressed.
_SUPPRESSED_METHODS = frozenset({"session/update"})


class ACPClient:
    """Manages a single `devin acp` subprocess and its JSON-RPC conversation."""

    def __init__(
        self,
        notification_handler: NotificationHandler,
        cmd: Optional[list[str]] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        request_handler: Optional[RequestHandler] = None,
        on_unavailable: Optional[UnavailableHandler] = None,
        cwd: Optional[str] = None,
    ):
        self.notification_handler = notification_handler
        # JSON-RPC requests contain both a method and an id. If no dedicated
        # request handler is provided, fall back to the notification handler
        # for backwards compatibility.
        self.request_handler = request_handler or notification_handler
        self.cwd = cwd
        self.compaction_thresholds = None
        self._compaction_config_dir = None
        self._auth_executable: str | None = None
        # Invoked once on unexpected ACP unavailability, including write failures.
        # The WebSocket layer uses the reason to report failure and reject permissions.
        self.on_unavailable = on_unavailable
        if cmd is None:
            # Allow tests / E2E setups to substitute a fake ACP binary via
            # the ``DEVIN_ACP_CMD`` env var without changing production
            # behavior.  When unset, the real ``devin`` binary is used.
            acp_cmd_env = os.environ.get("DEVIN_ACP_CMD")
            if acp_cmd_env:
                cmd = shlex.split(acp_cmd_env)
            else:
                devin_bin = _find_devin_binary()
                self._auth_executable = devin_bin
                cmd = [devin_bin]
                if model:
                    cmd += ["--model", model]
                if permission_mode == "bypass":
                    cmd += ["--permission-mode", "bypass"]
                self.compaction_thresholds = compaction_thresholds(cwd, model)
                if self.compaction_thresholds:
                    cmd += ["--compaction-thresholds", ",".join(map(str, self.compaction_thresholds))]
                cmd.append("acp")
        self.cmd = cmd
        self.process: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._initialized = False
        # Set to True once the process exits or stdin writes fail.  While
        # True, new requests raise immediately instead of hanging.
        self.unavailable = False
        self._unavailable_notified = False
        # Track inbound JSON-RPC requests so we can reply exactly once and
        # discard duplicate or late responses.
        self._pending_inbound: set[Any] = set()
        # Serialize all writes to the ACP subprocess stdin so multiple
        # concurrent callers (prompt, cancel, new_session, load_session,
        # permission_response) never interleave JSON-RPC lines.
        self._write_lock = asyncio.Lock()
        # When True, session/update notifications are dropped instead of
        # forwarded to the notification handler.  Used during session/load
        # to suppress the conversation replay (history comes from the DB).
        self.suppress_updates = False
        self._session_config_options: list[dict] | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._auth_executable:
            auth = await asyncio.to_thread(probe_devin_auth, self._auth_executable)
            if auth["authenticated"] is False:
                raise RuntimeError(auth["message"])
        if self.compaction_thresholds:
            capabilities = await asyncio.to_thread(probe_devin, self.cmd[0])
            if capabilities["compaction_supported"] is not True:
                raise RuntimeError(
                    'Custom compaction is unavailable for this CLI. Check Diagnostics and update DevinGUI, '
                    'or set "enabled": false on the matching model_compaction rule in '
                    '.devin/devingui.local.json to explicitly use CLI defaults. No thresholds were silently discarded.'
                )
            settings = user_config_for_compaction(self.compaction_thresholds[0])
            self._compaction_config_dir = tempfile.TemporaryDirectory(prefix="devingui-compaction-")
            config_path = Path(self._compaction_config_dir.name) / "config.json"
            config_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            self.cmd[-1:-1] = ["--config", str(config_path)]
        logger.info("Starting ACP subprocess: %s", self.cmd)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Large limit for stdout — session/load replay notifications
                # and config_option_update (model list) can exceed the default
                # 64 KB readline buffer.
                limit=10 * 1024 * 1024,
            )
        except BaseException:
            if self._compaction_config_dir:
                self._compaction_config_dir.cleanup()
                self._compaction_config_dir = None
            raise
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                pass
        if self._compaction_config_dir:
            self._compaction_config_dir.cleanup()
            self._compaction_config_dir = None
        # Mark unavailable without re-invoking the on_unavailable callback —
        # stop() is an intentional, orderly shutdown.
        self.unavailable = True
        self._unavailable_notified = True
        self._reject_pending("ACP client stopped")
        self._pending_inbound.clear()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # -- I/O loops -----------------------------------------------------------

    def _reject_pending(self, reason: str) -> None:
        """Reject all pending request futures with ``RuntimeError(reason)``."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending.clear()

    async def _mark_unavailable(self, reason: str) -> None:
        """Mark the client unavailable, reject pending futures, and notify
        the WebSocket layer exactly once via ``on_unavailable``."""
        if self.unavailable:
            self._reject_pending(reason)
            return
        self.unavailable = True
        self._reject_pending(reason)
        if self.on_unavailable and not self._unavailable_notified:
            self._unavailable_notified = True
            try:
                await self.on_unavailable(reason)
            except Exception:
                logger.exception("on_unavailable callback raised")

    async def _read_loop(self) -> None:
        assert self.process and self.process.stdout
        while True:
            try:
                line = await self.process.stdout.readline()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Error reading ACP stdout: %s", e)
                break
            if not line:
                logger.info("ACP stdout closed")
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                logger.warning("Unparseable ACP line: %s", line[:200])
                continue
            await self._handle_message(msg)
        # Once stdout reading stops, no responses can reach pending callers.
        # Reject their futures even if the process has not exited.
        await self._mark_unavailable("ACP process exited unexpectedly")
        # Await process termination so callers observing `process.wait()`
        # see the real exit code.
        if self.process:
            try:
                await self.process.wait()
            except (asyncio.CancelledError, ProcessLookupError):
                pass

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while True:
            try:
                line = await self.process.stderr.readline()
            except asyncio.CancelledError:
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                logger.debug("ACP stderr: %s", text)

    async def _handle_message(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(ACPError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
            return

        if "method" in msg and "id" in msg:
            self._pending_inbound.add(msg["id"])
            try:
                await self.request_handler(msg)
            except Exception:
                logger.exception("Error in request handler")
            return

        if "method" in msg:
            if msg["method"] == "session/update":
                params = msg.get("params")
                update = params.get("update") if isinstance(params, dict) else None
                if isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update":
                    self._remember_config_options(update)
            # Cache config_option_update before suppressing session/update
            # delivery. Permission requests use the inbound path above;
            # notifications with other methods are still forwarded.
            if self.suppress_updates and msg["method"] in _SUPPRESSED_METHODS:
                return
            try:
                await self.notification_handler(msg)
            except Exception:
                logger.exception("Error in notification handler")

    # -- request helpers -----------------------------------------------------

    async def _drain_with_timeout(self, timeout: float) -> None:
        """Drain ``stdin`` with a timeout without swallowing cancellation.

        Use ``asyncio.wait`` to avoid the CPython 3.10/3.11 ``wait_for`` race
        between completion and cancellation:
        https://github.com/python/cpython/issues/86296.
        """
        drain_task: asyncio.Task = asyncio.ensure_future(self.process.stdin.drain())
        try:
            done, pending = await asyncio.wait(
                {drain_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if drain_task in pending:
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass
                raise asyncio.TimeoutError()
            return drain_task.result()
        except asyncio.CancelledError:
            if not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass
            raise

    async def _write_message(self, data: bytes, timeout: float = WRITE_TIMEOUT) -> None:
        """Write an NDJSON line through the shared stdin lock.

        All requests, notifications, and responses use this writer.
        Write failure marks the client unavailable and rejects pending futures.
        """
        if not self.process or not self.process.stdin:
            raise RuntimeError("ACP client not started")
        if self.unavailable:
            raise RuntimeError("ACP client unavailable")
        async with self._write_lock:
            try:
                self.process.stdin.write(data)
                await self._drain_with_timeout(timeout)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError) as e:
                # stdin is no longer writable — the process is gone or
                # wedged.  Mark unavailable so callers fail fast.
                await self._mark_unavailable(f"ACP write failed: {e}")
                raise RuntimeError(f"ACP write failed: {e}") from e
            except Exception as e:
                logger.exception("Unexpected error writing to ACP stdin")
                await self._mark_unavailable(f"ACP write failed: {e}")
                raise RuntimeError(f"ACP write failed: {e}") from e

    async def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            notification["params"] = params
        data = (json.dumps(notification) + "\n").encode("utf-8")
        await self._write_message(data)

    async def _send_request(
        self,
        method: str,
        params: dict | None = None,
        timeout: float | None = DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        if not self.process or not self.process.stdin:
            raise RuntimeError("ACP client not started")
        if self.unavailable:
            raise RuntimeError("ACP client unavailable")

        req_id = self._next_id
        self._next_id += 1
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            request["params"] = params

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        data = (json.dumps(request) + "\n").encode("utf-8")
        try:
            await self._write_message(data)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # Reject only unresolved futures; wait_for may already have
            # cancelled or resolved this one.
            if not fut.done():
                fut.set_exception(
                    RuntimeError(f"ACP request '{method}' timed out after {timeout}s")
                )
            raise
        finally:
            # Cleanup also runs on CancelledError, leaving late responses
            # no future to resolve.
            self._pending.pop(req_id, None)

    async def send_response(
        self,
        request_id: Any,
        result: Any | None = None,
        error: dict | None = None,
        timeout: float = WRITE_TIMEOUT,
    ) -> None:
        """Write a JSON-RPC response for an inbound request from the agent.

        ``error`` takes precedence; otherwise send ``result`` (including None).
        Ignore duplicate or late responses for requests no longer pending.
        """
        if request_id not in self._pending_inbound:
            logger.warning("Ignoring response for unknown/expired request id %s", request_id)
            return
        self._pending_inbound.discard(request_id)
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result
        data = (json.dumps(response) + "\n").encode("utf-8")
        await self._write_message(data, timeout=timeout)

    # -- ACP methods ---------------------------------------------------------

    async def initialize(self) -> dict:
        result = await self._send_request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "prompt": {
                        "image": {},
                    },
                },
            },
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._initialized = True
        return result

    async def new_session(self, cwd: str) -> str:
        self._session_config_options = None
        result = await self._send_request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._remember_config_options(result)
        return result.get("sessionId", "")

    def _remember_config_options(self, result: Any) -> None:
        options = result.get("configOptions") if isinstance(result, dict) else None
        if isinstance(options, list):
            self._session_config_options = options

    async def _set_config_option(self, session_id: str, config_id: str, value: str) -> list[dict]:
        result = await self._send_request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._remember_config_options(result)
        options = result.get("configOptions") if isinstance(result, dict) else None
        return options if isinstance(options, list) else []

    async def set_session_model(self, session_id: str, model_id: str) -> str:
        """Apply a catalog variant through the ACP controls and confirm it.

        Older CLIs expose every variant directly in ``model``. Devin 11.1
        exposes a representative model, then separate ``thought_level`` and
        ``speed`` options. Do not start a turn unless every requested control
        is confirmed by the agent's response.
        """
        return await apply_model(self, session_id, model_id, logger=logger)

    async def set_session_mode(self, session_id: str, mode: str) -> str:
        """Set the permission mode and return the agent's confirmed value.

        ACP ignores ``--permission-mode``. Set the session's ``mode`` option
        (``accept-edits``, ``ask``, ``plan``, ``bypass``) after ``session/new``
        or ``session/load``, as with ``model``.

        Missing or mismatched ``currentValue``: RuntimeError. Session startup
        logs bypass failure and continues.
        """
        return await apply_mode(self, session_id, mode, logger=logger)

    async def load_session(self, session_id: str, cwd: str) -> dict:
        """Load (resume) an existing session.  The agent replays the
        conversation history via session/update notifications before
        responding.  Set ``suppress_updates`` before calling to drop
        the replay (useful when history is already loaded from the DB)."""
        self._session_config_options = None
        result = await self._send_request(
            "session/load",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
            timeout=LOAD_SESSION_TIMEOUT,
        )
        self._remember_config_options(result)
        return result

    async def prompt(
        self, session_id: str, text: str, images: list[dict] | None = None
    ) -> dict:
        content: list[dict] = [{"type": "text", "text": text}]
        for img in images or []:
            content.append({
                "type": "image",
                "data": img.get("data", ""),
                "mimeType": img.get("mimeType", ""),
            })
        return await self._send_request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": content,
            },
            timeout=None,
        )

    async def cancel(self, session_id: str) -> None:
        # session/cancel is a notification, not a request; the agent responds
        # by completing the original session/prompt request with a cancelled
        # stop reason.  The write itself is bounded by WRITE_TIMEOUT.
        await self._send_notification("session/cancel", {"sessionId": session_id})

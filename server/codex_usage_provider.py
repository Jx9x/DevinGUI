"""Read ChatGPT plan quotas through the local Codex app-server.

Codex owns authentication and token refresh. This client only initializes the
stdio connection and reads account/rateLimits data; it never starts a turn.
The usage route offloads blocking pipe I/O with ``asyncio.to_thread``.
Protocol: https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Sequence


class CodexUsageError(Exception):
    """An actionable message safe to show without upstream data or tokens."""


def find_codex_binary() -> str:
    configured = os.environ.get("DEVINGUI_CODEX_BIN")
    if configured:
        if Path(configured).is_file():
            return configured
        raise CodexUsageError("DEVINGUI_CODEX_BIN must point to the Codex executable.")
    binary = shutil.which("codex.exe") if os.name == "nt" else None
    binary = binary or shutil.which("codex")
    if binary:
        return binary
    # Desktop installs keep versioned binaries here, which may not be on the
    # PATH inherited by a server started outside the Codex desktop app.
    local_appdata = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local_appdata:
        candidates = list((Path(local_appdata) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))
    raise CodexUsageError("Install Codex or set DEVINGUI_CODEX_BIN to its executable, then sign in with ChatGPT.")


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _window(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CodexUsageError("Codex returned an invalid quota window. Update Codex and refresh usage.")
    used = value.get("usedPercent")
    duration = value.get("windowDurationMins")
    reset = value.get("resetsAt")
    if ((used is not None and not _number(used))
            or (duration is not None and (not _number(duration) or duration <= 0))
            or (reset is not None and (not _number(reset) or reset <= 0))):
        raise CodexUsageError("Codex returned an invalid quota window. Update Codex and refresh usage.")
    return {
        "used_percent": min(100, max(0, used)) if used is not None else None,
        "window_minutes": duration,
        "reset_unix": reset,
    }


def decode_rate_limits(result: dict[str, Any]) -> dict[str, Any]:
    """Prefer the Codex bucket; never mistake a model-specific bucket for it."""
    buckets = result.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) and buckets else result.get("rateLimits")
    if not isinstance(bucket, dict):
        raise CodexUsageError("ChatGPT allowance is not available in the Codex rate limits response.")
    primary, secondary = _window(bucket.get("primary")), _window(bucket.get("secondary"))
    if primary is None and secondary is None:
        raise CodexUsageError("ChatGPT quota windows are unavailable for this account.")
    plan = bucket.get("planType")
    return {"primary": primary, "secondary": secondary,
            "plan_name": plan if isinstance(plan, str) else None}


class CodexUsageProvider:
    def __init__(self, command: Sequence[str] | None = None, *, timeout: float = 15.0,
                 cache_ttl: float = 60.0):
        self.command = list(command) if command else None
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        """Return cached quotas unless forced or expired.

        Refresh failures retain prior quotas with ``error`` and ``stale`` metadata.
        """
        # Serialize reads from multiple GUI tabs and avoid launching a second
        # app-server while another read is still running.
        with self._lock:
            if self._cached is not None and not force and time.monotonic() - self._cached_at < self.cache_ttl:
                return dict(self._cached)
            try:
                data = self._read_limits()
                snapshot = {**data, "fetched_at": int(time.time()), "error": None, "stale": False}
            except (CodexUsageError, OSError) as error:
                message = str(error) if isinstance(error, CodexUsageError) else "Could not start Codex. Check DEVINGUI_CODEX_BIN and refresh usage."
                snapshot = {
                    **(self._cached or {"primary": None, "secondary": None, "plan_name": None,
                                       "account_email": None, "fetched_at": int(time.time())}),
                    "error": message,
                    "stale": bool(self._cached and (self._cached["primary"] or self._cached["secondary"])),
                }
            self._cached = snapshot
            self._cached_at = time.monotonic()
            return dict(snapshot)

    def _read_limits(self) -> dict[str, Any]:
        command = self.command or [find_codex_binary(), "app-server"]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            try:
                # Killing the app-server unblocks the synchronous stdout read.
                process.kill()
            except OSError:
                pass

        timer = threading.Timer(self.timeout, expire)
        timer.daemon = True
        timer.start()

        def send(message: dict[str, Any]) -> None:
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            process.stdin.flush()

        def request(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            send({"id": request_id, "method": method, "params": params or {}})
            while True:
                line = process.stdout.readline(1024 * 1024 + 1)
                if not line:
                    if timed_out.is_set():
                        raise CodexUsageError("ChatGPT usage refresh timed out. Try refreshing again.")
                    raise CodexUsageError("Codex closed the usage connection. Update Codex and refresh usage.")
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise CodexUsageError("Codex returned an invalid usage response. Update Codex and refresh usage.") from None
                if not isinstance(message, dict) or len(line) > 1024 * 1024:
                    raise CodexUsageError("Codex returned an invalid usage response. Update Codex and refresh usage.")
                if message.get("id") != request_id:
                    continue  # Account updates and other notifications can interleave responses.
                if "error" in message:
                    # Raw RPC errors may include private upstream details. Only
                    # display our own messages, never stderr or upstream bodies.
                    raise CodexUsageError("Could not read ChatGPT usage. Check your Codex ChatGPT sign-in and refresh usage.")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise CodexUsageError("Codex returned an invalid usage response. Update Codex and refresh usage.")
                return result

        try:
            request(1, "initialize", {"clientInfo": {"name": "devin_gui_usage", "title": "Devin GUI usage", "version": "0.1.0"}})
            send({"method": "initialized", "params": {}})
            account = request(2, "account/read", {"refreshToken": False}).get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise CodexUsageError("Sign in to Codex with the ChatGPT account linked to Devin (run codex login).")
            data = decode_rate_limits(request(3, "account/rateLimits/read"))
            email, plan = account.get("email"), account.get("planType")
            return {**data, "account_email": email if isinstance(email, str) else None,
                    "plan_name": data["plan_name"] or (plan if isinstance(plan, str) else None)}
        except (OSError, ValueError):
            if timed_out.is_set():
                raise CodexUsageError("ChatGPT usage refresh timed out. Try refreshing again.") from None
            raise CodexUsageError("Codex usage connection failed. Check your Codex ChatGPT sign-in and refresh usage.") from None
        finally:
            timer.cancel()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()

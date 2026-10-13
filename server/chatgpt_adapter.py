"""Backend-only transport to the custom Chat On Steroids loopback API.

Read discovery for each request: its port and token rotate on restart. Never
retry a mutation after a lost response; the companion's outbox owns delivery.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
from typing import Any

OPERATIONS = frozenset({
    "sessions.list", "sessions.events", "sessions.controls", "sessions.send",
    "sessions.stop", "sessions.outbox", "sessions.cancelInput", "sessions.open",
    "models.get", "models.refresh",
})
MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 8 * 1024 * 1024


class LocalAPIError(RuntimeError):
    """Safe to display: never contains transport exceptions or credentials."""

    def __init__(self, message: str, *, code: str = "adapter_unavailable",
                 status_code: int = 503, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.uncertain = uncertain


class ChatOnSteroidsClient:
    def __init__(self, profile: str | Path | None = None, timeout: float = 90):
        configured = profile or os.environ.get("DEVINGUI_CHATGPT_PROFILE")
        appdata = os.environ.get("APPDATA")
        self.profile = Path(configured) if configured else (
            Path(appdata) / "chat-on-steroids" if appdata else None
        )
        self.timeout = timeout

    def _discovery(self) -> tuple[int, str]:
        if self.profile is None:
            raise LocalAPIError("Set DEVINGUI_CHATGPT_PROFILE to the companion profile directory.")
        try:
            with (self.profile / "devin-gui-api" / "connection.json").open("rb") as file:
                raw = file.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            descriptor = json.loads(raw)
            if not isinstance(descriptor, dict):
                raise ValueError()
            url, token = descriptor.get("baseUrl"), descriptor.get("token")
            address = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", url) if isinstance(url, str) else None
            if (type(descriptor.get("apiVersion")) is not int or descriptor["apiVersion"] != 1
                    or not address or not isinstance(token, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", token)):
                raise ValueError()
            port = int(address.group(1))
            if not 1 <= port <= 65535:
                raise ValueError()
            return port, token
        except OSError:
            raise LocalAPIError("Start the custom Chat On Steroids build under the same Windows account as DevinGUI.") from None
        except (ValueError, UnicodeError):
            raise LocalAPIError("Local adapter discovery is invalid; restart the custom companion.", code="invalid_discovery") from None

    def _request(self, operation: str | None, payload: dict[str, Any] | None = None) -> Any:
        if operation is not None and operation not in OPERATIONS:
            raise LocalAPIError("Unsupported chat operation.", code="invalid_operation", status_code=400)
        body = json.dumps(payload if payload is not None else {}, allow_nan=False).encode("utf-8") if operation else None
        if body and len(body) > MAX_REQUEST:
            raise LocalAPIError("Chat request is too large.", code="request_too_large", status_code=413)
        port, token = self._discovery()
        # Direct connection: no environment proxies, redirects, browser cookies, or API billing key.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=min(self.timeout, 5) if operation is None else self.timeout)
        attempted = False
        try:
            connection.connect()
            # Mark before sending: a failed write may still deliver the operation.
            attempted = True
            connection.request("POST" if operation else "GET", "/v1/" + (operation or "status"), body=body,
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise LocalAPIError("Adapter response is too large; request a smaller history page.", code="response_too_large", status_code=502, uncertain=bool(operation))
            # Do not forward upstream error strings, headers or unexpected envelope fields.
            if response.status != 200:
                status = response.status
                if status in (401, 403):
                    raise LocalAPIError("Adapter authentication failed; restart the custom companion.", code="adapter_authentication", status_code=503)
                raise LocalAPIError("Adapter rejected the operation; check the companion's Activity and outbox before retrying.",
                                    code="adapter_rejected", status_code=status if status in (400, 404, 409, 413, 429, 503) else 502,
                                    uncertain=bool(operation) and status >= 500)
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("ok") is not True or "data" not in result:
                raise ValueError()
            return result["data"]
        except (OSError, http.client.HTTPException):
            raise LocalAPIError("Adapter connection interrupted; inspect the outbox before retrying a send.",
                                code="adapter_unavailable", uncertain=attempted and bool(operation)) from None
        except (ValueError, UnicodeError):
            raise LocalAPIError("Adapter returned an invalid response; inspect the outbox before retrying a send.",
                                code="invalid_response", status_code=502, uncertain=bool(operation)) from None
        finally:
            connection.close()

    def status(self) -> dict[str, Any]:
        data = self._request(None)
        if not isinstance(data, dict) or data.get("apiVersion") != 1:
            raise LocalAPIError("Adapter status is incompatible.", code="invalid_response", status_code=502)
        # Discovery credentials and future status fields must never reach the browser.
        bridge = data.get("bridge") if isinstance(data.get("bridge"), dict) else {}
        return {**{key: data.get(key) for key in ("apiVersion", "build", "appVersion", "connectionState", "recording", "modelCatalogState")},
                "bridge": {key: bridge.get(key) for key in ("running", "paired", "present", "lastSeenAt", "extensionVersion")}}

    def call(self, operation: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request(operation, payload)

#!/usr/bin/env python3
"""Fake ACP executable for end-to-end testing.

Speaks newline-delimited JSON-RPC 2.0 over stdin/stdout, mimicking
``devin acp`` just enough to drive the Devin GUI backend through a
permission-request flow without requiring the real ``devin`` binary.

Behavior:
- ``initialize``      -> canned capabilities object.
- ``session/new``     -> fixed session id.
- ``session/load``    -> echoes the requested session id (no replay).
- ``session/prompt``  -> emits a ``session/request_permission`` request
  with a known ``toolCallId`` and ``options`` (allow/deny), then waits
  for the permission response from the backend.  When the response
  arrives it is written verbatim to ``FAKE_ACP_OUTPUT_FILE`` (default:
  ``test-results/fake_acp_last_permission_response.json``) so the
  Playwright test can assert against the exact JSON-RPC response.  The
  prompt is then completed with a ``session/update`` notification and a
  final result.
- ``session/cancel``  -> completes the in-flight prompt with a
  ``cancelled`` stop reason.
- stdin EOF           -> clean exit.

Runnable as ``python fake_acp.py``.  Self-contained — no ``devin``
binary required.
"""

from __future__ import annotations

import json
import os
import sys

# Known permission-request parameters.  The Playwright test asserts
# against these exact values.
TOOL_CALL_ID = "fake-tool-call-1"
PERMISSION_OPTIONS = [
    {"optionId": "allow", "kind": "allow_once", "name": "Allow"},
    {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
]
SESSION_ID = "fake-session-001"

# Where to write the permission response received from the backend.
OUTPUT_FILE = os.environ.get(
    "FAKE_ACP_OUTPUT_FILE",
    os.path.join("test-results", "fake_acp_last_permission_response.json"),
)

# Monotonically increasing id for outbound JSON-RPC requests.
_next_outbound_id = 100


def _send(msg: dict) -> None:
    """Write a single NDJSON line to stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _send_response(req_id, result=None, error=None) -> None:
    msg: dict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    _send(msg)


def _send_request(method: str, params: dict, req_id) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})


def _send_notification(method: str, params: dict | None = None) -> None:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    _send(msg)


def _write_permission_response(response: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_FILE)), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(response, f, indent=2)


def main() -> None:
    global _next_outbound_id

    pending_prompt_id = None      # id of the in-flight session/prompt request
    permission_request_id = None  # id of the session/request_permission we sent
    cancelled = False

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        req_id = msg.get("id")

        # Response to one of our outbound requests (e.g. the permission
        # request).  These messages have an ``id`` and a ``result`` or
        # ``error`` but no ``method``.
        if method is None and "id" in msg and ("result" in msg or "error" in msg):
            # Only treat it as the permission response if it matches the id
            # we sent; otherwise ignore (late/duplicate response).
            if req_id == permission_request_id:
                _write_permission_response(msg)
                permission_request_id = None
                # Complete the prompt.
                if pending_prompt_id is not None:
                    _send_notification(
                        "session/update",
                        {
                            "sessionId": SESSION_ID,
                            "update": {
                                "type": "agent_message",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Permission handled.",
                                        }
                                    ],
                                },
                            },
                        },
                    )
                    stop_reason = "cancelled" if cancelled else "stop"
                    _send_response(pending_prompt_id, {"stopReason": stop_reason})
                    pending_prompt_id = None
            continue

        if method == "initialize":
            _send_response(
                req_id,
                {
                    "protocolVersion": 1,
                    "serverCapabilities": {"prompt": {}, "permission": {}},
                },
            )
        elif method == "session/new":
            _send_response(req_id, {"sessionId": SESSION_ID})
        elif method == "session/load":
            # Echo the requested session id; no conversation replay.
            _send_response(req_id, {"sessionId": msg.get("params", {}).get("sessionId", SESSION_ID)})
        elif method == "session/set_config_option":
            # The backend applies (and verifies) the selected model this way
            # before every prompt.  Mirror the real agent: accept any value
            # and report it back as the option's ``currentValue``, since the
            # backend refuses to prompt unless the change is confirmed.
            params = msg.get("params", {})
            _send_response(
                req_id,
                {
                    "configOptions": [
                        {
                            "id": params.get("configId"),
                            "type": "select",
                            "currentValue": params.get("value"),
                        }
                    ]
                },
            )
        elif method == "session/prompt":
            pending_prompt_id = req_id
            cancelled = False
            permission_request_id = _next_outbound_id
            _next_outbound_id += 1
            _send_request(
                "session/request_permission",
                {
                    "sessionId": SESSION_ID,
                    "toolCall": {
                        "toolCallId": TOOL_CALL_ID,
                        "title": "Fake tool call",
                        "kind": "tool",
                    },
                    "options": PERMISSION_OPTIONS,
                },
                permission_request_id,
            )
        elif method == "session/cancel":
            # Notification (no id).  Mark the in-flight prompt as cancelled
            # and complete it immediately if still pending.
            cancelled = True
            if pending_prompt_id is not None:
                _send_response(pending_prompt_id, {"stopReason": "cancelled"})
                pending_prompt_id = None
                permission_request_id = None
        else:
            if req_id is not None:
                _send_response(
                    req_id,
                    error={"code": -32601, "message": f"Method not supported: {method}"},
                )

    # stdin closed — exit cleanly.
    sys.exit(0)


if __name__ == "__main__":
    main()

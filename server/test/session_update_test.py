"""Tests for ACP ``session/update`` normalization.

The payloads below are verbatim captures from ``devin acp`` (CLI 3000.2.17)
recorded by driving the agent directly over stdio.  The frontend validates
every WebSocket frame and silently drops any frame that fails validation, so
normalization must emit *only* the flat keys the frontend schema allows.
"""

from server.main import normalize_session_update


def test_agent_message_chunk_is_flattened():
    update = normalize_session_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "The sky"}}
    )
    assert update == {"type": "agent_message_chunk", "chunk": "The sky"}


def test_agent_thought_chunk_is_flattened():
    update = normalize_session_update(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": " user asks"}}
    )
    assert update == {"type": "agent_thought_chunk", "chunk": " user asks"}


def test_nested_content_object_never_leaks():
    """Regression: the frontend requires ``content`` to be an array.

    Leaving ACP's ``content`` object on the payload made every chunk fail
    validation, so no token ever reached the UI.
    """
    update = normalize_session_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    )
    assert "content" not in update
    assert "sessionUpdate" not in update


def test_tool_call_content_is_flattened_to_text_items():
    update = normalize_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "functions.exec:0",
            "title": "Ran command",
            "kind": "execute",
            "content": [
                {
                    "type": "content",
                    "content": {
                        "type": "resource",
                        "resource": {"text": "Get-ChildItem", "uri": "tool://preview"},
                    },
                }
            ],
            "rawInput": {"command": "Get-ChildItem"},
        }
    )
    assert update == {
        "type": "tool_call",
        "toolCallId": "functions.exec:0",
        "title": "Ran command",
        "kind": "execute",
        "content": [{"text": "Get-ChildItem"}],
    }
    assert "rawInput" not in update


def test_tool_call_update_without_content_omits_the_key():
    """Status-only updates must not clear output the UI already holds."""
    update = normalize_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "functions.exec:0",
            "status": "in_progress",
            "_meta": {"cognition.ai/inferenceToolName": "exec"},
        }
    )
    assert update == {
        "type": "tool_call_update",
        "toolCallId": "functions.exec:0",
        "status": "in_progress",
    }


def test_tool_call_update_with_output():
    update = normalize_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "functions.exec:0",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "\nName\n----"}}],
        }
    )
    assert update["content"] == [{"text": "\nName\n----"}]


def test_usage_update_keeps_exact_inference_input_tokens():
    update = normalize_session_update(
        {
            "sessionUpdate": "usage_update",
            "used": 12487,
            "size": 262000,
            "_meta": {"cognition.ai/inputTokens": 10659},
        }
    )
    assert update == {
        "type": "usage_update",
        "used": 12487,
        "size": 262000,
        "inputTokens": 10659,
    }


def test_unrenderable_update_kinds_are_dropped():
    for kind in (
        "config_option_update",
        "available_commands_update",
        "current_mode_update",
        "session_info_update",
        "plan",
    ):
        assert normalize_session_update({"sessionUpdate": kind, "foo": "bar"}) is None


def test_empty_text_chunk_is_dropped():
    assert (
        normalize_session_update(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": ""}}
        )
        is None
    )


def test_already_flat_payload_passes_through():
    update = normalize_session_update({"type": "agent_message_chunk", "chunk": "hello"})
    assert update == {"type": "agent_message_chunk", "chunk": "hello"}


def test_message_id_is_preserved():
    update = normalize_session_update(
        {"sessionUpdate": "agent_message_chunk", "chunk": "hi", "messageId": "m-1"}
    )
    assert update["messageId"] == "m-1"


def test_malformed_input_is_dropped():
    assert normalize_session_update({}) is None
    assert normalize_session_update({"sessionUpdate": 5}) is None

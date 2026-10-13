"""Characterize ACP display payloads and permission decisions without live I/O."""

from copy import deepcopy

import pytest

from server.acp_updates import (
    normalize_session_update,
    permission_detail,
    select_permission_option,
)


@pytest.mark.parametrize(
    ("metadata_command", "raw_command", "expected"),
    [
        ("  edited command\n", "original command", "  edited command\n"),
        (" \n", "  original command\n", "  original command\n"),
        (None, "original command", "original command"),
    ],
)
def test_permission_command_precedence_preserves_whitespace(
    metadata_command, raw_command, expected
):
    assert permission_detail(
        {
            "_meta": {"cognition.ai/editableCommand": metadata_command},
            "rawInput": {"command": raw_command, "path": "ignored.txt"},
            "content": [{"text": "ignored output"}],
        }
    ) == expected


def test_permission_paths_and_content_keep_order_and_deduplicate_locations():
    tool_call = {
        "rawInput": {"command": " ", "file_path": "first.txt", "path": "ignored.txt"},
        "locations": [
            {"path": "first.txt"},
            {"path": "second.txt"},
            {"path": "second.txt"},
            {"path": ""},
            None,
        ],
        "content": [
            {
                "type": "content",
                "content": {"type": "resource", "resource": {"text": "output"}},
            },
            {"type": "diff", "path": "first.txt", "oldText": "old", "newText": "new"},
        ],
    }
    assert permission_detail(tool_call) == (
        "first.txt\nsecond.txt\noutput\nfirst.txt\n@@ -1 +1 @@\n-old\n+new"
    )


@pytest.mark.parametrize(
    "tool_call", [None, [], {"rawInput": []}, {"content": [{"type": "image"}]}]
)
def test_permission_without_displayable_detail_is_omitted(tool_call):
    assert permission_detail(tool_call) is None


@pytest.mark.parametrize(
    ("decision", "options", "expected"),
    [
        (
            "ALLOW",
            [
                {"kind": "custom", "optionId": "allow-fallback"},
                {"kind": "ALLOW_ALWAYS", "optionId": "first"},
                {"kind": "allow_once", "optionId": "second"},
            ],
            "first",
        ),
        (
            "deny",
            [
                {"kind": "reject_once", "optionId": "first"},
                {"kind": "deny", "optionId": "second"},
            ],
            "first",
        ),
        ("allow", [{"optionId": 42}, {"optionId": "ALLOW-for-turn"}], "ALLOW-for-turn"),
        (None, [{"kind": "allow", "optionId": "first"}], None),
        ("deny", [], None),
        ("deny", [{"kind": "allow", "optionId": "first"}], None),
    ],
)
def test_permission_selection_prefers_first_matching_kind_before_id_fallback(
    decision, options, expected
):
    assert select_permission_option(decision, options) == expected


def test_tool_diff_omits_headers_and_keeps_path():
    assert normalize_session_update(
        {
            "sessionUpdate": "tool_call",
            "content": [
                {
                    "type": "content",
                    "content": {
                        "type": "diff", "path": "file.txt", "oldText": "old", "newText": "new"
                    },
                }
            ],
        }
    ) == {
        "type": "tool_call", "content": [{"text": "file.txt\n@@ -1 +1 @@\n-old\n+new"}]
    }


def test_tool_diff_truncation_counts_lines_after_removing_headers():
    old_lines = [f"old {index}" for index in range(50)]
    new_lines = [f"new {index}" for index in range(50)]
    update = normalize_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "content": [
                {"type": "diff", "oldText": "\n".join(old_lines), "newText": "\n".join(new_lines)}
            ],
        }
    )
    lines = update["content"][0]["text"].splitlines()
    assert len(lines) == 81
    assert lines[0] == "@@ -1,50 +1,50 @@"
    assert lines[1:51] == [f"-{line}" for line in old_lines]
    assert lines[51:80] == [f"+{line}" for line in new_lines[:29]]
    assert lines[80] == "… (21 more lines)"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (1, "new", "file.txt\n@@ -0,0 +1 @@\n+new"),
        (["old"], "new", "file.txt\n@@ -0,0 +1 @@\n+new"),
        ({"lines": "old"}, "new", "file.txt\n@@ -0,0 +1 @@\n+new"),
        ("old", 2, "file.txt\n@@ -1 +0,0 @@\n-old"),
        ("old", ["new"], "file.txt\n@@ -1 +0,0 @@\n-old"),
        ("old", {"lines": "new"}, "file.txt\n@@ -1 +0,0 @@\n-old"),
        (None, "new", "file.txt\n@@ -0,0 +1 @@\n+new"),
        ("", "new", "file.txt\n@@ -0,0 +1 @@\n+new"),
        ("old", None, "file.txt\n@@ -1 +0,0 @@\n-old"),
        ("old", "", "file.txt\n@@ -1 +0,0 @@\n-old"),
        (1, 2, "file.txt"),
        (None, [], "file.txt"),
    ],
)
def test_diff_non_string_side_is_absent_text(old, new, expected):
    # This malformed ACP input currently raises; fixing it needs a behavior change.
    assert normalize_session_update(
        {
            "sessionUpdate": "tool_call",
            "content": [{"type": "diff", "path": "file.txt", "oldText": old, "newText": new}],
        }
    ) == {"type": "tool_call", "content": [{"text": expected}]}


def test_diff_without_string_side_or_path_yields_no_content():
    assert normalize_session_update(
        {
            "sessionUpdate": "tool_call",
            "content": [{"type": "diff", "oldText": 1, "newText": {}}],
        }
    ) == {"type": "tool_call"}


def test_permission_diff_detail_treats_non_string_side_as_absent():
    assert permission_detail(
        {
            "rawInput": {"file_path": "edit.txt"},
            "content": [{"type": "diff", "oldText": 1, "newText": "new"}],
        }
    ) == "edit.txt\n@@ -0,0 +1 @@\n+new"


@pytest.mark.parametrize(
    ("chunk", "content", "expected"),
    [
        ("explicit", {"text": "fallback"}, {"type": "agent_message_chunk", "chunk": "explicit"}),
        ("", {"text": "fallback"}, None),
        (7, {"text": "fallback"}, {"type": "agent_message_chunk", "chunk": "fallback"}),
        (
            None,
            {"resource": {"text": "resource text"}},
            {"type": "agent_message_chunk", "chunk": "resource text"},
        ),
    ],
)
def test_stream_chunk_precedence_and_resource_fallback(chunk, content, expected):
    assert normalize_session_update(
        {
            "sessionUpdate": "agent_message_chunk", "chunk": chunk,
            "content": content, "messageId": 12,
        }
    ) == expected


@pytest.mark.parametrize(
    ("overrides", "expected_tokens"),
    [
        ({}, {"inputTokens": 8}),
        ({"inputTokens": 3}, {"inputTokens": 3}),
        ({"inputTokens": None}, {}),
        ({"inputTokens": True}, {}),
    ],
)
def test_explicit_usage_input_tokens_override_metadata_even_when_invalid(overrides, expected_tokens):
    assert normalize_session_update(
        {"sessionUpdate": "usage_update", "_meta": {"cognition.ai/inputTokens": 8}, **overrides}
    ) == {"type": "usage_update", **expected_tokens}


def test_usage_numbers_keep_fractional_values_and_exclude_booleans():
    assert normalize_session_update(
        {"sessionUpdate": "usage_update", "used": False, "size": 100.5, "inputTokens": 2.5}
    ) == {"type": "usage_update", "size": 100.5, "inputTokens": 2.5}


def test_tool_content_filtering_preserves_order_without_mutating_input():
    payload = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": 7,
        "title": None,
        "status": "completed",
        "rawInput": {"command": "private input"},
        "content": [
            {"type": "text", "text": "before"},
            {"type": "image", "mimeType": "image/png", "data": "", "text": "ignored"},
            {
                "type": "image", "mimeType": "application/pdf",
                "data": "data", "text": "ignored",
            },
            {
                "type": "content",
                "content": {
                    "type": "image", "mimeType": " IMAGE/PNG ",
                    "data": "data", "extra": "ignored",
                },
            },
            None,
            {"type": "diff", "path": "path-only.txt"},
            {"type": "text", "text": "after"},
        ],
    }
    original = deepcopy(payload)
    assert normalize_session_update(payload) == {
        "type": "tool_call_update",
        "status": "completed",
        "content": [
            {"text": "before"},
            {"type": "image", "mimeType": "image/png", "data": "data"},
            {"text": "path-only.txt"},
            {"text": "after"},
        ],
    }
    assert payload == original

"""Pure ACP payload translation for frontend updates and permission prompts.

Keep display formatting and permission selection independent of app startup,
session ownership, and ACP transport I/O.
"""

from __future__ import annotations

from typing import Any

from .image_content import image_mime_type


# Text/thought chunks are deltas; replay only undelivered chunks to avoid duplicate output.
TEXT_CHUNK_UPDATES = frozenset({"agent_message_chunk", "agent_thought_chunk"})


def _acp_content_text(content: Any) -> str:
    """Extract plain text from an ACP content block.

    Devin's ACP wraps streamed text as ``{"type": "text", "text": "..."}``
    and tool output as ``{"type": "resource", "resource": {"text": ...}}``.
    Returns an empty string for shapes that carry no text.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    text = content.get("text")
    if isinstance(text, str):
        return text
    resource = content.get("resource")
    if isinstance(resource, dict) and isinstance(resource.get("text"), str):
        return resource["text"]
    return ""


_DIFF_MAX_LINES = 80


def _render_diff_text(old: Any, new: Any) -> str:
    """Render an ACP ``diff`` content item (``oldText``/``newText``) as a
    compact unified-style diff.  Returns "" when neither side is a string."""
    if not isinstance(old, str) and not isinstance(new, str):
        return ""
    import difflib

    lines = list(
        difflib.unified_diff(
            (old if isinstance(old, str) else "").splitlines(),
            (new if isinstance(new, str) else "").splitlines(), lineterm="",
        )
    )
    # Drop the ---/+++ header lines; the path is carried on the diff item.
    if len(lines) > 2 and lines[0].startswith("---") and lines[1].startswith("+++"):
        lines = lines[2:]
    if len(lines) > _DIFF_MAX_LINES:
        lines = lines[:_DIFF_MAX_LINES] + [f"… ({len(lines) - _DIFF_MAX_LINES} more lines)"]
    return "\n".join(lines)


def _tool_call_content_item_text(item: Any) -> str:
    """Extract text from wrapped content, plain text/resources, or diff items.

    Diffs include the path and rendered changes even without a ``text`` field.
    """
    if not isinstance(item, dict):
        return ""
    inner = item.get("content")
    if isinstance(inner, dict):
        item = inner
    if item.get("type") == "diff" or "oldText" in item or "newText" in item:
        path = item.get("path")
        diff = _render_diff_text(item.get("oldText"), item.get("newText"))
        parts = [p for p in (path if isinstance(path, str) else "", diff) if p]
        return "\n".join(parts)
    return _acp_content_text(item)


def _normalize_tool_call_content(items: Any) -> list[dict] | None:
    """Flatten ACP tool-call content into the frontend's ``ToolCallContent[]``.

    Text/resources and diffs become ``{"text": ...}`` entries; images retain
    ``type``, ``data``, and ``mimeType``.
    Nothing renderable: None. Omit the key to preserve existing UI content.
    """
    if not isinstance(items, list):
        return None
    flattened: list[dict] = []
    for item in items:
        block = item.get("content", item) if isinstance(item, dict) else None
        if isinstance(block, dict) and block.get("type") == "image":
            mime = image_mime_type(block.get("mimeType"))
            data = block.get("data")
            if mime and isinstance(data, str) and data:
                flattened.append({"type": "image", "data": data, "mimeType": mime})
            continue
        text = _tool_call_content_item_text(item)
        if text:
            flattened.append({"text": text})
    return flattened or None


def permission_detail(tool_call: Any) -> str | None:
    """Return display detail for a ``session/request_permission`` tool call.

    Prefer ``_meta``'s editable command over ``rawInput.command``; preserve
    command whitespace. Otherwise join paths and text/diffs, or return None
    if nothing is displayable.
    """
    if not isinstance(tool_call, dict):
        return None
    meta = tool_call.get("_meta") if isinstance(tool_call.get("_meta"), dict) else {}
    raw = tool_call.get("rawInput") if isinstance(tool_call.get("rawInput"), dict) else {}
    for candidate in (meta.get("cognition.ai/editableCommand"), raw.get("command")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    parts: list[str] = []
    path = raw.get("file_path") or raw.get("path")
    if isinstance(path, str) and path:
        parts.append(path)
    locations = tool_call.get("locations")
    if isinstance(locations, list):
        for loc in locations:
            lp = loc.get("path") if isinstance(loc, dict) else None
            if isinstance(lp, str) and lp and lp not in parts:
                parts.append(lp)
    for item in tool_call.get("content") or []:
        text = _tool_call_content_item_text(item)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else None


def normalize_session_update(update: dict) -> dict | None:
    """Translate ACP or frontend-shaped updates into flat ``SessionUpdate`` payloads.

    The frontend rejects non-array ``content``. Extract text chunks and
    flatten tool content rather than forwarding ACP's nested objects.
    Unsupported kinds or empty text chunks: None.
    """
    if not isinstance(update, dict):
        return None
    # Frontend-shaped updates still undergo filtering and content normalization.
    kind = update.get("sessionUpdate") or update.get("type")
    if not isinstance(kind, str):
        return None

    if kind in TEXT_CHUNK_UPDATES:
        chunk = update.get("chunk")
        if not isinstance(chunk, str):
            chunk = _acp_content_text(update.get("content"))
        if not chunk:
            return None
        normalized: dict[str, Any] = {"type": kind, "chunk": chunk}
        if isinstance(update.get("messageId"), str):
            normalized["messageId"] = update["messageId"]
        return normalized

    if kind in ("tool_call", "tool_call_update"):
        normalized = {"type": kind}
        for key in ("toolCallId", "title", "kind", "status"):
            value = update.get(key)
            if isinstance(value, str):
                normalized[key] = value
        content = _normalize_tool_call_content(update.get("content"))
        if content is not None:
            normalized["content"] = content
        return normalized

    if kind == "usage_update":
        normalized = {"type": kind}
        for key in ("used", "size"):
            value = update.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                normalized[key] = value
        metadata = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
        input_tokens = update.get("inputTokens", metadata.get("cognition.ai/inputTokens"))
        if isinstance(input_tokens, (int, float)) and not isinstance(input_tokens, bool):
            normalized["inputTokens"] = input_tokens
        return normalized

    return None


def select_permission_option(decision: str | None, options: list[dict]) -> str | None:
    """Map a simple allow/deny decision to an ACP permission option ID.

    Prefer the first matching kind in agent order, then an ID substring match.
    Matching is case-insensitive; return None when no option matches.
    """
    if not decision or not options:
        return None
    decision_lower = decision.lower()
    for opt in options:
        kind = (opt.get("kind") or "").lower()
        if decision_lower == "allow" and kind in ("allow", "allow_once", "allow_always"):
            return opt.get("optionId")
        if decision_lower == "deny" and kind in ("deny", "reject", "reject_once", "reject_always"):
            return opt.get("optionId")
    for opt in options:
        option_id = opt.get("optionId")
        if option_id and isinstance(option_id, str) and decision_lower in option_id.lower():
            return option_id
    return None

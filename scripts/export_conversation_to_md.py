#!/usr/bin/env python3
"""Export a Devin CLI conversation to Markdown.

Reads Devin's ``sessions.db`` and optional per-session transcript JSON, then
writes a user-facing Markdown file. The default database uses the CLI's
transcript location; other databases use an adjacent ``transcripts`` directory.
By default only the user's prompts and the model's non-reasoning output are
included. Use the flags below to include reasoning, tool calls, and tool outputs.

Examples
--------
    python scripts/export_conversation_to_md.py "Devin CLI Conversation to Markdown Exporter"
    python scripts/export_conversation_to_md.py iron-river -o export.md --include-tool-calls --include-tool-outputs
    python scripts/export_conversation_to_md.py --list
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.paths import (
    default_db_path as _default_db_path,
    default_transcripts_dir as _default_transcripts_dir,
)

EXPORT_DESTINATION_EXISTS = 3
EXPORT_DESTINATION_INVALID = 4

_SUMMARY_MARKERS = (
    "Conversation to summarize:",
    "Now summarize the conversation above",
)


def _safe_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def _content_to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _content_to_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts) if parts else None
    if isinstance(value, dict):
        for key in ("text", "content", "output", "message"):
            if key in value:
                text = _content_to_text(value[key])
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _extract_thinking(cm: dict) -> str | None:
    th = cm.get("thinking")
    if th is None:
        return None
    if isinstance(th, str):
        return th or None
    if isinstance(th, dict):
        for key in ("thinking", "text", "content", "reasoning_content"):
            if key in th:
                text = _content_to_text(th[key])
                if text:
                    return text
    text = _content_to_text(th)
    return text or None


def _ts_to_iso(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            numeric = float(stripped)
        except ValueError:
            return stripped
        raw = numeric
    try:
        value = float(raw)
        while abs(value) > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _code_fence(text: str, lang: str = "") -> str:
    text = (text or "").rstrip("\n")
    if not text:
        return "*(empty)*"
    max_run = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    n = max(3, max_run + 1)
    fence = "`" * n
    return f"{fence}{lang}\n{text}\n{fence}"


def _is_summary_user_text(text: str) -> bool:
    return any(marker in text for marker in _SUMMARY_MARKERS)


def _is_summary_assistant_text(text: str) -> bool:
    return text.lstrip().startswith("<summary>")


def _normalize_tool_calls(tcs: Any) -> list[dict]:
    if not isinstance(tcs, list):
        return []
    out = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        tid = tc.get("tool_call_id") or tc.get("id")
        name = tc.get("function_name") or tc.get("name")
        args = tc.get("arguments") or tc.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        out.append({"id": tid, "name": name, "arguments": args})
    return out


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = f"file:{quote(Path(db_path).resolve().as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _find_session(conn: sqlite3.Connection, name: str, exact: bool = False) -> dict | list[dict] | None:
    cur = conn.cursor()
    if exact:
        cur.execute(
            """SELECT id, title, working_directory, model, created_at, last_activity_at
               FROM sessions
               WHERE (hidden = 0 OR hidden IS NULL)
                 AND (id = ? COLLATE NOCASE OR title = ? COLLATE NOCASE)""",
            (name, name),
        )
    else:
        like = f"%{name}%"
        cur.execute(
            """SELECT id, title, working_directory, model, created_at, last_activity_at
               FROM sessions
               WHERE (hidden = 0 OR hidden IS NULL)
                 AND (id = ? COLLATE NOCASE OR title LIKE ? COLLATE NOCASE)""",
            (name, like),
        )
    rows = cur.fetchall()
    if not rows:
        return None
    if exact or len(rows) == 1:
        row = rows[0]
        return {
            "id": row["id"],
            "title": row["title"],
            "working_directory": row["working_directory"],
            "model": row["model"],
            "created_at": _ts_to_iso(row["created_at"]),
            "last_activity_at": _ts_to_iso(row["last_activity_at"]),
        }
    return [dict(r) for r in rows]


def _list_sessions(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """SELECT id, title, working_directory, last_activity_at
           FROM sessions
           WHERE hidden = 0 OR hidden IS NULL
           ORDER BY last_activity_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "working_directory": r["working_directory"],
            "last_activity_at": _ts_to_iso(r["last_activity_at"]),
        }
        for r in cur.fetchall()
    ]


def _load_transcript(db_path: str, session_id: str) -> list[dict] | None:
    if Path(db_path).resolve() == Path(_default_db_path()).resolve():
        # The CLI's transcript location does not follow database relocation.
        transcript_dir = Path(_default_transcripts_dir())
    else:
        transcript_dir = Path(db_path).parent / "transcripts"
    transcript_path = transcript_dir / f"{session_id}.json"
    if not transcript_path.exists():
        return None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return None

    messages = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        source = step.get("source")
        if source == "system":
            continue
        if source == "user":
            content = _content_to_text(step.get("message")) or ""
            if not content:
                continue
            messages.append(
                {
                    "role": "user",
                    "content": content,
                    "reasoning": None,
                    "tool_calls": [],
                    "tool_outputs": {},
                    "is_user_input": None,
                }
            )
            continue
        if source == "agent":
            content = _content_to_text(step.get("message")) or ""
            reasoning = _content_to_text(step.get("reasoning_content")) or None
            tool_calls = _normalize_tool_calls(step.get("tool_calls"))
            tool_outputs = {}
            observation = step.get("observation")
            if isinstance(observation, dict):
                for result in observation.get("results", []):
                    if not isinstance(result, dict):
                        continue
                    tcid = result.get("source_call_id")
                    out = _content_to_text(result.get("content"))
                    if tcid and out:
                        tool_outputs[tcid] = out
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "reasoning": reasoning,
                    "tool_calls": tool_calls,
                    "tool_outputs": tool_outputs,
                    "is_user_input": None,
                }
            )
            continue
    return messages


def _load_db(db_path: str, session_id: str) -> list[dict]:
    conn = _connect_ro(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT row_id, chat_message FROM message_nodes WHERE session_id = ? ORDER BY row_id",
            (session_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    tool_outputs: dict[str, str] = {}
    entries: list[dict] = []
    for row_id, raw in rows:
        cm = _safe_json(raw)
        if not isinstance(cm, dict):
            continue
        role = cm.get("role")
        if role == "tool":
            tcid = cm.get("tool_call_id")
            text = _content_to_text(cm.get("content"))
            if tcid and text:
                tool_outputs[tcid] = text
            continue
        if role not in ("user", "assistant"):
            continue
        content = _content_to_text(cm.get("content")) or ""
        msg_id = cm.get("message_id")
        metadata = cm.get("metadata") or {}
        is_user_input = metadata.get("is_user_input")
        reasoning = _extract_thinking(cm)
        tool_calls = _normalize_tool_calls(cm.get("tool_calls"))

        entries.append(
            {
                "row_id": row_id,
                "message_id": msg_id,
                "role": role,
                "content": content,
                "reasoning": reasoning,
                "tool_calls": tool_calls,
                "is_user_input": is_user_input,
            }
        )

    # Deduplicate by message_id, keeping the latest row_id.
    by_id: dict[Any, dict] = {}
    for e in entries:
        key = e["message_id"]
        if key is None:
            key = e["row_id"]
        by_id[key] = e

    sorted_entries = sorted(by_id.values(), key=lambda e: e["row_id"])
    messages = []
    for e in sorted_entries:
        messages.append(
            {
                "role": e["role"],
                "content": e["content"],
                "reasoning": e["reasoning"],
                "tool_calls": e["tool_calls"],
                "tool_outputs": tool_outputs,
                "is_user_input": e["is_user_input"],
            }
        )
    return messages


def _load_messages(db_path: str, session_id: str, prefer_transcript: bool) -> list[dict]:
    if prefer_transcript:
        transcript = _load_transcript(db_path, session_id)
        if transcript is not None:
            return transcript
    return _load_db(db_path, session_id)


def _render_markdown(
    session: dict,
    messages: list[dict],
    include_reasoning: bool,
    include_tool_calls: bool,
    include_tool_outputs: bool,
    include_summaries: bool,
) -> tuple[str, int]:
    include_tool_calls = include_tool_calls or include_tool_outputs

    lines: list[str] = []
    title = session.get("title") or session.get("id")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- **Session:** `{session['id']}`")
    if session.get("working_directory"):
        lines.append(f"- **Workspace:** {session['working_directory']}")
    if session.get("model"):
        lines.append(f"- **Model:** {session['model']}")
    if session.get("created_at"):
        lines.append(f"- **Created:** {session['created_at']}")
    if session.get("last_activity_at"):
        lines.append(f"- **Last activity:** {session['last_activity_at']}")
    lines.append("")

    rendered = 0
    for msg in messages:
        role = msg["role"]
        if role == "user":
            content = msg["content"].strip()
            if not content:
                continue
            if not include_summaries:
                if msg.get("is_user_input") in (False, 0):
                    continue
                if not msg.get("is_user_input") and _is_summary_user_text(content):
                    continue
            lines.append("## User")
            lines.append("")
            lines.append(content)
            lines.append("")
            rendered += 1
            continue

        if role == "assistant":
            content = msg["content"]
            reasoning = msg.get("reasoning")
            tool_calls = msg.get("tool_calls", [])
            tool_outputs = msg.get("tool_outputs", {})

            if not include_summaries and _is_summary_assistant_text(content) and not tool_calls:
                continue

            has_content = bool(content.strip())
            has_tool_calls = include_tool_calls and bool(tool_calls)
            has_reasoning = include_reasoning and bool(reasoning and reasoning.strip())

            if not has_content and not has_tool_calls and not has_reasoning:
                continue

            lines.append("## Assistant")
            lines.append("")
            if has_reasoning:
                lines.append("> **Reasoning**")
                for line in reasoning.splitlines():
                    lines.append(f"> {line}")
                lines.append("")
            if has_content:
                lines.append(content)
                lines.append("")
            if has_tool_calls:
                for tc in tool_calls:
                    name = tc.get("name") or "unknown"
                    lines.append(f"### Tool: {name}")
                    lines.append("")
                    args = tc.get("arguments", {})
                    lines.append("**Arguments**")
                    lines.append("")
                    lines.append(
                        _code_fence(json.dumps(args, ensure_ascii=False, indent=2), "json")
                    )
                    lines.append("")
                    if include_tool_outputs:
                        out = tool_outputs.get(tc.get("id"))
                        if out:
                            lines.append("**Output**")
                            lines.append("")
                            lines.append(_code_fence(out, "text"))
                            lines.append("")
                        else:
                            lines.append("*No output captured.*")
                            lines.append("")
            rendered += 1

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines), rendered


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = name.strip(" .")
    if not name:
        name = "conversation"
    return name


def _estimated_tokens(markdown: str) -> int:
    """Estimate model tokens in rendered Markdown at roughly four UTF-8 bytes each.

    The session may use different model tokenizers, so this is intentionally an
    estimate of the export text rather than a claimed API usage total.
    """
    return math.ceil(len(markdown.encode("utf-8")) / 4)


def _preview_counts(
    session: dict, messages: list[dict], selected: dict[str, bool]
) -> dict[str, int]:
    """Count the base export, each option's added text, and the selected export.

    Enabled option contributions are applied in a fixed order so they add up
    to the selected total. Disabled option counts show the increase from
    enabling that option with the current selection.
    """
    option_order = (
        "include_reasoning",
        "include_tool_calls",
        "include_tool_outputs",
        "include_summaries",
    )

    def count(options: dict[str, bool]) -> int:
        markdown, _ = _render_markdown(session, messages, **options)
        return _estimated_tokens(markdown)

    staged = dict.fromkeys(option_order, False)
    base = count(staged)
    previous = base
    counts: dict[str, int] = {"base": base}
    selected_count = count(selected)
    for option in option_order:
        if selected[option]:
            staged[option] = True
            next_count = count(staged)
            counts[option] = next_count - previous
            previous = next_count
        else:
            enabled = {**selected, option: True}
            counts[option] = count(enabled) - selected_count
    counts["total"] = selected_count
    return counts


def validate_output_path(output: str) -> Path:
    if not output.strip() or re.search(r"[\x00-\x1f\x7f]", output):
        raise ValueError("Choose a local .md file for the export.")
    if output.replace("\\", "/").startswith("//"):
        raise ValueError("Network and device paths are not allowed for exports.")
    drive = re.match(r"[A-Za-z]:[/\\]", output)
    if ":" in (output[2:] if drive else output):
        raise ValueError("URLs, device paths, and alternate data streams are not allowed for exports.")
    windows_path = PureWindowsPath(output)
    if os.name == "nt":
        if windows_path.root and not windows_path.drive:
            raise ValueError("Choose a local drive path or a relative .md filename.")
        for part in windows_path.parts:
            if part in (windows_path.anchor, ".", ".."):
                continue
            if (
                PureWindowsPath(part).is_reserved()
                or part.endswith((" ", "."))
                or re.search(r'[<>:"|?*]', part)
            ):
                raise ValueError("Device names and invalid Windows path components are not allowed for exports.")
    elif windows_path.drive:
        raise ValueError("Choose a local .md path for this operating system.")
    output_path = Path(output).absolute()
    if output_path.suffix.lower() != ".md":
        raise ValueError("Exports must be saved as local .md files.")
    if os.name == "nt":
        if output_path.anchor.replace("\\", "/").startswith("//"):
            raise ValueError("Network paths are not allowed for exports.")
        if ctypes.windll.kernel32.GetDriveTypeW(output_path.anchor) not in {2, 3, 5, 6}:
            raise ValueError("Exports must be saved on a local drive.")
    for component in (*reversed(output_path.parents), output_path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Export paths must not traverse symbolic links or reparse points.")
        if component == output_path:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("The export destination must be a regular .md file.")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("The export destination's parent must be a directory.")
    return output_path


def _write_markdown(output: str, markdown: str, *, overwrite: bool = False) -> Path:
    output_path = validate_output_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        with output_path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(markdown)
        return output_path
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=output_path.parent,
            prefix=f".{output_path.name}.", suffix=".tmp", delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(markdown)
        validate_output_path(str(output_path))
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("name", nargs="?", help="Session id or title (substring) to export.")
    parser.add_argument("--db", default=_default_db_path(), help="Path to Devin sessions.db.")
    parser.add_argument("-o", "--output", help="Output local .md file path (created exclusively by default).")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing local .md file.")
    parser.add_argument("--list", action="store_true", help="List recent sessions and exit.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum sessions to list (default: 50).")
    parser.add_argument("--exact", action="store_true", help="Match session name exactly.")
    parser.add_argument(
        "--preview-json",
        action="store_true",
        help="Print Markdown and estimated token counts as JSON without writing a file.",
    )
    parser.add_argument("--include-reasoning", action="store_true", help="Include assistant reasoning blocks.")
    parser.add_argument("--include-tool-calls", action="store_true", help="Include assistant tool calls.")
    parser.add_argument(
        "--include-tool-outputs",
        action="store_true",
        help="Include tool outputs (implies --include-tool-calls).",
    )
    parser.add_argument(
        "--include-summaries",
        action="store_true",
        help="Include compactor summary prompts and responses.",
    )
    parser.add_argument(
        "--db-only",
        action="store_true",
        help="Force reading from sessions.db even if a transcript JSON exists.",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    conn = _connect_ro(args.db)
    try:
        if args.list:
            sessions = _list_sessions(conn, args.limit)
            print(f"Recent sessions (limit {len(sessions)}):")
            for s in sessions:
                print(
                    f"  {s['id']:24} {s['last_activity_at'] or '':24} {s['title']}"
                )
            return 0

        if not args.name:
            parser.error("conversation name is required unless --list is used")

        result = _find_session(conn, args.name, args.exact)
    finally:
        conn.close()

    if result is None:
        print(f"ERROR: no session matching {args.name!r}", file=sys.stderr)
        return 1
    if isinstance(result, list):
        print(
            f"ERROR: multiple sessions match {args.name!r}. Use --exact or the session id:",
            file=sys.stderr,
        )
        for r in result:
            print(f"  {r['id']:24} {r['title']}", file=sys.stderr)
        return 1
    session = result

    messages = _load_messages(args.db, session["id"], prefer_transcript=not args.db_only)
    md, count = _render_markdown(
        session,
        messages,
        include_reasoning=args.include_reasoning,
        include_tool_calls=args.include_tool_calls,
        include_tool_outputs=args.include_tool_outputs,
        include_summaries=args.include_summaries,
    )

    if args.preview_json:
        selected = {
            "include_reasoning": args.include_reasoning,
            "include_tool_calls": args.include_tool_calls or args.include_tool_outputs,
            "include_tool_outputs": args.include_tool_outputs,
            "include_summaries": args.include_summaries,
        }
        counts = _preview_counts(session, messages, selected)
        if args.db_only:
            counts["db_only"] = counts["total"]
        else:
            db_messages = _load_db(args.db, session["id"])
            db_markdown, _ = _render_markdown(session, db_messages, **selected)
            counts["db_only"] = _estimated_tokens(db_markdown)
        # ASCII JSON survives Windows subprocess pipes regardless of console encoding.
        print(json.dumps({"markdown": md, "counts": counts}))
        return 0

    output = args.output
    if output is None:
        output = _safe_filename(session.get("title") or session["id"]) + ".md"
    try:
        output_path = _write_markdown(output, md, overwrite=args.overwrite)
    except FileExistsError:
        print(f"ERROR: destination already exists: {output}. Choose another filename or use --overwrite.", file=sys.stderr)
        return EXPORT_DESTINATION_EXISTS
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXPORT_DESTINATION_INVALID
    except OSError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Wrote {count} messages to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

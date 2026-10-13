"""Read-only access to Devin's ``sessions.db`` SQLite database.

The database is kept in WAL mode by the CLI, so a read-only connection
is safe and will see live updates without blocking the CLI.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .image_content import image_mime_type
from .message_projection import (
    content_to_text, decode_json, image_metadata, is_compaction_prompt, normalize_metrics,
    project_messages, project_reasoning, thinking_to_text, timestamp_to_iso,
)
from .paths import default_db_path as _default_db_path
from .pricing_history import _timestamp

logger = logging.getLogger(__name__)


class DBReader:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _default_db_path()

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        if not path.exists():
            raise FileNotFoundError(
                f"No Devin session database at {self.db_path}. "
                "Start a conversation with the signed-in official Devin CLI to create it."
            )
        uri = path.absolute().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError as error:
            raise sqlite3.OperationalError(
                f"Cannot read Devin session database at {self.db_path}: {error}. "
                "Check that the file is accessible to the user running the server."
            ) from error
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_if_present(self) -> sqlite3.Connection | None:
        try:
            return self._connect()
        except FileNotFoundError as error:
            logger.info("%s", error)
            return None

    def inspect_schema(self) -> dict:
        required = {
            "sessions": {"id", "title", "working_directory", "model", "agent_mode", "created_at", "last_activity_at", "hidden"},
            "message_nodes": {"row_id", "session_id", "node_id", "parent_node_id", "chat_message", "created_at", "metadata"},
        }
        optional = {
            "rendered_history": {"rendered_commits": {"session_id", "sequence_number", "rendered_html", "created_at"}},
            "prompt_history": {"prompt_history": {"id", "content", "timestamp", "session_id", "is_shell"}},
            "turn_tracking": {"sessions": {"main_chain_id"}},
        }
        result = {"path": self.db_path, "status": "ok", "missing": [], "features": {}}
        try:
            conn = self._connect()
            try:
                conn.execute("SELECT json_valid('{}')")
                columns = {
                    table: {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                    for table in {"sessions", "message_nodes", "rendered_commits", "prompt_history"}
                }
                def missing(requirements: dict[str, set[str]]) -> list[str]:
                    return sorted(f"{table}.{column}" for table, names in requirements.items()
                                  for column in names - columns[table])
                result["missing"] = missing(required)
                result["status"] = "incompatible" if result["missing"] else "ok"
                result["features"] = {name: missing(requirements) for name, requirements in optional.items()}
            finally:
                conn.close()
        except FileNotFoundError:
            result["status"] = "missing"
        except sqlite3.DatabaseError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            result["status"] = "corrupt" if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} else "unreadable"
        except OSError:
            result["status"] = "unreadable"
        return result

    def _safe_json(self, raw: Any) -> Any:
        return decode_json(raw)

    @staticmethod
    def _ts_to_iso(raw: Any) -> str | None:
        """Convert SQLite timestamps to ISO-8601 without discarding ISO strings."""
        return timestamp_to_iso(raw)

    @staticmethod
    def _content_to_text(content: Any) -> str | None:
        """Normalize string or structured message content for the browser."""
        # Resolve the original base-class hook at every recursive call.
        return content_to_text(content, nested_text=lambda value: DBReader._content_to_text(value))

    @staticmethod
    def _thinking_to_text(value: Any) -> str | None:
        """Normalize persisted reasoning from a ``thinking`` object or a legacy string."""
        return thinking_to_text(value, convert_text=lambda value: DBReader._content_to_text(value))

    @staticmethod
    def _normalize_metrics(value: Any) -> dict[str, int | float | None] | None:
        return normalize_metrics(value)

    @staticmethod
    def _latest_turn_nodes(cur: sqlite3.Cursor, session_id: str) -> tuple[set, set]:
        """Identify the current main-agent turn, excluding side branches.

        Match message ids as well as node ids because replayed rows can move
        the same output to a new node. If compaction removed the user boundary,
        use the latest explicit user input's timestamp to bound the main chain.
        """
        try:
            row = cur.execute(
                "SELECT main_chain_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return set(), set()
        if not row or row["main_chain_id"] is None:
            return set(), set()
        rows = cur.execute(
            """
            SELECT node_id, parent_node_id,
                   json_extract(chat_message, '$.role') AS role,
                   json_extract(chat_message, '$.message_id') AS message_id,
                   json_extract(chat_message, '$.metadata.is_user_input') AS is_user_input,
                   json_extract(chat_message, '$.metadata.created_at') AS message_time
            FROM message_nodes WHERE session_id = ? ORDER BY row_id
            """,
            (session_id,),
        ).fetchall()
        nodes = {node["node_id"]: node for node in rows}
        node_id = row["main_chain_id"]
        turn_nodes: set = set()
        message_ids: set = set()
        while node_id in nodes and node_id not in turn_nodes:
            node = nodes[node_id]
            if node["role"] == "user" and node["is_user_input"] != 0:
                return turn_nodes, message_ids
            turn_nodes.add(node_id)
            if node["message_id"] is not None:
                message_ids.add(node["message_id"])
            node_id = node["parent_node_id"]

        def message_time(node: sqlite3.Row) -> float | None:
            # Row created_at can be a replay/import time. Use only the original
            # message timestamp when reconstructing a compacted turn.
            iso = DBReader._ts_to_iso(node["message_time"])
            if iso:
                try:
                    value = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                    return value.timestamp()
                except (ValueError, OverflowError, OSError):
                    pass
            return None

        user_times = [
            timestamp for node in rows
            if node["role"] == "user" and node["is_user_input"] == 1
            and (timestamp := message_time(node)) is not None
        ]
        if not user_times:
            return set(), set()
        latest_user_time = max(user_times)
        turn_nodes = {
            node_id for node_id in turn_nodes
            if (timestamp := message_time(nodes[node_id])) is not None
            and timestamp >= latest_user_time
        }
        message_ids = {
            nodes[node_id]["message_id"] for node_id in turn_nodes
            if nodes[node_id]["message_id"] is not None
        }
        return turn_nodes, message_ids

    # -- sessions ------------------------------------------------------------

    def list_sessions(self, working_directory: str | None = None) -> list[dict]:
        conn = self._connect_if_present()
        if conn is None:
            return []
        try:
            cur = conn.cursor()
            # ``sessions.model`` can retain its creation-time value after a model
            # switch. Prefer the latest assistant ``generation_model`` for the
            # sidebar and cost fallback.
            latest_model_subquery = (
                ", (SELECT json_extract(mn.chat_message, '$.metadata.generation_model') "
                "FROM message_nodes mn "
                "WHERE mn.session_id = sessions.id "
                "  AND json_extract(mn.chat_message, '$.role') = 'assistant' "
                "  AND json_extract(mn.chat_message, '$.metadata.generation_model') IS NOT NULL "
                "ORDER BY mn.row_id DESC LIMIT 1) AS latest_model"
            )
            conversation_activity_subquery = (
                ", COALESCE((SELECT mn_activity.created_at "
                "FROM message_nodes mn_activity "
                "WHERE mn_activity.session_id = sessions.id "
                "ORDER BY mn_activity.row_id DESC LIMIT 1), sessions.created_at) "
                "AS conversation_activity_at"
            )
            if working_directory:
                # Normalize slashes and compare case-insensitively so that
                # paths like "C:\...\HexWeb" and "c:/.../HexWeb" match.
                cur.execute(
                    f"""
                    SELECT id, title, working_directory, model, agent_mode,
                           created_at, hidden{latest_model_subquery}{conversation_activity_subquery}
                    FROM sessions
                    WHERE (hidden = 0 OR hidden IS NULL)
                      AND REPLACE(working_directory, '/', '\\') =
                          REPLACE(?, '/', '\\') COLLATE NOCASE
                    ORDER BY conversation_activity_at DESC
                    """,
                    (working_directory,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, title, working_directory, model, agent_mode,
                           created_at, hidden{latest_model_subquery}{conversation_activity_subquery}
                    FROM sessions
                    WHERE hidden = 0 OR hidden IS NULL
                    ORDER BY conversation_activity_at DESC
                    """
                )
            results = []
            for row in cur.fetchall():
                entry = {
                    **dict(row),
                    "created_at": self._ts_to_iso(row["created_at"]),
                    "last_activity_at": self._ts_to_iso(row["conversation_activity_at"]),
                }
                latest = row["latest_model"] if "latest_model" in row.keys() else None
                if latest:
                    entry["model"] = latest
                entry.pop("latest_model", None)
                entry.pop("conversation_activity_at", None)
                results.append(entry)
            return results
        finally:
            conn.close()

    def list_projects(self) -> list[dict]:
        """Return distinct working directories from the sessions table,
        ordered by most recently active.  Paths are normalized so that
        slash/case variants of the same directory are grouped together."""
        conn = self._connect_if_present()
        if conn is None:
            return []
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT working_directory,
                       COUNT(*) AS session_count,
                       MAX(last_activity_at) AS last_activity
                FROM sessions
                WHERE (hidden = 0 OR hidden IS NULL)
                  AND working_directory IS NOT NULL
                  AND working_directory != ''
                GROUP BY REPLACE(working_directory, '/', '\\') COLLATE NOCASE
                ORDER BY last_activity DESC
                """
            )
            results = []
            for row in cur.fetchall():
                path = os.path.normpath(row["working_directory"])
                name = os.path.basename(path) or path
                results.append(
                    {
                        "path": path,
                        "name": name,
                        "session_count": row["session_count"],
                        "last_activity_at": self._ts_to_iso(row["last_activity"]),
                    }
                )
            return results
        finally:
            conn.close()

    def get_session(self, session_id: str) -> dict | None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cur.fetchone()
            if not row:
                return None
            data = dict(row)
            data["created_at"] = self._ts_to_iso(data.get("created_at"))
            data["last_activity_at"] = self._ts_to_iso(data.get("last_activity_at"))
            # Usage records without a model_id fall back to session["model"].
            # Prefer the latest inference over the stale creation-time value.
            latest = self._latest_inference_model(cur, session_id)
            if latest:
                data["model"] = latest
            return data
        finally:
            conn.close()

    @staticmethod
    def _latest_inference_model(cur: sqlite3.Cursor, session_id: str) -> str | None:
        """Return the ``generation_model`` from the most recent assistant
        message in the session, or ``None`` if there are no assistant
        messages with a model recorded."""
        cur.execute(
            """
            SELECT json_extract(mn.chat_message, '$.metadata.generation_model') AS gm
            FROM message_nodes mn
            WHERE mn.session_id = ?
              AND json_extract(mn.chat_message, '$.role') = 'assistant'
              AND json_extract(mn.chat_message, '$.metadata.generation_model') IS NOT NULL
            ORDER BY mn.row_id DESC
            LIMIT 1
            """,
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row["gm"] if row["gm"] else None

    # -- message nodes -------------------------------------------------------

    def get_usage_turns(self, session_id: str) -> dict[Any, tuple[int, Any]]:
        """Assign all branches to real main-chain turns without counting branch prompts.

        Parent links and copied message ids take precedence. Detached sidekick
        contexts use original message timestamps; replay/import times are unsafe.
        A zero turn means the original boundary could not be recovered.
        """
        conn = self._connect()
        try:
            turns, _ = self._usage_turn_assignments(conn, session_id)
            return turns
        finally:
            conn.close()

    @staticmethod
    def _usage_turn_assignments(
        conn: sqlite3.Connection, session_id: str,
    ) -> tuple[dict[Any, tuple[int, Any]], dict[Any, int]]:
        """Share main-turn boundaries between inference pricing and history display."""
        try:
            session = conn.execute(
                "SELECT main_chain_id FROM sessions WHERE id = ?", (session_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return {}, {}
        if not session or session["main_chain_id"] is None:
            return {}, {}
        rows = conn.execute(
            """SELECT node_id, parent_node_id,
                      json_extract(chat_message, '$.role') AS role,
                      json_extract(chat_message, '$.content') AS content,
                      json_extract(chat_message, '$.message_id') AS message_id,
                      json_extract(chat_message, '$.metadata.is_user_input') AS is_user_input,
                      json_extract(chat_message, '$.metadata.created_at') AS message_time
               FROM message_nodes WHERE session_id = ? ORDER BY row_id""",
            (session_id,),
        ).fetchall()
        nodes = {row["node_id"]: row for row in rows}
        chain = []
        seen = set()
        node_id = session["main_chain_id"]
        while node_id in nodes and node_id not in seen:
            seen.add(node_id)
            chain.append(nodes[node_id])
            node_id = nodes[node_id]["parent_node_id"]
        turns: dict[Any, tuple[int, Any]] = {}
        prompts: dict[Any, int] = {}
        copied_inputs = {}
        boundaries = []
        current = (0, None)
        for node in reversed(chain):
            if (node["role"] == "user" and node["is_user_input"] != 0
                    and not is_compaction_prompt(decode_json(node["content"]))):
                current = (current[0] + 1, timestamp_to_iso(node["message_time"]))
                prompts[node["node_id"]] = current[0]
                if node["message_id"] is not None:
                    copied_inputs[node["message_id"]] = current
                timestamp = _timestamp(current[1])
                if timestamp is not None:
                    boundaries.append((timestamp, current))
            turns[node["node_id"]] = current
        boundaries.sort(key=lambda boundary: boundary[0])
        times = [boundary[0] for boundary in boundaries]
        for node in rows:
            copied = copied_inputs.get(node["message_id"])
            if copied is not None:
                turns[node["node_id"]] = copied
                if node["role"] == "user":
                    prompts[node["node_id"]] = copied[0]
        for node in rows:
            path = []
            visited = set()
            node_id = node["node_id"]
            while node_id in nodes and node_id not in turns and node_id not in visited:
                visited.add(node_id)
                path.append(node_id)
                node_id = nodes[node_id]["parent_node_id"]
            inherited = turns.get(node_id)
            for pending in reversed(path):
                if inherited is None or inherited[0] == 0:
                    timestamp = _timestamp(timestamp_to_iso(nodes[pending]["message_time"]))
                    index = bisect_right(times, timestamp) - 1 if timestamp is not None else -1
                    inherited = boundaries[index][1] if index >= 0 else (0, None)
                turns[pending] = inherited
        return turns, prompts

    def get_messages(self, session_id: str) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            # Some Devin versions omit message_id on user input. Retain those rows
            # while deduplicating identified messages to their latest row.
            cur.execute(
                """
                WITH nodes AS (
                    SELECT mn.row_id, mn.node_id, mn.parent_node_id,
                           mn.chat_message, mn.created_at, mn.metadata,
                           json_extract(mn.chat_message, '$.role') AS extracted_role,
                           json_extract(mn.chat_message, '$.message_id') AS extracted_message_id
                    FROM message_nodes mn
                    WHERE mn.session_id = ?
                ),
                latest AS (
                    SELECT extracted_message_id, MAX(row_id) AS latest_row_id
                    FROM nodes
                    WHERE extracted_message_id IS NOT NULL
                    GROUP BY extracted_message_id
                )
                SELECT nodes.row_id, nodes.node_id, nodes.parent_node_id,
                       nodes.chat_message, nodes.created_at, nodes.metadata
                FROM nodes
                LEFT JOIN latest
                  ON latest.extracted_message_id = nodes.extracted_message_id
                WHERE nodes.extracted_role != 'system'
                  AND (
                    nodes.extracted_message_id IS NULL
                    OR nodes.row_id = latest.latest_row_id
                  )
                ORDER BY nodes.row_id
                """,
                (session_id,),
            )
            messages = project_messages(cur.fetchall(), decoder=self)
            _, prompts = self._usage_turn_assignments(conn, session_id)
            for message in messages:
                if message["role"] == "user":
                    # Zero marks an internal or unidentified prompt. Copied main
                    # inputs retain the same index as their original boundary.
                    message["turn_index"] = prompts.get(message["node_id"], 0)
            return messages
        finally:
            conn.close()

    def get_latest_main_chain_inference(self, session_id: str) -> dict | None:
        """Return ``{"model_id", "metrics"}`` for the newest main-agent inference.

        Walks parent links back from ``sessions.main_chain_id`` so sidekick and
        subagent chains, side branches and compaction inferences are skipped.
        Returns ``None`` when the session has no main chain or no such inference.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT main_chain_id FROM sessions WHERE id = ?", (session_id,)
                )
            except sqlite3.OperationalError:
                return None
            row = cur.fetchone()
            if not row or row["main_chain_id"] is None:
                return None
            cur.execute(
                "SELECT node_id, parent_node_id FROM message_nodes WHERE session_id = ?",
                (session_id,),
            )
            parents = {r["node_id"]: r["parent_node_id"] for r in cur.fetchall()}
            node = row["main_chain_id"]
            seen = set()
            while node is not None and node in parents and node not in seen:
                seen.add(node)
                cur.execute(
                    "SELECT chat_message FROM message_nodes WHERE session_id = ? AND node_id = ?",
                    (session_id, node),
                )
                node_row = cur.fetchone()
                cm = self._safe_json(node_row["chat_message"]) if node_row else None
                if isinstance(cm, dict) and cm.get("role") == "assistant":
                    chat_metadata = cm.get("metadata")
                    if not isinstance(chat_metadata, dict):
                        chat_metadata = {}
                    metrics = self._normalize_metrics(chat_metadata.get("metrics"))
                    model_id = (
                        chat_metadata.get("generation_model")
                        or chat_metadata.get("model_id")
                        or chat_metadata.get("model")
                        or cm.get("model_id")
                        or cm.get("model")
                    )
                    if metrics and model_id != "compactor":
                        return {
                            "model_id": str(model_id) if model_id is not None else None,
                            "metrics": metrics,
                        }
                node = parents[node]
            return None
        finally:
            conn.close()

    @staticmethod
    def _image_metadata(message: Any) -> list[dict]:
        return image_metadata(message)

    def get_message_image(self, row_id: int, index: int) -> tuple[bytes, str] | None:
        """Decode a single stored image on demand, never in history/WS payloads."""
        if row_id < 1 or index < 0:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT chat_message FROM message_nodes WHERE row_id = ?", (row_id,)
            ).fetchone()
            message = self._safe_json(row["chat_message"]) if row else None
            images = message.get("images") if isinstance(message, dict) else None
            if not isinstance(images, list) or index >= len(images):
                return None
            image = images[index]
            if not isinstance(image, dict):
                return None
            mime = image_mime_type(image.get("mime_type"))
            data = image.get("base64_data")
            if not mime or not isinstance(data, str):
                return None
            try:
                decoded = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                return None
            return (decoded, mime) if decoded else None
        finally:
            conn.close()

    def get_reasoning_steps(self, session_id: str) -> list[dict]:
        """Read reasoning from assistant ``chat_message.thinking``.

        Keep the latest row per message id to avoid duplicate panels from
        drafts/replays, as in :meth:`get_messages`.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            turn_nodes, turn_message_ids = self._latest_turn_nodes(cur, session_id)
            cur.execute(
                """
                WITH nodes AS (
                    SELECT mn.row_id, mn.node_id, mn.chat_message, mn.created_at,
                           json_extract(mn.chat_message, '$.role') AS extracted_role,
                           json_extract(mn.chat_message, '$.message_id') AS extracted_message_id,
                           json_type(mn.chat_message, '$.thinking') AS thinking_type
                    FROM message_nodes mn
                    WHERE mn.session_id = ?
                ),
                latest AS (
                    SELECT extracted_message_id, MAX(row_id) AS latest_row_id
                    FROM nodes
                    WHERE extracted_message_id IS NOT NULL
                    GROUP BY extracted_message_id
                )
                SELECT nodes.row_id, nodes.node_id, nodes.chat_message, nodes.created_at
                FROM nodes
                LEFT JOIN latest
                  ON latest.extracted_message_id = nodes.extracted_message_id
                WHERE nodes.extracted_role = 'assistant'
                  AND nodes.thinking_type IS NOT NULL
                  AND (
                    nodes.extracted_message_id IS NULL
                    OR nodes.row_id = latest.latest_row_id
                  )
                ORDER BY nodes.row_id
                """,
                (session_id,),
            )
            return project_reasoning(
                cur.fetchall(), turn_nodes=turn_nodes,
                turn_message_ids=turn_message_ids, decoder=self,
            )
        finally:
            conn.close()

    def get_message_signature(self, session_id: str) -> str:
        """Return a newest-row signature for live DB polling.

        Assistant draft updates append rows. Avoid loading and JSON-decoding
        the full conversation on every idle poll.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT row_id, LENGTH(chat_message) AS content_length, created_at
                FROM message_nodes
                WHERE session_id = ?
                ORDER BY row_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return "empty"
            return f"{row['row_id']}:{row['content_length']}:{row['created_at']}"
        finally:
            conn.close()

    def get_turn_tip_state(self, session_id: str) -> dict | None:
        """Return the role/finish_reason of the newest ``message_nodes`` row.

        Used by the WebSocket layer as a provisional turn-settlement fallback.
        The CLI persists ``metadata.finish_reason`` on assistant messages
        before answering ``session/prompt``, including intermediate messages
        that can precede internal compaction and more output.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT chat_message
                FROM message_nodes
                WHERE session_id = ?
                ORDER BY row_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            cm = self._safe_json(row["chat_message"])
            if not isinstance(cm, dict):
                return None
            metadata = cm.get("metadata") or {}
            finish_reason = (
                metadata.get("finish_reason") if isinstance(metadata, dict) else None
            )
            return {
                "role": cm.get("role"),
                "finish_reason": str(finish_reason)
                if finish_reason is not None
                else None,
            }
        finally:
            conn.close()

    # -- rendered commits ----------------------------------------------------

    def get_rendered_commits(self, session_id: str) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT sequence_number, rendered_html, created_at
                FROM rendered_commits
                WHERE session_id = ?
                ORDER BY sequence_number
                """,
                (session_id,),
            )
            return [
                {
                    "sequence_number": row["sequence_number"],
                    "rendered_html": row["rendered_html"],
                    "created_at": self._ts_to_iso(row["created_at"]),
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    # -- prompt history ------------------------------------------------------

    def get_prompt_history(self, limit: int = 50) -> list[dict]:
        conn = self._connect_if_present()
        if conn is None:
            return []
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, content, timestamp, session_id, is_shell
                FROM prompt_history
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()


# -- live DB watcher --------------------------------------------------------

DBWatchCallback = Callable[[str, list[dict]], Awaitable[None]]


class DBWatcher:
    """Poll newest-row signatures and emit full history only after a change.

    Back off while idle to avoid repeatedly loading long conversations.
    ``start_watching`` wakes an existing watcher for a new prompt.
    """

    def __init__(self, db_reader: "DBReader"):
        self.db_reader = db_reader
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_sig: dict[str, str] = {}
        self._wake_events: dict[str, asyncio.Event] = {}

    async def watch(
        self,
        session_id: str,
        callback: DBWatchCallback,
        interval: float = 1.0,
        max_idle_interval: float = 5.0,
    ) -> None:
        wake_event = self._wake_events[session_id]
        delay = interval
        try:
            while True:
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=delay)
                    wake_event.clear()
                    delay = interval
                except asyncio.TimeoutError:
                    pass

                try:
                    sig = self.db_reader.get_message_signature(session_id)
                    if sig != self._last_sig.get(session_id):
                        self._last_sig[session_id] = sig
                        messages = self.db_reader.get_messages(session_id)
                        await callback(session_id, messages)
                        delay = interval
                    else:
                        delay = min(max_idle_interval, max(interval, delay * 1.5))
                except Exception:
                    delay = min(max_idle_interval, max(interval, delay * 1.5))
        except asyncio.CancelledError:
            pass

    def start_watching(
        self,
        session_id: str,
        callback: DBWatchCallback,
        interval: float = 1.0,
    ) -> None:
        existing = self._tasks.get(session_id)
        if existing and not existing.done():
            # A new prompt has started. Wake the idle/backed-off watcher so it
            # checks for persisted reasoning/messages immediately.
            self._wake_events[session_id].set()
            return

        wake_event = asyncio.Event()
        wake_event.set()
        self._wake_events[session_id] = wake_event
        self._tasks[session_id] = asyncio.create_task(
            self.watch(session_id, callback, interval)
        )

    def stop_watching(self, session_id: str) -> None:
        task = self._tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        self._wake_events.pop(session_id, None)
        self._last_sig.pop(session_id, None)

    def stop_all(self) -> None:
        for session_id in list(self._tasks):
            self.stop_watching(session_id)

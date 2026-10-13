from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from server.db_reader import DBReader


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            working_directory TEXT NOT NULL,
            model TEXT NOT NULL,
            agent_mode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            hidden INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE message_nodes (
            row_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            node_id TEXT,
            parent_node_id TEXT,
            chat_message TEXT,
            created_at TEXT,
            metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "session-1",
                "Existing conversation",
                "/project",
                "old-model",
                "bypass",
                "2026-07-14T09:59:00Z",
                "2026-08-24T04:29:14Z",
                0,
            ),
            (
                "empty-session",
                "Empty conversation",
                "/project",
                "old-model",
                "bypass",
                "2026-07-13T09:00:00Z",
                "2026-08-24T04:30:00Z",
                0,
            ),
        ],
    )
    rows = [
        (
            1,
            "session-1",
            "n1",
            None,
            json.dumps(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Please inspect the project"}],
                    "metadata": {"created_at": "2026-07-14T10:00:00.123456700Z"},
                }
            ),
            "2026-07-14T10:00:00Z",
            "{}",
        ),
        (
            2,
            "session-1",
            "n2",
            "n1",
            json.dumps(
                {
                    "message_id": "assistant-1",
                    "role": "assistant",
                    "content": "old draft",
                    "metadata": {"created_at": "2026-07-14T10:00:01Z"},
                }
            ),
            "2026-07-14T10:00:01Z",
            "{}",
        ),
        (
            3,
            "session-1",
            "n3",
            "n1",
            json.dumps(
                {
                    "message_id": "assistant-1",
                    "role": "assistant",
                    "content": {"text": "latest answer"},
                    "thinking": {"thinking": "I should use the latest row.", "signature": ""},
                    "metadata": {
                        "created_at": "2026-07-14T10:00:02Z",
                        "metrics": {"input_tokens": 10, "output_tokens": 5, "tpot_ms": 3},
                    },
                }
            ),
            "2026-07-14T10:00:02Z",
            json.dumps({"unstable_internal_field": [1, 2, 3]}),
        ),
    ]
    conn.executemany(
        "INSERT INTO message_nodes VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()


def test_get_messages_keeps_rows_without_message_id_and_normalizes_content(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    reader = DBReader(str(db_path))

    messages = reader.get_messages("session-1")

    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Please inspect the project"
    assert messages[0]["created_at"] == "2026-07-14T10:00:00.123456700Z"
    assert messages[1]["content"] == "latest answer"
    assert messages[1]["metrics"] == {"input_tokens": 10, "output_tokens": 5}
    assert messages[1]["metadata"] is None


def test_list_sessions_uses_conversation_content_time_instead_of_cli_activity(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    reader = DBReader(str(db_path))

    sessions = reader.list_sessions()

    assert [session["id"] for session in sessions] == ["session-1", "empty-session"]
    assert sessions[0]["last_activity_at"] == "2026-07-14T10:00:02Z"
    assert sessions[1]["last_activity_at"] == "2026-07-13T09:00:00Z"


def test_get_reasoning_steps_reads_persisted_thinking_and_metrics(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    reader = DBReader(str(db_path))

    steps = reader.get_reasoning_steps("session-1")

    assert len(steps) == 1
    assert steps[0]["reasoning_content"] == "I should use the latest row."
    assert steps[0]["timestamp"] == "2026-07-14T10:00:02Z"
    assert steps[0]["metrics"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    assert steps[0]["tool_calls"] == []
    assert steps[0]["message"] == reader.get_messages("session-1")[-1]["content"]


def test_reasoning_keeps_its_answer_when_generation_finishes_later(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    with sqlite3.connect(db_path) as conn:
        message = json.loads(conn.execute(
            "SELECT chat_message FROM message_nodes WHERE row_id = 3"
        ).fetchone()[0])
        message["content"] = "Final summary."
        message["metadata"]["created_at"] = "2026-10-07T23:51:50.146263200Z"
        conn.execute(
            "UPDATE message_nodes SET chat_message = ? WHERE row_id = 3",
            (json.dumps(message),),
        )

    step = DBReader(str(db_path)).get_reasoning_steps("session-1")[0]
    assert step["message"] == "Final summary."
    assert step["timestamp"] == "2026-10-07T23:51:50.146263200Z"


def _replace_metrics(path: Path, metrics: dict) -> None:
    with sqlite3.connect(path) as conn:
        message = json.loads(conn.execute(
            "SELECT chat_message FROM message_nodes WHERE row_id = 3"
        ).fetchone()[0])
        message["metadata"]["metrics"] = metrics
        conn.execute(
            "UPDATE message_nodes SET chat_message = ? WHERE row_id = 3",
            (json.dumps(message),),
        )


@pytest.mark.parametrize("ttft_ms", [6621, 7000, None])
def test_throughput_uses_full_inference_time_for_buffered_output(tmp_path: Path, ttft_ms):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    # Regression: a real Devin block reported 282k tok/s after all but 1 ms
    # of its inference had already elapsed before the first output arrived.
    _replace_metrics(db_path, {
        "output_tokens": 282,
        "ttft_ms": ttft_ms,
        "total_time_ms": 6622,
        "tokens_per_sec": 282000,
    })
    reader = DBReader(str(db_path))

    message_rate = reader.get_messages("session-1")[-1]["metrics"]["tokens_per_sec"]
    reasoning_rate = reader.get_reasoning_steps("session-1")[0]["metrics"]["tokens_per_sec"]

    assert message_rate == pytest.approx(42.5853, abs=0.0001)
    assert reasoning_rate == message_rate
    assert not any(step.get("is_throughput_sample") for step in reader.get_reasoning_steps("session-1"))


@pytest.mark.parametrize("metrics", [
    {"output_tokens": 282},
    {"output_tokens": 282, "total_time_ms": 0},
    {"output_tokens": 282, "total_time_ms": -1},
    {"output_tokens": 282, "total_time_ms": True},
    {"output_tokens": True, "total_time_ms": 6622},
    {"output_tokens": 0, "total_time_ms": 6622},
])
def test_throughput_omits_unusable_measurements_instead_of_using_stored_rate(tmp_path: Path, metrics):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    _replace_metrics(db_path, {**metrics, "tokens_per_sec": 282000})
    reader = DBReader(str(db_path))

    assert "tokens_per_sec" not in reader.get_messages("session-1")[-1]["metrics"]
    assert "tokens_per_sec" not in reader.get_reasoning_steps("session-1")[0]["metrics"]


def _make_chain_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            main_chain_id INTEGER
        );
        CREATE TABLE message_nodes (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            node_id INTEGER NOT NULL,
            parent_node_id INTEGER,
            chat_message TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?)",
        [("s", 4), ("idle", None)],
    )
    nodes = [
        (
            1,
            None,
            {"role": "user", "content": "hi"},
        ),
        (
            2,
            1,
            {
                "role": "assistant",
                "content": "answer",
                "metadata": {
                    "generation_model": "claude-opus-5-5-max",
                    "metrics": {
                        "input_tokens": 2,
                        "cache_read_tokens": 164788,
                        "cache_creation_tokens": 166,
                        "output_tokens": 2465,
                    },
                },
            },
        ),
        (3, 2, {"role": "tool", "content": "done"}),
        (
            4,
            3,
            {
                "role": "assistant",
                "content": "summary",
                "metadata": {
                    "generation_model": "compactor",
                    "metrics": {"input_tokens": 26866, "output_tokens": 4583},
                },
            },
        ),
        (5, None, {"role": "user", "content": "sidekick prompt"}),
        (
            6,
            5,
            {
                "role": "assistant",
                "content": "sidekick answer",
                "metadata": {
                    "generation_model": "swe-2-high",
                    "metrics": {
                        "input_tokens": 2012,
                        "cache_read_tokens": 197037,
                        "output_tokens": 247,
                    },
                },
            },
        ),
        (
            7,
            1,
            {
                "role": "assistant",
                "content": "side branch",
                "metadata": {
                    "generation_model": "claude-opus-5-5-max",
                    "metrics": {
                        "input_tokens": 4,
                        "cache_read_tokens": 115176,
                        "output_tokens": 1,
                    },
                },
            },
        ),
    ]
    conn.executemany(
        "INSERT INTO message_nodes "
        "(session_id, node_id, parent_node_id, chat_message, created_at, metadata) "
        "VALUES ('s', ?, ?, ?, 0, '{}')",
        [(node_id, parent, json.dumps(message)) for node_id, parent, message in nodes],
    )
    conn.commit()
    conn.close()


def test_get_latest_main_chain_inference_walks_main_chain_only(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_chain_db(db_path)
    reader = DBReader(str(db_path))

    assert reader.get_latest_main_chain_inference("s") == {
        "model_id": "claude-opus-5-5-max",
        "metrics": {
            "input_tokens": 2,
            "cache_read_tokens": 164788,
            "cache_creation_tokens": 166,
            "output_tokens": 2465,
        },
    }
    assert reader.get_latest_main_chain_inference("idle") is None


def test_get_latest_main_chain_inference_returns_none_without_column(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    reader = DBReader(str(db_path))

    assert reader.get_latest_main_chain_inference("session-1") is None


@pytest.mark.parametrize("compactor_metadata", [{"is_user_input": False}, {}])
def test_usage_turns_ignore_sidekick_users_and_follow_original_main_turn(tmp_path: Path, compactor_metadata):
    db_path = tmp_path / "sessions.db"
    _make_chain_db(db_path)
    nodes = [
        (8, 4, {"role": "user", "message_id": "second-prompt", "metadata": {
            "is_user_input": True, "created_at": "2026-10-09T13:00:00Z",
        }}),
        (9, 8, {"role": "assistant"}),
        # A detached sidekick user is not another conversation turn.
        (10, None, {"role": "user", "metadata": {"created_at": "2026-10-09T12:01:00Z"}}),
        (11, 10, {"role": "assistant", "metadata": {"created_at": "2026-10-09T12:02:00Z"}}),
        # Copied main input and descendants retain its turn, even if replayed later.
        (12, None, {"role": "user", "message_id": "first-prompt", "metadata": {
            "is_user_input": True, "created_at": "2026-10-09T14:00:00Z",
        }}),
        (13, 12, {"role": "assistant"}),
        (14, 2, {"role": "user", "metadata": {"is_user_input": True}}),
        (15, 14, {"role": "assistant"}),
        # Malformed cyclic branches terminate safely and retain unknown turn status.
        (16, 17, {"role": "assistant"}), (17, 16, {"role": "user"}),
        (18, 9, {"role": "user", "content": "Now summarize the conversation above as per the format given.",
                 "metadata": compactor_metadata}),
        (19, 18, {"role": "assistant"}),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE message_nodes SET chat_message = ? WHERE node_id = 1", (
            json.dumps({"role": "user", "message_id": "first-prompt", "metadata": {
                "created_at": "2026-10-09T12:00:00Z", "is_user_input": True,
            }}),
        ))
        conn.execute("UPDATE sessions SET main_chain_id = 19 WHERE id = 's'")
        conn.executemany(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) "
            "VALUES ('s', ?, ?, ?, 1791596210)",
            [(node_id, parent, json.dumps(message)) for node_id, parent, message in nodes],
        )
    reader = DBReader(str(db_path))
    turns = reader.get_usage_turns("s")
    assert turns[9][0] == turns[19][0] == 2
    assert turns[11][0] == turns[13][0] == turns[15][0] == 1
    assert turns[13][1] == "2026-10-09T12:00:00Z"
    assert turns[16] == turns[17] == (0, None)
    assert reader.get_usage_turns("idle") == {}
    prompts = {message["node_id"]: message["turn_index"] for message in reader.get_messages("s")
               if message["role"] == "user"}
    assert prompts[8] == 2
    assert prompts[12] == 1
    assert all(prompts[node] == 0 for node in (10, 14, 17, 18))


def test_usage_turns_without_main_chain_cannot_guess_a_turn(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    assert DBReader(str(db_path)).get_usage_turns("session-1") == {}


def test_sqlite_cost_preserves_branch_usage_and_prices_original_main_turns(tmp_path: Path):
    from types import SimpleNamespace
    from unittest.mock import patch

    from server.session_history import session_cost
    from server.transcript_watcher import TranscriptWatcher

    db_path = tmp_path / "sessions.db"
    _make_chain_db(db_path)
    def message(role, time, model=None):
        return {"role": role, "metadata": {"created_at": time, "generation_model": model,
                "metrics": {"input_tokens": 1_000_000} if model else None}}

    nodes = [
        (8, 4, message("user", "2026-10-09T13:00:00Z")),
        (9, 8, message("assistant", "2026-10-09T13:01:00Z", "lead")),
        (10, None, message("user", "2026-10-09T12:01:00Z")),
        (11, 10, message("assistant", "2026-10-09T12:02:00Z", "swe-2-high")),
        # A late persisted inference on the first branch uses first-turn prices.
        (12, 10, message("assistant", "2026-10-09T12:03:00Z", "lead")),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE message_nodes SET chat_message = ? WHERE node_id = 1",
                     (json.dumps(message("user", "2026-10-09T12:00:00Z")),))
        conn.execute("UPDATE sessions SET main_chain_id = 9 WHERE id = 's'")
        conn.execute("UPDATE message_nodes SET chat_message = ? WHERE node_id = 5",
                     (json.dumps(message("user", "2026-10-09T12:00:30Z")),))
        conn.executemany(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) "
            "VALUES ('s', ?, ?, ?, 1791596210)",
            [(node_id, parent, json.dumps(cm)) for node_id, parent, cm in nodes],
        )
    reader = DBReader(str(db_path))
    models = {"lead": {"id": "lead", "input_price": 100, "cached_input_price": 100, "output_price": 100},
              "swe-2-high": {"id": "swe-2-high", "tags": ["Free"]}}
    registry = SimpleNamespace(refresh=lambda: None, get_model=models.get)
    snapshots = [{"timestamp": f"2026-10-09T{hour}:00:00Z", "model_id": "fusion-lead-sidekick-swe-2-high",
                  "was_free": False, "input_price": price, "cached_input_price": 0.1, "output_price": 10}
                 for hour, price in (("12", 2), ("13", 4))]
    pricing = SimpleNamespace(snapshots=lambda sid: snapshots)
    with patch.object(reader, "get_session", return_value={"model": "fusion-lead-sidekick-swe-2-high"}):
        estimate = session_cost("s", db_reader=reader, transcript_watcher=TranscriptWatcher(str(tmp_path)),
                                model_registry=registry, pricing_history=pricing)
    assert [turn["turn_index"] for turn in estimate["turns"]] == [1, 2]
    lead = next(model for model in estimate["models"] if model["model_id"] == "lead")
    swe = next(model for model in estimate["models"] if model["model_id"] == "swe-2-high")
    assert lead["amount"] == 6
    assert swe["amount"] == 0
    assert swe["input_tokens"] == 1_002_012
    assert swe["free_steps"] == 2


def test_throughput_sample_selects_largest_measured_block_in_latest_main_turn(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_chain_db(db_path)

    def reasoning(message_id, size, output_tokens=1000, model="new-model-this-week"):
        return {
            "role": "assistant",
            "message_id": message_id,
            "thinking": {"thinking": message_id * size},
            "metadata": {"generation_model": model, "metrics": {
                "output_tokens": output_tokens, "total_time_ms": 20000,
                "ttft_ms": 19999, "tokens_per_sec": output_tokens * 1000,
            }},
        }

    nodes = [
        (8, 4, {"role": "user", "metadata": {"is_user_input": True}}),
        (9, 8, reasoning("large", 300)),
        (10, 9, {"role": "tool", "content": "tool result"}),
        (11, 10, {"role": "user", "metadata": {"is_user_input": False}}),
        (12, 11, reasoning("small", 100, 500)),
        (13, 12, reasoning("compaction", 1000, 6000, "compactor")),
        (14, 13, {"role": "assistant", "thinking": "unmeasured" * 1000}),
        (15, 14, {"role": "assistant", "content": "final summary"}),
        # A newer replay of node 9 shares its message id, but isn't itself on
        # the main chain. Use its final usage rather than the earlier draft.
        (16, 8, reasoning("large", 320, 1200)),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET main_chain_id = 15 WHERE id = 's'")
        for node_id in (2, 6, 7):
            conn.execute("UPDATE message_nodes SET chat_message = ? WHERE node_id = ?",
                         (json.dumps(reasoning(f"outside-{node_id}", 2000)), node_id))
        conn.executemany(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) "
            "VALUES ('s', ?, ?, ?, 0)",
            [(node_id, parent, json.dumps(message)) for node_id, parent, message in nodes],
        )
    reader = DBReader(str(db_path))
    steps = reader.get_reasoning_steps("s")
    samples = [step for step in steps if step.get("is_throughput_sample")]

    assert len(samples) == 1
    assert samples[0]["reasoning_content"] == "large" * 320
    assert samples[0]["metrics"]["tokens_per_sec"] == 60
    assert sum(step["reasoning_content"].startswith("large") for step in steps) == 1

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) "
            "VALUES ('s', 17, 15, ?, 1)",
            (json.dumps({"role": "user", "metadata": {"is_user_input": True}}),),
        )
        conn.execute("UPDATE sessions SET main_chain_id = 17 WHERE id = 's'")
    assert not any(step.get("is_throughput_sample") for step in reader.get_reasoning_steps("s"))


def test_throughput_sample_requires_a_recoverable_user_turn(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_chain_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET main_chain_id = 2 WHERE id = 's'")
        conn.execute("UPDATE message_nodes SET parent_node_id = NULL, chat_message = ? WHERE node_id = 2", (
            json.dumps({"role": "assistant", "thinking": "retained reasoning",
                        "metadata": {"metrics": {"output_tokens": 500, "total_time_ms": 10000}}}),
        ))
    steps = DBReader(str(db_path)).get_reasoning_steps("s")
    assert steps[0]["metrics"]["tokens_per_sec"] == 50
    assert not any(step.get("is_throughput_sample") for step in steps)


def test_throughput_sample_recovers_compacted_turn_using_original_timestamps(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_chain_db(db_path)
    current_prompt = {"role": "user", "metadata": {
        "is_user_input": True, "created_at": "2026-10-03T10:01:00Z",
    }}
    current_reasoning = {"role": "assistant", "thinking": "current reasoning" * 100,
                         "metadata": {"created_at": "2026-10-03T10:01:10Z", "metrics": {
                             "output_tokens": 1000, "total_time_ms": 20000,
                         }}}
    old_reasoning = {**current_reasoning, "thinking": "old replayed reasoning" * 1000,
                     "metadata": {**current_reasoning["metadata"],
                                  "created_at": "2026-10-03T10:00:01Z"}}
    nodes = [
        (8, 4, current_prompt),
        (9, None, {"role": "system", "content": "compacted context"}),
        (10, 9, current_reasoning),
        (11, 10, old_reasoning),
        # A later row may replay an older prompt. Its row/import time must
        # not override the current prompt's original timestamp.
        (12, 4, {**current_prompt, "metadata": {**current_prompt["metadata"],
                                               "created_at": "2026-10-03T10:00:00Z"}}),
        (13, 12, {**current_reasoning, "thinking": "side branch" * 1000}),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET main_chain_id = 11 WHERE id = 's'")
        conn.executemany(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) "
            "VALUES ('s', ?, ?, ?, 1791027000)",
            [(node_id, parent, json.dumps(message)) for node_id, parent, message in nodes],
        )
    samples = [step for step in DBReader(str(db_path)).get_reasoning_steps("s")
               if step.get("is_throughput_sample")]
    assert len(samples) == 1
    assert samples[0]["reasoning_content"] == current_reasoning["thinking"]
    assert samples[0]["metrics"]["tokens_per_sec"] == 50


def test_get_message_signature_changes_when_a_row_is_appended(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _make_db(db_path)
    reader = DBReader(str(db_path))

    before = reader.get_message_signature("session-1")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO message_nodes VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            4,
            "session-1",
            "n4",
            "n3",
            json.dumps({"role": "tool", "content": "done"}),
            "2026-07-14T10:00:03Z",
            "{}",
        ),
    )
    conn.commit()
    conn.close()

    assert reader.get_message_signature("session-1") != before

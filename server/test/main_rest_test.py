from __future__ import annotations

import json
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient

from server.main import app, _combined_reasoning_steps, _projects_from_visible_sessions, _session_cost, _visible_sessions


def test_combined_reasoning_keeps_transcript_details_and_database_throughput():
    transcript = {
        "step_id": 4, "reasoning_content": "A longer\nreasoning block",
        "timestamp": "2026-10-03T10:00:00Z",
        "tool_calls": [{"function_name": "read", "arguments": {"path": "app.py"}}],
        "message": "final answer", "metrics": {"completion_tokens": 1000, "prompt_tokens": 900},
    }
    database = {
        "step_id": None, "reasoning_content": "A longer reasoning block",
        "timestamp": "2026-10-03T10:00:00Z", "tool_calls": [], "message": "",
        "metrics": {"completion_tokens": 1200, "total_time_ms": 24000, "tokens_per_sec": 50},
        "is_throughput_sample": True,
    }
    with (
        patch("server.main.transcript_watcher.get_reasoning_steps", return_value=[transcript]),
        patch("server.main.db_reader.get_reasoning_steps", return_value=[database]),
    ):
        steps = _combined_reasoning_steps("s")
        assert len(steps) == 1
        assert steps[0]["step_id"] == 4
        assert steps[0]["tool_calls"] == transcript["tool_calls"]
        assert steps[0]["message"] == "final answer"
        assert steps[0]["metrics"] == {"completion_tokens": 1200, "prompt_tokens": 900,
                                       "total_time_ms": 24000, "tokens_per_sec": 50}
        assert steps[0]["is_throughput_sample"] is True
        # Watcher caches must not keep the previous turn's sample flag.
        database.pop("is_throughput_sample")
        assert not _combined_reasoning_steps("s")[0].get("is_throughput_sample")
    assert transcript["metrics"] == {"completion_tokens": 1000, "prompt_tokens": 900}


def test_history_schema_failure_is_actionable_but_health_stays_live():
    with patch("server.main.db_reader.list_sessions", side_effect=sqlite3.OperationalError("no such column: hidden")):
        client = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))
        assert client.get("/api/health").json()["status"] == "ok"
        for route in ("/api/sessions", "/api/projects"):
            response = client.get(route)
            assert response.status_code == 500
            assert "Open Diagnostics" in response.json()["detail"]
            assert "update DevinGUI" in response.json()["detail"]


@pytest.mark.parametrize("failure", [FileNotFoundError("missing database"), sqlite3.OperationalError("no such table")])
def test_transcript_reasoning_survives_unavailable_database(failure):
    transcript = {"step_id": 1, "reasoning_content": "Still available", "tool_calls": [], "message": "", "metrics": {}}
    with (
        patch("server.main.transcript_watcher.get_reasoning_steps", return_value=[transcript]),
        patch("server.main.db_reader.get_reasoning_steps", side_effect=failure),
    ):
        assert _combined_reasoning_steps("s")[0]["reasoning_content"] == "Still available"


def test_combined_reasoning_does_not_overwrite_sample_with_another_branch():
    sample = {
        "reasoning_content": "repeated reasoning", "timestamp": None,
        "metrics": {"completion_tokens": 1000, "total_time_ms": 20000, "tokens_per_sec": 50},
        "is_throughput_sample": True,
    }
    other_branch = {
        "reasoning_content": "repeated reasoning", "timestamp": None,
        "metrics": {"completion_tokens": 500, "total_time_ms": 20000, "tokens_per_sec": 25},
    }
    with (
        patch("server.main.transcript_watcher.get_reasoning_steps", return_value=[]),
        patch("server.main.db_reader.get_reasoning_steps", return_value=[sample, other_branch]),
    ):
        steps = _combined_reasoning_steps("s")
    assert len(steps) == 1
    assert steps[0]["metrics"]["tokens_per_sec"] == 50


def test_combined_reasoning_keeps_repeated_text_from_distinct_generations():
    earlier = {
        "reasoning_content": "I'm starting to write the code.",
        "timestamp": "2026-10-07T23:16:00Z", "message": "",
        "metrics": {"completion_tokens": 100, "tokens_per_sec": 20},
    }
    later = {
        **earlier, "timestamp": "2026-10-07T23:28:50Z",
        "metrics": {"completion_tokens": 1, "tokens_per_sec": 0.17},
    }
    with (
        patch("server.main.transcript_watcher.get_reasoning_steps", return_value=[]),
        patch("server.main.db_reader.get_reasoning_steps", return_value=[earlier, later]),
    ):
        steps = _combined_reasoning_steps("s")
    assert len(steps) == 2
    assert steps[0]["metrics"] == earlier["metrics"]
    assert steps[1]["metrics"] == later["metrics"]


def test_combined_reasoning_enriches_an_empty_transcript_answer_from_db():
    transcript = {
        "step_id": 4, "reasoning_content": "Preparing the final summary.",
        "timestamp": "2026-10-07T23:51:50Z", "message": "", "metrics": {},
    }
    database = {
        **transcript, "step_id": None, "message": "Final summary.",
        "timestamp": "2026-10-07T19:51:50.000000100-04:00",
    }
    with (
        patch("server.main.transcript_watcher.get_reasoning_steps", return_value=[transcript]),
        patch("server.main.db_reader.get_reasoning_steps", return_value=[database]),
    ):
        steps = _combined_reasoning_steps("s")
    assert len(steps) == 1
    assert steps[0]["message"] == "Final summary."
    assert transcript["message"] == ""


def test_models_endpoint_awaits_live_catalog_refresh():
    registry = MagicMock()
    registry.refresh_from_cli = AsyncMock()
    registry.get_all_models.return_value = [{"id": "grok-4-7-high"}]
    registry.get_selectable_models.return_value = [{"id": "grok-4-7-high"}]
    registry.get_recently_used.return_value = []
    registry.get_last_used_model.return_value = None
    registry.get_last_used_variants.return_value = {}
    registry.has_new_models.return_value = True
    registry.get_cache_info.return_value = {"path": None, "updated_at": 123}
    with patch("server.main.model_registry", registry):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/models")
    assert response.status_code == 200
    assert response.json()["selectable"] == [{"id": "grok-4-7-high"}]
    registry.refresh_from_cli.assert_awaited_once()


def test_visible_sessions_and_project_counts_exclude_archived_conversations():
    fake_db = MagicMock()
    fake_db.list_sessions.return_value = [
        {
            "id": "keep-a",
            "working_directory": "/code/project-a",
            "last_activity_at": "2026-07-14T12:00:00Z",
        },
        {
            "id": "archive-a",
            "working_directory": "/code/project-a",
            "last_activity_at": "2026-07-14T11:00:00Z",
        },
        {
            "id": "keep-b",
            "working_directory": "/code/project-b",
            "last_activity_at": "2026-07-14T10:00:00Z",
        },
    ]
    fake_archive = MagicMock()
    fake_archive.archived_session_ids.return_value = {"archive-a"}

    with (
        patch("server.main.db_reader", fake_db),
        patch("server.main.archive_manager", fake_archive),
    ):
        assert [session["id"] for session in _visible_sessions("/code/project-a")] == [
            "keep-a",
            "keep-b",
        ]
        fake_db.list_sessions.assert_called_with(working_directory="/code/project-a")

        projects = _projects_from_visible_sessions()

    assert [(project["name"], project["session_count"]) for project in projects] == [
        ("project-a", 1),
        ("project-b", 1),
    ]


def _session_cost_mocks():
    fake_db = MagicMock()
    fake_db.get_usage_turns.return_value = {}
    fake_db.get_session.return_value = {
        "model": "fusion-claude-opus-5-5-max-sidekick-swe-2-high"
    }
    fake_db.get_messages.return_value = [
        {"row_id": 1, "role": "user", "metrics": None},
        {
            "row_id": 2,
            "message_id": "lead",
            "role": "assistant",
            "model_id": "claude-opus-5-5-max",
            "metrics": {
                "input_tokens": 2,
                "cache_read_tokens": 168089,
                "cache_creation_tokens": 852,
                "output_tokens": 1425,
            },
        },
        {
            "row_id": 3,
            "message_id": "sidekick",
            "role": "assistant",
            "model_id": "swe-2-high",
            "metrics": {
                "input_tokens": 2012,
                "cache_read_tokens": 197037,
                "output_tokens": 247,
            },
        },
    ]
    fake_transcripts = MagicMock()
    fake_registry = MagicMock()
    fake_registry.get_model.side_effect = lambda mid: {
        "id": mid,
        "display_name": mid,
        "tags": [],
        "input_price": None,
        "cached_input_price": None,
        "output_price": None,
        "context_window": 1_000_000 if mid.startswith("claude") else 262_000,
    }
    fake_pricing = MagicMock()
    fake_pricing.snapshots.return_value = []
    return fake_db, fake_transcripts, fake_registry, fake_pricing


def test_session_cost_sqlite_context_uses_latest_main_chain_inference():
    fake_db, fake_transcripts, fake_registry, fake_pricing = _session_cost_mocks()
    fake_transcripts.get_usage_records.return_value = []
    fake_db.get_latest_main_chain_inference.return_value = {
        "model_id": "claude-opus-5-5-max",
        "metrics": {
            "input_tokens": 2,
            "cache_read_tokens": 168089,
            "cache_creation_tokens": 852,
            "output_tokens": 1425,
        },
    }
    with (
        patch("server.main.db_reader", fake_db),
        patch("server.main.transcript_watcher", fake_transcripts),
        patch("server.main.model_registry", fake_registry),
        patch("server.main.pricing_history", fake_pricing),
    ):
        estimate = _session_cost("s")
    assert estimate["context"] == {"used": 168943, "size": 1_000_000}
    fake_db.get_latest_main_chain_inference.assert_called_once_with("s")


def test_session_cost_sqlite_context_falls_back_to_newest_record():
    fake_db, fake_transcripts, fake_registry, fake_pricing = _session_cost_mocks()
    fake_transcripts.get_usage_records.return_value = []
    fake_db.get_latest_main_chain_inference.return_value = None
    with (
        patch("server.main.db_reader", fake_db),
        patch("server.main.transcript_watcher", fake_transcripts),
        patch("server.main.model_registry", fake_registry),
        patch("server.main.pricing_history", fake_pricing),
    ):
        estimate = _session_cost("s")
    assert estimate["context"] == {"used": 199049, "size": 262_000}


def test_session_cost_transcript_context_skips_db_lookup():
    fake_db, fake_transcripts, fake_registry, fake_pricing = _session_cost_mocks()
    fake_transcripts.get_usage_records.return_value = [
        {
            "id": 1,
            "turn_index": 1,
            "model_id": "claude-opus-5-5-max",
            "input_tokens": 500,
            "cached_input_tokens": 99_500,
            "output_tokens": 10,
            "context_tokens": 100_000,
        }
    ]
    with (
        patch("server.main.db_reader", fake_db),
        patch("server.main.transcript_watcher", fake_transcripts),
        patch("server.main.model_registry", fake_registry),
        patch("server.main.pricing_history", fake_pricing),
    ):
        estimate = _session_cost("s")
    assert estimate["context"] == {"used": 100_000, "size": 1_000_000}
    fake_db.get_latest_main_chain_inference.assert_not_called()


def test_usage_endpoint_returns_error_when_no_api_key():
    """When no windsurf_api_key is found in credentials.toml, /api/usage
    still returns 200 with a graceful ``error`` message and null fields."""
    with (
        patch("server.main.load_api_key", return_value=None),
        patch("server.main._api_key_cache", None),
    ):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["daily_used_percent"] is None
    assert body["weekly_used_percent"] is None
    assert body["weekly_reset_unix"] is None
    assert body["error"] is not None
    assert "credentials.toml" in body["error"]


def test_usage_endpoint_decodes_quota_from_provider():
    """When an api key is available, /api/usage calls UsageProvider.fetch and
    converts remaining-percent into used-percent for the frontend."""
    async def fake_fetch(self, *, timeout=15.0):
        return {
            "plan_name": "Pro",
            "plan_end_unix": 1_750_000_000,
            "daily_remaining_percent": 95,
            "weekly_remaining_percent": 90,
            "daily_reset_unix": 1_750_100_000,
            "weekly_reset_unix": 1_750_200_000,
        }

    with (
        patch("server.main.load_api_key", return_value="devin-session-token$fake-key"),
        patch("server.main._api_key_cache", None),
        patch("server.main.UsageProvider.fetch", fake_fetch),
    ):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["daily_used_percent"] == 5
    assert body["weekly_used_percent"] == 10
    assert body["weekly_reset_unix"] == 1_750_200_000
    assert body["plan_name"] == "Pro"


def test_usage_endpoint_surfaces_provider_error():
    from server.usage_provider import UsageProviderError

    async def fake_fetch(self, *, timeout=15.0):
        raise UsageProviderError("GetPlanStatus HTTP 401: bad token")

    with (
        patch("server.main.load_api_key", return_value="devin-session-token$fake-key"),
        patch("server.main._api_key_cache", None),
        patch("server.main.UsageProvider.fetch", fake_fetch),
    ):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["error"] == "GetPlanStatus HTTP 401: bad token"
    assert body["daily_used_percent"] is None


def test_browse_without_path_returns_roots_listing(tmp_path):
    """With no ``path`` query parameter, /api/browse returns the top-level
    browse targets (drive roots on Windows, ``/`` elsewhere) and an empty
    ``entries`` list."""
    response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/browse", headers={"X-DevinGUI-Browse": "1"})
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == ""
    assert body["parent"] == ""
    assert body["entries"] == []
    roots = body["roots"]
    assert isinstance(roots, list) and roots
    for root in roots:
        assert root["is_dir"] is True
        assert isinstance(root["name"], str) and root["name"]
        assert isinstance(root["path"], str) and root["path"]


def test_browse_with_path_lists_only_subdirectories(tmp_path):
    """/api/browse?path=... lists the subdirectories of the given folder and
    reports a ``parent`` for navigating up. Files are filtered out."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "a-file.txt").write_text("ignored")

    response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/browse", params={"path": str(tmp_path)}, headers={"X-DevinGUI-Browse": "1"})
    assert response.status_code == 200
    body = response.json()
    assert os.path.normcase(body["path"]) == os.path.normcase(str(tmp_path))
    assert body["parent"] == os.path.normpath(os.path.dirname(str(tmp_path)))
    names = {entry["name"] for entry in body["entries"]}
    assert names == {"alpha", "beta"}
    assert all(entry["is_dir"] for entry in body["entries"])


def test_browse_unreadable_directory_returns_error_without_500(tmp_path):
    """A directory the server cannot list returns 200 with an ``error`` field
    rather than a 5xx, so the frontend can keep the picker usable."""
    missing = tmp_path / "does-not-exist"
    response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/browse", params={"path": str(missing)}, headers={"X-DevinGUI-Browse": "1"})
    assert response.status_code == 200
    body = response.json()
    assert body["error"]
    assert body["entries"] == []


@pytest.mark.parametrize("path,target,result", [
    ("/api/sessions/s-1", "db_reader.get_session", None),
    ("/api/sessions/s-1/messages", "db_reader.get_messages", [{"role": "user"}]),
    ("/api/sessions/s-1/rendered", "db_reader.get_rendered_commits", [{"id": "commit"}]),
    ("/api/sessions/s-1/transcript", "transcript_watcher.get_transcript", {"steps": []}),
    ("/api/sessions/s-1/metrics", "transcript_watcher.get_final_metrics", {"tokens": 7}),
    ("/api/sessions/s-1/reasoning", "_combined_reasoning_steps", [{"step_id": 2}]),
    ("/api/sessions/s-1/cost", "_session_cost", {"source": "sqlite"}),
])
def test_history_routes_return_service_results_without_rewriting(path, target, result):
    with patch(f"server.main.{target}", return_value=result) as service:
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get(path)
    assert response.status_code == 200
    assert response.json() == result
    service.assert_called_once_with("s-1")


@pytest.mark.parametrize("method,path,target,operation", [
    ("get", "/api/sessions", "db_reader.list_sessions", "list_sessions"),
    ("get", "/api/projects", "db_reader.list_sessions", "list_projects"),
    ("get", "/api/prompt-history", "db_reader.get_prompt_history", "get_prompt_history"),
    ("post", "/api/sessions/s-1/archive", "archive_manager.archive_session", "archive_session"),
])
def test_rest_service_failures_keep_structured_operation_errors(method, path, target, operation):
    with (
        patch("server.main.archive_manager.archived_session_ids", return_value=set()),
        patch(f"server.main.{target}", side_effect=RuntimeError("storage unavailable")),
    ):
        response = getattr(TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)), method)(path)
    assert response.status_code == 500
    assert response.json() == {"detail": "storage unavailable", "error": operation}


def test_session_listing_and_projects_share_archive_filter_and_directory_grouping():
    sessions = [
        {"id": "older", "working_directory": "/code/a", "last_activity_at": None},
        {"id": "newer", "working_directory": "/code/a/.", "last_activity_at": "2026-10-08"},
        {"id": "hidden", "working_directory": "/code/b", "last_activity_at": "2026-10-09"},
        {"id": "no-directory", "working_directory": None, "last_activity_at": "2026-10-10"},
    ]
    with (
        patch("server.main.db_reader.list_sessions", return_value=sessions) as listing,
        patch("server.main.archive_manager.archived_session_ids", return_value={"hidden"}),
    ):
        client = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))
        response = client.get("/api/sessions", params={"cwd": "/code/a"})
        listing.assert_called_once_with(working_directory="/code/a")
        projects = client.get("/api/projects")
    assert response.json() == [sessions[0], sessions[1], sessions[3]]
    assert projects.json() == [{
        "path": os.path.normpath("/code/a"), "name": "a",
        "session_count": 2, "last_activity_at": "2026-10-08",
    }]


@pytest.mark.parametrize("result,error,status,body", [
    ({"archived": True}, None, 200, {"archived": True}),
    (None, KeyError("missing"), 404, {"detail": "Session not found", "error": "archive_session"}),
])
def test_archive_route_preserves_success_and_missing_session_responses(result, error, status, body):
    with patch("server.main.archive_manager.archive_session", return_value=result, side_effect=error) as archive:
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).post("/api/sessions/s-1/archive")
    assert response.status_code == status
    assert response.json() == body
    archive.assert_called_once_with("s-1")


@pytest.mark.parametrize("path,method,args,body", [
    ("/api/models/bulk-exclude", "exclude_models", (["a", "b"],), {"model_ids": ["a", "b"]}),
    ("/api/models/bulk-include", "include_models", (["a", "b"],), {"model_ids": ["a", "b"]}),
    ("/api/models/a/exclude", "exclude_model", ("a",), None),
    ("/api/models/a/include", "include_model", ("a",), None),
    ("/api/models/a/acknowledge", "acknowledge_new_model", ("a",), None),
])
def test_model_mutations_call_the_matching_registry_operation(path, method, args, body):
    registry = MagicMock()
    with patch("server.main.model_registry", registry):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).post(path, json=body)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    getattr(registry, method).assert_called_once_with(*args)


@pytest.mark.parametrize("body,updates", [
    (None, {}),
    ({"description": None, "vision": False, "tags": []}, {"vision": False, "tags": []}),
    ({"display_name": "Custom", "description": ""}, {"display_name": "Custom", "description": ""}),
])
def test_model_updates_preserve_empty_false_and_null_field_rules(body, updates):
    registry = MagicMock()
    with patch("server.main.model_registry", registry):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).put("/api/models/a", json=body)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    registry.update_model.assert_called_once_with("a", updates)


@pytest.mark.parametrize("body,error_type,field", [
    ({"pricing": {}}, "extra_forbidden", "pricing"),
    ({"tags": "single tag"}, "list_type", "tags"),
])
def test_invalid_model_updates_fail_validation_before_mutating_registry(body, error_type, field):
    registry = MagicMock()
    with patch("server.main.model_registry", registry):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).put("/api/models/a", json=body)
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["type"] == error_type
    assert error["loc"] == ["body", field]
    registry.update_model.assert_not_called()


def test_models_refresh_disk_then_cli_before_reading_response_fields():
    events = []
    registry = MagicMock()
    registry.refresh.side_effect = lambda: events.append("disk")

    async def refresh_cli():
        events.append("cli")

    registry.refresh_from_cli = AsyncMock(side_effect=refresh_cli)
    fields = {
        "models": ("get_all_models", [{"id": "a"}]),
        "selectable": ("get_selectable_models", [{"id": "a", "name": "A"}]),
        "recent": ("get_recently_used", ["a"]),
        "last_used": ("get_last_used_model", "a"),
        "last_used_variants": ("get_last_used_variants", {"a": "high"}),
        "has_new": ("has_new_models", True),
        "cache_info": ("get_cache_info", {"updated_at": 123}),
    }
    for field, (method, result) in fields.items():
        def read(field=field, result=result):
            events.append(field)
            return result

        getattr(registry, method).side_effect = read
    with patch("server.main.model_registry", registry):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/models")
    assert response.json() == {field: value for field, (_, value) in fields.items()}
    assert events == ["disk", "cli", *fields]


@pytest.mark.parametrize("endpoint,stdout,stderr,returncode,failure,detail", [
    ("export/preview", b"not json", b"", 0, None, "Invalid export preview"),
    ("export/preview", b"", b"", 1, None, "Export failed"),
    ("export", b"", b"  failed: \xff  ", 1, None, "failed: \ufffd"),
    ("export", b"", b"", 0, OSError("cannot spawn"), "cannot spawn"),
])
def test_export_failures_preserve_command_arguments_and_operation_errors(
    tmp_path, endpoint, stdout, stderr, returncode, failure, detail,
):
    process = MagicMock(returncode=returncode)
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    spawn = AsyncMock(return_value=process, side_effect=failure)
    options = {key: True for key in (
        "include_reasoning", "include_tool_calls", "include_tool_outputs", "include_summaries", "db_only",
    )}
    output = str(tmp_path / "saved.md")
    if endpoint == "export":
        options["output"] = output
    with (
        patch("server.main.db_reader", MagicMock(db_path="synthetic.db")),
        patch("asyncio.create_subprocess_exec", spawn),
    ):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).post(f"/api/sessions/s-1/{endpoint}", json=options)
    operation = "export_session" if endpoint == "export" else "preview_export"
    assert response.status_code == 500
    assert response.json() == {"detail": detail, "error": operation}
    args = spawn.await_args.args
    assert args[0] == sys.executable
    assert os.path.isabs(args[1])
    assert args[1].endswith(os.path.join("scripts", "export_conversation_to_md.py"))
    destination = ("-o", output) if endpoint == "export" else ("--preview-json",)
    assert args[2:] == (
        "s-1", "--db", "synthetic.db", "--exact", *destination,
        "--include-reasoning", "--include-tool-calls", "--include-tool-outputs", "--include-summaries", "--db-only",
    )
    assert not (tmp_path / "saved.md").exists()


def test_export_requires_destination_before_spawning_process():
    with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).post("/api/sessions/s-1/export", json={})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "output"]
    spawn.assert_not_awaited()


@pytest.mark.parametrize("cached,age,expected,reloaded", [
    ("cached", 59, "cached", False),
    ("cached", 60, "loaded", True),
    ("cached", 61, "loaded", True),
    (None, 1, "loaded", True),
])
def test_api_key_cache_preserves_ttl_boundary_and_missing_key_reload(cached, age, expected, reloaded):
    from server.main import _get_api_key

    with (
        patch("server.main._api_key_cache", cached),
        patch("server.main._api_key_cached_at", 100 - age),
        patch("server.main._API_KEY_CACHE_TTL", 60),
        patch("server.main.load_api_key", return_value="loaded") as load,
        patch("time.time", return_value=100),
    ):
        assert _get_api_key() == expected
    assert load.called is reloaded


def test_browse_sorts_directories_and_skips_entries_that_cannot_be_statted(tmp_path):
    (tmp_path / "Zulu").mkdir()
    (tmp_path / "unreadable").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "file.txt").write_text("not a directory")

    def is_directory(path):
        if os.path.basename(path) == "unreadable":
            raise PermissionError("cannot stat")
        return os.path.basename(path) != "file.txt"

    with (
        patch("os.listdir", return_value=["Zulu", "unreadable", "alpha", "file.txt"]),
        patch("os.path.isdir", side_effect=is_directory),
    ):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/browse", params={"path": str(tmp_path)}, headers={"X-DevinGUI-Browse": "1"})
    assert response.status_code == 200
    assert [entry["name"] for entry in response.json()["entries"]] == ["alpha", "Zulu"]


def test_unexpected_browse_failure_uses_structured_500_response(tmp_path):
    with patch("os.listdir", side_effect=RuntimeError("unexpected browse failure")):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/browse", params={"path": str(tmp_path)}, headers={"X-DevinGUI-Browse": "1"})
    assert response.status_code == 500
    assert response.json() == {"detail": "unexpected browse failure", "error": "browse"}


async def _raw_request(method, path, body=b""):
    encoded = quote(path, safe="/").encode("ascii")
    messages = []
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method.upper(), "scheme": "http", "path": unquote(encoded.decode("ascii")),
        "raw_path": encoded, "query_string": b"", "root_path": "",
        "headers": [(b"host", b"localhost"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000), "server": ("localhost", 8000),
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, payload


INVALID_SESSION_IDS = [
    "..",
    "..\\synthetic-secret",
    "..\\..\\transcripts\\synthetic-secret",
    "%2e%2e",
    "C:\\synthetic-secret",
    "\\\\server\\share\\s",
    "-dash",
    "_under",
    "has space",
    "has.dot",
    "x.json",
    "a" * 129,
    "NUL",
    "con",
    "COM1",
    "id\x00x",
    "a:b",
    "ünïcödé",
]

SESSION_ID_ROUTES = [
    ("get", "/api/sessions/{sid}", b""),
    ("get", "/api/sessions/{sid}/messages", b""),
    ("get", "/api/sessions/{sid}/rendered", b""),
    ("get", "/api/sessions/{sid}/transcript", b""),
    ("get", "/api/sessions/{sid}/reasoning", b""),
    ("get", "/api/sessions/{sid}/metrics", b""),
    ("get", "/api/sessions/{sid}/cost", b""),
    ("post", "/api/sessions/{sid}/archive", b""),
    ("post", "/api/sessions/{sid}/export", b'{"output":"x.md"}'),
    ("post", "/api/sessions/{sid}/export/preview", b"{}"),
]


@pytest.mark.parametrize("method,template,body", SESSION_ID_ROUTES)
@pytest.mark.parametrize("sid", INVALID_SESSION_IDS)
async def test_session_routes_reject_invalid_ids_before_services(method, template, body, sid):
    reader, watcher, archiver = MagicMock(), MagicMock(), MagicMock()
    with (
        patch("server.main.db_reader", reader),
        patch("server.main.transcript_watcher", watcher),
        patch("server.main.archive_manager", archiver),
        patch("server.main._session_cost") as cost,
        patch("asyncio.create_subprocess_exec") as spawn,
    ):
        status, payload = await _raw_request(method, template.replace("{sid}", sid), body)
    assert status == 422
    assert json.loads(payload)["detail"] == "Invalid session ID"
    assert reader.mock_calls == watcher.mock_calls == archiver.mock_calls == []
    assert cost.mock_calls == [] and spawn.mock_calls == []


@pytest.mark.parametrize("sid", [
    "../synthetic-secret",
    "/absolute",
    "../../transcripts/synthetic-secret",
    "//server/share/s",
])
async def test_traversal_session_ids_never_reach_services(sid):
    reader, watcher, archiver = MagicMock(), MagicMock(), MagicMock()
    with (
        patch("server.main.db_reader", reader),
        patch("server.main.transcript_watcher", watcher),
        patch("server.main.archive_manager", archiver),
        patch("server.main._session_cost") as cost,
    ):
        status, payload = await _raw_request("get", f"/api/sessions/{sid}/transcript")
    assert status in (404, 422)
    assert reader.mock_calls == watcher.mock_calls == archiver.mock_calls == []
    assert cost.mock_calls == []

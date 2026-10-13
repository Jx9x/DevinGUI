import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.pricing_history import usage_pricing_snapshot
from server.transcript_watcher import TranscriptWatcher


@pytest.mark.parametrize("internal_prompt", [
    "Conversation to summarize: earlier context",
    "Now summarize the conversation above as per the format given.",
    [{"type": "text", "text": "<summary>Earlier context</summary>"}],
])
def test_usage_keeps_compaction_in_the_original_user_turn(tmp_path, internal_prompt):
    transcript = {
        "agent": {"model_id": "lead"},
        "steps": [
            {"source": "user", "message": "First prompt", "timestamp": "2026-10-09T12:00:01Z"},
            {"source": "agent", "step_id": 1, "timestamp": "2026-10-09T12:01:00Z",
             "metrics": {"prompt_tokens": 100, "cached_tokens": 80, "completion_tokens": 5}},
            {"source": "user", "message": internal_prompt, "timestamp": "2026-10-09T12:02:00Z"},
            # Even a late inference uses the price captured for its original prompt.
            {"source": "agent", "step_id": 2, "timestamp": "2026-10-09T13:01:00Z",
             "extra": {"generation_model": "compactor"}, "metrics": {"completion_tokens": 10}},
            {"source": "user", "message": "Second prompt", "timestamp": "2026-10-09T13:00:01Z"},
            {"source": "agent", "step_id": 3, "timestamp": "2026-10-09T13:02:00Z",
             "metrics": {"prompt_tokens": 200, "completion_tokens": 20}},
        ],
    }
    watcher = TranscriptWatcher(str(tmp_path))
    with patch.object(watcher, "get_transcript", return_value=transcript):
        records = watcher.get_usage_records("s")

    assert [record["turn_index"] for record in records] == [1, 1, 2]
    assert [record["turn_timestamp"] for record in records] == [
        "2026-10-09T12:00:01Z", "2026-10-09T12:00:01Z", "2026-10-09T13:00:01Z",
    ]
    assert [record["model_id"] for record in records] == ["lead", "compactor", "lead"]
    assert records[0]["input_tokens"] == 20
    snapshots = [{"timestamp": "2026-10-09T12:00:00Z"}, {"timestamp": "2026-10-09T13:00:00Z"}]
    assert usage_pricing_snapshot(records[1], snapshots) is snapshots[0]
    assert usage_pricing_snapshot(records[2], snapshots) is snapshots[1]


@pytest.mark.parametrize("sid", [
    "..",
    "../synthetic-secret",
    "..\\synthetic-secret",
    "..\\..\\transcripts\\synthetic-secret",
    "/absolute",
    "C:\\synthetic-secret",
    "\\\\server\\share\\s",
    "a" * 129,
    "-dash",
    "_under",
    "has space",
    "has.dot",
    "x.json",
    "NUL",
    "con",
    "COM1",
    "lpt9",
    "id\x00x",
    "a/b",
    "a:b",
    "ünïcödé",
    None,
    123,
    True,
    b"s-1",
    ["s"],
    {"s": 1},
])
def test_reads_and_watching_reject_invalid_session_ids(tmp_path, sid):
    watcher = TranscriptWatcher(str(tmp_path))
    for read in (
        watcher.get_transcript,
        watcher.get_reasoning_steps,
        watcher.get_usage_records,
        watcher.get_final_metrics,
    ):
        with pytest.raises(ValueError, match="Invalid session ID"):
            read(sid)
    with pytest.raises(ValueError, match="Invalid session ID"):
        watcher.start_watching(sid, AsyncMock())
    assert watcher._tasks == {}


def test_valid_id_reads_local_and_missing_transcripts(tmp_path):
    (tmp_path / "session-1.json").write_text(
        json.dumps({"steps": [], "final_metrics": {"tokens": 7}}), encoding="utf-8"
    )
    watcher = TranscriptWatcher(str(tmp_path))
    assert watcher.get_transcript("session-1") == {
        "steps": [], "final_metrics": {"tokens": 7},
    }
    assert watcher.get_final_metrics("session-1") == {"tokens": 7}
    assert watcher.get_transcript("missing") is None
    assert watcher.get_reasoning_steps("missing") == []
    assert watcher.get_usage_records("missing") == []
    assert watcher.get_final_metrics("missing") is None


def test_resolved_escape_outside_transcripts_dir_is_rejected(tmp_path):
    watcher = TranscriptWatcher(str(tmp_path))
    real_resolve = Path.resolve

    def resolve(self):
        if self.name == "escape.json":
            return Path("/outside/escape.json")
        return real_resolve(self)

    with patch.object(Path, "resolve", resolve):
        with pytest.raises(ValueError, match="escapes"):
            watcher.get_transcript("escape")


def test_symlink_transcript_escape_is_rejected(tmp_path):
    root = tmp_path / "transcripts"
    root.mkdir()
    secret = tmp_path / "synthetic-secret.json"
    secret.write_text("{}", encoding="utf-8")
    link = root / "escape.json"
    try:
        link.symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlink creation requires privilege: {exc}")
    watcher = TranscriptWatcher(str(root))
    with pytest.raises(ValueError, match="escapes"):
        watcher.get_transcript("escape")


async def test_watch_revalidates_path_on_every_poll(tmp_path):
    watcher = TranscriptWatcher(str(tmp_path))
    calls = 0
    original = watcher._path

    def counted(sid):
        nonlocal calls
        calls += 1
        return original(sid)

    watcher._path = counted
    received = []

    async def callback(sid, steps):
        received.append(steps)

    task = asyncio.create_task(watcher.watch("s-1", callback, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls >= 2
    assert received == []


async def test_watch_skips_stats_for_invalid_id(tmp_path):
    watcher = TranscriptWatcher(str(tmp_path))
    with patch("os.path.exists") as exists:
        task = asyncio.create_task(watcher.watch("../x", AsyncMock(), interval=0.01))
        await asyncio.sleep(0.03)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    exists.assert_not_called()
    assert watcher._mtimes == {}

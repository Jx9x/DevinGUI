from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from .paths import user_config_path


def compaction_thresholds(cwd: str | None, model: str | None) -> tuple[int, int, int] | None:
    """Return ``(spawn, apply, hard)`` token limits from the matching project rule.

    Missing cwd/model, settings file, or matching rule: None.
    Multiple matches or invalid thresholds: ValueError. Read/JSON errors propagate.
    """
    if not cwd or not model:
        return None
    path = Path(cwd) / ".devin" / "devingui.local.json"
    if not path.is_file():
        return None
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    rules = settings.get("model_compaction", {})
    matches = [rule for pattern, rule in rules.items() if fnmatch.fnmatchcase(model, pattern)]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"Multiple compaction rules match {model} in {path}")
    rule = matches[0]
    if not isinstance(rule, dict) or type(rule.get("enabled", True)) is not bool:
        raise ValueError(f"Compaction rule must be an object with an optional boolean enabled field in {path}")
    if not rule.get("enabled", True):
        return None
    values = tuple(rule.get(key) for key in ("spawn_tokens", "apply_tokens", "hard_tokens"))
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError(f"Compaction thresholds must be positive integers in {path}")
    if not values[0] <= values[1] <= values[2]:
        raise ValueError(f"Compaction thresholds must satisfy spawn <= apply <= hard in {path}")
    return values


def user_config_for_compaction(spawn_tokens: int) -> dict:
    """Return user settings with a spawn override, without modifying the file."""
    path = user_config_path()
    settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    settings.setdefault("agent", {})["compaction_threshold_tokens"] = spawn_tokens
    return settings

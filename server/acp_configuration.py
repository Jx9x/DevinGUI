"""ACP session configuration policy, independent of subprocess transport."""

from __future__ import annotations

import logging
import re
from typing import Protocol

from .acp_errors import ACPError, ModelNotAppliedError


class ConfigurationClient(Protocol):
    """Cached session options and the request operation used by configuration."""

    _session_config_options: list[dict] | None

    async def _set_config_option(self, session_id: str, config_id: str, value: str) -> list[dict]: ...


_THOUGHT_SUFFIX = re.compile(r"-(none|minimal|low|medium|high|xhigh|max|ultra)$")


def model_controls(model_id: str) -> tuple[str, str | None, bool]:
    """Return ``(model_group, thought_level, fast)`` for a catalog variant.

    Since CLI 11.1, ACP's ``model`` option contains one representative per
    family/pair. Its separate controls select the variant. Fusion's sidekick
    is part of the model group; effort and speed apply to the lead only.
    ``fast`` is true for ``-fast``, ``-priority``, or SWE-2's ``-lightning``.
    """
    prefix = ""
    suffix = ""
    variant = model_id
    if model_id.startswith("fusion-") and "-sidekick-" in model_id:
        prefix = "fusion-"
        variant, sidekick = model_id[len(prefix):].split("-sidekick-", 1)
        suffix = f"-sidekick-{sidekick}"
    # Some context variants put the effort before their 1M suffix:
    # glm-5-2-max-1m selects model glm-5-2-1m with thought_level max.
    context = ""
    if variant.endswith("-1m"):
        variant, context = variant[:-3], "-1m"
    fast = variant.endswith(("-fast", "-priority")) or (
        variant.startswith("swe-2-") and variant.endswith("-lightning")
    )
    if fast:
        variant = variant.rsplit("-", 1)[0]
    match = _THOUGHT_SUFFIX.search(variant)
    thought = match.group(1) if match else None
    if match:
        variant = variant[:match.start()]
    return prefix + variant + context + suffix, thought, fast


def config_option(options: list[dict], option_id: str) -> dict | None:
    return next(
        (option for option in options if isinstance(option, dict) and option.get("id") == option_id),
        None,
    )


def option_values(option: dict) -> set[str]:
    return {
        entry["value"] for entry in option.get("options", [])
        if isinstance(entry, dict) and isinstance(entry.get("value"), str)
    }


async def apply_model(
    client: ConfigurationClient, session_id: str, model_id: str,
    *, logger: logging.Logger,
) -> str:
    """Apply a catalog variant through the ACP controls and confirm it.

    Older CLIs expose every variant directly in ``model``. Devin 11.1
    exposes a representative model, then separate ``thought_level`` and
    ``speed`` options. Do not start a turn unless every requested control
    is confirmed by the agent's response.
    """
    options = client._session_config_options
    target_model = model_id
    thought: str | None = None
    fast = False
    requested_thought = False
    requested_speed: str | None = None
    if options is not None:
        model_option = config_option(options, "model")
        if model_option is None:
            raise ModelNotAppliedError(f"agent did not offer a model control for session {session_id}")
        values = option_values(model_option)
        group, thought, fast = model_controls(model_id)
        if model_id not in values:
            matches = [value for value in values if model_controls(value)[0] == group]
            if len(matches) != 1:
                raise ModelNotAppliedError(
                    f"model '{model_id}' is not selectable in this Devin ACP session"
                )
            target_model = matches[0]
    try:
        # Set model first: changing it resets the agent's effort and speed.
        options = await client._set_config_option(session_id, "model", target_model)
        applied_model = config_option(options, "model")
        if applied_model is None:
            raise ModelNotAppliedError(
                f"agent did not report a current model after setting '{model_id}' on session {session_id}"
            )
        if applied_model.get("currentValue") != target_model:
            raise ModelNotAppliedError(
                f"agent is running '{applied_model.get('currentValue')}' "
                f"after being asked for '{model_id}' on session {session_id}"
            )
        thought_option = config_option(options, "thought_level")
        if thought and thought_option:
            if thought not in option_values(thought_option):
                raise ModelNotAppliedError(
                    f"agent does not offer thinking level '{thought}' for model '{model_id}'"
                )
            requested_thought = True
            options = await client._set_config_option(session_id, "thought_level", thought)
        elif thought and target_model != model_id:
            raise ModelNotAppliedError(
                f"agent cannot select thinking level '{thought}' for model '{model_id}'"
            )
        speed_option = config_option(options, "speed")
        if speed_option:
            speed = "fast" if fast else "standard"
            if speed not in option_values(speed_option):
                raise ModelNotAppliedError(
                    f"agent does not offer speed '{speed}' for model '{model_id}'"
                )
            requested_speed = speed
            options = await client._set_config_option(session_id, "speed", speed)
        elif fast and target_model != model_id:
            raise ModelNotAppliedError(
                f"agent cannot select fast speed for model '{model_id}'"
            )
    except ACPError as e:
        # An unrecognized model id comes back as ``-32002 Resource not
        # found`` whose ``data.uri`` carries the useful detail plus the
        # full list of accepted ids.  Keep the detail, drop the list.
        detail = e.message
        if isinstance(e.data, dict) and isinstance(e.data.get("uri"), str):
            detail = e.data["uri"].split(". Available models:")[0]
        raise ModelNotAppliedError(
            f"agent rejected model '{model_id}' for session {session_id}: {detail}"
        ) from e
    # Later replies can change earlier selections; verify all controls against the final reply.
    model_option = config_option(options, "model")
    applied = model_option.get("currentValue") if model_option else None
    if applied != target_model:
        raise ModelNotAppliedError(
            f"agent is running '{applied}' after being asked for '{model_id}' "
            f"on session {session_id}"
        )
    thought_option = config_option(options, "thought_level")
    if thought and (requested_thought or thought_option is not None):
        actual = thought_option.get("currentValue") if thought_option is not None else None
        if actual != thought:
            raise ModelNotAppliedError(
                f"agent is running thinking level '{actual}' after being asked for '{thought}' "
                f"on session {session_id}"
            )
    speed_option = config_option(options, "speed")
    if requested_speed is not None or speed_option is not None:
        actual = speed_option.get("currentValue") if speed_option is not None else None
        expected = requested_speed if requested_speed is not None else ("fast" if fast else "standard")
        if actual != expected:
            raise ModelNotAppliedError(
                f"agent is running speed '{actual}' after being asked for '{expected}' "
                f"on session {session_id}"
            )
    logger.info("ACP session %s model variant confirmed as %s", session_id, model_id)
    return model_id


async def apply_mode(
    client: ConfigurationClient, session_id: str, mode: str,
    *, logger: logging.Logger,
) -> str:
    """Set the session's ``mode`` option and return its confirmed value.

    Set after ``session/new`` or ``session/load``; ACP ignores the
    ``--permission-mode`` CLI flag.
    Missing or mismatched ``currentValue`` raises RuntimeError.
    Agent and transport errors propagate unchanged.
    """
    options = await client._set_config_option(session_id, "mode", mode)
    mode_option = config_option(options, "mode")
    applied = mode_option.get("currentValue") if mode_option else None
    if applied != mode:
        raise RuntimeError(
            f"agent is running mode '{applied}' after being asked for "
            f"'{mode}' on session {session_id}"
        )
    logger.info("ACP session %s mode confirmed as %s", session_id, applied)
    return applied

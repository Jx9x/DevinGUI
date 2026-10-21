"""Characterize ACP configuration decisions and their transport boundaries."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from server.acp_client import (
    ACPClient, ACPError, ModelNotAppliedError,
    _config_option, _model_controls, _option_values,
)
from server import acp_configuration, acp_errors


@pytest.fixture
def client():
    return ACPClient(notification_handler=AsyncMock(), cmd=["synthetic-acp"])


def options(model="family-high", thought="high", speed="standard", values=None, speed_values=None):
    result = [{"id": "model", "currentValue": model,
               "options": [{"value": value} for value in (["family-high"] if values is None else values)]}]
    if thought is not None:
        result.append({"id": "thought_level", "currentValue": thought,
                       "options": [{"value": value} for value in ("low", "high", "max")]})
    if speed is not None:
        result.append({"id": "speed", "currentValue": speed,
                       "options": [{"value": value} for value in (["standard", "fast"] if speed_values is None else speed_values)]})
    return result


def replies(client, values):
    client._send_request = AsyncMock(side_effect=[
        value if isinstance(value, BaseException) else {"configOptions": value}
        for value in values
    ])
    return client._send_request


def controls_sent(request):
    return [(call.args[1]["configId"], call.args[1]["value"]) for call in request.await_args_list]


@pytest.mark.parametrize("variant,expected", [
    ("family", ("family", None, False)),
    ("family-none", ("family", "none", False)),
    ("family-ultra-priority", ("family", "ultra", True)),
    ("family-max-fast-1m", ("family-1m", "max", True)),
    ("family-max-1m-fast", ("family-max-1m", None, True)),
    ("swe-2-high-lightning", ("swe-2", "high", True)),
    ("swe-2-medium-lightning", ("swe-2", "medium", True)),
    ("swe-2-max-lightning", ("swe-2", "max", True)),
    ("swe-1.7-lightning", ("swe-1.7-lightning", None, False)),
    ("fusion-lead-xhigh-fast-sidekick-helper-max", ("fusion-lead-sidekick-helper-max", "xhigh", True)),
    ("family-unknown", ("family-unknown", None, False)),
])
def test_model_controls_preserve_suffix_order_and_sidekick(variant, expected):
    assert _model_controls(variant) == expected


@pytest.mark.parametrize("level", ["medium", "high", "max"])
async def test_swe2_lightning_selects_and_confirms_effort_and_fast_speed(client, level):
    def swe_options(thought="high", speed="standard"):
        result = options(model="swe-2-high", thought=thought, speed=speed, values=["swe-2-high"])
        result[1]["options"] = [{"value": value} for value in ("medium", "high", "max")]
        return result

    client._session_config_options = swe_options()
    request = replies(client, [swe_options(), swe_options(level), swe_options(level, "fast")])
    variant = f"swe-2-{level}-lightning"
    assert await client.set_session_model("session", variant) == variant
    assert controls_sent(request) == [
        ("model", "swe-2-high"), ("thought_level", level), ("speed", "fast"),
    ]


@pytest.mark.parametrize("speed_values,final_speed", [(["standard"], "standard"), (["standard", "fast"], "standard")])
async def test_swe2_lightning_rejects_missing_or_unconfirmed_fast_speed(client, speed_values, final_speed):
    client._session_config_options = options(model="swe-2-high", values=["swe-2-high"])
    final = options(model="swe-2-high", speed=final_speed, speed_values=speed_values)
    replies(client, [options(model="swe-2-high"), final, final])
    with pytest.raises(ModelNotAppliedError, match="speed"):
        await client.set_session_model("session", "swe-2-high-lightning")


def test_config_option_uses_first_matching_dictionary():
    first = {"id": "model", "currentValue": "first"}
    assert _config_option([None, "model", first, {"id": "model", "currentValue": "later"}], "model") is first
    assert _config_option([first], "missing") is None


def test_option_values_keep_only_strings_and_deduplicate():
    assert _option_values({"options": [None, "ignored", {}, {"value": False},
                                       {"value": ""}, {"value": "first"}, {"value": "first"}]}) == {"", "first"}


@pytest.mark.parametrize("result", [None, [], {"configOptions": None}, {"configOptions": ()}])
def test_invalid_cache_updates_preserve_previous_options(client, result):
    previous = options()
    client._session_config_options = previous
    client._remember_config_options(result)
    assert client._session_config_options is previous


def test_cache_updates_retain_supplied_list_identity_and_permissive_entries(client):
    supplied = [None, "unvalidated", {"id": "model"}]
    client._remember_config_options({"configOptions": supplied})
    assert client._session_config_options is supplied
    client._remember_config_options({"configOptions": []})
    assert client._session_config_options == []


@pytest.mark.parametrize("result", [None, {}, {"configOptions": "invalid"}])
async def test_setting_option_does_not_use_stale_cache_as_confirmation(client, result):
    previous = options()
    client._session_config_options = previous
    client._send_request = AsyncMock(return_value=result)
    assert await client._set_config_option("session", "model", "selected") == []
    assert client._session_config_options is previous
    client._send_request.assert_awaited_once_with(
        "session/set_config_option", {"sessionId": "session", "configId": "model", "value": "selected"}, timeout=30.0,
    )


@pytest.mark.parametrize("method", ["new_session", "load_session"])
@pytest.mark.parametrize("ending", ["reply", "missing_options", "error"])
async def test_session_creation_resets_cache_before_request_and_remembers_reply(client, method, ending):
    client._session_config_options = options()
    supplied = options(model="new-model")
    result = {"sessionId": "created"}
    if ending == "reply":
        result["configOptions"] = supplied

    async def request(name, params, timeout):
        assert client._session_config_options is None
        assert name == ("session/new" if method == "new_session" else "session/load")
        assert params == ({"cwd": "synthetic-cwd", "mcpServers": []} if method == "new_session"
                          else {"sessionId": "session", "cwd": "synthetic-cwd", "mcpServers": []})
        assert timeout == (30.0 if method == "new_session" else 60.0)
        if ending == "error":
            raise RuntimeError("request failed")
        return result

    client._send_request = request
    call = client.new_session("synthetic-cwd") if method == "new_session" else client.load_session("session", "synthetic-cwd")
    if ending == "error":
        with pytest.raises(RuntimeError, match="request failed"):
            await call
    else:
        assert await call == ("created" if method == "new_session" else result)
    assert client._session_config_options is (supplied if ending == "reply" else None)


@pytest.mark.parametrize("suppressed", [False, True])
async def test_config_notifications_refresh_cache_before_existing_suppression_policy(client, suppressed):
    supplied = options()
    client.suppress_updates = suppressed

    async def notified(message):
        assert client._session_config_options is supplied

    client.notification_handler = AsyncMock(side_effect=notified)
    message = {"method": "session/update", "params": {"update": {
        "sessionUpdate": "config_option_update", "configOptions": supplied,
    }}}
    await client._handle_message(message)
    assert client._session_config_options is supplied
    if suppressed:
        client.notification_handler.assert_not_awaited()
    else:
        client.notification_handler.assert_awaited_once_with(message)


@pytest.mark.parametrize("cached,error", [
    ([], "agent did not offer a model control for session session"),
    (options(values=[]), "model 'family-max' is not selectable in this Devin ACP session"),
    (options(values=["other-high"]), "model 'family-max' is not selectable in this Devin ACP session"),
    (options(values=["family-high", "family-low"]), "model 'family-max' is not selectable in this Devin ACP session"),
])
async def test_model_preflight_failure_sends_no_requests(client, cached, error):
    client._session_config_options = cached
    client._send_request = AsyncMock()
    with pytest.raises(ModelNotAppliedError) as caught:
        await client.set_session_model("session", "family-max")
    assert str(caught.value) == error
    client._send_request.assert_not_awaited()
    assert client._session_config_options is cached


@pytest.mark.parametrize("variant,responses,expected_controls,error", [
    ("family-max", [options(), options(thought="max", speed_values=["fast"])],
     [("model", "family-high"), ("thought_level", "max")],
     "agent does not offer speed 'standard' for model 'family-max'"),
    ("family-max", [[]], [("model", "family-high")],
     "agent did not report a current model after setting 'family-max' on session session"),
    ("family-max", [options(model="other-high")], [("model", "family-high")],
     "agent is running 'other-high' after being asked for 'family-max' on session session"),
    ("family-max", [options(thought=None)], [("model", "family-high")],
     "agent cannot select thinking level 'max' for model 'family-max'"),
    ("family-ultra", [options()], [("model", "family-high")],
     "agent does not offer thinking level 'ultra' for model 'family-ultra'"),
    ("family-max-fast", [options(), options(thought="max", speed=None)],
     [("model", "family-high"), ("thought_level", "max")],
     "agent cannot select fast speed for model 'family-max-fast'"),
    ("family-max", [options(), options(model="drifted", thought="max", speed=None)],
     [("model", "family-high"), ("thought_level", "max")],
     "agent is running 'drifted' after being asked for 'family-max' on session session"),
    ("family-max", [options(), options(thought="high", speed=None)],
     [("model", "family-high"), ("thought_level", "max")],
     "agent is running thinking level 'high' after being asked for 'max' on session session"),
    ("family-max-fast", [options(), options(thought="max"), options(thought="max", speed="standard")],
     [("model", "family-high"), ("thought_level", "max"), ("speed", "fast")],
     "agent is running speed 'standard' after being asked for 'fast' on session session"),
])
async def test_model_reply_failures_preserve_control_order_and_exact_errors(client, variant, responses, expected_controls, error, caplog):
    client._session_config_options = options()
    request = replies(client, responses)
    with caplog.at_level(logging.INFO, logger="server.acp_client"):
        with pytest.raises(ModelNotAppliedError) as caught:
            await client.set_session_model("session", variant)
    assert str(caught.value) == error
    assert controls_sent(request) == expected_controls
    assert client._session_config_options is responses[-1]
    assert "confirmed as" not in caplog.text


@pytest.mark.parametrize("stage", ["model", "thought_level", "speed"])
async def test_agent_errors_at_each_control_keep_cause_and_trim_available_models(client, stage):
    failure = ACPError({"code": -32002, "message": "fallback", "data": {
        "uri": "precise detail. Available models: private-list",
    }})
    prior = {"model": [], "thought_level": [options()], "speed": [options(), options(thought="max")]}[stage]
    client._session_config_options = options()
    request = replies(client, [*prior, failure])
    with pytest.raises(ModelNotAppliedError) as caught:
        await client.set_session_model("session", "family-max-fast")
    assert str(caught.value) == "agent rejected model 'family-max-fast' for session session: precise detail"
    assert caught.value.__cause__ is failure
    assert controls_sent(request)[-1][0] == stage


@pytest.mark.parametrize("data", [None, "invalid", {"uri": 5}])
async def test_agent_error_detail_falls_back_to_original_message(client, data):
    failure = ACPError({"code": -32002, "message": "original detail", "data": data})
    replies(client, [failure])
    with pytest.raises(ModelNotAppliedError) as caught:
        await client.set_session_model("session", "family-max")
    assert str(caught.value) == "agent rejected model 'family-max' for session session: original detail"
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("stage,failure", [
    ("model", TimeoutError("deadline")),
    ("thought_level", asyncio.CancelledError()),
    ("speed", RuntimeError("transport failed")),
])
async def test_other_transport_failures_stop_controls_without_wrapping(client, stage, failure):
    prior = {"model": [], "thought_level": [options()], "speed": [options(), options(thought="max")]}[stage]
    client._session_config_options = options()
    request = replies(client, [*prior, failure])
    with pytest.raises(type(failure)) as caught:
        await client.set_session_model("session", "family-max-fast")
    assert caught.value is failure
    assert controls_sent(request)[-1][0] == stage


@pytest.mark.parametrize("missing,expected_error", [
    ("thought", "agent is running thinking level 'None' after being asked for 'max' on session session"),
    ("speed", "agent is running speed 'None' after being asked for 'fast' on session session"),
    ("both", "agent is running thinking level 'None' after being asked for 'max' on session session"),
])
async def test_final_reply_missing_requested_controls_fails_confirmation(client, missing, expected_error, caplog):
    final = options(thought=None if missing in {"thought", "both"} else "max",
                    speed=None if missing in {"speed", "both"} else "fast")
    client._session_config_options = options()
    request = replies(client, [options(), options(thought="max"), final])
    with caplog.at_level(logging.INFO, logger="server.acp_client"):
        with pytest.raises(ModelNotAppliedError) as caught:
            await client.set_session_model("session", "family-max-fast")
    assert str(caught.value) == expected_error
    assert controls_sent(request) == [("model", "family-high"), ("thought_level", "max"), ("speed", "fast")]
    assert "confirmed as" not in caplog.text


async def test_final_reply_missing_requested_standard_speed_fails_confirmation(client, caplog):
    client._session_config_options = options()
    request = replies(client, [options(), options(thought="max"), options(thought="max", speed=None)])
    with caplog.at_level(logging.INFO, logger="server.acp_client"):
        with pytest.raises(ModelNotAppliedError) as caught:
            await client.set_session_model("session", "family-max")
    assert str(caught.value) == (
        "agent is running speed 'None' after being asked for 'standard' on session session"
    )
    assert controls_sent(request) == [("model", "family-high"), ("thought_level", "max"), ("speed", "standard")]
    assert "confirmed as" not in caplog.text


async def test_model_controls_resolve_replaced_option_hook_after_each_await(client):
    client._session_config_options = options()
    calls = []

    async def third(session, control, value):
        calls.append((control, value))
        return options(thought="max", speed="fast")

    async def second(session, control, value):
        calls.append((control, value))
        client._set_config_option = third
        return options(thought="max")

    async def first(session, control, value):
        calls.append((control, value))
        client._set_config_option = second
        return options()

    client._set_config_option = first
    assert await client.set_session_model("session", "family-max-fast") == "family-max-fast"
    assert calls == [("model", "family-high"), ("thought_level", "max"), ("speed", "fast")]


async def test_mode_missing_confirmation_has_exact_error(client):
    request = replies(client, [[]])
    with pytest.raises(RuntimeError) as caught:
        await client.set_session_mode("session", "bypass")
    assert str(caught.value) == "agent is running mode 'None' after being asked for 'bypass' on session session"
    assert controls_sent(request) == [("mode", "bypass")]


async def test_mode_agent_error_keeps_original_protocol_error(client):
    failure = ACPError({"code": -32002, "message": "mode rejected"})
    replies(client, [failure])
    with pytest.raises(ACPError) as caught:
        await client.set_session_mode("session", "bypass")
    assert caught.value is failure


@pytest.mark.parametrize("cached,variant,responses,expected", [
    (None, "exact-legacy-model", [options(model="exact-legacy-model", thought=None, speed=None)],
     [("model", "exact-legacy-model")]),
    (options(values=["family-max"], thought=None, speed=None), "family-max",
     [options(model="family-max", thought=None, speed=None)],
     [("model", "family-max")]),
    (options(), "family-max-fast", [options(), options(thought="max"), options(thought="max", speed="fast")],
     [("model", "family-high"), ("thought_level", "max"), ("speed", "fast")]),
])
async def test_standalone_model_configuration_needs_only_options_and_request(cached, variant, responses, expected):
    original = deepcopy(cached)
    adapter = SimpleNamespace(_session_config_options=cached,
                              _set_config_option=AsyncMock(side_effect=responses))
    logger = MagicMock()
    assert await acp_configuration.apply_model(adapter, "session", variant, logger=logger) == variant
    assert adapter._set_config_option.await_args_list == [call("session", control, value) for control, value in expected]
    assert adapter._session_config_options == original
    logger.info.assert_called_once_with("ACP session %s model variant confirmed as %s", "session", variant)


@pytest.mark.parametrize("mode", ["ask", "bypass"])
async def test_standalone_mode_configuration_uses_only_option_request(mode):
    adapter = SimpleNamespace(_set_config_option=AsyncMock(return_value=[{"id": "mode", "currentValue": mode}]))
    logger = MagicMock()
    assert await acp_configuration.apply_mode(adapter, "session", mode, logger=logger) == mode
    adapter._set_config_option.assert_awaited_once_with("session", "mode", mode)
    logger.info.assert_called_once_with("ACP session %s mode confirmed as %s", "session", mode)


async def test_standalone_model_preflight_rejects_without_transport():
    adapter = SimpleNamespace(_session_config_options=[], _set_config_option=AsyncMock())
    with pytest.raises(ModelNotAppliedError, match="did not offer a model control"):
        await acp_configuration.apply_model(adapter, "session", "family-max", logger=MagicMock())
    adapter._set_config_option.assert_not_awaited()


def test_shared_errors_and_helper_exports_keep_legacy_import_identity():
    assert ACPError is acp_errors.ACPError is acp_configuration.ACPError
    assert ModelNotAppliedError is acp_errors.ModelNotAppliedError is acp_configuration.ModelNotAppliedError
    assert _model_controls is acp_configuration.model_controls
    assert _config_option is acp_configuration.config_option
    assert _option_values is acp_configuration.option_values

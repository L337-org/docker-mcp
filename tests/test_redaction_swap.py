import asyncio
import base64
import inspect
import secrets
import types
import typing
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import docker_mcp._hosts as hosts
from docker_mcp._hosts import parse_registry
from docker_mcp.redaction import settings, swap, wire
from docker_mcp.redaction.markers import MARKER_PATTERN
from docker_mcp.redaction.redactor import Redactor
from docker_mcp.redaction.settings import NameRules
from docker_mcp.redaction.store import UNSTORED_VALUE_ID
from docker_mcp.redaction.wire import Scope

# Generated per run, so no fixed value can be mistaken for a real credential.
_SECRET = "pw-" + secrets.token_urlsafe(18)


class _NoHarvest:
    def ensure_fresh(self):
        pass

    def harvest_if_unseen(self, ids):
        return False


@pytest.fixture(autouse=True)
def redactor(monkeypatch):
    # `prod` is redacted and accepts writes, where markers are swapped back; `stage` is redacted and
    # read-only; `dev` is not redacted.
    monkeypatch.setattr(
        hosts, "_registry", parse_registry("dev=ssh://dev, prod=ssh://prod(redact), stage=ssh://stage(ro,redact)")
    )
    monkeypatch.setattr(settings, "_global", False)
    monkeypatch.setattr(settings, "_names", NameRules())
    redactor = Redactor(host_labels=hosts.labels)
    monkeypatch.setattr(wire, "REDACTOR", redactor)
    monkeypatch.setattr(wire, "_harvest_for", lambda label: _NoHarvest())
    redactor.remember(_SECRET, "prod")
    return redactor


def _marker(redactor: Redactor, text: str = _SECRET, host: str = "prod") -> str:
    """A marker the redactor issues for `text` in a result from `host`."""
    found = MARKER_PATTERN.search(redactor.redact(f"value {text} end", host))
    assert found, "the value was not redacted"
    return found.group(0)


def _write_tool(name="container_create", scope=Scope.HOST_ARGUMENT, writes=True):
    calls: list[dict[str, Any]] = []

    def target(
        image: str = "alpine",
        environment: dict | None = None,
        command: list | None = None,
        labels: dict | None = None,
        extra_kwargs: dict | None = None,
        buildargs: dict | None = None,
        set_overrides: list | None = None,
        data: bytes | None = None,
        host: str | None = None,
    ) -> str:
        calls.append(
            {
                "image": image,
                "environment": environment,
                "command": command,
                "labels": labels,
                "extra_kwargs": extra_kwargs,
                "buildargs": buildargs,
                "set_overrides": set_overrides,
                "data": data,
                "host": host,
            }
        )
        return "ok"

    return wire.wrap(target, name, scope, ToolError, writes=writes), calls


# ---------- swapping back ----------


def test_a_marker_in_a_write_to_its_own_host_reaches_the_tool_as_exactly_the_text_it_replaced(redactor):
    wrapped, calls = _write_tool()
    marker = _marker(redactor)
    wrapped(
        environment={"DB_PASSWORD": marker},
        command=["psql", "-p", f"prefix-{marker}-suffix"],
        host="prod",
    )
    assert calls[0]["environment"] == {"DB_PASSWORD": _SECRET}
    assert calls[0]["command"] == ["psql", "-p", f"prefix-{_SECRET}-suffix"]


def test_an_encoded_form_is_swapped_back_in_the_form_it_was_replaced_in(redactor):
    encoded = base64.b64encode(_SECRET.encode()).decode()
    wrapped, calls = _write_tool()
    wrapped(environment={"BLOB": _marker(redactor, encoded)}, host="prod")
    assert calls[0]["environment"] == {"BLOB": encoded}


def test_a_marker_in_bytes_is_swapped_back_as_utf8(redactor):
    wrapped, calls = _write_tool(name="secret_create")
    wrapped(data=f"password={_marker(redactor)}\n".encode(), host="prod")
    assert calls[0]["data"] == f"password={_SECRET}\n".encode()


def test_a_call_with_no_marker_reaches_the_tool_exactly_as_it_arrived(redactor):
    received = []

    def target(environment: dict | None = None, host: str | None = None) -> str:
        received.append(environment)
        return "ok"

    environment = {"A": "1"}
    wire.wrap(target, "container_create", Scope.HOST_ARGUMENT, ToolError, writes=True)(
        environment=environment, host="prod"
    )
    assert received[0] is environment


def test_an_async_write_is_swapped_back_the_same_way(redactor):
    received = []

    async def target(environment: dict | None = None, host: str | None = None) -> str:
        received.append(environment)
        return "ok"

    wrapped = wire.wrap(target, "container_create", Scope.HOST_ARGUMENT, ToolError, writes=True)
    asyncio.run(wrapped(environment={"PW": _marker(redactor)}, host="prod"))
    assert received == [{"PW": _SECRET}]


def test_a_read_only_tool_is_not_touched(redactor):
    wrapped, calls = _write_tool(name="container_logs", writes=False)
    marker = _marker(redactor)
    wrapped(command=[marker], host="prod")
    assert calls[0]["command"] == [marker]


def test_with_redaction_off_everywhere_a_marker_shaped_string_is_left_alone(redactor, monkeypatch):
    monkeypatch.setattr(hosts, "_registry", parse_registry("dev=ssh://dev, prod=ssh://prod"))
    wrapped, calls = _write_tool()
    text = "[REDACTED:AAAAAAAAAAAAAAAAAAAAAA]"
    wrapped(command=[text], host="prod")
    assert calls[0]["command"] == [text]


def test_a_write_to_a_read_only_redacted_host_is_left_for_the_host_guard_to_refuse(redactor):
    # Swapping first would refuse with a marker message, or swap a value into a call the guard then
    # refuses anyway; the guard's own message says why the call cannot run there.
    wrapped, calls = _write_tool()
    marker = "[REDACTED:AAAAAAAAAAAAAAAAAAAAAA]"
    wrapped(command=[marker], host="stage")
    assert calls[0]["command"] == [marker]


# ---------- refusals ----------


def _refusal(wrapped, calls, **arguments) -> str:
    with pytest.raises(ToolError) as caught:
        wrapped(**arguments)
    assert calls == [], "the call ran"
    message = str(caught.value)
    assert _SECRET not in message
    return message


def test_a_marker_from_another_host_is_refused_before_anything_runs(redactor, monkeypatch):
    monkeypatch.setattr(settings, "_global", True)
    redactor.remember(_SECRET, "dev")
    wrapped, calls = _write_tool()
    message = _refusal(wrapped, calls, environment={"PW": _marker(redactor, host="dev")}, host="prod")
    assert "`environment['PW']`" in message
    assert "issued for another host" in message
    assert "Read the value again from host 'prod'" in message


def test_a_marker_from_an_earlier_process_is_refused(redactor):
    earlier = Redactor(host_labels=hosts.labels)
    earlier.remember(_SECRET, "prod")
    wrapped, calls = _write_tool()
    message = _refusal(wrapped, calls, command=["x", _marker(earlier)], host="prod")
    assert "`command[1]`" in message
    assert "issued before the server last started" in message


def test_an_altered_marker_is_refused(redactor):
    marker = _marker(redactor)
    match = MARKER_PATTERN.fullmatch(marker)
    assert match
    token = match.group(1)
    altered = f"[REDACTED:{token[:10]}{'A' if token[10] != 'A' else 'B'}{token[11:]}]"
    wrapped, calls = _write_tool()
    assert "was altered" in _refusal(wrapped, calls, command=[altered], host="prod")


def test_a_marker_for_an_unstored_value_tells_the_agent_to_ask_the_user(redactor):
    token = redactor._state.codec.issue(UNSTORED_VALUE_ID, 0, redactor._host_index("prod"))
    wrapped, calls = _write_tool()
    message = _refusal(wrapped, calls, command=[f"[REDACTED:{token}]"], host="prod")
    assert "not kept for use in writes" in message
    assert "Reading it again will not help: stop and ask the user what to do" in message


def test_a_marker_in_a_write_to_an_unfiltered_host_is_refused(redactor):
    wrapped, calls = _write_tool()
    message = _refusal(wrapped, calls, command=[_marker(redactor)], host="dev")
    assert "on host 'dev'" in message
    assert "only on a redacted host that accepts writes" in message


def test_a_marker_in_a_tool_that_reaches_no_daemon_is_refused(redactor):
    wrapped, calls = _write_tool(name="context_create", scope=Scope.NO_DAEMON)
    message = _refusal(wrapped, calls, command=[_marker(redactor)])
    assert "by a tool that reaches no daemon" in message


@pytest.mark.parametrize(
    ("arguments", "where", "reason"),
    [
        ({"image": "{m}"}, "`image`", "a name or identifier"),
        ({"labels": {"owner": "{m}"}}, "`labels['owner']`", "a label, annotation or commit field"),
        ({"buildargs": {"TOKEN": "{m}"}}, "`buildargs['TOKEN']`", "recorded in the image's history"),
        ({"set_overrides": ["app.args.TOKEN={m}"]}, "`set_overrides[0]`", "passed on the command line"),
        ({"extra_kwargs": {"labels": {"a": "{m}"}}}, "`extra_kwargs['labels']['a']`", "a label"),
        ({"extra_kwargs": {"name": "{m}"}}, "`extra_kwargs['name']`", "a name or identifier"),
    ],
)
def test_the_guardrail_refuses_a_marker_where_a_credential_is_never_legitimate(redactor, arguments, where, reason):
    marker = _marker(redactor)

    def fill(value):
        if isinstance(value, str):
            return value.replace("{m}", marker)
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        return [fill(item) for item in value]

    wrapped, calls = _write_tool()
    message = _refusal(wrapped, calls, host="prod", **{key: fill(value) for key, value in arguments.items()})
    assert where in message
    assert reason in message


def test_a_build_argument_refusal_points_to_vars_and_a_secret(redactor):
    wrapped, calls = _write_tool()
    message = _refusal(wrapped, calls, buildargs={"T": _marker(redactor)}, host="prod")
    assert "`vars`" in message and "secret" in message


def test_the_guardrail_refuses_even_a_marker_that_would_swap_back(redactor):
    wrapped, calls = _write_tool()
    _refusal(wrapped, calls, labels={"a": _marker(redactor)}, host="prod")


def test_swapped_arguments_are_never_logged(redactor, caplog):
    caplog.set_level("DEBUG")
    wrapped, _ = _write_tool()
    wrapped(environment={"PW": _marker(redactor)}, host="prod")
    assert _SECRET not in caplog.text


# ---------- the table ----------

_SCALARS = {bool, int, float, type(None)}


def _can_hold_text(annotation: Any) -> bool:
    if annotation is inspect.Parameter.empty:
        return True
    members = typing.get_args(annotation) if isinstance(annotation, types.UnionType) else (annotation,)
    return any(member not in _SCALARS for member in members)


def test_every_argument_of_every_write_tool_that_can_hold_text_is_classified():
    """A write tool added later cannot take a marker without someone deciding what happens to it."""
    from docker_mcp.server import TOOL_CATEGORIES, ToolCategory, mcp

    unclassified = []
    for name, registered in mcp._tool_manager._tools.items():
        if TOOL_CATEGORIES.get(name, ToolCategory.MUTATING) is ToolCategory.READ_ONLY:
            continue
        for parameter in inspect.signature(registered.fn).parameters.values():
            if parameter.name == "host" or not _can_hold_text(parameter.annotation):
                continue
            if swap.kind_of(name, parameter.name) is None:
                unclassified.append(f"{name}.{parameter.name}")
    assert not unclassified, (
        f"Classify these in ARGUMENTS in docker_mcp/redaction/swap.py, as swapped back or refused: {unclassified}"
    )


def test_the_table_names_only_arguments_that_exist():
    from docker_mcp.server import TOOL_CATEGORIES, ToolCategory, mcp

    present = {
        parameter
        for name, registered in mcp._tool_manager._tools.items()
        if TOOL_CATEGORIES.get(name, ToolCategory.MUTATING) is not ToolCategory.READ_ONLY
        for parameter in inspect.signature(registered.fn).parameters
    }
    assert set(swap.ARGUMENTS) - present == set()

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import docker_mcp._hosts as hosts
from docker_mcp._hosts import parse_registry
from docker_mcp.redaction import settings, wire
from docker_mcp.redaction.markers import MARKER_PATTERN
from docker_mcp.redaction.redactor import Redactor
from docker_mcp.redaction.settings import NameRules
from docker_mcp.redaction.wire import Scope

_SECRET = "correct-horse-battery-staple-42"


class _NoHarvest:
    """Stands in for `HostHarvest`: records what the wire asked of it, harvests nothing."""

    def __init__(self):
        self.fresh_calls = 0
        self.unseen: list[set[str]] = []

    def ensure_fresh(self):
        self.fresh_calls += 1

    def harvest_if_unseen(self, ids):
        self.unseen.append(set(ids))
        return False


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(hosts, "_registry", parse_registry("dev=ssh://dev, prod=ssh://prod(ro,redact)"))
    monkeypatch.setattr(settings, "_global", False)
    monkeypatch.setattr(settings, "_names", NameRules())
    redactor = Redactor(host_labels=hosts.labels)
    monkeypatch.setattr(wire, "REDACTOR", redactor)
    harvest = _NoHarvest()
    monkeypatch.setattr(wire, "_harvest_for", lambda label: harvest)
    return SimpleNamespace(redactor=redactor, harvest=harvest)


def _tool(result: Any = None, error: str | None = None, name="container_inspect", scope=Scope.HOST_ARGUMENT, **kw):
    calls = []

    def target(id_or_name: str = "x", host: str | None = None, dest_path: str | None = None) -> Any:
        calls.append(host)
        if error is not None:
            raise ToolError(error)
        return result

    return wire.wrap(target, name, scope, ToolError, **kw), calls


def test_output_from_a_redacted_host_is_redacted(_setup):
    _setup.redactor.remember(_SECRET, "prod")
    wrapped, _ = _tool({"Env": [f"DB_PASS={_SECRET}"]})
    redacted = wrapped(host="prod")
    assert _SECRET not in json.dumps(redacted)
    assert MARKER_PATTERN.search(redacted["Env"][0])
    assert _setup.harvest.fresh_calls == 1


def test_output_from_an_unfiltered_host_is_never_redacted_even_with_a_value_stored_elsewhere(_setup):
    _setup.redactor.remember(_SECRET, "prod")
    result = {"Env": [f"DB_PASS={_SECRET}"]}
    wrapped, _ = _tool(result)
    assert wrapped(host="dev") is result
    assert _setup.harvest.fresh_calls == 0


def test_a_call_naming_no_host_is_judged_by_the_default_host(_setup, monkeypatch):
    monkeypatch.setattr(hosts, "_registry", parse_registry("prod=ssh://prod(redact), dev=ssh://dev"))
    _setup.redactor.remember(_SECRET, "prod")
    wrapped, _ = _tool(f"log {_SECRET}")
    assert _SECRET not in wrapped()


def test_a_resource_scoped_to_the_default_host_is_redacted_when_that_host_is(_setup, monkeypatch):
    monkeypatch.setattr(settings, "_global", True)
    _setup.redactor.remember(_SECRET, "dev")
    wrapped, _ = _tool(f"log {_SECRET}", scope=Scope.DEFAULT_HOST)
    assert _SECRET not in wrapped(host="prod")  # the URI names no host: the default, dev, decides


def test_a_call_that_reaches_no_daemon_is_never_redacted(_setup, monkeypatch):
    monkeypatch.setattr(settings, "_global", True)
    _setup.redactor.remember(_SECRET, "prod")
    wrapped, _ = _tool(f"x {_SECRET}", name="registry_tags", scope=Scope.NO_DAEMON)
    assert wrapped() == f"x {_SECRET}"


def test_error_text_from_a_redacted_host_is_redacted_and_carries_no_unredacted_cause(_setup):
    _setup.redactor.remember(_SECRET, "prod")
    wrapped, _ = _tool(error=f"compose failed: DB_PASS={_SECRET}")
    with pytest.raises(ToolError) as caught:
        wrapped(host="prod")
    assert _SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_at_the_budget_every_call_to_a_redacted_host_is_refused_before_it_runs(_setup, monkeypatch):
    monkeypatch.setattr(_setup.redactor, "budget_reached", lambda: True)
    wrapped, calls = _tool("x")
    with pytest.raises(ToolError, match="restart the server"):
        wrapped(host="prod")
    assert calls == []
    assert wrapped(host="dev") == "x"


@pytest.mark.parametrize("name", ["container_archive_get", "container_export", "image_save"])
def test_bytes_in_band_are_refused_on_a_redacted_host_only(_setup, name):
    wrapped, calls = _tool(b"tar bytes", name=name)
    with pytest.raises(ToolError, match=f"{name} cannot return bytes in band on host 'prod'"):
        wrapped(host="prod")
    assert calls == []
    assert wrapped(host="dev") == b"tar bytes"


@pytest.mark.parametrize("name", ["container_export", "image_save"])
def test_writing_bytes_to_a_file_stays_allowed_on_a_redacted_host(_setup, name):
    wrapped, calls = _tool({"path": "/tmp/out.tar"}, name=name)
    assert wrapped(host="prod", dest_path="/tmp/out.tar") == {"path": "/tmp/out.tar"}
    assert calls == ["prod"]


def test_ids_in_a_result_are_offered_to_the_harvest(_setup):
    wrapped, _ = _tool([{"Id": "abcdef0123456789", "Names": ["/web"]}, {"ID": "not-an-id"}])
    wrapped(host="prod")
    assert _setup.harvest.unseen == [{"abcdef0123456789"}]


def test_a_json_resource_is_redacted_as_data_so_escaped_values_are_still_found(_setup):
    awkward = 'multi\nline "quoted" credential'
    _setup.redactor.remember(awkward, "prod")
    body = json.dumps({"log": awkward}, indent=2)
    assert awkward not in body  # escaped in the text, so only parsing finds it
    wrapped, _ = _tool(body, json_text=True)
    out = wrapped(host="prod")
    assert awkward not in json.loads(out)["log"] and out.startswith("{\n  ")


def test_a_compose_projects_env_file_is_harvested(_setup, tmp_path):
    (tmp_path / ".env").write_text("# comment\nDB_PASSWORD='wordlike1'\nexport API_TOKEN=tok-value\nTAG=v1\n")

    def compose_config(project_dir: str | None = None, host: str | None = None) -> str:
        return "password is wordlike1, token tok-value, tag v1"

    wrapped = wire.wrap(compose_config, "compose_config", Scope.HOST_ARGUMENT, ToolError)
    out = wrapped(project_dir=str(tmp_path), host="prod")
    assert "wordlike1" not in out and "tok-value" not in out and "tag v1" in out


def test_a_failure_inside_redaction_propagates_rather_than_returning_output(_setup, monkeypatch):
    def broken(value, host):
        raise RuntimeError("redactor failed")

    monkeypatch.setattr(_setup.redactor, "redact", broken)
    wrapped, _ = _tool("output")
    with pytest.raises(RuntimeError, match="redactor failed"):
        wrapped(host="prod")


def test_an_async_callable_is_redacted_the_same_way(_setup):
    _setup.redactor.remember(_SECRET, "prod")

    async def target(host: str | None = None):
        return f"x {_SECRET}"

    async def failing(host: str | None = None):
        raise ToolError(f"failed: {_SECRET}")

    assert _SECRET not in asyncio.run(wire.wrap(target, "t", Scope.HOST_ARGUMENT, ToolError)(host="prod"))
    with pytest.raises(ToolError) as caught:
        asyncio.run(wire.wrap(failing, "t", Scope.HOST_ARGUMENT, ToolError)(host="prod"))
    assert _SECRET not in str(caught.value)


def test_the_wrapper_keeps_the_signature_and_records_its_scope():
    def target(id_or_name: str, host: str | None = None) -> dict:
        return {}

    wrapped = wire.wrap(target, "t", Scope.HOST_ARGUMENT, ToolError)
    import inspect

    assert inspect.signature(wrapped) == inspect.signature(target)
    assert getattr(wrapped, wire.REDACTION_SCOPE) is Scope.HOST_ARGUMENT


def test_core_dumps_are_switched_off():
    posix_resource = pytest.importorskip("resource")  # POSIX only; elsewhere there is nothing to set
    wire.disable_core_dumps()
    assert posix_resource.getrlimit(posix_resource.RLIMIT_CORE)[0] == 0

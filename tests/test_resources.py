import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

import docker_mcp  # noqa: F401 — side-effect import: docker_mcp/__init__ runs _hosts.load() to pin the registry
import docker_mcp._hosts as _hosts_mod
from docker_mcp._hosts import parse_registry
from docker_mcp.server import TOOL_CATEGORIES
from docker_mcp.tools.resources import (
    DOCKER_DOCS_BASE_URL,
    EXTERNAL_SECTIONS,
    SDK_SECTIONS,
    get_container_logs_resource,
    get_container_stats_resource,
    get_docs_section,
    get_host_container_logs_resource,
    get_host_container_stats_resource,
    get_hosts_resource,
    get_tool_catalog,
    list_container_resources,
    list_docs_sections,
    list_host_container_resources,
)


def test_list_docs_sections_returns_json_with_sdk_and_external_sections():
    payload = json.loads(list_docs_sections())
    # Backward-compatible fields: `base_url` (SDK base) and `sections` (list of section names).
    assert payload["base_url"] == DOCKER_DOCS_BASE_URL
    assert payload["sdk_base_url"] == DOCKER_DOCS_BASE_URL
    assert isinstance(payload["sections"], list)
    for section in SDK_SECTIONS:
        assert section in payload["sections"]
    for section in EXTERNAL_SECTIONS:
        assert section in payload["sections"]
    # New field: `section_urls` maps each section name to its absolute URL.
    for section in SDK_SECTIONS:
        assert payload["section_urls"][section] == f"{DOCKER_DOCS_BASE_URL}/{section}.html"
    for section, url in EXTERNAL_SECTIONS.items():
        assert payload["section_urls"][section] == url
    assert "usage" in payload


def _docs_response(body: bytes) -> MagicMock:
    response = MagicMock()
    response.content = body
    response.raise_for_status.return_value = None
    return response


def test_get_docs_section_fetches_sdk_section_at_base_url():
    with patch(
        "docker_mcp.tools.resources.httpx.get", return_value=_docs_response(b"<html>containers</html>")
    ) as mock_get:
        result = get_docs_section("containers")
    assert result == "<html>containers</html>"
    args, kwargs = mock_get.call_args
    assert args[0] == f"{DOCKER_DOCS_BASE_URL}/containers.html"
    # A bounded timeout is mandatory — a stalled fetch must not hang the resource read.
    assert kwargs["timeout"] == 30.0


def test_get_docs_section_fetches_external_section_at_absolute_url():
    with patch(
        "docker_mcp.tools.resources.httpx.get", return_value=_docs_response(b"<html>compose</html>")
    ) as mock_get:
        result = get_docs_section("compose")
    assert result == "<html>compose</html>"
    assert mock_get.call_args.args[0] == EXTERNAL_SECTIONS["compose"]


def test_get_docs_section_raises_for_status():
    response = MagicMock()
    response.content = b""
    response.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
    with patch("docker_mcp.tools.resources.httpx.get", return_value=response):
        with pytest.raises(httpx.HTTPStatusError):
            get_docs_section("containers")


def test_get_docs_section_rejects_unknown_section():
    with pytest.raises(ValueError, match="Unknown documentation section"):
        get_docs_section("not-a-section")


def test_get_tool_catalog_returns_json_covering_every_tool():
    payload = json.loads(get_tool_catalog())
    assert {t["name"] for t in payload["tools"]} == set(TOOL_CATEGORIES)
    assert "DOCKER_MCP_SERVER_DISABLE" in payload["switches"]
    assert payload["domains"]  # per-domain summary is populated


def test_get_hosts_resource_returns_the_configured_hosts():
    # Default test env (no DOCKER_MCP_SERVER_HOSTS) -> a single synthesized default host.
    payload = json.loads(get_hosts_resource())
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["default"] is True
    assert set(payload[0]) == {"name", "url", "read_only", "tls", "default"}


# ---------- DOCKER_MCP_SERVER_DISABLE also hides a disabled domain's doc sections ----------
# `_section_enabled` reads the live `server.DISABLED_DOMAINS` (via `is_domain_disabled`), so unlike the
# import-time tool/prompt gating these can be exercised in-process by monkeypatching that set.


def test_list_docs_sections_hides_sections_for_disabled_domains(monkeypatch):
    monkeypatch.setattr("docker_mcp.server.DISABLED_DOMAINS", frozenset({"scout"}))
    payload = json.loads(list_docs_sections())
    assert "scout" not in payload["sections"]
    assert "scout-cli" not in payload["sections"]
    assert "scout" not in payload["section_urls"]
    assert sorted(payload["disabled_sections"]) == ["scout", "scout-cli"]
    # A different domain's sections are untouched.
    assert "compose" in payload["sections"]


def test_list_docs_sections_disabled_is_empty_by_default():
    assert json.loads(list_docs_sections())["disabled_sections"] == []


def test_get_docs_section_refuses_a_disabled_section(monkeypatch):
    monkeypatch.setattr("docker_mcp.server.DISABLED_DOMAINS", frozenset({"scout"}))
    with pytest.raises(ValueError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
        get_docs_section("scout")
    with pytest.raises(ValueError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
        get_docs_section("scout-cli")


def test_get_docs_section_still_serves_enabled_sections_when_another_is_disabled(monkeypatch):
    monkeypatch.setattr("docker_mcp.server.DISABLED_DOMAINS", frozenset({"scout"}))
    with patch(
        "docker_mcp.tools.resources.httpx.get", return_value=_docs_response(b"<html>containers</html>")
    ) as mock_get:
        assert get_docs_section("containers") == "<html>containers</html>"
    assert mock_get.call_args.args[0] == f"{DOCKER_DOCS_BASE_URL}/containers.html"


# ---------- container observability resources (docker://containers, docker-logs://, docker-stats://) ----------


def _container(name, short_id, status, image, exit_code=None):
    c = MagicMock()
    c.name = name
    c.short_id = short_id
    state = {"Status": status}
    if exit_code is not None:
        state["ExitCode"] = exit_code
    c.attrs = {"State": state, "Config": {"Image": image}}
    return c


def test_list_container_resources_indexes_running_and_stopped():
    running = _container("web", "abc123", "running", "nginx")
    exited = _container("job", "def456", "exited", "alpine", exit_code=1)
    with patch("docker_mcp.tools.resources._get_client") as mock_client:
        mock_client.return_value.containers.list.return_value = [running, exited]
        payload = json.loads(list_container_resources())
    mock_client.return_value.containers.list.assert_called_once_with(all=True)
    by_name = {c["name"]: c for c in payload["containers"]}
    # Running container: both logs and stats URIs.
    assert by_name["web"]["logs"] == "docker-logs://web"
    assert by_name["web"]["stats"] == "docker-stats://web"
    assert by_name["web"]["image"] == "nginx"
    # Stopped container: logs URI but no stats URI, plus the exit code as a triage signal.
    assert by_name["job"]["logs"] == "docker-logs://job"
    assert by_name["job"]["stats"] is None
    assert by_name["job"]["exit_code"] == 1


def test_container_logs_resource_returns_tail():
    with patch("docker_mcp.tools.resources._read_log_tail", return_value="line1\nline2") as mock_read:
        assert get_container_logs_resource("web") == "line1\nline2"
    mock_read.assert_called_once_with("web")


def test_container_stats_resource_returns_json_summary():
    summary = {"container": "web", "cpu_percent": 3.4, "mem_percent": 25.1}
    with patch("docker_mcp.tools.resources._read_stats_summary", return_value=summary):
        payload = json.loads(get_container_stats_resource("web"))
    assert payload == summary


def test_container_resources_refused_when_containers_domain_disabled(monkeypatch):
    monkeypatch.setattr("docker_mcp.server.DISABLED_DOMAINS", frozenset({"containers"}))
    for call in (
        list_container_resources,
        lambda: get_container_logs_resource("web"),
        lambda: get_container_stats_resource("web"),
    ):
        with pytest.raises(ValueError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
            call()


# ---------- slice 5: host-qualified container resource URIs ----------


def _set_multi(monkeypatch):
    monkeypatch.setattr(_hosts_mod, "_registry", parse_registry("local=unix:///l.sock, prod=tcp://p:2376"))


def test_default_index_emits_empty_authority_children_in_multi_host(monkeypatch):
    _set_multi(monkeypatch)
    running = _container("web", "abc123", "running", "nginx")
    with patch("docker_mcp.tools.resources._get_client") as mock_client:
        mock_client.return_value.containers.list.return_value = [running]
        web = json.loads(list_container_resources())["containers"][0]
    assert web["logs"] == "docker-logs:///web"  # empty authority = default host
    assert web["stats"] == "docker-stats:///web"


def test_host_index_emits_host_qualified_children_and_routes(monkeypatch):
    _set_multi(monkeypatch)
    running = _container("web", "abc123", "running", "nginx")
    with patch("docker_mcp.tools.resources._get_client") as mock_client:
        mock_client.return_value.containers.list.return_value = [running]
        web = json.loads(list_host_container_resources("prod"))["containers"][0]
    assert web["logs"] == "docker-logs://prod/web"
    assert web["stats"] == "docker-stats://prod/web"
    mock_client.assert_called_once_with("prod")  # index routed to the named host


def test_host_logs_resource_routes_to_host():
    with patch("docker_mcp.tools.resources._read_log_tail", return_value="L1\nL2") as mock_read:
        assert get_host_container_logs_resource("prod", "web") == "L1\nL2"
    mock_read.assert_called_once_with("web", host="prod")


def test_host_stats_resource_routes_to_host():
    with patch("docker_mcp.tools.resources._read_stats_summary", return_value={"container": "web"}) as mock_read:
        assert json.loads(get_host_container_stats_resource("prod", "web")) == {"container": "web"}
    mock_read.assert_called_once_with("web", host="prod")


def _registered_resource_uris(hosts_value: str | None) -> set[str]:
    """Import the package in a child process; return the registered static + template resource URIs."""
    env = dict(os.environ)
    env.pop("DOCKER_MCP_SERVER_HOSTS", None)
    if hosts_value:
        env["DOCKER_MCP_SERVER_HOSTS"] = hosts_value
    code = (
        "import asyncio, docker_mcp; from docker_mcp.server import mcp; "
        "u=[str(r.uri) for r in asyncio.run(mcp.list_resources())]"
        "+[t.uriTemplate for t in asyncio.run(mcp.list_resource_templates())]; "
        "print('\\n'.join(u))"
    )
    out = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout
    return {line for line in out.splitlines() if line}


def test_single_host_registers_bare_container_uris_end_to_end():
    uris = _registered_resource_uris(None)
    assert "docker://containers" in uris
    assert "docker-logs://{id_or_name}" in uris
    assert not any("{host}" in u for u in uris)  # no host-qualified variants single-host


def test_multi_host_registers_empty_authority_and_host_qualified_uris_end_to_end():
    uris = _registered_resource_uris("local=ssh://a, prod=ssh://b")
    assert {"docker:///containers", "docker://{host}/containers"} <= uris
    assert {"docker-logs:///{id_or_name}", "docker-logs://{host}/{id_or_name}"} <= uris
    assert {"docker-stats:///{id_or_name}", "docker-stats://{host}/{id_or_name}"} <= uris
    assert "docker://containers" not in uris  # bare form replaced by empty-authority in multi-host

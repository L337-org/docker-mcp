import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from docker_mcp.exceptions import CapabilityError, ToolInputError, ToolRefusalError
import docker_mcp  # noqa: F401 — side-effect import: docker_mcp/__init__ runs _hosts.load() to pin the registry
import docker_mcp._hosts as _hosts_mod
from docker_mcp._hosts import parse_registry
from docker_mcp.server import query_catalog, TOOL_CATEGORIES
from docker_mcp.tools.resources import (
    DOCKER_DOCS_BASE_URL,
    EXTERNAL_SECTIONS,
    SDK_SECTIONS,
    _MAX_DOCS_RESPONSE_BYTES,
    docs_lookup,
    tool_list,
    get_container_logs_resource,
    get_container_stats_resource,
    get_docs_section,
    get_host_container_logs_resource,
    get_host_container_stats_resource,
    get_host_service_logs_resource,
    get_host_service_tasks_resource,
    get_hosts_resource,
    get_service_logs_resource,
    get_service_tasks_resource,
    get_tool_catalog,
    list_container_resources,
    list_docs_sections,
    list_host_container_resources,
    list_host_node_resources,
    list_host_service_resources,
    list_node_resources,
    list_service_resources,
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
    """Build a mock `httpx.stream(...)` context manager yielding `body` as a single chunk."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.iter_bytes.return_value = [body] if body else []
    ctx = MagicMock()
    ctx.__enter__.return_value = response
    ctx.__exit__.return_value = None
    return ctx


def test_get_docs_section_fetches_sdk_section_at_base_url():
    with patch(
        "docker_mcp.tools.resources.httpx.stream", return_value=_docs_response(b"<html>containers</html>")
    ) as mock_stream:
        result = get_docs_section("containers")
    assert result == "<html>containers</html>"
    args, kwargs = mock_stream.call_args
    assert args == ("GET", f"{DOCKER_DOCS_BASE_URL}/containers.html")
    # A bounded timeout is mandatory — a stalled fetch must not hang the resource read.
    assert kwargs["timeout"] == 30.0


def test_get_docs_section_fetches_external_section_at_absolute_url():
    with patch(
        "docker_mcp.tools.resources.httpx.stream", return_value=_docs_response(b"<html>compose</html>")
    ) as mock_stream:
        result = get_docs_section("compose")
    assert result == "<html>compose</html>"
    assert mock_stream.call_args.args == ("GET", EXTERNAL_SECTIONS["compose"])


def test_get_docs_section_raises_for_status():
    response = MagicMock()
    response.iter_bytes.return_value = []
    response.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
    ctx = MagicMock()
    ctx.__enter__.return_value = response
    ctx.__exit__.return_value = None
    with patch("docker_mcp.tools.resources.httpx.stream", return_value=ctx):
        with pytest.raises(httpx.HTTPStatusError):
            get_docs_section("containers")


def test_get_docs_section_rejects_a_response_over_the_byte_cap():
    ctx = _docs_response(b"x" * (_MAX_DOCS_RESPONSE_BYTES + 1))
    with patch("docker_mcp.tools.resources.httpx.stream", return_value=ctx):
        with pytest.raises(ToolRefusalError, match="exceeded the .*-byte limit"):
            get_docs_section("containers")


def test_get_docs_section_rejects_unknown_section():
    with pytest.raises(ToolInputError, match="Unknown documentation section"):
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
    assert set(payload[0]) == {"name", "url", "read_only", "non_destructive", "tls", "default"}


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
    with pytest.raises(CapabilityError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
        get_docs_section("scout")
    with pytest.raises(CapabilityError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
        get_docs_section("scout-cli")


def test_get_docs_section_still_serves_enabled_sections_when_another_is_disabled(monkeypatch):
    monkeypatch.setattr("docker_mcp.server.DISABLED_DOMAINS", frozenset({"scout"}))
    with patch(
        "docker_mcp.tools.resources.httpx.stream", return_value=_docs_response(b"<html>containers</html>")
    ) as mock_stream:
        assert get_docs_section("containers") == "<html>containers</html>"
    assert mock_stream.call_args.args == ("GET", f"{DOCKER_DOCS_BASE_URL}/containers.html")


# ---------- docs_lookup: tool-callable mirror of the docker-docs:// resources ----------


def test_docs_lookup_with_no_section_mirrors_list_docs_sections():
    assert docs_lookup() == list_docs_sections()


def test_docs_lookup_with_section_mirrors_get_docs_section():
    with patch(
        "docker_mcp.tools.resources.httpx.stream", return_value=_docs_response(b"<html>containers</html>")
    ) as mock_stream:
        assert docs_lookup("containers") == "<html>containers</html>"
    assert mock_stream.call_args.args == ("GET", f"{DOCKER_DOCS_BASE_URL}/containers.html")


def test_docs_lookup_still_refuses_a_disabled_section(monkeypatch):
    # The tool itself is un-disablable, but an individual section still respects its own domain's
    # DOCKER_MCP_SERVER_DISABLE state, exactly like the docker-docs://{section} resource.
    monkeypatch.setattr("docker_mcp.server.DISABLED_DOMAINS", frozenset({"scout"}))
    with pytest.raises(CapabilityError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
        docs_lookup("scout")


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
        with pytest.raises(CapabilityError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
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
        "+[t.uri_template for t in asyncio.run(mcp.list_resource_templates())]; "
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


# ---------- service observability resources (docker://services, service-logs://, service-tasks://) ----------


def _service(name, short_id, mode, image, replicas=None):
    s = MagicMock()
    s.name = name
    s.short_id = short_id
    mode_spec = {"Replicated": {"Replicas": replicas}} if mode == "replicated" else {"Global": {}}
    s.attrs = {"Spec": {"Mode": mode_spec, "TaskTemplate": {"ContainerSpec": {"Image": image}}}}
    return s


def test_list_service_resources_indexes_replicated_and_global():
    replicated = _service("web", "abc123", "replicated", "nginx", replicas=3)
    global_svc = _service("agent", "def456", "global", "fluentd")
    with patch("docker_mcp.tools.resources._get_client") as mock_client:
        mock_client.return_value.services.list.return_value = [replicated, global_svc]
        payload = json.loads(list_service_resources())
    mock_client.return_value.services.list.assert_called_once_with()
    by_name = {s["name"]: s for s in payload["services"]}
    assert by_name["web"]["mode"] == "replicated"
    assert by_name["web"]["desired_replicas"] == 3
    assert by_name["web"]["image"] == "nginx"
    assert by_name["web"]["logs"] == "service-logs://web"
    assert by_name["web"]["tasks"] == "service-tasks://web"
    assert by_name["agent"]["mode"] == "global"
    assert by_name["agent"]["desired_replicas"] is None


def test_service_logs_resource_returns_tail():
    with patch("docker_mcp.tools.resources._read_service_log_tail", return_value="l1\nl2") as mock_read:
        assert get_service_logs_resource("web") == "l1\nl2"
    mock_read.assert_called_once_with("web")


def test_service_tasks_resource_returns_json_summary():
    summary = {"service": "web", "running_tasks": 2, "desired_tasks": 2}
    with patch("docker_mcp.tools.resources._read_service_task_summary", return_value=summary):
        assert json.loads(get_service_tasks_resource("web")) == summary


def test_service_resources_refused_when_services_domain_disabled(monkeypatch):
    monkeypatch.setattr("docker_mcp.server.DISABLED_DOMAINS", frozenset({"services"}))
    for call in (
        list_service_resources,
        lambda: get_service_logs_resource("web"),
        lambda: get_service_tasks_resource("web"),
    ):
        with pytest.raises(CapabilityError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
            call()


def test_service_default_index_emits_empty_authority_children_in_multi_host(monkeypatch):
    _set_multi(monkeypatch)
    svc = _service("web", "abc123", "replicated", "nginx", replicas=1)
    with patch("docker_mcp.tools.resources._get_client") as mock_client:
        mock_client.return_value.services.list.return_value = [svc]
        web = json.loads(list_service_resources())["services"][0]
    assert web["logs"] == "service-logs:///web"
    assert web["tasks"] == "service-tasks:///web"


def test_service_host_index_emits_host_qualified_children_and_routes(monkeypatch):
    _set_multi(monkeypatch)
    svc = _service("web", "abc123", "replicated", "nginx", replicas=1)
    with patch("docker_mcp.tools.resources._get_client") as mock_client:
        mock_client.return_value.services.list.return_value = [svc]
        web = json.loads(list_host_service_resources("prod"))["services"][0]
    assert web["logs"] == "service-logs://prod/web"
    assert web["tasks"] == "service-tasks://prod/web"
    mock_client.assert_called_once_with("prod")


def test_host_service_logs_resource_routes_to_host():
    with patch("docker_mcp.tools.resources._read_service_log_tail", return_value="L1") as mock_read:
        assert get_host_service_logs_resource("prod", "web") == "L1"
    mock_read.assert_called_once_with("web", host="prod")


def test_host_service_tasks_resource_routes_to_host():
    with patch("docker_mcp.tools.resources._read_service_task_summary", return_value={"service": "web"}) as mock_read:
        assert json.loads(get_host_service_tasks_resource("prod", "web")) == {"service": "web"}
    mock_read.assert_called_once_with("web", host="prod")


def test_single_host_registers_bare_service_uris_end_to_end():
    uris = _registered_resource_uris(None)
    assert "docker://services" in uris
    assert "service-logs://{id_or_name}" in uris
    assert "service-tasks://{id_or_name}" in uris


def test_multi_host_registers_service_uris_end_to_end():
    uris = _registered_resource_uris("local=ssh://a, prod=ssh://b")
    assert {"docker:///services", "docker://{host}/services"} <= uris
    assert {"service-logs:///{id_or_name}", "service-logs://{host}/{id_or_name}"} <= uris
    assert {"service-tasks:///{id_or_name}", "service-tasks://{host}/{id_or_name}"} <= uris
    assert "docker://services" not in uris


# ---------- node observability resource (docker://nodes — index only) ----------


def _node(short_id, hostname, state, availability, role, reachability=None):
    n = MagicMock()
    n.short_id = short_id
    attrs = {
        "Description": {"Hostname": hostname},
        "Status": {"State": state},
        "Spec": {"Availability": availability, "Role": role},
    }
    if reachability is not None:
        attrs["ManagerStatus"] = {"Reachability": reachability}
    n.attrs = attrs
    return n


def test_list_node_resources_indexes_state_availability_role():
    manager = _node("n1", "host-a", "ready", "active", "manager", reachability="reachable")
    worker = _node("n2", "host-b", "down", "drain", "worker")
    with patch("docker_mcp.tools.resources._get_client") as mock_client:
        mock_client.return_value.nodes.list.return_value = [manager, worker]
        payload = json.loads(list_node_resources())
    mock_client.return_value.nodes.list.assert_called_once_with()
    by_host = {n["hostname"]: n for n in payload["nodes"]}
    assert by_host["host-a"]["state"] == "ready"
    assert by_host["host-a"]["role"] == "manager"
    assert by_host["host-a"]["manager_reachability"] == "reachable"
    assert by_host["host-b"]["state"] == "down"
    assert by_host["host-b"]["availability"] == "drain"
    assert by_host["host-b"]["manager_reachability"] is None


def test_node_resources_refused_when_nodes_domain_disabled(monkeypatch):
    monkeypatch.setattr("docker_mcp.server.DISABLED_DOMAINS", frozenset({"nodes"}))
    with pytest.raises(CapabilityError, match="disabled via DOCKER_MCP_SERVER_DISABLE"):
        list_node_resources()


def test_node_host_index_routes_to_host(monkeypatch):
    _set_multi(monkeypatch)
    node = _node("n1", "host-a", "ready", "active", "manager")
    with patch("docker_mcp.tools.resources._get_client") as mock_client:
        mock_client.return_value.nodes.list.return_value = [node]
        list_host_node_resources("prod")
    mock_client.assert_called_once_with("prod")


def test_single_host_registers_bare_node_uri_end_to_end():
    uris = _registered_resource_uris(None)
    assert "docker://nodes" in uris


def test_multi_host_registers_node_uris_end_to_end():
    uris = _registered_resource_uris("local=ssh://a, prod=ssh://b")
    assert {"docker:///nodes", "docker://{host}/nodes"} <= uris
    assert "docker://nodes" not in uris


# ---------- tool_list: tool-callable mirror of docker-mcp://tool-catalog ----------


def test_tool_list_mirrors_query_catalog():
    # The tool is a thin pass-through, so behaviour cannot drift between the two entry points -- the
    # same reason docs_lookup calls list_docs_sections()/get_docs_section() rather than duplicating them.
    assert tool_list(domain="volumes") == query_catalog(domain="volumes")
    assert tool_list(category="destructive", keyword="prune") == query_catalog(category="destructive", keyword="prune")


def test_tool_list_gives_a_client_without_resources_the_same_registered_surface():
    # The acceptance question this answers: a client that cannot read MCP resources must not be
    # second-class. Every tool the catalog resource reports as registered is reachable through here.
    from docker_mcp.server import tool_catalog

    via_resource = {t["name"] for t in tool_catalog()["tools"] if t["registered"]}
    via_tool = {row["name"] for row in tool_list()["tools"]}
    assert via_tool == via_resource


def test_tool_list_briefing_on_a_domain_is_far_cheaper_than_the_definitions_it_replaces():
    # The point of one-line rows: orienting in an unfamiliar domain should not cost what fetching
    # every definition in it costs. Compared against the real advertised descriptions.
    import asyncio

    from docker_mcp.server import mcp

    rows = tool_list(domain="buildx")["tools"]
    assert rows
    briefing = sum(len(row["summary"]) for row in rows)
    registered = {t.name: t for t in asyncio.run(mcp.list_tools())}
    definitions = sum(len(registered[row["name"]].description or "") for row in rows)
    assert briefing * 4 < definitions, f"briefing {briefing} chars vs {definitions} of definitions"


def test_tool_list_is_registered_and_read_only():
    from docker_mcp.server import _NO_DOMAIN_TOOLS, TOOL_CATEGORIES, ToolCategory, _tool_registry

    assert TOOL_CATEGORIES["tool_list"] is ToolCategory.READ_ONLY
    assert "tool_list" in _NO_DOMAIN_TOOLS
    assert _tool_registry["tool_list"].domain is None


def test_an_unknown_docs_section_tells_the_caller_where_the_list_is(on_the_wire):
    """The message names the resource that lists the valid sections, so a client can recover in one
    step. Asserted on the wire because that is the only place it matters: the SDK withholds the text
    of anything it classifies as a crash, and a direct call cannot tell the two apart."""
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    with pytest.raises(ToolError) as excinfo:
        on_the_wire("docs_lookup", {"section": "no-such-section"})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "docker-docs://contents" in str(excinfo.value)

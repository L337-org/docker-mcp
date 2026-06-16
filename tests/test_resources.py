import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from docker_mcp.server import TOOL_CATEGORIES
from docker_mcp.tools.resources import (
    DOCKER_DOCS_BASE_URL,
    EXTERNAL_SECTIONS,
    SDK_SECTIONS,
    get_container_logs_resource,
    get_container_stats_resource,
    get_docs_section,
    get_tool_catalog,
    list_container_resources,
    list_docs_sections,
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
    assert "DOCKER_MCP_DISABLE" in payload["switches"]
    assert payload["domains"]  # per-domain summary is populated


# ---------- DOCKER_MCP_DISABLE also hides a disabled domain's doc sections ----------
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
    with pytest.raises(ValueError, match="disabled via DOCKER_MCP_DISABLE"):
        get_docs_section("scout")
    with pytest.raises(ValueError, match="disabled via DOCKER_MCP_DISABLE"):
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
        with pytest.raises(ValueError, match="disabled via DOCKER_MCP_DISABLE"):
            call()

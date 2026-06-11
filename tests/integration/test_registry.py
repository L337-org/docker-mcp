# integration tests for the registry/Hub tools — require network access to docker.io.
# These do NOT need a Docker daemon, so the daemon-required autouse fixture from
# tests/integration/conftest.py is overridden below.
# run with: uv run pytest -m integration

import httpx
import pytest

from docker_mcp.tools.registry import (
    hub_list_tags,
    hub_rate_limit,
    hub_repo_info,
    registry_get_config,
    registry_inspect_manifest,
    registry_list_tags,
)


@pytest.fixture(autouse=True, scope="module")
def skip_if_no_daemon():
    """Override the conftest fixture — these tests don't need a daemon."""
    yield


@pytest.fixture(autouse=True, scope="module")
def _skip_if_no_hub_network():
    """Skip the whole module if docker.io is unreachable so we don't fail on offline CI."""
    try:
        httpx.get("https://hub.docker.com/v2/", timeout=5.0)
    except httpx.HTTPError as exc:
        pytest.skip(f"Docker Hub unreachable; skipping registry integration tests: {exc}")
    yield


def test_registry_list_tags_alpine_public():
    result = registry_list_tags("alpine", limit=20)
    assert result["registry"] == "registry-1.docker.io"
    assert result["name"] == "library/alpine"
    assert result["tags"], "expected at least one tag for library/alpine"
    assert len(result["tags"]) <= 20


def test_registry_inspect_manifest_alpine_latest():
    result = registry_inspect_manifest("alpine", reference="latest")
    assert result["digest"].startswith("sha256:")
    assert result["media_type"]
    assert isinstance(result["manifest"], dict)


def test_hub_list_tags_alpine_returns_metadata():
    result = hub_list_tags("alpine", limit=10)
    assert result["name"] == "library/alpine"
    assert result["tags"]
    entry = result["tags"][0]
    # Hub returns these metadata keys; they may be None for some tags but the keys exist.
    assert "name" in entry
    assert "last_updated" in entry


def test_hub_repo_info_alpine_has_pull_count():
    info = hub_repo_info("alpine")
    assert info.get("name") == "alpine"
    assert info.get("user") == "library"
    assert isinstance(info.get("pull_count"), int)


def test_registry_get_config_alpine_amd64():
    result = registry_get_config("alpine", reference="latest", platform="linux/amd64")
    assert result["config_digest"].startswith("sha256:")
    # alpine publishes a multi-platform index, so a platform should have been selected.
    assert result["platform"] == "linux/amd64"
    config = result["config"]
    assert config.get("architecture") == "amd64"
    assert config.get("os") == "linux"
    # The config blob carries the runtime config (Cmd/Entrypoint live under "config").
    assert "config" in config


def test_hub_rate_limit_anonymous_returns_budget():
    result = hub_rate_limit()
    assert result["authenticated"] is False
    # Anonymous Hub pulls are metered, so we expect a numeric budget (unless Docker changes policy,
    # in which case "unlimited" would be True — accept either rather than asserting a brittle number).
    assert result["unlimited"] or isinstance(result["remaining"], int)

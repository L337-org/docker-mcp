# integration tests for the registry/Hub tools — require network access to docker.io.
# These do NOT need a Docker daemon, so the daemon-required autouse fixture from
# tests/integration/conftest.py is overridden below.
# run with: uv run pytest -m integration

import httpx
import pytest

from tools.registry import (
    hub_list_tags,
    hub_repo_info,
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

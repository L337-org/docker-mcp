# integration tests for compose — require a real Docker daemon AND the `docker compose` plugin.
# run with: uv run pytest -m integration

import uuid
from pathlib import Path

import pytest

from docker_mcp.tools._cli import has_plugin
from docker_mcp.tools.compose import (
    compose_config,
    compose_down,
    compose_images,
    compose_ls,
    compose_pause,
    compose_ps,
    compose_top,
    compose_unpause,
    compose_up,
)

# A tiny compose project: one alpine container that sleeps. Avoids pulling a large image
# while still exercising the up/ps/down cycle.
_COMPOSE_YAML = """\
services:
  sleeper:
    image: alpine:3
    command: ["sleep", "300"]
"""


@pytest.fixture(scope="module", autouse=True)
def _require_compose_plugin():
    if not has_plugin("compose"):
        pytest.skip("docker compose plugin not installed on this host; skipping compose integration tests")
    yield


@pytest.fixture
def compose_project(tmp_path: Path):
    """Create a temp compose project with a unique name and tear it down after the test."""
    project = f"docker-mcp-it-{uuid.uuid4().hex[:8]}"
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(_COMPOSE_YAML)
    yield {"dir": str(tmp_path), "name": project, "file": str(compose_file)}
    # Best-effort teardown; ignore failures so a missing project doesn't mask the real assertion.
    compose_down(project_dir=str(tmp_path), project_name=project, volumes=True, remove_orphans=True)


def test_compose_config_renders_yaml(compose_project):
    result = compose_config(project_dir=compose_project["dir"], project_name=compose_project["name"])
    assert result["raw"]["returncode"] == 0
    assert "sleeper" in (result["config"] or "")


def test_compose_config_json_parses(compose_project):
    result = compose_config(
        project_dir=compose_project["dir"],
        project_name=compose_project["name"],
        format="json",
    )
    assert result["raw"]["returncode"] == 0
    assert isinstance(result["config"], dict)
    assert "sleeper" in result["config"].get("services", {})


def _pull_or_skip(compose_project):
    """Pull project images upfront. Skip the test if the registry can't be reached in a reasonable time."""
    import subprocess

    from docker_mcp.tools.compose import compose_pull

    try:
        result = compose_pull(
            project_dir=compose_project["dir"],
            project_name=compose_project["name"],
            timeout_seconds=180.0,
        )
    except subprocess.TimeoutExpired:
        # A slow registry makes the pull subprocess time out (run_docker raises rather than returning
        # non-zero), so catch it here and skip cleanly instead of failing the lifecycle test.
        pytest.skip("compose pull timed out (slow network/registry); skipping")
    if result["returncode"] != 0:
        pytest.skip(f"could not pull compose project images (network/registry?): {result['stderr'][:200]}")


def test_compose_lifecycle_up_ps_down(compose_project):
    _pull_or_skip(compose_project)

    up_result = compose_up(
        project_dir=compose_project["dir"],
        project_name=compose_project["name"],
        timeout_seconds=120.0,
    )
    assert up_result["returncode"] == 0, up_result["stderr"]

    ps_result = compose_ps(project_dir=compose_project["dir"], project_name=compose_project["name"])
    assert ps_result["raw"]["returncode"] == 0
    service_names = {svc.get("Service") for svc in ps_result["services"]}
    assert "sleeper" in service_names

    down_result = compose_down(
        project_dir=compose_project["dir"],
        project_name=compose_project["name"],
        volumes=True,
        remove_orphans=True,
    )
    assert down_result["returncode"] == 0, down_result["stderr"]


def test_compose_ls_after_up_includes_project(compose_project):
    _pull_or_skip(compose_project)
    compose_up(
        project_dir=compose_project["dir"],
        project_name=compose_project["name"],
        timeout_seconds=120.0,
    )
    projects = compose_ls()
    names = {p.get("Name") for p in projects}
    assert compose_project["name"] in names


def test_compose_images_top_pause_unpause(compose_project):
    _pull_or_skip(compose_project)
    up = compose_up(
        project_dir=compose_project["dir"],
        project_name=compose_project["name"],
        timeout_seconds=120.0,
    )
    assert up["returncode"] == 0, up["stderr"]

    # images: the sleeper service runs an alpine image.
    images = compose_images(project_dir=compose_project["dir"], project_name=compose_project["name"])
    assert any("alpine" in (img.get("Repository") or "") for img in images)

    # top: the sleeper's process table is returned as raw stdout.
    top = compose_top(project_dir=compose_project["dir"], project_name=compose_project["name"])
    assert top["returncode"] == 0, top["stderr"]

    # pause then unpause round-trips cleanly.
    paused = compose_pause(project_dir=compose_project["dir"], project_name=compose_project["name"])
    assert paused["returncode"] == 0, paused["stderr"]
    unpaused = compose_unpause(project_dir=compose_project["dir"], project_name=compose_project["name"])
    assert unpaused["returncode"] == 0, unpaused["stderr"]

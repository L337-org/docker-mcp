"""Shared pytest configuration for integration tests.

Auto-marks every test in this directory with `@pytest.mark.integration` so
new files don't need to declare the marker, and provides a module-scoped
autouse `skip_if_no_daemon` fixture that skips the suite when the Docker
daemon is unreachable. `skip_if_no_swarm` is a separate, non-autouse
fixture that swarm-dependent test files opt into explicitly.
"""

from pathlib import Path

import pytest
from docker.errors import DockerException

from docker_mcp.tools.system import system_info, system_ping

_INTEGRATION_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _INTEGRATION_DIR in Path(item.path).parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True, scope="module")
def skip_if_no_daemon():
    try:
        system_ping()
    except (DockerException, RuntimeError) as exc:
        pytest.skip(f"Docker daemon not reachable: {exc}")


@pytest.fixture(scope="module")
def skip_if_no_swarm():
    """Skip a test module's tests if the daemon isn't a swarm manager (`docker swarm init` first)."""
    info = system_info()
    if (info.get("Swarm") or {}).get("LocalNodeState") != "active":
        pytest.skip("Docker daemon is not a swarm manager (run `docker swarm init` first)")

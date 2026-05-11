"""Shared pytest configuration for integration tests.

Auto-marks every test in this directory with `@pytest.mark.integration` so
new files don't need to declare the marker, and provides a module-scoped
autouse `skip_if_no_daemon` fixture that skips the suite when the Docker
daemon is unreachable.
"""

from pathlib import Path

import pytest
from docker.errors import DockerException

from tools.client import ping

_INTEGRATION_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _INTEGRATION_DIR in Path(item.path).parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True, scope="module")
def skip_if_no_daemon():
    try:
        ping()
    except (DockerException, RuntimeError) as exc:
        pytest.skip(f"Docker daemon not reachable: {exc}")

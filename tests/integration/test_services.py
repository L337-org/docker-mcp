# integration tests for swarm service tools that need a real daemon in swarm mode.
# run with: uv run pytest -m integration (requires `docker swarm init` first)

import json
import uuid

import pytest

from docker_mcp.tools.services import (
    service_create,
    service_remove,
    service_scale,
)
from docker_mcp.tools.resources import get_service_tasks_resource
from docker_mcp.tools.system import system_wait


def _wait_running(service):
    return system_wait(kind="service", target=service, until="running", timeout_seconds=30, poll_interval=1.0)


pytestmark = pytest.mark.usefixtures("skip_if_no_swarm")


@pytest.fixture
def running_service():
    """Create a tiny replicated service; remove it afterwards."""
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    try:
        # init: `sleep` as PID 1 ignores SIGTERM, so without one each removal waits out the grace period.
        service_create(
            "alpine:3",
            command=["sleep", "120"],
            extra_kwargs={"name": name, "init": True, "mode": {"Replicated": {"Replicas": 2}}},
        )
        yield name
    finally:
        try:
            service_remove(name)
        except Exception:  # noqa: S110, BLE001 — best-effort teardown, don't mask the real test failure
            pass


def test_system_wait_service_running_converges_real(running_service):
    result = _wait_running(running_service)
    assert result["met"] is True
    assert result["detail"]["running_tasks"] == 2
    assert result["detail"]["desired_tasks"] == 2
    assert result["timed_out"] is False


def test_system_wait_service_running_converges_after_scale_real(running_service):
    _wait_running(running_service)
    service_scale(running_service, 3)
    result = _wait_running(running_service)
    assert result["met"] is True
    assert result["detail"]["running_tasks"] == 3


def test_service_resource_reflects_running_service(running_service):
    _wait_running(running_service)
    summary = json.loads(get_service_tasks_resource(running_service))
    assert summary["running_tasks"] == 2

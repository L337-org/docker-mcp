# integration tests for swarm service tools that need a real daemon in swarm mode.
# run with: uv run pytest -m integration (requires `docker swarm init` first)

import json
import uuid

import pytest

from docker_mcp.tools.services import service_create, service_remove, service_scale, service_wait
from docker_mcp.tools.resources import get_service_tasks_resource, list_service_resources

pytestmark = pytest.mark.usefixtures("skip_if_no_swarm")


@pytest.fixture
def running_service():
    """Create a tiny replicated service; remove it afterwards."""
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    try:
        service_create(
            "alpine:3",
            command=["sleep", "120"],
            extra_kwargs={"name": name, "mode": {"Replicated": {"Replicas": 2}}},
        )
        yield name
    finally:
        try:
            service_remove(name)
        except Exception:  # noqa: S110, BLE001 — best-effort teardown, don't mask the real test failure
            pass


def test_service_wait_running_converges_real(running_service):
    result = service_wait(running_service, until="running", timeout_seconds=30, poll_interval=1.0)
    assert result["met"] is True
    assert result["running_tasks"] == 2
    assert result["desired_tasks"] == 2
    assert result["timed_out"] is False


def test_service_wait_running_converges_after_scale_real(running_service):
    service_wait(running_service, until="running", timeout_seconds=30, poll_interval=1.0)
    service_scale(running_service, 3)
    result = service_wait(running_service, until="running", timeout_seconds=30, poll_interval=1.0)
    assert result["met"] is True
    assert result["running_tasks"] == 3


def test_service_resource_reflects_running_service(running_service):
    index = json.loads(list_service_resources())
    assert any(s["name"] == running_service for s in index["services"])
    service_wait(running_service, until="running", timeout_seconds=30, poll_interval=1.0)
    summary = json.loads(get_service_tasks_resource(running_service))
    assert summary["running_tasks"] == 2

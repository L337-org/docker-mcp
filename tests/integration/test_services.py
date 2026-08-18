# integration tests for swarm service tools that need a real daemon in swarm mode.
# run with: uv run pytest -m integration (requires `docker swarm init` first)

import json
import uuid

import pytest

from docker.errors import NotFound

from docker_mcp.tools.services import (
    service_create,
    service_ps,
    service_remove,
    service_scale,
    service_wait,
    swarm_task_inspect,
    swarm_task_list,
)
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


def test_swarm_task_list_sees_the_service_and_agrees_with_service_ps(running_service):
    service_wait(running_service, until="running", timeout_seconds=30, poll_interval=1.0)
    per_service = service_ps(running_service)
    assert per_service

    # The cluster-wide read is a superset of the per-service one, and the `service` filter narrows
    # it back down to exactly what `service_ps` returns -- the equivalence the docstring promises
    # when it tells callers to prefer `service_ps` for a single service.
    cluster_wide = swarm_task_list()
    assert {t["ID"] for t in per_service} <= {t["ID"] for t in cluster_wide}
    filtered = swarm_task_list(filters={"service": running_service})
    assert {t["ID"] for t in filtered} == {t["ID"] for t in per_service}


def test_swarm_task_inspect_resolves_id_prefix_and_full_name_but_not_the_ps_name(running_service):
    service_wait(running_service, until="running", timeout_seconds=30, poll_interval=1.0)
    task = service_ps(running_service)[0]

    assert swarm_task_inspect(task["ID"])["ID"] == task["ID"]
    assert swarm_task_inspect(task["ID"][:8])["ID"] == task["ID"]

    # A task's resolvable name is the container-name form, not the `<service>.<slot>` that
    # `docker service ps` prints. Both halves are asserted because the docstring warns about the
    # difference: if the daemon ever starts resolving the short form, this fails and the docstring
    # gets updated rather than quietly becoming wrong.
    full_name = f"{running_service}.{task['Slot']}.{task['ID']}"
    assert swarm_task_inspect(full_name)["ID"] == task["ID"]
    with pytest.raises(NotFound):
        swarm_task_inspect(f"{running_service}.{task['Slot']}")

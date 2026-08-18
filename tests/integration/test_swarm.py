# integration tests for the cluster-wide swarm task tools, which need a real daemon in swarm mode.
# run with: uv run pytest -m integration (requires `docker swarm init` first)
#
# The swarm lifecycle tools (init/join/leave/unlock) have no integration coverage on purpose -- they
# reconfigure the daemon the whole suite runs against. These two only read, so they are testable.

import uuid

import pytest
from docker.errors import NotFound

from docker_mcp.tools.services import service_create, service_ps, service_remove, service_wait
from docker_mcp.tools.swarm import swarm_task_inspect, swarm_task_list

pytestmark = pytest.mark.usefixtures("skip_if_no_swarm")


@pytest.fixture
def running_service():
    """One single-replica service, so there is a task to find; removed afterwards.

    Deliberately a local copy rather than a shared fixture: this module only needs *a* task to
    exist, while test_services.py's equivalent runs two replicas because its own assertions count
    them, and coupling the two would make either test's replica count load-bearing for the other.
    """
    name = f"dmcp-it-task-{uuid.uuid4().hex[:8]}"
    try:
        service_create(
            "alpine:3",
            command=["sleep", "120"],
            extra_kwargs={"name": name, "mode": {"Replicated": {"Replicas": 1}}},
        )
        yield name
    finally:
        try:
            service_remove(name)
        except Exception:  # noqa: S110, BLE001 -- best-effort teardown, don't mask the real failure
            pass


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

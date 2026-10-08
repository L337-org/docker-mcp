# integration tests for reading swarm tasks - every task in the swarm, one task found by filter, and
# one task's logs - which need a real daemon in swarm mode. These jobs are done by `service_ps` and
# `service_logs`, but are kept here, apart from the per-service tests in test_services.py, because what
# they pin is the daemon's own task resolution.
# run with: uv run pytest -m integration (requires `docker swarm init` first)
#
# The swarm lifecycle tools (init/join/leave/unlock) have no integration coverage on purpose -- they
# reconfigure the daemon the whole suite runs against. The task reads only read, so they are testable
# against the daemon the suite already uses.

import uuid

import pytest

from docker_mcp.tools.services import service_create, service_logs, service_ps, service_remove, service_wait

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


def test_service_ps_without_a_service_sees_every_task_and_agrees_with_the_per_service_view(running_service):
    service_wait(running_service, until="running", timeout_seconds=30, poll_interval=1.0)
    per_service = service_ps(running_service)
    assert per_service

    # The cluster-wide read is a superset of the per-service one, and the `service` filter narrows
    # it back down to exactly what naming the service returns.
    cluster_wide = service_ps()
    assert {t["ID"] for t in per_service} <= {t["ID"] for t in cluster_wide}
    filtered = service_ps(filters={"service": running_service})
    assert {t["ID"] for t in filtered} == {t["ID"] for t in per_service}


def test_service_ps_finds_one_task_by_id_prefix_full_name_and_slot_name(running_service):
    """The list filters do the single-task lookup the removed `swarm_task_inspect` did.

    moby maps the `id` and `name` filters to prefix matches against the task's full name, which is
    what `service_ps`'s description now promises - including that `<service>.<slot>`, which inspect
    could not resolve, finds that slot's tasks. If the daemon changes any of this, the description
    is wrong and this fails rather than it quietly becoming so.
    """
    service_wait(running_service, until="running", timeout_seconds=30, poll_interval=1.0)
    task = service_ps(running_service)[0]

    assert [t["ID"] for t in service_ps(filters={"id": task["ID"]})] == [task["ID"]]
    assert task["ID"] in {t["ID"] for t in service_ps(filters={"id": task["ID"][:8]})}

    full_name = f"{running_service}.{task['Slot']}.{task['ID']}"
    assert [t["ID"] for t in service_ps(filters={"name": full_name})] == [task["ID"]]
    assert task["ID"] in {t["ID"] for t in service_ps(filters={"name": f"{running_service}.{task['Slot']}"})}


def test_service_logs_reads_a_real_task_through_the_hand_built_route(running_service):
    """The route and the frame handling only prove out against a real daemon.

    `service_logs(task=...)` builds `/tasks/{id}/logs` itself, because docker-py exposes no task-logs
    method to get it wrong on our behalf: a mistyped path or the wrong TTY mode is invisible to the
    unit tests, which mock the very helpers that would reject it.

    Single-node only, and deliberately so: this suite runs against one daemon. That does not weaken
    the check, because the call asks the manager and the manager fetches from whichever node holds
    the task, so the code path is identical - what a multi-node cluster would add is coverage of the
    Engine's own forwarding, not of anything here.
    """
    service_wait(running_service, until="running", timeout_seconds=120)
    tasks = service_ps(running_service)
    assert tasks, "expected the service to have produced a task"

    logs = service_logs(task=tasks[0]["ID"], tail=10)

    assert isinstance(logs, str)
    # `sleep 120` prints nothing, so an empty string is the correct result. What matters is that the
    # daemon accepted the route and the demultiplexer left no 8-byte frame headers behind: those
    # start with a stream byte of 0x01 or 0x02 followed by three NULs, which is not valid log text.
    assert "\x00" not in logs, f"frame headers survived demultiplexing: {logs[:60]!r}"

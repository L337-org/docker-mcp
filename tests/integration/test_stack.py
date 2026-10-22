# integration tests for the stack tools — require a real Docker daemon that is ALSO a swarm manager.
# We never init a swarm here (that would mutate the host); the module skips cleanly when the daemon
# isn't already a manager. run with: uv run pytest -m integration

import uuid

import pytest

from docker_mcp.tools.system import _get_client, system_info, system_wait
from docker_mcp.tools.stack import stack_deploy, stack_list, stack_ps, stack_remove, stack_services

# A minimal stack: one alpine service that sleeps. `deploy.replicas` is what makes it a swarm service.
# Give every service here `init: true`: `sleep` as PID 1 ignores SIGTERM, so without an init every
# update and removal waits out the stop grace period before the task is killed.
_STACK_YAML = """\
services:
  sleeper:
    image: alpine:3
    init: true
    command: ["sleep", "300"]
    deploy:
      replicas: 1
"""


@pytest.fixture(scope="module", autouse=True)
def _require_swarm_manager():
    swarm = system_info().get("Swarm", {})
    if not swarm.get("ControlAvailable"):
        pytest.skip("Docker daemon is not a swarm manager; skipping stack integration tests")
    yield


@pytest.fixture
def deployed_stack(tmp_path):
    """Deploy a uniquely-named stack and tear it down afterwards."""
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    compose_file = tmp_path / "stack.yml"
    compose_file.write_text(_STACK_YAML, encoding="utf-8")
    result = stack_deploy(name, compose_files=[str(compose_file)])
    assert result["returncode"] == 0, result["stderr"]
    yield name
    # Teardown; a stack a test already removed is reported as not found rather than failing.
    stack_remove([name])


def test_stack_lifecycle(deployed_stack):
    name = deployed_stack
    assert {"name": name, "services": 1} in stack_list()
    [service] = stack_services(name)
    assert (service["name"], service["mode"], service["desired_tasks"]) == (f"{name}_sleeper", "replicated", 1)
    assert service["image"].startswith("alpine:3")
    # The scheduler creates the task just after deploy returns, so wait for the stack before reading it.
    assert system_wait(kind="stack", target=name, until="converged", timeout_seconds=120, poll_interval=1.0)["met"]
    [task] = stack_ps(name, filters={"desired-state": "running"})
    assert (task["name"], task["service"], task["state"], task["error"]) == (
        f"{name}_sleeper.1",
        f"{name}_sleeper",
        "running",
        None,
    )
    # The hostname of whichever node runs it, which on a multi-node swarm need not be this one.
    assert task["node"] in {n.attrs["Description"]["Hostname"] for n in _get_client().nodes.list()}


def test_stack_remove_reports_what_it_removed_and_what_was_not_there(deployed_stack):
    [removed, missing] = stack_remove([deployed_stack, f"{deployed_stack}-absent"])
    assert removed["found"] is True and removed["failed"] == []
    assert removed["removed"]["services"] == [f"{deployed_stack}_sleeper"]
    assert removed["removed"]["networks"] == [f"{deployed_stack}_default"]
    assert missing == {
        "name": f"{deployed_stack}-absent",
        "found": False,
        "removed": {"services": [], "secrets": [], "configs": [], "networks": []},
        "failed": [],
    }
    assert {"name": deployed_stack, "services": 1} not in stack_list()


def test_stack_ps_names_a_global_service_task_by_its_node_on_every_node(tmp_path):
    # One task per node, each named by node id and placed by hostname: the two things a single-node swarm
    # cannot tell apart from a fixed answer.
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    compose_file = tmp_path / "stack.yml"
    compose_file.write_text(
        'services:\n  agent:\n    image: alpine:3\n    init: true\n    command: ["sleep", "300"]\n'
        "    deploy:\n      mode: global\n",
        encoding="utf-8",
    )
    nodes = {n.id: n.attrs["Description"]["Hostname"] for n in _get_client().nodes.list()}
    try:
        assert stack_deploy(name, compose_files=[str(compose_file)])["returncode"] == 0
        assert system_wait(kind="stack", target=name, until="converged", timeout_seconds=180, poll_interval=1.0)["met"]
        [service] = stack_services(name)
        assert (service["mode"], service["running_tasks"], service["desired_tasks"]) == (
            "global",
            len(nodes),
            len(nodes),
        )
        tasks = stack_ps(name, filters={"desired-state": "running"})
        assert sorted(t["name"] for t in tasks) == sorted(f"{name}_agent.{node_id}" for node_id in nodes)
        assert sorted(t["node"] for t in tasks) == sorted(nodes.values())
    finally:
        stack_remove([name])


def test_a_global_service_rejected_on_one_node_does_not_converge(tmp_path):
    # A file made on the manager exists on no other node, so a global service bind-mounting it runs on the
    # manager and is rejected on every other node, retried, and rejected again. Counting desired-running
    # tasks drops a rejected node from both counts and reads converged between retries.
    client = _get_client()
    if len(client.nodes.list()) < 2:
        pytest.skip("needs a swarm of two or more nodes for one of them to reject the task")
    marker = f"/tmp/dmcp-it-{uuid.uuid4().hex[:8]}"
    client.containers.run("alpine:3", ["touch", f"/host{marker}"], volumes={"/tmp": {"bind": "/host/tmp"}}, remove=True)
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    compose_file = tmp_path / "stack.yml"
    compose_file.write_text(
        'services:\n  agent:\n    image: alpine:3\n    init: true\n    command: ["sleep", "300"]\n'
        f"    volumes:\n      - type: bind\n        source: {marker}\n        target: /marker\n"
        "    deploy:\n      mode: global\n",
        encoding="utf-8",
    )
    try:
        assert stack_deploy(name, compose_files=[str(compose_file)])["returncode"] == 0
        waited = system_wait(kind="stack", target=name, until="converged", timeout_seconds=30, poll_interval=0.5)
        assert (waited["met"], waited["timed_out"]) == (False, True), waited
        [row] = waited["detail"]["services"]
        assert (row["current_tasks"], row["desired_tasks"]) == (1, len(client.nodes.list())), row
        service = system_wait(
            kind="service", target=f"{name}_agent", until="running", timeout_seconds=10, poll_interval=0.5
        )
        assert (service["met"], service["timed_out"]) == (False, True), service
    finally:
        stack_remove([name])
        client.containers.run(
            "alpine:3", ["rm", "-f", f"/host{marker}"], volumes={"/tmp": {"bind": "/host/tmp"}}, remove=True
        )


def test_stack_remove_removes_the_secrets_and_configs_a_stack_created(tmp_path):
    # File-based secrets and configs are created by the deploy and carry the stack label, so removal must
    # take them too - after the service using them, or the Engine refuses them as in use.
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    (tmp_path / "token.txt").write_text(uuid.uuid4().hex, encoding="utf-8")
    (tmp_path / "app.conf").write_text("level=debug\n", encoding="utf-8")
    compose_file = tmp_path / "stack.yml"
    compose_file.write_text(
        'services:\n  sleeper:\n    image: alpine:3\n    init: true\n    command: ["sleep", "300"]\n'
        "    secrets: [token]\n    configs: [app]\n"
        "secrets:\n  token:\n    file: ./token.txt\n"
        "configs:\n  app:\n    file: ./app.conf\n",
        encoding="utf-8",
    )
    try:
        assert stack_deploy(name, compose_files=[str(compose_file)], cwd=str(tmp_path))["returncode"] == 0
        assert system_wait(kind="stack", target=name, until="converged", timeout_seconds=120, poll_interval=1.0)["met"]
        [result] = stack_remove([name])
        assert result["failed"] == [], result
        assert result["removed"] == {
            "services": [f"{name}_sleeper"],
            "secrets": [f"{name}_token"],
            "configs": [f"{name}_app"],
            "networks": [f"{name}_default"],
        }
        assert system_wait(kind="stack", target=name, until="removed", timeout_seconds=120, poll_interval=1.0)["met"]
        [again] = stack_remove([name])
        assert again["found"] is False, again
    finally:
        stack_remove([name])


def test_system_wait_stack_converges_then_is_removed(deployed_stack):
    converged = system_wait(
        kind="stack", target=deployed_stack, until="converged", timeout_seconds=120, poll_interval=1.0
    )
    assert (converged["met"], converged["timed_out"]) == (True, False)
    assert [s["running_tasks"] for s in converged["detail"]["services"]] == [1]
    # The running task must be found by the stack label too, or "removed" would be met while it still runs.
    assert converged["detail"]["active_tasks"] == 1

    [result] = stack_remove([deployed_stack])
    assert result["found"] is True and result["failed"] == [], result
    removed = system_wait(kind="stack", target=deployed_stack, until="removed", timeout_seconds=120, poll_interval=1.0)
    assert (removed["met"], removed["timed_out"]) == (True, False)
    assert removed["detail"] == {"services": [], "active_tasks": 0}


def test_system_wait_stack_converged_waits_out_a_rolling_update(tmp_path):
    # A redeploy keeps the old tasks running until each is replaced, so a count-only check would be met
    # while the update is still rolling. One replica at a time, 3 s apart, makes the rollout observable.
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    compose_file = tmp_path / "stack.yml"

    def write(command):
        compose_file.write_text(
            "services:\n  sleeper:\n    image: alpine:3\n    init: true\n"
            f"    command: {command}\n"
            "    deploy:\n      replicas: 3\n      update_config:\n        parallelism: 1\n        delay: 3s\n",
            encoding="utf-8",
        )

    try:
        write('["sleep", "300"]')
        assert stack_deploy(name, compose_files=[str(compose_file)])["returncode"] == 0
        first = system_wait(kind="stack", target=name, until="converged", timeout_seconds=120, poll_interval=1.0)
        assert first["met"] is True

        write('["sleep", "301"]')
        assert stack_deploy(name, compose_files=[str(compose_file)])["returncode"] == 0
        second = system_wait(kind="stack", target=name, until="converged", timeout_seconds=180, poll_interval=1.0)
        assert (second["met"], second["timed_out"]) == (True, False)
        # Every running task is on the redeployed spec, whether or not swarm has recorded the update yet.
        [service] = second["detail"]["services"]
        assert (service["current_tasks"], service["desired_tasks"]) == (3, 3)
        assert service["update_state"] in (None, "completed")
    finally:
        stack_remove([name])

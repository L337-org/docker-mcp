# integration tests for the stack tools — require a real Docker daemon that is ALSO a swarm manager.
# We never init a swarm here (that would mutate the host); the module skips cleanly when the daemon
# isn't already a manager. run with: uv run pytest -m integration

import uuid

import pytest

from docker_mcp.tools.system import system_info, system_wait
from docker_mcp.tools.stack import stack_deploy, stack_list, stack_ps, stack_remove, stack_services

# A minimal stack: one alpine service that sleeps. `deploy.replicas` is what makes it a swarm service.
_STACK_YAML = """\
services:
  sleeper:
    image: alpine:3
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
    assert task["node"] == system_info()["Name"]


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
            "services:\n  sleeper:\n    image: alpine:3\n"
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

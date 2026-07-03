# integration tests for the stack tools — require a real Docker daemon that is ALSO a swarm manager.
# We never init a swarm here (that would mutate the host); the module skips cleanly when the daemon
# isn't already a manager. run with: uv run pytest -m integration

import uuid

import pytest

from docker_mcp.tools.system import system_info
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
    compose_file.write_text(_STACK_YAML)
    result = stack_deploy(name, compose_files=[str(compose_file)])
    assert result["returncode"] == 0, result["stderr"]
    yield name
    # Best-effort teardown; ignore failures so a missing stack doesn't mask the real assertion.
    stack_remove([name])


def test_stack_lifecycle(deployed_stack):
    name = deployed_stack
    # The stack appears in the list.
    assert any(s.get("Name") == name for s in stack_list())
    # Its single service is present.
    services = stack_services(name)
    assert any(svc.get("Name", "").startswith(name) for svc in services)
    # stack_ps returns a list of tasks (may be empty for a beat while the task schedules).
    assert isinstance(stack_ps(name), list)

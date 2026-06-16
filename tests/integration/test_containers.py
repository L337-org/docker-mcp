# integration tests for container tools that need a real daemon (and to actually run a container).
# run with: uv run pytest -m integration

import uuid

import pytest

from docker_mcp.tools.containers import (
    list_containers,
    remove_container,
    run_container,
    wait_for_container_healthy,
)


@pytest.fixture
def healthy_container():
    """Run a tiny container whose healthcheck passes immediately; remove it afterwards."""
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    # Healthcheck intervals are nanoseconds in the Engine API; "exit 0" is always healthy.
    healthcheck = {
        "test": ["CMD-SHELL", "exit 0"],
        "interval": 1_000_000_000,
        "timeout": 1_000_000_000,
        "retries": 1,
    }
    try:
        run_container(
            "alpine:3",
            command=["sleep", "120"],
            name=name,
            extra_kwargs={"healthcheck": healthcheck},
        )
    except Exception as exc:  # noqa: BLE001 — image pull / run can fail on a constrained CI; skip cleanly
        pytest.skip(f"could not start the healthcheck container (pull/run failed?): {exc}")
    yield name
    remove_container(name, force=True)


def test_wait_for_container_healthy_real(healthy_container):
    result = wait_for_container_healthy(healthy_container, timeout=30, poll_interval=1.0)
    assert result["healthy"] is True
    assert result["health"] == "healthy"
    assert result["timed_out"] is False


def test_run_container_stamps_provenance_and_managed_only_filters(healthy_container):
    # The container started by the fixture should carry the managed label by default,
    # and `managed_only=True` should be able to find it.
    matched = list_containers(all=True, managed_only=True)
    names = {name.lstrip("/") for c in matched for name in [c["Name"]]}
    assert healthy_container in names
    target = next(c for c in matched if c["Name"].lstrip("/") == healthy_container)
    assert target["Config"]["Labels"]["docker-mcp-server.managed"] == "true"
    assert target["Config"]["Labels"]["docker-mcp-server.tool"] == "run_container"

# integration tests for container tools that need a real daemon (and to actually run a container).
# run with: uv run pytest -m integration

import json
import uuid

import pytest

from docker_mcp.tools.containers import (
    container_stats,
    container_list,
    container_remove,
    container_run,
    container_wait_healthy,
)
from docker_mcp.tools.resources import (
    get_container_logs_resource,
    get_container_stats_resource,
    list_container_resources,
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
        container_run(
            "alpine:3",
            command=["sleep", "120"],
            name=name,
            extra_kwargs={"healthcheck": healthcheck},
        )
    except Exception as exc:  # noqa: BLE001 — image pull / run can fail on a constrained CI; skip cleanly
        pytest.skip(f"could not start the healthcheck container (pull/run failed?): {exc}")
    yield name
    container_remove(name, force=True)


def test_wait_for_container_healthy_real(healthy_container):
    result = container_wait_healthy(healthy_container, timeout=30, poll_interval=1.0)
    assert result["healthy"] is True
    assert result["health"] == "healthy"
    assert result["timed_out"] is False


def test_run_container_stamps_provenance_and_managed_only_filters(healthy_container):
    # The container started by the fixture should carry the managed label by default,
    # and `managed_only=True` should be able to find it.
    matched = container_list(all=True, managed_only=True)
    names = {name.lstrip("/") for c in matched for name in [c["Name"]]}
    assert healthy_container in names
    target = next(c for c in matched if c["Name"].lstrip("/") == healthy_container)
    assert target["Config"]["Labels"]["docker-mcp-server.managed"] == "true"
    assert target["Config"]["Labels"]["docker-mcp-server.tool"] == "container_run"


def test_container_observability_resources_against_real_container(healthy_container):
    # The index lists the running container with both a logs and a stats URI.
    index = json.loads(list_container_resources())
    entry = next(c for c in index["containers"] if c["name"] == healthy_container)
    assert entry["status"] == "running"
    assert entry["logs"] == f"docker-logs://{healthy_container}"
    assert entry["stats"] == f"docker-stats://{healthy_container}"

    # Logs resource returns a string (the container may be quiet; just assert the type and no error).
    assert isinstance(get_container_logs_resource(healthy_container), str)

    # Stats resource returns the computed summary with the expected numeric keys.
    stats = json.loads(get_container_stats_resource(healthy_container))
    assert stats["container"] == healthy_container
    for key in ("cpu_percent", "mem_used_mb", "mem_limit_mb", "mem_percent"):
        assert isinstance(stats[key], (int, float))


def test_container_stats_tool_against_real_container(healthy_container):
    # Regression guard for the decode/stream bug: container_stats must not raise against a real
    # daemon and must return the raw stats snapshot (the earlier decode=True+stream=False combo
    # was rejected by the engine).
    snapshot = container_stats(healthy_container)
    assert isinstance(snapshot, dict)
    # The raw snapshot carries the cgroup sections the summary is derived from.
    assert "memory_stats" in snapshot
    assert "cpu_stats" in snapshot

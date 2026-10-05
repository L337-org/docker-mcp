# integration tests for container tools that need a real daemon (and to actually run a container).
# run with: uv run pytest -m integration

import json
import re
import time
import uuid

import pytest
from docker.errors import NotFound

from docker_mcp.tools.containers import (
    container_stats,
    container_exec,
    container_exec_inspect,
    container_inspect,
    container_list,
    container_remove,
    container_run,
    container_wait,
)
from docker_mcp.tools.resources import get_container_logs_resource, get_container_stats_resource
from tests.integration.conftest import fail_unless_environmental_error


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
    except Exception as exc:  # noqa: BLE001 — narrowed below: only a named environmental cause skips
        fail_unless_environmental_error(exc, what="starting the healthcheck container")
    yield name
    container_remove(name, force=True)


def test_container_wait_healthy_real(healthy_container):
    result = container_wait(healthy_container, until="healthy", timeout_seconds=30, poll_interval=1.0)
    assert result["met"] is True
    assert result["health"] == "healthy"
    assert result["timed_out"] is False


@pytest.fixture
def log_emitting_container():
    """Run a container that sleeps briefly then logs a marker line; remove it afterwards."""
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    try:
        container_run(
            "alpine:3",
            command=["sh", "-c", "sleep 2; echo READY_MARKER; sleep 120"],
            name=name,
        )
    except Exception as exc:  # noqa: BLE001 — narrowed below: only a named environmental cause skips
        fail_unless_environmental_error(exc, what="starting the log-emitting container")
    yield name
    container_remove(name, force=True)


def test_container_wait_log_match_real(log_emitting_container):
    result = container_wait(
        log_emitting_container, until="log-match", pattern="READY_MARKER", timeout_seconds=15, poll_interval=0.5
    )
    assert result["met"] is True
    assert result["matched_line"] == "READY_MARKER"


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


def test_container_exec_inspect_round_trips_a_detached_exec(log_emitting_container):
    # The whole point of the tool: a detached exec returns no exit code, so this is the only route
    # to its outcome. Start one that outlives the assertions, find it by id, then read it to the end.
    started = container_exec(log_emitting_container, ["sh", "-c", "sleep 3; exit 7"], detach=True)
    assert started["exit_code"] is None

    exec_ids = container_inspect(log_emitting_container).get("ExecIDs") or []
    assert len(exec_ids) == 1, f"expected exactly one running exec, got {exec_ids}"
    exec_id = exec_ids[0]

    running = container_exec_inspect(exec_id)
    assert running["ID"] == exec_id
    assert running["Running"] is True
    assert running["ExitCode"] is None

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        finished = container_exec_inspect(exec_id)
        if not finished["Running"]:
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"exec {exec_id} was still running after 30s")

    # Inspectable by id after it has finished, even though it has left the container's `ExecIDs`.
    assert finished["ExitCode"] == 7
    assert exec_id not in (container_inspect(log_emitting_container).get("ExecIDs") or [])


def test_container_exec_inspect_stops_answering_once_the_container_is_removed():
    # The docstring promises the id stops working at once when the container goes, not after the
    # daemon's periodic clean-up. Its own container rather than a fixture, because removing it is
    # the step under test and a fixture's teardown would then fail on a container already gone.
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    try:
        container_run("alpine:3", command=["sleep", "120"], name=name)
    except Exception as exc:  # noqa: BLE001 - narrowed below: only a named environmental cause skips
        fail_unless_environmental_error(exc, what="starting the exec-inspect removal container")
    removed = False
    try:
        container_exec(name, ["sleep", "60"], detach=True)
        exec_ids = container_inspect(name).get("ExecIDs") or []
        assert len(exec_ids) == 1, f"expected exactly one running exec, got {exec_ids}"
        exec_id = exec_ids[0]
        assert container_exec_inspect(exec_id)["Running"] is True

        container_remove(name, force=True)
        removed = True
        with pytest.raises(NotFound, match=re.escape(f"No such exec instance: {exec_id}")):
            container_exec_inspect(exec_id)
    finally:
        if not removed:
            container_remove(name, force=True)

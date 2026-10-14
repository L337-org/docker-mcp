# integration tests for container tools that need a real daemon (and to actually run a container).
# run with: uv run pytest -m integration

import json
import time
import uuid

import pytest

from docker_mcp.tools.containers import (
    container_exec,
    container_logs,
    container_stats,
    container_list,
    container_remove,
    container_run,
)
from docker_mcp.tools.resources import get_container_logs_resource, get_container_stats_resource
from tests.integration.conftest import fail_unless_environmental_error
from docker_mcp.tools.system import _get_client, system_wait


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


def test_system_wait_container_healthy_real(healthy_container):
    result = system_wait(
        kind="container", target=healthy_container, until="healthy", timeout_seconds=30, poll_interval=1.0
    )
    assert result["met"] is True
    assert result["detail"]["health"] == "healthy"
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


def test_system_wait_container_log_match_real(log_emitting_container):
    result = system_wait(
        kind="container",
        target=log_emitting_container,
        until="log-match",
        pattern="READY_MARKER",
        timeout_seconds=15,
        poll_interval=0.5,
    )
    assert result["met"] is True
    assert result["detail"]["matched_line"] == "READY_MARKER"


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


def test_a_one_shot_container_run_gives_its_exit_code_and_output_afterwards():
    # container_run never blocks, so a one-shot command's result comes from waiting and then reading.
    name = f"docker-mcp-it-oneshot-{uuid.uuid4().hex[:8]}"
    started = container_run("alpine:3", command=["sh", "-c", "echo one-shot-output; exit 3"], name=name)
    try:
        assert started["Name"].lstrip("/") == name
        # The payload describes the started container, not the one docker-py fetched before starting it.
        assert started["State"]["Status"] != "created"
        done = system_wait(kind="container", target=name, until="not-running", timeout_seconds=60)
        assert (done["met"], done["detail"]["status_code"]) == (True, 3)
        assert container_logs(name).strip() == "one-shot-output"
    finally:
        container_remove(name, force=True)


def test_a_one_shot_container_run_with_wait_seconds_needs_one_call():
    name = f"docker-mcp-it-oneshot-wait-{uuid.uuid4().hex[:8]}"
    result = container_run(
        "alpine:3",
        command=["sh", "-c", "echo out; echo err >&2; exit 3"],
        name=name,
        wait_seconds=60,
        auto_remove=True,
    )
    try:
        assert (result["exit_code"], result["timed_out"], result["truncated"]) == (3, False, False)
        assert sorted(result["output"].split()) == ["err", "out"]
        assert result["removed"] is True
        assert not container_list(all=True, filters={"name": name})
    finally:
        if not result.get("removed"):
            container_remove(name, force=True)


def test_a_one_shot_container_run_that_outlives_wait_seconds_is_left_running():
    name = f"docker-mcp-it-oneshot-slow-{uuid.uuid4().hex[:8]}"
    try:
        result = container_run(
            "alpine:3", command=["sh", "-c", "echo started; sleep 300"], name=name, wait_seconds=2, auto_remove=True
        )
        assert (result["timed_out"], result["exit_code"], result["removed"]) == (True, None, False)
        assert result["output"].strip() == "started"
        assert container_list(filters={"name": name})[0]["State"]["Status"] == "running"
    finally:
        container_remove(name, force=True)


@pytest.fixture
def exec_target():
    """A running container to exec into, removed afterwards."""
    name = f"docker-mcp-it-exec-{uuid.uuid4().hex[:8]}"
    container_run("alpine:3", command=["sleep", "300"], name=name)
    yield name
    container_remove(name, force=True)


def test_container_exec_returns_output_and_exit_code(exec_target):
    result = container_exec(exec_target, ["sh", "-c", "echo hello; exit 5"])
    assert (result["exit_code"], result["output"], result["timed_out"], result["truncated"]) == (
        5,
        "hello\n",
        False,
        False,
    )


def test_container_exec_demux_separates_stdout_and_stderr(exec_target):
    result = container_exec(exec_target, ["sh", "-c", "echo out; echo err >&2"], demux=True)
    assert result["output"] == ["out\n", "err\n"]


def test_container_exec_returns_at_its_timeout_while_the_command_runs_on(exec_target):
    started = time.monotonic()
    result = container_exec(exec_target, ["sh", "-c", "echo started; sleep 60"], timeout_seconds=2)
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"returned after {elapsed:.1f}s, not near its 2s timeout"
    assert (result["timed_out"], result["exit_code"]) == (True, None)
    assert result["output"] == "started\n"
    # What the description promises: the command is still running in the container after the call returns.
    assert _get_client().api.exec_inspect(result["exec_id"])["Running"] is True


def test_container_exec_cuts_a_large_output_at_max_bytes(exec_target):
    result = container_exec(exec_target, ["sh", "-c", "yes | head -c 100000"], max_bytes=1000)
    assert result["truncated"] is True
    assert len(result["output"]) == 1000


def test_system_wait_not_running_reports_a_timeout_instead_of_raising():
    # The Engine sends the wait's headers at once, so the bound expires mid-body; that once surfaced as a
    # ConnectionError rather than timed_out.
    name = f"docker-mcp-it-wait-timeout-{uuid.uuid4().hex[:8]}"
    container_run("alpine:3", command=["sleep", "300"], name=name)
    try:
        result = system_wait(kind="container", target=name, until="not-running", timeout_seconds=2)
        assert (result["met"], result["timed_out"]) == (False, True)
    finally:
        container_remove(name, force=True)

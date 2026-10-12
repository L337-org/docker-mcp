from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.exceptions import RemoteFailureError, ToolInputError
from docker_mcp.tools._cli import CliResult
from docker_mcp.tools.stack import (
    _wait_stack,
    stack_deploy,
    stack_list,
    stack_ps,
    stack_remove,
    stack_services,
)


def _ok(stdout: str = "", stderr: str = "") -> CliResult:
    return CliResult(returncode=0, stdout=stdout, stderr=stderr, truncated=False)


def _fail(stderr: str, returncode: int = 1) -> CliResult:
    return CliResult(returncode=returncode, stdout="", stderr=stderr, truncated=False)


def _patch_run():
    return patch("docker_mcp.tools.stack.run_docker")


# ---------- stack_deploy (action tool: raw dict, never raises on non-zero) ----------


def test_stack_deploy_builds_args_and_returns_raw_dict():
    with _patch_run() as run:
        run.return_value = _ok(stdout="Creating service web")
        result = stack_deploy(
            "web",
            compose_files=["docker-compose.yml", "override.yml"],
            with_registry_auth=True,
            prune=True,
            resolve_image="changed",
            cwd="/srv/app",
        )
    assert result == {"returncode": 0, "stdout": "Creating service web", "stderr": "", "truncated": False}
    args, kwargs = run.call_args
    argv = args[0]
    assert argv[:2] == ["stack", "deploy"]
    # Each compose file is passed as its own -c.
    assert argv.count("-c") == 2
    assert "docker-compose.yml" in argv and "override.yml" in argv
    assert "--with-registry-auth" in argv
    assert "--prune" in argv
    assert "--resolve-image=changed" in argv
    assert "--detach=true" in argv  # default detach
    assert argv[-1] == "web"  # stack name is the trailing positional
    assert kwargs["cwd"] == "/srv/app"


def test_stack_deploy_detach_false_passes_flag():
    with _patch_run() as run:
        run.return_value = _ok()
        stack_deploy("web", compose_files=["c.yml"], detach=False)
    assert "--detach=false" in run.call_args.args[0]


def test_stack_deploy_does_not_raise_on_non_zero():
    with _patch_run() as run:
        run.return_value = _fail("this node is not a swarm manager")
        result = stack_deploy("web", compose_files=["c.yml"])
    assert result["returncode"] == 1
    assert "swarm manager" in result["stderr"]


def test_stack_deploy_requires_a_compose_file():
    with pytest.raises(ToolInputError, match="at least one"):
        stack_deploy("web", compose_files=[])


def test_stack_deploy_rejects_invalid_resolve_image():
    # The bad value is the point of the test, so the type error is suppressed at this one call
    # rather than the test being softened to a legal value. `resolve_image` is a Literal, so a
    # caller arriving through MCP is stopped by schema validation before the body runs; this
    # asserts the runtime guard that still covers a direct Python call, which validation never sees.
    with pytest.raises(ToolInputError, match="resolve_image"):
        stack_deploy("web", compose_files=["c.yml"], resolve_image="sometimes")  # pyright: ignore[reportArgumentType]


def test_stack_deploy_rejects_flag_like_stack_name():
    with pytest.raises(ToolInputError, match="flag"):
        stack_deploy("--rm", compose_files=["c.yml"])


# ---------- stack_list (parsed query: raises on non-zero) ----------


def test_stack_ls_parses_ndjson():
    ndjson = '{"Name":"web","Services":"3"}\n{"Name":"db","Services":"1"}'
    with _patch_run() as run:
        run.return_value = _ok(stdout=ndjson)
        result = stack_list()
    assert [s["Name"] for s in result] == ["web", "db"]
    # JSON via the Go template, not the --format json shorthand (which needs docker >= ~v23).
    assert run.call_args.args[0] == ["stack", "ls", "--format", "{{json .}}"]


def test_stack_ls_single_object_wrapped_in_list():
    with _patch_run() as run:
        run.return_value = _ok(stdout='{"Name":"web","Services":"3"}')
        result = stack_list()
    assert result == [{"Name": "web", "Services": "3"}]


def test_stack_ls_raises_on_failure():
    with _patch_run() as run:
        run.return_value = _fail("This node is not a swarm manager")
        with pytest.raises(RemoteFailureError, match="stack ls"):
            stack_list()


# ---------- stack_ps / stack_services ----------


def test_stack_ps_builds_args_with_no_trunc_and_filters():
    with _patch_run() as run:
        run.return_value = _ok(stdout='{"Name":"web.1","CurrentState":"Running"}')
        result = stack_ps("web", no_trunc=True, filters={"desired-state": "running"})
    assert result == [{"Name": "web.1", "CurrentState": "Running"}]
    argv = run.call_args.args[0]
    assert argv[:2] == ["stack", "ps"]
    assert "--no-trunc" in argv
    assert argv.count("--filter") == 1
    assert "desired-state=running" in argv
    assert argv[-1] == "web"


def test_stack_ps_raises_on_failure():
    with _patch_run() as run:
        run.return_value = _fail("nothing found in stack: web")
        with pytest.raises(RemoteFailureError, match="stack ps"):
            stack_ps("web")


def test_stack_services_builds_args_with_filters():
    with _patch_run() as run:
        run.return_value = _ok(stdout='{"Name":"web_api","Replicas":"3/3"}')
        result = stack_services("web", filters={"name": "web_api"})
    assert result == [{"Name": "web_api", "Replicas": "3/3"}]
    argv = run.call_args.args[0]
    assert argv[:2] == ["stack", "services"]
    assert "name=web_api" in argv
    assert argv[-1] == "web"


def test_stack_services_rejects_flag_like_name():
    with pytest.raises(ToolInputError, match="flag"):
        stack_services("-x")


# ---------- stack_remove (action tool) ----------


def test_stack_rm_builds_args_for_multiple_stacks():
    with _patch_run() as run:
        run.return_value = _ok(stdout="Removing stack: web")
        result = stack_remove(["web", "db"])
    assert result["returncode"] == 0
    argv = run.call_args.args[0]
    assert argv[:2] == ["stack", "rm"]
    assert "--detach=true" in argv
    assert argv[-2:] == ["web", "db"]


def test_stack_rm_detach_false():
    with _patch_run() as run:
        run.return_value = _ok()
        stack_remove(["web"], detach=False)
    assert "--detach=false" in run.call_args.args[0]


def test_stack_rm_requires_a_stack_name():
    with pytest.raises(ToolInputError, match="at least one"):
        stack_remove([])


def test_stack_rm_rejects_flag_like_name():
    with pytest.raises(ToolInputError, match="flag"):
        stack_remove(["web", "-rf"])


# ---------- remote-exec fallback ----------


def test_stack_deploy_stages_its_working_directory_when_there_is_no_local_cli():
    with (
        patch("docker_mcp.tools.stack.should_remote_exec", return_value=True) as should,
        patch("docker_mcp.tools.stack.remote_stage_and_exec", return_value=_ok("")) as staged,
        patch("docker_mcp.tools.stack.run_docker") as run,
    ):
        stack_deploy("api", ["docker-stack.yml"], cwd="/srv/app", host="prod")
    run.assert_not_called()
    # `docker stack` is core CLI, not a plugin, so the probe asks about the binary itself.
    should.assert_called_with("prod", plugin=None)
    assert staged.call_args.kwargs["cwd"] == "/srv/app"
    assert staged.call_args.kwargs["path_values"] == ["docker-stack.yml"]


def test_stack_queries_run_remotely_without_staging_anything():
    # `ls`/`ps`/`services`/`rm` name nothing local, so there is no directory worth copying.
    for call, expected in (
        (lambda: stack_list(host="prod"), ["stack", "ls", "--format", "{{json .}}"]),
        (lambda: stack_ps("api", host="prod"), ["stack", "ps", "--format", "{{json .}}", "api"]),
        (lambda: stack_services("api", host="prod"), ["stack", "services", "--format", "{{json .}}", "api"]),
    ):
        with (
            patch("docker_mcp.tools.stack.should_remote_exec", return_value=True),
            patch("docker_mcp.tools.stack.remote_stage_and_exec") as staged,
            patch("docker_mcp.tools.stack.remote_exec_cli", return_value=_ok("[]")) as remote,
        ):
            call()
        staged.assert_not_called()
        assert remote.call_args.args == ("prod", expected)


def test_stack_remove_runs_remotely_without_staging():
    with (
        patch("docker_mcp.tools.stack.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.stack.remote_stage_and_exec") as staged,
        patch("docker_mcp.tools.stack.remote_exec_cli", return_value=_ok("")) as remote,
    ):
        stack_remove(["api"], host="prod")
    staged.assert_not_called()
    assert remote.call_args.args[1][:2] == ["stack", "rm"]


def test_stack_uses_the_local_cli_when_it_can():
    with (
        patch("docker_mcp.tools.stack.should_remote_exec", return_value=False),
        patch("docker_mcp.tools.stack.remote_exec_cli") as remote,
        patch("docker_mcp.tools.stack.remote_stage_and_exec") as staged,
        patch("docker_mcp.tools.stack.run_docker", return_value=_ok("[]")) as run,
    ):
        stack_list(host="prod")
    remote.assert_not_called()
    staged.assert_not_called()
    assert run.call_args.kwargs["host"] == "prod"


def test_a_stack_deploy_with_no_compose_file_says_so_on_the_wire(on_the_wire):
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    with pytest.raises(ToolError) as excinfo:
        on_the_wire("stack_deploy", {"name": "s", "compose_files": []})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "at least one entry in compose_files" in str(excinfo.value)


# ---------- the stack wait (system_wait(kind="stack")) ----------


_TEMPLATE = {"ContainerSpec": {"Image": "alpine:3", "Args": ["sleep", "301"]}, "ForceUpdate": 0, "Runtime": "container"}
_OLD_SPEC = {"ContainerSpec": {"Image": "alpine:3", "Args": ["sleep", "300"]}, "ForceUpdate": 0}
# A task's spec carries every template key but Runtime, as the Engine reports it.
_CURRENT_SPEC = {k: v for k, v in _TEMPLATE.items() if k != "Runtime"}


def _service(name):
    service = MagicMock()
    service.name = name
    service.id = f"id-{name}"
    service.attrs = {"Spec": {"TaskTemplate": _TEMPLATE}}
    return service


def _running(*specs):
    """A service's tasks with desired state running, each on the given spec."""
    return [{"Status": {"State": "running"}, "Spec": spec} for spec in specs]


def _summary(running, desired, update_state=None):
    return {"running_tasks": running, "desired_tasks": desired, "failed_tasks": [], "update_state": update_state}


def _task(state):
    return {"Status": {"State": state}}


def _stack_wait(until, services, summaries=None, tasks=None, service_tasks=None, **kwargs):
    """Run the stack wait against successive readings, without sleeping.

    `services` and `tasks` are one list per poll; `summaries` and `service_tasks` map a service id to its
    successive summaries and running-task lists. A service with no `service_tasks` has every running task
    its summary counts on the current spec.
    """
    summaries = {k: iter(v) for k, v in (summaries or {}).items()}
    per_service = {k: iter(v) for k, v in (service_tasks or {}).items()}
    stack_tasks = iter(tasks or [[] for _ in services])
    last_running: dict[str, int] = {}

    def summary(service_id, host=None):
        reading = next(summaries[service_id])
        last_running[service_id] = reading["running_tasks"]
        return reading

    def api_tasks(filters):
        if "service" not in filters:
            return next(stack_tasks)
        if filters["service"] in per_service:
            return next(per_service[filters["service"]])
        return _running(*[_CURRENT_SPEC] * last_running.get(filters["service"], 0))

    with (
        patch("docker_mcp.tools.stack._get_client") as get_client,
        patch("docker_mcp.tools.stack._read_service_task_summary", side_effect=summary),
        patch("docker_mcp.tools.stack.time.sleep") as sleep,
    ):
        client = get_client.return_value
        client.services.list.side_effect = services
        client.api.tasks.side_effect = api_tasks
        timeout, interval = kwargs.pop("timeout_seconds", 5), kwargs.pop("poll_interval", 0.01)
        result = _wait_stack("shop", until, timeout, interval, **kwargs)
    return result, client, sleep


def test_stack_wait_finds_the_stack_by_its_namespace_label():
    _, client, _ = _stack_wait("removed", [[]])
    client.services.list.assert_called_with(filters={"label": "com.docker.stack.namespace=shop"})
    client.api.tasks.assert_called_with(filters={"label": "com.docker.stack.namespace=shop"})


def test_stack_wait_converged_once_every_service_has_its_tasks():
    web, db = _service("shop_web"), _service("shop_db")
    result, _, sleep = _stack_wait(
        "converged",
        [[web, db], [web, db]],
        summaries={"id-shop_web": [_summary(1, 2), _summary(2, 2)], "id-shop_db": [_summary(1, 1), _summary(1, 1)]},
    )
    assert (result["met"], result["timed_out"]) == (True, False)
    assert [r["service"] for r in result["services"]] == ["shop_db", "shop_web"]
    assert sleep.call_count == 1


def test_stack_wait_converged_counts_only_tasks_on_the_current_spec():
    # Just after a redeploy the old tasks still run and swarm may not have recorded an update yet: the
    # running count is met and update_state is None, but the tasks are on the previous spec.
    web = _service("shop_web")
    result, _, sleep = _stack_wait(
        "converged",
        [[web], [web]],
        summaries={"id-shop_web": [_summary(2, 2), _summary(2, 2)]},
        service_tasks={"id-shop_web": [_running(_OLD_SPEC, _OLD_SPEC), _running(_CURRENT_SPEC, _CURRENT_SPEC)]},
    )
    assert result["met"] is True
    assert result["services"][0]["current_tasks"] == 2
    assert sleep.call_count == 1


def test_runs_current_spec_ignores_the_runtime_key_tasks_do_not_carry():
    from docker_mcp.tools.stack import _runs_current_spec

    assert _runs_current_spec({"Spec": _CURRENT_SPEC}, _TEMPLATE) is True
    assert _runs_current_spec({"Spec": _OLD_SPEC}, _TEMPLATE) is False


def test_stack_wait_converged_waits_for_a_rolling_update_to_finish():
    # During a rollout the old tasks keep running until replaced, so the count alone is met too early.
    web = _service("shop_web")
    result, _, sleep = _stack_wait(
        "converged",
        [[web], [web]],
        summaries={"id-shop_web": [_summary(3, 3, update_state="updating"), _summary(3, 3, update_state="completed")]},
    )
    assert result["met"] is True
    assert sleep.call_count == 1


@pytest.mark.parametrize("state", ["paused", "rollback_paused"])
def test_stack_wait_converged_gives_up_at_once_on_a_paused_update(state):
    web = _service("shop_web")
    result, _, sleep = _stack_wait(
        "converged", [[web]], summaries={"id-shop_web": [_summary(3, 3, update_state=state)]}
    )
    assert (result["met"], result["timed_out"]) == (False, False)
    sleep.assert_not_called()


def test_stack_wait_skips_a_service_removed_while_it_is_read():
    import docker.errors

    web, db = _service("shop_web"), _service("shop_db")

    def summary(service_id, host=None):
        if service_id == "id-shop_db":
            raise docker.errors.NotFound("service gone")
        return _summary(1, 1)

    with (
        patch("docker_mcp.tools.stack._get_client") as get_client,
        patch("docker_mcp.tools.stack._read_service_task_summary", side_effect=summary),
        patch("docker_mcp.tools.stack.time.sleep"),
    ):
        get_client.return_value.services.list.return_value = [web, db]
        get_client.return_value.api.tasks.side_effect = lambda filters: (
            _running(_CURRENT_SPEC) if "service" in filters else []
        )
        result = _wait_stack("shop", "converged", 5, 0.01)
    assert result["met"] is True
    assert [r["service"] for r in result["services"]] == ["shop_web"]


def test_stack_wait_removed_reads_no_per_service_summary():
    web = _service("shop_web")
    with (
        patch("docker_mcp.tools.stack._get_client") as get_client,
        patch("docker_mcp.tools.stack._read_service_task_summary") as summary,
        patch("docker_mcp.tools.stack.time.sleep"),
    ):
        get_client.return_value.services.list.side_effect = [[web], []]
        get_client.return_value.api.tasks.return_value = []
        result = _wait_stack("shop", "removed", 5, 0.01)
    assert result["met"] is True
    summary.assert_not_called()


def test_stack_wait_converged_with_no_services_is_not_met_and_does_not_poll():
    result, _, sleep = _stack_wait("converged", [[]])
    assert (result["met"], result["timed_out"], result["services"]) == (False, False, [])
    sleep.assert_not_called()


def test_stack_wait_converged_times_out_with_the_last_reading():
    web = _service("shop_web")
    result, _, _ = _stack_wait("converged", [[web]], summaries={"id-shop_web": [_summary(0, 2)]}, timeout_seconds=0.0)
    assert (result["met"], result["timed_out"]) == (False, True)
    assert result["services"][0]["running_tasks"] == 0


def test_stack_wait_removed_waits_for_the_last_live_task_to_stop():
    result, _, sleep = _stack_wait(
        "removed",
        [[], []],
        tasks=[[_task("running"), _task("shutdown")], [_task("shutdown"), _task("complete")]],
    )
    assert (result["met"], result["active_tasks"]) == (True, 0)
    assert sleep.call_count == 1


def test_stack_wait_removed_is_not_met_while_a_service_remains():
    web = _service("shop_web")
    result, _, _ = _stack_wait("removed", [[web]], timeout_seconds=0.0)
    assert (result["met"], result["timed_out"]) == (False, True)


@pytest.mark.parametrize("state", ["new", "pending", "assigned", "preparing", "starting", "running"])
def test_stack_wait_counts_every_pre_terminal_task_state_as_live(state):
    result, _, _ = _stack_wait("removed", [[]], tasks=[[_task(state)]], timeout_seconds=0.0)
    assert (result["met"], result["active_tasks"]) == (False, 1)


@pytest.mark.parametrize("state", ["complete", "shutdown", "failed", "rejected", "orphaned", "remove"])
def test_stack_wait_does_not_count_a_stopped_task(state):
    result, _, _ = _stack_wait("removed", [[]], tasks=[[_task(state)]])
    assert (result["met"], result["active_tasks"]) == (True, 0)


@pytest.mark.parametrize(
    ("timeout", "interval", "message"),
    [(-1, 1.0, "timeout_seconds must be >= 0"), (5, 0, "poll_interval must be > 0")],
)
def test_stack_wait_rejects_a_bad_bound_before_reading(timeout, interval, message):
    with patch("docker_mcp.tools.stack._get_client") as get_client, pytest.raises(ToolInputError, match=message):
        _wait_stack("shop", "removed", timeout, interval)
    get_client.assert_not_called()

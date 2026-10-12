from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.exceptions import ToolInputError
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


def test_stack_deploy_always_detaches():
    with _patch_run() as run:
        run.return_value = _ok()
        stack_deploy("web", compose_files=["c.yml"])
    assert "--detach=true" in run.call_args.args[0]


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


# ---------- the Engine-API tools (stack_list / stack_services / stack_ps / stack_remove) ----------


_NS = "com.docker.stack.namespace"


def _svc_obj(name, stack="shop"):
    """A docker-py Service model as `services.list` returns it."""
    service = MagicMock()
    service.name = name
    service.attrs = {"Spec": {"Name": name, "Labels": {_NS: stack}}}
    return service


def _named(name):
    obj = MagicMock()
    obj.name = name
    return obj


def _patch_client():
    return patch("docker_mcp.tools.stack._get_client")


def test_stack_list_groups_services_by_their_stack_label():
    with _patch_client() as get_client:
        client = get_client.return_value
        client.services.list.return_value = [
            _svc_obj("web_api", "web"),
            _svc_obj("shop_db", "shop"),
            _svc_obj("web_ui", "web"),
        ]
        result = stack_list(host="prod")
    assert result == [{"name": "shop", "services": 1}, {"name": "web", "services": 2}]
    client.services.list.assert_called_once_with(filters={"label": [_NS]})
    get_client.assert_called_once_with("prod")


def test_stack_list_skips_a_service_without_the_stack_label():
    # The Engine filter should make this impossible; an unlabelled row must not become a stack named None.
    stray = _svc_obj("stray")
    stray.attrs["Spec"]["Labels"] = {}
    with _patch_client() as get_client:
        get_client.return_value.services.list.return_value = [stray, _svc_obj("web_ui", "web")]
        assert stack_list() == [{"name": "web", "services": 1}]


def test_stack_list_with_no_stacks_is_empty():
    with _patch_client() as get_client:
        get_client.return_value.services.list.return_value = []
        assert stack_list() == []


def _api_service(name, *, mode, running=1, desired=1, ports=(), update=None):
    service = {
        "ID": f"id-{name}",
        "Spec": {"Name": name, "Mode": mode, "TaskTemplate": {"ContainerSpec": {"Image": "nginx:1@sha256:abc"}}},
        "ServiceStatus": {"RunningTasks": running, "DesiredTasks": desired},
        "Endpoint": {"Ports": list(ports)},
    }
    if update:
        service["UpdateStatus"] = {"State": update}
    return service


def test_stack_services_reports_each_service_with_its_task_counts():
    with _patch_client() as get_client:
        client = get_client.return_value
        client.api.services.return_value = [
            _api_service(
                "web_ui",
                mode={"Replicated": {"Replicas": 3}},
                running=2,
                desired=3,
                ports=[{"Protocol": "tcp", "TargetPort": 80, "PublishedPort": 8080}],
                update="updating",
            ),
            _api_service("web_agent", mode={"Global": {}}),
        ]
        result = stack_services("web", filters={"name": "web_"})
    client.api.services.assert_called_once_with(filters={"name": "web_", "label": [f"{_NS}=web"]}, status=True)
    assert result == [
        {
            "id": "id-web_agent",
            "name": "web_agent",
            "mode": "global",
            "running_tasks": 1,
            "desired_tasks": 1,
            "image": "nginx:1@sha256:abc",
            "ports": [],
            "update_state": None,
        },
        {
            "id": "id-web_ui",
            "name": "web_ui",
            "mode": "replicated",
            "running_tasks": 2,
            "desired_tasks": 3,
            "image": "nginx:1@sha256:abc",
            "ports": ["8080:80/tcp"],
            "update_state": "updating",
        },
    ]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [({"ReplicatedJob": {}}, "replicated-job"), ({"GlobalJob": {}}, "global-job"), ({}, None)],
)
def test_stack_services_names_job_modes_as_the_cli_does(mode, expected):
    with _patch_client() as get_client:
        get_client.return_value.api.services.return_value = [_api_service("s_job", mode=mode)]
        [row] = stack_services("s")
    assert row["mode"] == expected


@pytest.mark.parametrize(
    ("theirs", "expected"),
    [
        ("tier=front", [f"{_NS}=web", "tier=front"]),
        (["tier=front", "team=a"], [f"{_NS}=web", "tier=front", "team=a"]),
    ],
)
def test_a_caller_label_filter_narrows_the_stack_rather_than_replacing_it(theirs, expected):
    with _patch_client() as get_client:
        client = get_client.return_value
        client.api.services.return_value = []
        stack_services("web", filters={"label": theirs})
    assert client.api.services.call_args.kwargs["filters"]["label"] == expected


def test_stack_services_does_not_change_the_callers_filters():
    filters = {"label": ["tier=front"]}
    with _patch_client() as get_client:
        get_client.return_value.api.services.return_value = []
        stack_services("web", filters=filters)
    assert filters == {"label": ["tier=front"]}


def test_stack_ps_names_tasks_by_slot_or_node_and_resolves_node_hostnames():
    node = MagicMock()
    node.id = "node-1"
    node.attrs = {"Description": {"Hostname": "worker-a"}}
    tasks = [
        {
            "ID": "t2",
            "ServiceID": "id-web_ui",
            "NodeID": "node-1",
            "Slot": 1,
            "DesiredState": "shutdown",
            "Spec": {"ContainerSpec": {"Image": "nginx:1"}},
            "Status": {"State": "failed", "Err": "task: non-zero exit (1)", "Timestamp": "2026-10-12T10:00:00Z"},
        },
        {
            "ID": "t1",
            "ServiceID": "id-web_agent",
            "NodeID": "node-1",
            "DesiredState": "running",
            "Spec": {"ContainerSpec": {"Image": "agent:2"}},
            "Status": {"State": "running", "Timestamp": "2026-10-12T10:01:00Z"},
        },
    ]
    with _patch_client() as get_client:
        client = get_client.return_value
        client.api.services.return_value = [
            {"ID": "id-web_ui", "Spec": {"Name": "web_ui"}},
            {"ID": "id-web_agent", "Spec": {"Name": "web_agent"}},
        ]
        client.nodes.list.return_value = [node]
        client.api.tasks.return_value = tasks
        result = stack_ps("web", filters={"desired-state": "running"})
    client.api.tasks.assert_called_once_with(filters={"desired-state": "running", "label": [f"{_NS}=web"]})
    assert result == [
        {
            "id": "t1",
            "name": "web_agent.node-1",
            "service": "web_agent",
            "node": "worker-a",
            "image": "agent:2",
            "desired_state": "running",
            "state": "running",
            "error": None,
            "since": "2026-10-12T10:01:00Z",
        },
        {
            "id": "t2",
            "name": "web_ui.1",
            "service": "web_ui",
            "node": "worker-a",
            "image": "nginx:1",
            "desired_state": "shutdown",
            "state": "failed",
            "error": "task: non-zero exit (1)",
            "since": "2026-10-12T10:00:00Z",
        },
    ]


def test_stack_ps_falls_back_to_ids_for_a_service_or_node_it_cannot_name():
    # A task can outlive its service between the two reads, and a pending task has no node yet.
    with _patch_client() as get_client:
        client = get_client.return_value
        client.api.services.return_value = []
        client.nodes.list.return_value = []
        client.api.tasks.return_value = [{"ID": "t", "ServiceID": "gone", "NodeID": "node-9", "Slot": 2}]
        [row] = stack_ps("web")
    assert (row["name"], row["service"], row["node"]) == ("gone.2", "gone", "node-9")


def _stack_parts(client, services=(), secrets=(), configs=(), networks=()):
    client.services.list.return_value = list(services)
    client.secrets.list.return_value = list(secrets)
    client.configs.list.return_value = list(configs)
    client.networks.list.return_value = list(networks)


def test_stack_remove_removes_services_before_what_they_use():
    order = []
    parts = {
        "services": [_named("web_ui"), _named("web_api")],
        "secrets": [_named("web_key")],
        "configs": [_named("web_conf")],
        "networks": [_named("web_default")],
    }
    for objects in parts.values():
        for obj in objects:
            obj.remove.side_effect = lambda name=obj.name: order.append(name)
    with _patch_client() as get_client:
        client = get_client.return_value
        _stack_parts(client, **parts)
        [result] = stack_remove(["web"])
    label = {"label": [f"{_NS}=web"]}
    for collection in (client.services, client.secrets, client.configs, client.networks):
        collection.list.assert_called_once_with(filters=label)
    assert order == ["web_api", "web_ui", "web_key", "web_conf", "web_default"]
    assert result == {
        "name": "web",
        "found": True,
        "removed": {
            "services": ["web_api", "web_ui"],
            "secrets": ["web_key"],
            "configs": ["web_conf"],
            "networks": ["web_default"],
        },
        "failed": [],
    }


def test_stack_remove_reports_a_failure_and_still_removes_the_rest():
    import docker.errors

    busy = _named("web_default")
    busy.remove.side_effect = docker.errors.APIError("network web_default has active endpoints")
    with _patch_client() as get_client:
        _stack_parts(get_client.return_value, services=[_named("web_ui")], networks=[busy, _named("web_back")])
        [result] = stack_remove(["web"])
    assert result["removed"] == {"services": ["web_ui"], "secrets": [], "configs": [], "networks": ["web_back"]}
    assert result["failed"] == [
        {"kind": "network", "name": "web_default", "error": "network web_default has active endpoints"}
    ]


def test_stack_remove_counts_a_part_already_gone_as_removed():
    import docker.errors

    gone = _named("web_ui")
    gone.remove.side_effect = docker.errors.NotFound("service web_ui not found")
    with _patch_client() as get_client:
        _stack_parts(get_client.return_value, services=[gone])
        [result] = stack_remove(["web"])
    assert (result["removed"]["services"], result["failed"]) == (["web_ui"], [])


def test_stack_remove_says_when_a_stack_has_nothing_to_remove():
    with _patch_client() as get_client:
        _stack_parts(get_client.return_value)
        result = stack_remove(["nope", "also-nope"], host="prod")
    get_client.assert_called_once_with("prod")
    empty = {"services": [], "secrets": [], "configs": [], "networks": []}
    assert result == [
        {"name": "nope", "found": False, "removed": empty, "failed": []},
        {"name": "also-nope", "found": False, "removed": empty, "failed": []},
    ]


def test_stack_remove_requires_a_stack_name():
    with pytest.raises(ToolInputError, match="at least one"):
        stack_remove([])


def test_only_stack_deploy_reaches_the_docker_cli():
    # Every stack tool but deploy is Engine calls, so none may shell out on any path - local or remote.
    with (
        _patch_client() as get_client,
        patch("docker_mcp.tools.stack.run_docker") as run,
        patch("docker_mcp.tools.stack.remote_stage_and_exec") as staged,
        patch("docker_mcp.tools.stack.should_remote_exec", return_value=True) as should,
    ):
        client = get_client.return_value
        _stack_parts(client)
        client.api.services.return_value = []
        client.api.tasks.return_value = []
        client.nodes.list.return_value = []
        stack_list(host="prod")
        stack_services("web", host="prod")
        stack_ps("web", host="prod")
        stack_remove(["web"], host="prod")
    run.assert_not_called()
    staged.assert_not_called()
    should.assert_not_called()


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


def test_stack_deploy_uses_the_local_cli_when_it_can():
    with (
        patch("docker_mcp.tools.stack.should_remote_exec", return_value=False),
        patch("docker_mcp.tools.stack.remote_stage_and_exec") as staged,
        patch("docker_mcp.tools.stack.run_docker", return_value=_ok("")) as run,
    ):
        stack_deploy("api", ["docker-stack.yml"], host="prod")
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
        [[web], [web], [web]],
        summaries={"id-shop_web": [_summary(2, 2), _summary(2, 2), _summary(2, 2)]},
        service_tasks={
            "id-shop_web": [
                _running(_OLD_SPEC, _OLD_SPEC),
                # On the current spec but not started yet: counting it would make this poll converge.
                _running(_CURRENT_SPEC) + [{"Status": {"State": "starting"}, "Spec": _CURRENT_SPEC}],
                _running(_CURRENT_SPEC, _CURRENT_SPEC),
            ]
        },
    )
    assert result["met"] is True
    assert result["services"][0]["current_tasks"] == 2
    assert sleep.call_count == 2


def test_stack_wait_reports_running_tasks_from_the_read_it_decides_on():
    # The summary read saw the task still starting; the task read just after it saw the task running.
    web = _service("shop_web")
    result, _, _ = _stack_wait(
        "converged",
        [[web]],
        summaries={"id-shop_web": [_summary(0, 1)]},
        service_tasks={"id-shop_web": [_running(_CURRENT_SPEC)]},
    )
    assert result["met"] is True
    [row] = result["services"]
    assert (row["running_tasks"], row["current_tasks"]) == (1, 1)


def test_stack_wait_does_not_count_a_task_desired_running_that_is_not_yet():
    web = _service("shop_web")
    starting = [{"Status": {"State": "starting"}, "Spec": _CURRENT_SPEC}]
    result, _, _ = _stack_wait(
        "converged",
        [[web]],
        summaries={"id-shop_web": [_summary(1, 1)]},
        service_tasks={"id-shop_web": [starting]},
        timeout_seconds=0,
    )
    [row] = result["services"]
    assert (result["met"], row["running_tasks"], row["current_tasks"]) == (False, 0, 0)


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

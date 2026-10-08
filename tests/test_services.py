from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.exceptions import CapabilityError, RemoteFailureError, ToolInputError
from docker_mcp.tools.services import (
    _read_service_log_tail,
    _read_service_task_summary,
    service_create,
    service_inspect,
    service_list,
    service_remove,
    service_rollback,
    service_scale,
    service_logs,
    service_ps,
    service_update,
    service_wait,
)


def _task_api(*, tty: bool = False, chunks=(b"line1\n", b"line2\n")):
    """An APIClient mock wired for the private helpers `service_logs(task=...)` reaches through.

    Returns:
        MagicMock: the api object, with `_get_result_tty` yielding `chunks`
    """
    api = MagicMock()
    api.inspect_task.return_value = {"ID": "fulltaskid", "Spec": {"ContainerSpec": {"TTY": tty}}}
    api._get_result_tty.return_value = iter(chunks)
    return api


def _patch():
    return patch("docker_mcp.tools.services._get_client")


def test_service_create():
    service = MagicMock()
    service.attrs = {"ID": "svc1"}
    with _patch() as mock_client:
        mock_client.return_value.services.create.return_value = service
        result = service_create("nginx", command="nginx", extra_kwargs={"name": "web"})
    assert result == {"ID": "svc1"}
    args, kwargs = mock_client.return_value.services.create.call_args
    assert args == ("nginx",)
    assert kwargs["command"] == "nginx"
    assert kwargs["name"] == "web"
    # service-level labels carry the provenance stamp (on by default)
    assert kwargs["labels"]["docker-mcp-server.managed"] == "true"
    assert kwargs["labels"]["docker-mcp-server.tool"] == "service_create"


def test_create_service_does_not_stamp_container_labels():
    service = MagicMock()
    service.attrs = {"ID": "svc1"}
    with _patch() as mock_client:
        mock_client.return_value.services.create.return_value = service
        service_create("nginx", extra_kwargs={"container_labels": {"app": "web"}})
    kwargs = mock_client.return_value.services.create.call_args.kwargs
    # container_labels is left untouched; provenance only goes on the service-level labels
    assert kwargs["container_labels"] == {"app": "web"}
    assert "docker-mcp-server.managed" in kwargs["labels"]


def test_service_inspect():
    service = MagicMock()
    service.attrs = {"ID": "svc1"}
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_inspect("svc1", insert_defaults=True) == {"ID": "svc1"}
    mock_client.return_value.services.get.assert_called_once_with("svc1", insert_defaults=True)


def test_service_list():
    service = MagicMock()
    service.attrs = {"ID": "svc1"}
    with _patch() as mock_client:
        mock_client.return_value.services.list.return_value = [service]
        assert service_list() == [{"ID": "svc1"}]


def test_service_list_omits_status_unless_asked():
    # docker-py version-checks `status` on `is not None`, so forwarding a literal False would start
    # raising InvalidVersion against a pre-v1.41 daemon on the call that has always worked there.
    with _patch() as mock_client:
        mock_client.return_value.services.list.return_value = []
        service_list()
    assert "status" not in mock_client.return_value.services.list.call_args.kwargs


def test_service_list_forwards_status_and_returns_the_task_counts():
    service = MagicMock()
    service.attrs = {"ID": "svc1", "ServiceStatus": {"RunningTasks": 2, "DesiredTasks": 3, "CompletedTasks": 0}}
    with _patch() as mock_client:
        mock_client.return_value.services.list.return_value = [service]
        result = service_list(status=True)
    assert mock_client.return_value.services.list.call_args.kwargs["status"] is True
    assert result[0]["ServiceStatus"]["DesiredTasks"] == 3


def test_list_services_managed_only_injects_label_filter():
    with _patch() as mock_client:
        mock_client.return_value.services.list.return_value = []
        service_list(managed_only=True)
    kwargs = mock_client.return_value.services.list.call_args.kwargs
    assert kwargs["filters"]["label"] == "docker-mcp-server.managed=true"


def test_service_update():
    service = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_update("svc1", {"image": "nginx:1.25"}) is True
    service.update.assert_called_once_with(image="nginx:1.25")


def test_service_remove():
    service = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_remove("svc1") is True
    service.remove.assert_called_once()


def test_service_ps():
    service = MagicMock()
    service.tasks.return_value = [{"ID": "t1"}]
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_ps("svc1") == [{"ID": "t1"}]
    service.tasks.assert_called_once_with(filters=None)


def test_service_logs_decodes_chunks():
    service = MagicMock()
    service.logs.return_value = iter([b"line1\n", b"line2\n"])
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_logs("svc1") == "line1\nline2\n"
    # follow is never forwarded — this tool always takes a bounded snapshot.
    assert service.logs.call_args.kwargs["follow"] is False


def test_service_logs_aborts_when_exceeding_max_bytes():
    service = MagicMock()
    service.logs.return_value = iter([b"x" * 6, b"y" * 6])
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        with pytest.raises(ToolInputError, match="exceeded max_bytes"):
            service_logs("svc1", max_bytes=10)


def test_service_logs_handles_bytearray_chunks():
    """Same latent corruption as container_logs had: bytearray is not a bytes subclass."""
    service = MagicMock()
    service.logs.return_value = iter([bytearray(b"line1\n"), b"line2\n"])
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_logs("svc1") == "line1\nline2\n"


def test_service_logs_coerces_str_chunks():
    service = MagicMock()
    service.logs.return_value = iter(["already-text\n"])
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_logs("svc1") == "already-text\n"


def test_service_scale():
    service = MagicMock()
    service.scale.return_value = True
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_scale("svc1", 5) is True
    service.scale.assert_called_once_with(5)


def test_service_update_force_redeploys_unchanged():
    service = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_update("svc1", force=True) is True
    service.force_update.assert_called_once()


def test_service_update_rejects_ambiguous_arguments():
    with pytest.raises(ToolInputError, match="exactly one"):
        service_update("svc1")
    with pytest.raises(ToolInputError, match="exactly one"):
        service_update("svc1", updates={"labels": {}}, force=True)


def test_rollback_service_reapplies_previous_spec_at_current_version():
    previous = {
        "Name": "web",
        "Labels": {"role": "web"},
        "TaskTemplate": {"ContainerSpec": {"Image": "nginx:1.24"}},
        "Mode": {"Replicated": {"Replicas": 3}},
        "UpdateConfig": {"Parallelism": 1},
        "RollbackConfig": {"Parallelism": 1},
        "EndpointSpec": {"Ports": []},
    }
    info = {"Version": {"Index": 42}, "Spec": {"TaskTemplate": {}}, "PreviousSpec": previous}
    with _patch() as mock_client:
        api = mock_client.return_value.api
        api.inspect_service.return_value = info
        api.update_service.return_value = {"Warnings": None}
        assert service_rollback("svc1") == {"Warnings": None}
    args, kwargs = api.update_service.call_args
    assert args == ("svc1", 42)  # current version index, so the daemon accepts the update
    assert kwargs["task_template"] == previous["TaskTemplate"]
    assert kwargs["name"] == "web"
    assert kwargs["mode"] == previous["Mode"]
    assert kwargs["endpoint_spec"] == previous["EndpointSpec"]
    assert kwargs["networks"] is None  # absent from PreviousSpec -> unset
    # Must replace with PreviousSpec, not merge over the current spec — so fetch_current_spec is False.
    assert kwargs["fetch_current_spec"] is False


def test_rollback_service_without_previous_spec_raises():
    info = {"Version": {"Index": 7}, "Spec": {}, "PreviousSpec": None}
    with _patch() as mock_client:
        api = mock_client.return_value.api
        api.inspect_service.return_value = info
        with pytest.raises(ToolInputError, match="no PreviousSpec"):
            service_rollback("svc1")
    api.update_service.assert_not_called()


def test_read_service_log_tail_decodes_chunks():
    service = MagicMock()
    service.logs.return_value = iter([b"line1\n", b"line2\n"])
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert _read_service_log_tail("svc1") == "line1\nline2\n"
    assert service.logs.call_args.kwargs["follow"] is False
    assert service.logs.call_args.kwargs["tail"] == 200


def test_read_service_task_summary_replicated_converged():
    service = MagicMock()
    service.name = "web"
    service.attrs = {"Spec": {"Mode": {"Replicated": {"Replicas": 2}}}}
    service.tasks.return_value = [
        {"ID": "t1", "Status": {"State": "running"}},
        {"ID": "t2", "Status": {"State": "running"}},
    ]
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        summary = _read_service_task_summary("svc1")
    assert summary == {
        "service": "web",
        "mode": "replicated",
        "running_tasks": 2,
        "desired_tasks": 2,
        "failed_tasks": [],
        "update_state": None,
    }
    service.tasks.assert_called_once_with(filters={"desired-state": "running"})


def test_read_service_task_summary_surfaces_failing_tasks():
    service = MagicMock()
    service.name = "web"
    service.attrs = {
        "Spec": {"Mode": {"Replicated": {"Replicas": 2}}},
        "UpdateStatus": {"State": "updating"},
    }
    service.tasks.return_value = [
        {"ID": "t1", "Status": {"State": "running"}},
        {"ID": "t2", "NodeID": "n1", "Status": {"State": "rejected", "Err": "no suitable node"}},
    ]
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        summary = _read_service_task_summary("svc1")
    assert summary["running_tasks"] == 1
    assert summary["desired_tasks"] == 2
    assert summary["update_state"] == "updating"
    assert summary["failed_tasks"] == [
        {"id": "t2", "node_id": "n1", "state": "rejected", "err": "no suitable node", "message": None}
    ]


def test_read_service_task_summary_global_mode_desired_is_task_count():
    service = MagicMock()
    service.name = "worker"
    service.attrs = {"Spec": {"Mode": {"Global": {}}}}
    service.tasks.return_value = [
        {"ID": "t1", "Status": {"State": "running"}},
        {"ID": "t2", "Status": {"State": "starting"}},
    ]
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        summary = _read_service_task_summary("svc1")
    assert summary["mode"] == "global"
    assert summary["running_tasks"] == 1
    assert summary["desired_tasks"] == 2  # no fixed target: len(returned tasks)


def test_read_service_task_summary_replicated_without_replicas_falls_back_to_task_count():
    # Replicas is optional in the daemon's own schema (no documented default) — a Replicated
    # service that omits it must not surface desired_tasks=None (which would later blow up
    # service_wait's `running_tasks >= desired_tasks` comparison with a TypeError).
    service = MagicMock()
    service.name = "web"
    service.attrs = {"Spec": {"Mode": {"Replicated": {}}}}
    service.tasks.return_value = [
        {"ID": "t1", "Status": {"State": "running"}},
        {"ID": "t2", "Status": {"State": "running"}},
    ]
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        summary = _read_service_task_summary("svc1")
    assert summary["desired_tasks"] == 2
    assert isinstance(summary["desired_tasks"], int)


def _task_service(mode_spec, tasks, update_status=None):
    s = MagicMock()
    s.attrs = {"Spec": {"Mode": mode_spec}, **({"UpdateStatus": update_status} if update_status else {})}
    s.tasks.return_value = tasks
    return s


def test_service_wait_running_already_converged_returns_immediately():
    svc = _task_service(
        {"Replicated": {"Replicas": 2}},
        [{"ID": "t1", "Status": {"State": "running"}}, {"ID": "t2", "Status": {"State": "running"}}],
    )
    with _patch() as mock_client, patch("docker_mcp.tools.services.time.sleep") as sleep:
        mock_client.return_value.services.get.return_value = svc
        result = service_wait("web", until="running", timeout_seconds=5)
    assert result["met"] is True
    assert result["timed_out"] is False
    assert result["running_tasks"] == 2
    assert result["desired_tasks"] == 2
    sleep.assert_not_called()


def test_service_wait_running_polls_through_scale_up():
    early = _task_service({"Replicated": {"Replicas": 3}}, [{"ID": "t1", "Status": {"State": "running"}}])
    converged = _task_service(
        {"Replicated": {"Replicas": 3}},
        [{"ID": "t1", "Status": {"State": "running"}}] * 3,
    )
    with _patch() as mock_client, patch("docker_mcp.tools.services.time.sleep") as sleep:
        mock_client.return_value.services.get.side_effect = [early, converged]
        result = service_wait("web", until="running", timeout_seconds=10, poll_interval=0.01)
    assert result["met"] is True
    assert result["running_tasks"] == 3
    sleep.assert_called_once()


def test_service_wait_running_replicas_override():
    svc = _task_service(
        {"Replicated": {"Replicas": 1}},  # daemon spec not yet reflecting a same-turn scale
        [{"ID": "t1", "Status": {"State": "running"}}] * 3,
    )
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = svc
        result = service_wait("web", until="running", replicas=3, timeout_seconds=5)
    assert result["met"] is True
    assert result["desired_tasks"] == 3


def test_service_wait_running_times_out():
    svc = _task_service({"Replicated": {"Replicas": 3}}, [{"ID": "t1", "Status": {"State": "running"}}])
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = svc
        result = service_wait("web", until="running", timeout_seconds=0.0)
    assert result["met"] is False
    assert result["timed_out"] is True


def test_service_wait_surfaces_failed_tasks():
    svc = _task_service(
        {"Replicated": {"Replicas": 2}},
        [
            {"ID": "t1", "Status": {"State": "running"}},
            {"ID": "t2", "NodeID": "n1", "Status": {"State": "rejected", "Err": "no suitable node"}},
        ],
    )
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = svc
        result = service_wait("web", until="running", timeout_seconds=0.0)
    assert result["failed_tasks"] == [
        {"id": "t2", "node_id": "n1", "state": "rejected", "err": "no suitable node", "message": None}
    ]


def test_service_wait_update_converged_no_update_status_returns_promptly():
    svc = _task_service({"Replicated": {"Replicas": 1}}, [{"ID": "t1", "Status": {"State": "running"}}])
    with _patch() as mock_client, patch("docker_mcp.tools.services.time.sleep") as sleep:
        mock_client.return_value.services.get.return_value = svc
        result = service_wait("web", until="update-converged", timeout_seconds=5)
    assert result["met"] is False
    assert result["timed_out"] is False
    assert result["update_state"] is None
    sleep.assert_not_called()  # nothing to converge to; don't poll to the timeout


def test_service_wait_update_converged_polls_through_updating():
    updating = _task_service(
        {"Replicated": {"Replicas": 1}},
        [{"ID": "t1", "Status": {"State": "running"}}],
        update_status={"State": "updating"},
    )
    completed = _task_service(
        {"Replicated": {"Replicas": 1}},
        [{"ID": "t1", "Status": {"State": "running"}}],
        update_status={"State": "completed"},
    )
    with _patch() as mock_client, patch("docker_mcp.tools.services.time.sleep") as sleep:
        mock_client.return_value.services.get.side_effect = [updating, completed]
        result = service_wait("web", until="update-converged", timeout_seconds=10, poll_interval=0.01)
    assert result["met"] is True
    assert result["update_state"] == "completed"
    sleep.assert_called_once()


def test_service_wait_update_converged_rollback_completed_is_terminal():
    svc = _task_service(
        {"Replicated": {"Replicas": 1}},
        [{"ID": "t1", "Status": {"State": "running"}}],
        update_status={"State": "rollback_completed"},
    )
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = svc
        result = service_wait("web", until="update-converged", timeout_seconds=5)
    assert result["met"] is True


def test_service_wait_rejects_negative_timeout():
    with pytest.raises(ToolInputError, match="timeout_seconds"):
        service_wait("web", timeout_seconds=-1)


def test_service_wait_rejects_nonpositive_poll_interval():
    with pytest.raises(ToolInputError, match="poll_interval"):
        service_wait("web", poll_interval=0)


def test_service_wait_rejects_negative_replicas():
    # A negative override must not make `running_tasks >= desired` trivially true.
    with pytest.raises(ToolInputError, match="replicas"):
        service_wait("web", replicas=-1)


def test_service_wait_accepts_zero_replicas():
    # 0 is a legitimate scale target (pausing a service) and must not be rejected.
    svc = _task_service({"Replicated": {"Replicas": 0}}, [])
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = svc
        result = service_wait("web", replicas=0, timeout_seconds=5)
    assert result["met"] is True
    assert result["desired_tasks"] == 0


def test_service_ps_without_a_service_asks_the_daemon_for_every_task():
    with _patch() as mock_client:
        mock_client.return_value.api.tasks.return_value = [{"ID": "t1"}, {"ID": "t2"}]
        assert service_ps() == [{"ID": "t1"}, {"ID": "t2"}]
    # No service lookup: with no service named, this is the cluster-wide read.
    mock_client.return_value.services.get.assert_not_called()
    mock_client.return_value.api.tasks.assert_called_once_with(filters=None)


def test_service_ps_without_a_service_forwards_filters():
    with _patch() as mock_client:
        mock_client.return_value.api.tasks.return_value = []
        assert service_ps(filters={"node": "node1", "desired-state": "running"}) == []
    mock_client.return_value.api.tasks.assert_called_once_with(filters={"node": "node1", "desired-state": "running"})


def test_service_ps_finds_one_task_by_forwarding_the_filter_unmodified():
    # One task is found through the list's `id`/`name` filters, which the daemon matches by prefix
    # itself (moby's newListTasksFilters maps them to IDPrefixes/NamePrefixes), so the reference must
    # reach it as given rather than being parsed or expanded here.
    with _patch() as mock_client:
        mock_client.return_value.api.tasks.return_value = [{"ID": "t1"}]
        assert service_ps(filters={"name": "web.1.abc123"}) == [{"ID": "t1"}]
    mock_client.return_value.api.tasks.assert_called_once_with(filters={"name": "web.1.abc123"})


def test_service_logs_for_a_task_reaches_the_published_task_route():
    # docker-py has no task_logs at any level, so the task path drives `_url`/`_get` itself. Pin the
    # route and the parameters: a typo here is invisible until a real daemon 404s.
    api = _task_api()
    with _patch() as mock_client:
        mock_client.return_value.api = api
        assert service_logs(task="task1") == "line1\nline2\n"
    # The resolved id, not the caller's reference: `task` advertises id prefixes and full task
    # names, and whether the logs route resolves those is undocumented, so the id from
    # `inspect_task` is what gets used.
    assert api._url.call_args.args == ("/tasks/{0}/logs", "fulltaskid")
    assert api.inspect_task.call_args.args == ("task1",)
    params = api._get.call_args.kwargs["params"]
    assert params["follow"] is False, "service_logs always takes a bounded snapshot"
    assert params["tail"] == 200
    assert api._get.call_args.kwargs["stream"] is True


def test_service_logs_for_a_task_reads_the_tty_flag_from_the_task_spec():
    # Without a TTY the Engine frames stdout and stderr with 8-byte headers, so passing the wrong
    # mode to `_get_result_tty` returns those headers as though they were log text.
    for tty in (True, False):
        api = _task_api(tty=tty)
        with _patch() as mock_client:
            mock_client.return_value.api = api
            service_logs(task="task1")
        assert api._get_result_tty.call_args.args[2] is tty


def test_service_logs_for_a_task_raises_for_status_before_decoding():
    # `_get_result_tty` checks the status itself on the multiplexed path but not the TTY one, where
    # an error body would otherwise be returned as log output.
    api = _task_api(tty=True)
    with _patch() as mock_client:
        mock_client.return_value.api = api
        service_logs(task="task1")
    assert api._raise_for_status.called


def test_service_logs_for_a_task_reports_a_missing_docker_py_internal_by_name():
    api = _task_api()
    api._get_result_tty = None
    with _patch() as mock_client:
        mock_client.return_value.api = api
        with pytest.raises(CapabilityError, match=r"_get_result_tty"):
            service_logs(task="task1")


def test_service_logs_for_a_task_releases_the_connection_on_both_paths():
    # A stream read to the end frees its own pooled connection, so the happy path would pass without
    # an explicit close and hide the real case: the max_bytes abort leaves the body part-read, which
    # strands a connection for every oversized task until the pool is exhausted.
    api = _task_api()
    with _patch() as mock_client:
        mock_client.return_value.api = api
        service_logs(task="task1")
    assert api._get.return_value.close.called, "connection not released after a normal read"

    api = _task_api(chunks=(b"x" * 6, b"y" * 6))
    with _patch() as mock_client:
        mock_client.return_value.api = api
        with pytest.raises(ToolInputError):
            service_logs(task="task1", max_bytes=10)
    assert api._get.return_value.close.called, "connection not released after the max_bytes abort"


def test_service_logs_for_a_task_refuses_a_task_document_with_no_id():
    # Falling back to the caller's reference here would reinstate the unresolved-identifier bug on
    # the one path where the daemon has already misbehaved, and a bare KeyError is not in
    # `_LIBRARY_FAILURES`, so it would reach the client as "Error executing tool" with no detail.
    api = _task_api()
    api.inspect_task.return_value = {"Spec": {"ContainerSpec": {"TTY": False}}}
    with _patch() as mock_client:
        mock_client.return_value.api = api
        with pytest.raises(RemoteFailureError, match="no 'ID' field"):
            service_logs(task="task1")
    assert not api._get.called, "must not fall back to the unresolved reference"


def test_service_logs_for_a_task_aborts_when_exceeding_max_bytes():
    api = _task_api(chunks=(b"x" * 6, b"y" * 6))
    with _patch() as mock_client:
        mock_client.return_value.api = api
        with pytest.raises(ToolInputError, match="exceeded max_bytes"):
            service_logs(task="task1", max_bytes=10)


@pytest.mark.parametrize("kwargs", [{}, {"id_or_name": "svc1", "task": "task1"}])
def test_service_logs_needs_exactly_one_of_a_service_or_a_task(kwargs):
    # Neither would read nothing, and both is ambiguous about which stream `tail` and `max_bytes`
    # bound - so the caller is told, and nothing is read.
    with _patch() as mock_client:
        with pytest.raises(ToolInputError, match="exactly one of `id_or_name`"):
            service_logs(**kwargs)
    assert not mock_client.called


def test_service_logs_for_a_service_does_not_reach_the_task_route():
    service = MagicMock()
    service.logs.return_value = iter([b"line\n"])
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_logs("svc1") == "line\n"
    assert not mock_client.return_value.api._get.called
    assert service.logs.call_args.kwargs["follow"] is False

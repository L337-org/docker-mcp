from unittest.mock import MagicMock, patch

import pytest

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
        with pytest.raises(ValueError, match="exceeded max_bytes"):
            service_logs("svc1", max_bytes=10)


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
    with pytest.raises(ValueError, match="exactly one"):
        service_update("svc1")
    with pytest.raises(ValueError, match="exactly one"):
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
        with pytest.raises(ValueError, match="no PreviousSpec"):
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
    with pytest.raises(ValueError, match="timeout_seconds"):
        service_wait("web", timeout_seconds=-1)


def test_service_wait_rejects_nonpositive_poll_interval():
    with pytest.raises(ValueError, match="poll_interval"):
        service_wait("web", poll_interval=0)

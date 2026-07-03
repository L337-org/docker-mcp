from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.tools.services import (
    service_create,
    service_force_update,
    service_inspect,
    service_list,
    service_remove,
    service_rollback,
    service_scale,
    service_logs,
    service_tasks,
    service_update,
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


def test_service_tasks():
    service = MagicMock()
    service.tasks.return_value = [{"ID": "t1"}]
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_tasks("svc1") == [{"ID": "t1"}]
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


def test_service_force_update():
    service = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert service_force_update("svc1") is True
    service.force_update.assert_called_once()


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

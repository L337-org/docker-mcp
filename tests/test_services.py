from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.tools.services import (
    create_service,
    force_update_service,
    get_service,
    list_services,
    remove_service,
    scale_service,
    service_logs,
    service_tasks,
    update_service,
)


def _patch():
    return patch("docker_mcp.tools.services._get_client")


def test_create_service():
    service = MagicMock()
    service.attrs = {"ID": "svc1"}
    with _patch() as mock_client:
        mock_client.return_value.services.create.return_value = service
        result = create_service("nginx", command="nginx", extra_kwargs={"name": "web"})
    assert result == {"ID": "svc1"}
    args, kwargs = mock_client.return_value.services.create.call_args
    assert args == ("nginx",)
    assert kwargs == {"command": "nginx", "name": "web"}


def test_get_service():
    service = MagicMock()
    service.attrs = {"ID": "svc1"}
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert get_service("svc1", insert_defaults=True) == {"ID": "svc1"}
    mock_client.return_value.services.get.assert_called_once_with("svc1", insert_defaults=True)


def test_list_services():
    service = MagicMock()
    service.attrs = {"ID": "svc1"}
    with _patch() as mock_client:
        mock_client.return_value.services.list.return_value = [service]
        assert list_services() == [{"ID": "svc1"}]


def test_update_service():
    service = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert update_service("svc1", {"image": "nginx:1.25"}) is True
    service.update.assert_called_once_with(image="nginx:1.25")


def test_remove_service():
    service = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert remove_service("svc1") is True
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


def test_scale_service():
    service = MagicMock()
    service.scale.return_value = True
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert scale_service("svc1", 5) is True
    service.scale.assert_called_once_with(5)


def test_force_update_service():
    service = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.services.get.return_value = service
        assert force_update_service("svc1") is True
    service.force_update.assert_called_once()

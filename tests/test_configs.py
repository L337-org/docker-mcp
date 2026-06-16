from unittest.mock import MagicMock, patch

from docker_mcp.tools.configs import create_config, get_config, list_configs, remove_config


def _patch():
    return patch("docker_mcp.tools.configs._get_client")


def test_create_config():
    config = MagicMock()
    config.attrs = {"ID": "cfg1"}
    with _patch() as mock_client:
        mock_client.return_value.configs.create.return_value = config
        result = create_config("myconfig", b"data", labels={"a": "b"})
    assert result == {"ID": "cfg1"}
    kwargs = mock_client.return_value.configs.create.call_args.kwargs
    assert kwargs["name"] == "myconfig"
    assert kwargs["data"] == b"data"
    # caller label preserved alongside the provenance stamp (on by default)
    assert kwargs["labels"]["a"] == "b"
    assert kwargs["labels"]["docker-mcp-server.managed"] == "true"


def test_get_config():
    config = MagicMock()
    config.attrs = {"ID": "cfg1"}
    with _patch() as mock_client:
        mock_client.return_value.configs.get.return_value = config
        assert get_config("cfg1") == {"ID": "cfg1"}


def test_list_configs():
    config = MagicMock()
    config.attrs = {"ID": "cfg1"}
    with _patch() as mock_client:
        mock_client.return_value.configs.list.return_value = [config]
        assert list_configs() == [{"ID": "cfg1"}]


def test_remove_config():
    config = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.configs.get.return_value = config
        assert remove_config("cfg1") is True
    config.remove.assert_called_once()

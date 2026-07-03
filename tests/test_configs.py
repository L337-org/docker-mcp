from unittest.mock import MagicMock, patch

from docker_mcp.tools.configs import config_create, config_inspect, config_list, config_remove


def _patch():
    return patch("docker_mcp.tools.configs._get_client")


def test_config_create():
    config = MagicMock()
    config.attrs = {"ID": "cfg1"}
    with _patch() as mock_client:
        mock_client.return_value.configs.create.return_value = config
        result = config_create("myconfig", b"data", labels={"a": "b"})
    assert result == {"ID": "cfg1"}
    kwargs = mock_client.return_value.configs.create.call_args.kwargs
    assert kwargs["name"] == "myconfig"
    assert kwargs["data"] == b"data"
    # caller label preserved alongside the provenance stamp (on by default)
    assert kwargs["labels"]["a"] == "b"
    assert kwargs["labels"]["docker-mcp-server.managed"] == "true"


def test_config_inspect():
    config = MagicMock()
    config.attrs = {"ID": "cfg1"}
    with _patch() as mock_client:
        mock_client.return_value.configs.get.return_value = config
        assert config_inspect("cfg1") == {"ID": "cfg1"}


def test_config_list():
    config = MagicMock()
    config.attrs = {"ID": "cfg1"}
    with _patch() as mock_client:
        mock_client.return_value.configs.list.return_value = [config]
        assert config_list() == [{"ID": "cfg1"}]


def test_config_remove():
    config = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.configs.get.return_value = config
        assert config_remove("cfg1") is True
    config.remove.assert_called_once()

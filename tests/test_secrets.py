from unittest.mock import MagicMock, patch

from docker_mcp.tools.secrets import secret_create, secret_inspect, secret_list, secret_remove


def _patch():
    return patch("docker_mcp.tools.secrets._get_client")


def test_secret_create():
    secret = MagicMock()
    secret.attrs = {"ID": "sec1"}
    with _patch() as mock_client:
        mock_client.return_value.secrets.create.return_value = secret
        result = secret_create("mysecret", b"shh", labels={"a": "b"})
    assert result == {"ID": "sec1"}
    kwargs = mock_client.return_value.secrets.create.call_args.kwargs
    assert kwargs["name"] == "mysecret"
    assert kwargs["data"] == b"shh"
    # caller label preserved alongside the provenance stamp (on by default)
    assert kwargs["labels"]["a"] == "b"
    assert kwargs["labels"]["docker-mcp-server.managed"] == "true"


def test_secret_inspect():
    secret = MagicMock()
    secret.attrs = {"ID": "sec1"}
    with _patch() as mock_client:
        mock_client.return_value.secrets.get.return_value = secret
        assert secret_inspect("sec1") == {"ID": "sec1"}


def test_secret_list():
    secret = MagicMock()
    secret.attrs = {"ID": "sec1"}
    with _patch() as mock_client:
        mock_client.return_value.secrets.list.return_value = [secret]
        assert secret_list() == [{"ID": "sec1"}]


def test_secret_remove():
    secret = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.secrets.get.return_value = secret
        assert secret_remove("sec1") is True
    secret.remove.assert_called_once()

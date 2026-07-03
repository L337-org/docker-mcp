from unittest.mock import MagicMock, patch

from docker_mcp.tools.networks import (
    network_connect,
    network_create,
    network_disconnect,
    network_inspect,
    network_list,
    network_prune,
    network_remove,
)


def _patch():
    return patch("docker_mcp.tools.networks._get_client")


def test_network_create():
    network = MagicMock()
    network.attrs = {"Id": "net1"}
    with _patch() as mock_client:
        mock_client.return_value.networks.create.return_value = network
        result = network_create("mynet", driver="bridge", labels={"a": "b"})
    assert result == {"Id": "net1"}
    args, kwargs = mock_client.return_value.networks.create.call_args
    assert args == ("mynet",)
    assert kwargs["driver"] == "bridge"
    # caller label preserved alongside the provenance stamp (on by default)
    assert kwargs["labels"]["a"] == "b"
    assert kwargs["labels"]["docker-mcp-server.managed"] == "true"


def test_network_inspect():
    network = MagicMock()
    network.attrs = {"Id": "net1"}
    with _patch() as mock_client:
        mock_client.return_value.networks.get.return_value = network
        assert network_inspect("mynet") == {"Id": "net1"}


def test_network_list():
    network = MagicMock()
    network.attrs = {"Id": "net1"}
    with _patch() as mock_client:
        mock_client.return_value.networks.list.return_value = [network]
        result = network_list(filters={"driver": "bridge"})
    assert result == [{"Id": "net1"}]
    kwargs = mock_client.return_value.networks.list.call_args.kwargs
    assert kwargs["filters"] == {"driver": "bridge"}


def test_list_networks_managed_only_injects_label_filter():
    with _patch() as mock_client:
        mock_client.return_value.networks.list.return_value = []
        network_list(managed_only=True, filters={"driver": "bridge"})
    kwargs = mock_client.return_value.networks.list.call_args.kwargs
    assert kwargs["filters"]["driver"] == "bridge"
    assert kwargs["filters"]["label"] == "docker-mcp-server.managed=true"


def test_network_prune():
    with _patch() as mock_client:
        mock_client.return_value.networks.prune.return_value = {"NetworksDeleted": ["net1"]}
        assert network_prune() == {"NetworksDeleted": ["net1"]}


def test_network_remove():
    network = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.networks.get.return_value = network
        assert network_remove("mynet") is True
    network.remove.assert_called_once()


def test_network_connect():
    network = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.networks.get.return_value = network
        assert network_connect("mynet", "web", aliases=["api"]) is True
    network.connect.assert_called_once_with(
        "web",
        aliases=["api"],
        links=None,
        ipv4_address=None,
        ipv6_address=None,
        link_local_ips=None,
        driver_opt=None,
    )


def test_network_disconnect():
    network = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.networks.get.return_value = network
        assert network_disconnect("mynet", "web", force=True) is True
    network.disconnect.assert_called_once_with("web", force=True)

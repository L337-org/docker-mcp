from unittest.mock import MagicMock, patch

from docker_mcp.tools.networks import (
    connect_network,
    create_network,
    disconnect_network,
    get_network,
    list_networks,
    prune_networks,
    remove_network,
)


def _patch():
    return patch("docker_mcp.tools.networks._get_client")


def test_create_network():
    network = MagicMock()
    network.attrs = {"Id": "net1"}
    with _patch() as mock_client:
        mock_client.return_value.networks.create.return_value = network
        result = create_network("mynet", driver="bridge", labels={"a": "b"})
    assert result == {"Id": "net1"}
    args, kwargs = mock_client.return_value.networks.create.call_args
    assert args == ("mynet",)
    assert kwargs["driver"] == "bridge"
    # caller label preserved alongside the provenance stamp (on by default)
    assert kwargs["labels"]["a"] == "b"
    assert kwargs["labels"]["docker-mcp-server.managed"] == "true"


def test_get_network():
    network = MagicMock()
    network.attrs = {"Id": "net1"}
    with _patch() as mock_client:
        mock_client.return_value.networks.get.return_value = network
        assert get_network("mynet") == {"Id": "net1"}


def test_list_networks():
    network = MagicMock()
    network.attrs = {"Id": "net1"}
    with _patch() as mock_client:
        mock_client.return_value.networks.list.return_value = [network]
        result = list_networks(filters={"driver": "bridge"})
    assert result == [{"Id": "net1"}]
    kwargs = mock_client.return_value.networks.list.call_args.kwargs
    assert kwargs["filters"] == {"driver": "bridge"}


def test_prune_networks():
    with _patch() as mock_client:
        mock_client.return_value.networks.prune.return_value = {"NetworksDeleted": ["net1"]}
        assert prune_networks() == {"NetworksDeleted": ["net1"]}


def test_remove_network():
    network = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.networks.get.return_value = network
        assert remove_network("mynet") is True
    network.remove.assert_called_once()


def test_connect_network():
    network = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.networks.get.return_value = network
        assert connect_network("mynet", "web", aliases=["api"]) is True
    network.connect.assert_called_once_with(
        "web",
        aliases=["api"],
        links=None,
        ipv4_address=None,
        ipv6_address=None,
        link_local_ips=None,
        driver_opt=None,
    )


def test_disconnect_network():
    network = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.networks.get.return_value = network
        assert disconnect_network("mynet", "web", force=True) is True
    network.disconnect.assert_called_once_with("web", force=True)

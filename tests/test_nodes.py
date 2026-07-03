from unittest.mock import MagicMock, patch

from docker_mcp.tools.nodes import node_inspect, node_list, node_remove, node_update


def _patch():
    return patch("docker_mcp.tools.nodes._get_client")


def test_node_inspect():
    node = MagicMock()
    node.attrs = {"ID": "n1"}
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        assert node_inspect("n1") == {"ID": "n1"}


def test_node_list():
    node = MagicMock()
    node.attrs = {"ID": "n1"}
    with _patch() as mock_client:
        mock_client.return_value.nodes.list.return_value = [node]
        assert node_list() == [{"ID": "n1"}]


def test_list_nodes_with_filters():
    with _patch() as mock_client:
        mock_client.return_value.nodes.list.return_value = []
        node_list(filters={"role": "manager"})
    mock_client.return_value.nodes.list.assert_called_once_with(filters={"role": "manager"})


def test_node_update():
    node = MagicMock()
    spec = {"Availability": "drain", "Role": "worker"}
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        assert node_update("n1", spec) is True
    node.update.assert_called_once_with(spec)


def test_remove_node_resolves_name_then_uses_high_level_remove():
    node = MagicMock()
    # The tool returns True for its documented bool contract, not the SDK call's return value, so a
    # non-bool here (a plausible future docker-py change) must not leak through.
    node.remove.return_value = None
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        assert node_remove("worker-1") is True
    # The id-or-name is resolved through nodes.get, then removed via the high-level Node.remove().
    mock_client.return_value.nodes.get.assert_called_once_with("worker-1")
    node.remove.assert_called_once_with(force=False)


def test_remove_node_force():
    node = MagicMock()
    node.remove.return_value = True
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        assert node_remove("n1", force=True) is True
    node.remove.assert_called_once_with(force=True)

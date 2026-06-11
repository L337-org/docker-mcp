from unittest.mock import MagicMock, patch

from docker_mcp.tools.nodes import get_node, list_nodes, remove_node, update_node


def _patch():
    return patch("docker_mcp.tools.nodes._get_client")


def test_get_node():
    node = MagicMock()
    node.attrs = {"ID": "n1"}
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        assert get_node("n1") == {"ID": "n1"}


def test_list_nodes():
    node = MagicMock()
    node.attrs = {"ID": "n1"}
    with _patch() as mock_client:
        mock_client.return_value.nodes.list.return_value = [node]
        assert list_nodes() == [{"ID": "n1"}]


def test_list_nodes_with_filters():
    with _patch() as mock_client:
        mock_client.return_value.nodes.list.return_value = []
        list_nodes(filters={"role": "manager"})
    mock_client.return_value.nodes.list.assert_called_once_with(filters={"role": "manager"})


def test_update_node():
    node = MagicMock()
    spec = {"Availability": "drain", "Role": "worker"}
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        assert update_node("n1", spec) is True
    node.update.assert_called_once_with(spec)


def test_remove_node_resolves_name_to_id_then_uses_low_level_api():
    node = MagicMock()
    node.id = "abc123fullid"
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        mock_client.return_value.api.remove_node.return_value = True
        assert remove_node("worker-1") is True
    # A name must be resolved to its ID first — APIClient.remove_node only accepts an ID.
    mock_client.return_value.nodes.get.assert_called_once_with("worker-1")
    mock_client.return_value.api.remove_node.assert_called_once_with("abc123fullid", force=False)


def test_remove_node_force():
    node = MagicMock()
    node.id = "abc123fullid"
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        mock_client.return_value.api.remove_node.return_value = True
        assert remove_node("n1", force=True) is True
    mock_client.return_value.api.remove_node.assert_called_once_with("abc123fullid", force=True)

from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.tools.nodes import node_inspect, node_list, node_remove, node_update, node_wait


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


def _reloading_node(*states: dict) -> MagicMock:
    """A mock node whose attrs advance through `states` on each reload() call."""
    node = MagicMock()
    node.attrs = states[0]
    seq = {"i": 0}

    def _reload():
        node.attrs = states[min(seq["i"], len(states) - 1)]
        seq["i"] += 1

    node.reload.side_effect = _reload
    return node


def test_node_wait_ready_returns_when_ready():
    node = _reloading_node({"Status": {"State": "ready"}, "Spec": {"Availability": "active"}})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        result = node_wait("n1", until="ready", timeout_seconds=5)
    assert result["met"] is True
    assert result["state"] == "ready"
    assert result["availability"] == "active"
    assert result["timed_out"] is False
    node.reload.assert_called()


def test_node_wait_polls_through_down_to_ready():
    node = _reloading_node(
        {"Status": {"State": "down"}, "Spec": {"Availability": "active"}},
        {"Status": {"State": "ready"}, "Spec": {"Availability": "active"}},
    )
    with _patch() as mock_client, patch("docker_mcp.tools.nodes.time.sleep") as sleep:
        mock_client.return_value.nodes.get.return_value = node
        result = node_wait("n1", until="ready", timeout_seconds=10, poll_interval=0.01)
    assert result["met"] is True
    assert node.reload.call_count == 2
    sleep.assert_called_once()


def test_node_wait_times_out():
    node = _reloading_node({"Status": {"State": "down"}, "Spec": {"Availability": "active"}})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        result = node_wait("n1", until="ready", timeout_seconds=0.0)
    assert result["met"] is False
    assert result["timed_out"] is True
    assert result["state"] == "down"


def test_node_wait_sleep_bounded_by_timeout():
    node = _reloading_node({"Status": {"State": "down"}, "Spec": {"Availability": "active"}})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        result = node_wait("n1", until="ready", timeout_seconds=0.05, poll_interval=100)
    assert result["timed_out"] is True


def test_node_wait_rejects_negative_timeout():
    with pytest.raises(ValueError, match="timeout_seconds"):
        node_wait("n1", timeout_seconds=-1)


def test_node_wait_rejects_nonpositive_poll_interval():
    with pytest.raises(ValueError, match="poll_interval"):
        node_wait("n1", poll_interval=0)

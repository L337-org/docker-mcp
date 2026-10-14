from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.exceptions import ToolInputError
from docker_mcp.tools.nodes import node_inspect, node_list, node_remove, node_update
from docker_mcp.tools.nodes import _wait_node


def wait_node(id_or_name, until="ready", timeout_seconds=600.0, poll_interval=2.0):
    """The node helper with the defaults `system_wait` supplies, so these cases read as they did."""
    return _wait_node(id_or_name, until, timeout_seconds, poll_interval)


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


def _node_with_spec(spec):
    node = MagicMock()
    node.attrs = {"ID": "n1", "Version": {"Index": 7}, "Spec": spec}
    return node


def test_node_update_changes_only_the_fields_sent():
    # The Engine replaces the whole spec, so draining by sending only Availability once wiped the
    # node's role and labels. The current spec has to go back with the change laid over it.
    node = _node_with_spec({"Availability": "active", "Role": "manager", "Labels": {"zone": "a"}})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        assert node_update("n1", {"Availability": "drain"}) is True
    node.update.assert_called_once_with({"Availability": "drain", "Role": "manager", "Labels": {"zone": "a"}})


def test_node_update_merges_labels_and_removes_a_null_one():
    node = _node_with_spec({"Role": "worker", "Labels": {"zone": "a", "disk": "ssd", "old": "x"}})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        node_update("n1", {"Labels": {"disk": "hdd", "gpu": "yes", "old": None}})
    node.update.assert_called_once_with({"Role": "worker", "Labels": {"zone": "a", "disk": "hdd", "gpu": "yes"}})


def test_node_update_adds_labels_to_a_node_that_has_none():
    node = _node_with_spec({"Role": "worker", "Availability": "active"})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        node_update("n1", {"Labels": {"zone": "b"}})
    node.update.assert_called_once_with({"Role": "worker", "Availability": "active", "Labels": {"zone": "b"}})


def test_node_update_does_not_modify_the_fetched_spec():
    # The merge works on a copy, so a failed update leaves the node object describing the daemon's state.
    spec = {"Role": "worker", "Labels": {"zone": "a"}}
    node = _node_with_spec(spec)
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        node_update("n1", {"Role": "manager", "Labels": {"zone": None}})
    assert spec == {"Role": "worker", "Labels": {"zone": "a"}}


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


def test_wait_node_ready_returns_when_ready():
    node = _reloading_node({"Status": {"State": "ready"}, "Spec": {"Availability": "active"}})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        result = wait_node("n1", until="ready", timeout_seconds=5)
    assert result["met"] is True
    assert result["state"] == "ready"
    assert result["availability"] == "active"
    assert result["timed_out"] is False
    node.reload.assert_called()


def test_wait_node_polls_through_down_to_ready():
    node = _reloading_node(
        {"Status": {"State": "down"}, "Spec": {"Availability": "active"}},
        {"Status": {"State": "ready"}, "Spec": {"Availability": "active"}},
    )
    with _patch() as mock_client, patch("docker_mcp.tools.nodes.time.sleep") as sleep:
        mock_client.return_value.nodes.get.return_value = node
        result = wait_node("n1", until="ready", timeout_seconds=10, poll_interval=0.01)
    assert result["met"] is True
    assert node.reload.call_count == 2
    sleep.assert_called_once()


def test_wait_node_times_out():
    node = _reloading_node({"Status": {"State": "down"}, "Spec": {"Availability": "active"}})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        result = wait_node("n1", until="ready", timeout_seconds=0.0)
    assert result["met"] is False
    assert result["timed_out"] is True
    assert result["state"] == "down"


def test_wait_node_sleep_bounded_by_timeout():
    node = _reloading_node({"Status": {"State": "down"}, "Spec": {"Availability": "active"}})
    with _patch() as mock_client:
        mock_client.return_value.nodes.get.return_value = node
        result = wait_node("n1", until="ready", timeout_seconds=0.05, poll_interval=100)
    assert result["timed_out"] is True


def test_wait_node_rejects_negative_timeout():
    with pytest.raises(ToolInputError, match="timeout_seconds"):
        wait_node("n1", timeout_seconds=-1)


def test_wait_node_rejects_nonpositive_poll_interval():
    with pytest.raises(ToolInputError, match="poll_interval"):
        wait_node("n1", poll_interval=0)

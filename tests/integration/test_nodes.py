# integration tests for swarm node tools that need a real daemon in swarm mode.
# run with: uv run pytest -m integration (requires `docker swarm init` first)


import pytest

from docker_mcp.tools.nodes import node_inspect, node_list, node_update
from docker_mcp.tools.system import system_wait

pytestmark = pytest.mark.usefixtures("skip_if_no_swarm")


def test_system_wait_node_ready_on_local_manager_real():
    local_node_id = node_list()[0]["ID"]
    result = system_wait(kind="node", target=local_node_id, until="ready", timeout_seconds=10)
    assert result["met"] is True
    assert result["detail"]["state"] == "ready"


def test_node_update_changes_a_label_and_leaves_the_rest_of_the_spec_alone():
    # Labels only: draining the local node would evict whatever else the daemon is running.
    node = node_list()[0]
    node_id, before = node["ID"], node["Spec"]
    key = "docker-mcp-it.node-update"
    try:
        node_update(node_id, {"Labels": {key: "set"}})
        after = node_inspect(node_id)["Spec"]
        assert after["Labels"][key] == "set"
        assert {k: v for k, v in after.items() if k != "Labels"} == {k: v for k, v in before.items() if k != "Labels"}
        assert {k: v for k, v in after["Labels"].items() if k != key} == (before.get("Labels") or {})
    finally:
        node_update(node_id, {"Labels": {key: None}})
    assert key not in (node_inspect(node_id)["Spec"].get("Labels") or {})

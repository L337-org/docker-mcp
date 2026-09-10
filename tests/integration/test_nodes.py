# integration tests for swarm node tools that need a real daemon in swarm mode.
# run with: uv run pytest -m integration (requires `docker swarm init` first)


import pytest

from docker_mcp.tools.nodes import node_list, node_wait

pytestmark = pytest.mark.usefixtures("skip_if_no_swarm")


def test_node_wait_ready_on_local_manager_real():
    local_node_id = node_list()[0]["ID"]
    result = node_wait(local_node_id, until="ready", timeout_seconds=10)
    assert result["met"] is True
    assert result["state"] == "ready"

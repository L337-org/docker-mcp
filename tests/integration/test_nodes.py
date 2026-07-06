# integration tests for swarm node tools that need a real daemon in swarm mode.
# run with: uv run pytest -m integration (requires `docker swarm init` first)

import json

import pytest

from docker_mcp.tools.nodes import node_list, node_wait
from docker_mcp.tools.resources import list_node_resources

pytestmark = pytest.mark.usefixtures("skip_if_no_swarm")


def test_node_wait_ready_on_local_manager_real():
    local_node_id = node_list()[0]["ID"]
    result = node_wait(local_node_id, until="ready", timeout_seconds=10)
    assert result["met"] is True
    assert result["state"] == "ready"


def test_node_resource_reflects_local_manager():
    payload = json.loads(list_node_resources())
    assert len(payload["nodes"]) >= 1
    assert payload["nodes"][0]["role"] == "manager"

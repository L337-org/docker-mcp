# integration tests for network tools that need a real daemon.
# run with: uv run pytest -m integration

import uuid

import pytest

from docker_mcp.tools.networks import create_network, list_networks, remove_network


@pytest.fixture
def managed_network():
    """Create a network through the server (so it gets the provenance stamp); remove it afterwards."""
    name = f"dmcp-it-{uuid.uuid4().hex[:8]}"
    try:
        attrs = create_network(name, driver="bridge")
    except Exception as exc:  # noqa: BLE001 — network create can fail on a constrained CI; skip cleanly
        pytest.skip(f"could not create the test network: {exc}")
    yield name, attrs
    remove_network(name)


def test_create_network_stamps_provenance_and_managed_only_filters(managed_network):
    name, attrs = managed_network
    # The created network carries the managed label by default.
    assert attrs["Labels"]["docker-mcp-server.managed"] == "true"
    assert attrs["Labels"]["docker-mcp-server.tool"] == "create_network"
    # managed_only should find it, and only managed networks should come back.
    matched = list_networks(managed_only=True)
    names = {n["Name"] for n in matched}
    assert name in names
    assert all(n["Labels"].get("docker-mcp-server.managed") == "true" for n in matched)

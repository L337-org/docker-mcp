# library of mcp tools relating to swarm node management

from docker_mcp.server import tool
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.client import _get_client


@tool()
def get_node(id_or_name: str) -> dict:
    """
    Get a swarm node by id or name.

    args: id_or_name: str - The node id or name
    returns: dict - The node's attrs
    """
    return _get_client().nodes.get(id_or_name).attrs


@tool()
def list_nodes(filters: dict | None = None) -> list:
    """
    List swarm nodes.

    args: filters: dict - Filter by attributes (id, name, membership, role)
    returns: list - A list of node attrs dicts
    """
    return [n.attrs for n in _get_client().nodes.list(**drop_none(filters=filters))]


@tool()
def update_node(id_or_name: str, node_spec: dict) -> bool:
    """
    Update a node's spec (availability, name, role, labels).

    args:
        id_or_name: str - The node id or name
        node_spec: dict - The new node spec
    returns: bool - True after the update
    """
    node = _get_client().nodes.get(id_or_name)
    node.update(node_spec)
    return True

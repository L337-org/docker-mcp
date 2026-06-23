# library of mcp tools relating to swarm node management

from docker_mcp.server import tool
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.client import _get_client


@tool()
def get_node(id_or_name: str, host: str | None = None) -> dict:
    """
    Get a swarm node by id or name.

    args: id_or_name - The node id or name
    returns: dict - The node's attrs
    """
    return _get_client(host).nodes.get(id_or_name).attrs


@tool()
def list_nodes(filters: dict | None = None, host: str | None = None) -> list:
    """
    List swarm nodes.

    args: filters - Filter by attributes (id, name, membership, role)
    returns: list - A list of node attrs dicts
    """
    return [n.attrs for n in _get_client(host).nodes.list(**drop_none(filters=filters))]


@tool()
def update_node(id_or_name: str, node_spec: dict, host: str | None = None) -> bool:
    """
    Update a node's spec (availability, name, role, labels).

    args:
        id_or_name - The node id or name
        node_spec - The new node spec
    returns: bool - True after the update
    """
    node = _get_client(host).nodes.get(id_or_name)
    node.update(node_spec)
    return True


@tool()
def remove_node(node_id: str, force: bool = False, host: str | None = None) -> bool:
    """
    Remove a node from the swarm.

    A node should normally be drained (`update_node` with Availability "drain") and have left the
    swarm first, so its tasks reschedule cleanly. Removing an active/reachable node requires `force=True`.

    args:
        node_id - The node id or name to remove
        force - Force removal of an active/reachable node
    returns: bool - True after the node is removed
    """
    _get_client(host).nodes.get(node_id).remove(force=force)
    return True

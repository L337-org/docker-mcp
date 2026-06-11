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


@tool()
def remove_node(node_id: str, force: bool = False) -> bool:
    """
    Remove a node from the swarm.

    A node should normally be drained (`update_node` with Availability "drain") and have left the
    swarm before removal. Removing an active or reachable node requires `force=True`; prefer draining
    first so its tasks reschedule cleanly.

    The high-level SDK has no Node.remove(), so this uses the low-level APIClient. `APIClient.remove_node`
    only accepts a node *ID* (the Engine API is `DELETE /nodes/{id}`), so a name passed here is resolved
    to its ID first via `nodes.get` — like `get_node` / `update_node`, the argument accepts an id or a name.

    args:
        node_id: str - The node id or name to remove
        force: bool - Force removal of an active/reachable node
    returns: bool - True after the node is removed
    """
    client = _get_client()
    resolved_id = client.nodes.get(node_id).id
    return client.api.remove_node(resolved_id, force=force)

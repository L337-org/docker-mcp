# library of mcp tools relating to swarm node management

from docker_mcp.server import tool
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.system import _get_client


@tool()
def node_inspect(id_or_name: str, host: str | None = None) -> dict:
    """
    Get a swarm node by id or name.

    args: id_or_name - The node id or name
    returns: dict - The node's attrs
    """
    return _get_client(host).nodes.get(id_or_name).attrs


@tool()
def node_list(filters: dict | None = None, host: str | None = None) -> list:
    """
    List swarm nodes.

    args: filters - Filter by attributes (id, name, membership, role)
    returns: list - A list of node attrs dicts
    """
    return [n.attrs for n in _get_client(host).nodes.list(**drop_none(filters=filters))]


@tool()
def node_update(id_or_name: str, spec: dict, host: str | None = None) -> bool:
    """
    Replace a node's spec (availability, name, role, labels).

    Replacement, not a merge: `spec` becomes the node's entire spec, and omitted keys are cleared.
    Fetch the current spec via `node_inspect` (its `Spec` key), modify it, and resubmit the whole
    dict — e.g. sending just {"Availability": "drain"} would also wipe the node's role and labels.

    args:
        id_or_name - The node id or name
        spec - The complete new node spec (see description — omitted keys are cleared)
    returns: bool - True after the update
    """
    node = _get_client(host).nodes.get(id_or_name)
    node.update(spec)
    return True


@tool()
def node_remove(id_or_name: str, force: bool = False, host: str | None = None) -> bool:
    """
    Remove a node from the swarm.

    A node should normally be drained (`node_update` with Availability "drain") and have left the
    swarm first, so its tasks reschedule cleanly. Removing an active/reachable node requires `force=True`.

    args:
        id_or_name - The node id or name to remove
        force - Force removal of an active/reachable node
    returns: bool - True after the node is removed
    """
    _get_client(host).nodes.get(id_or_name).remove(force=force)
    return True

"""Tools for Swarm nodes: the machines a cluster schedules onto."""

# library of mcp tools relating to swarm node management

import time
from typing import Literal, TypedDict

from pydantic import ConfigDict, with_config

from docker_mcp.exceptions import ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.system import _get_client


@tool()
def node_inspect(id_or_name: str, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Get a swarm node's full inspect payload by id or name.

    Must run against a swarm manager. Shows role, availability, status, and manager reachability -
    use `node_list` to enumerate nodes first; `service_ps(filters={"node": ...})` shows what a
    service runs on one node.

    Args:
        id_or_name: The node id or hostname (as shown by `node_list`)

    Returns:
        dict: The node's full document (Spec{Role, Availability}, Status, ManagerStatus for managers)
    """
    return _get_client(host).nodes.get(id_or_name).attrs


@tool()
def node_list(filters: dict | None = None, host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List swarm nodes.

    Must run against a swarm manager. The fleet view of membership, role, and state; drill into
    one node with `node_inspect`.

    Args:
        filters: Filter by attributes (id, name, membership, role)

    Returns:
        list: One full node document per node (Spec, Status, ManagerStatus for managers)
    """
    return [n.attrs for n in _get_client(host).nodes.list(**drop_none(filters=filters))]


# Refuse unknown keys: the Engine ignores a field it does not know, so {"availability": "drain"} would otherwise
# return success with the node still active.
@with_config(ConfigDict(extra="forbid"))
class NodeSpecUpdates(TypedDict, total=False):
    """Node spec changes for `node_update`.

    Attributes:
        Availability: whether the node takes tasks.
        Role: the node's swarm role.
        Name: the node's spec name.
        Labels: labels to add or change.
        RemoveLabels: label keys to remove.
    """

    Availability: Literal["active", "pause", "drain"]
    Role: Literal["worker", "manager"]
    Name: str
    Labels: dict[str, str]
    RemoveLabels: list[str]


@tool()
def node_update(  # noqa: DOC101,DOC103,DOC501,DOC503
    id_or_name: str, updates: NodeSpecUpdates, host: str | None = None
) -> bool:
    """
    Change a node's availability, role, name or labels, leaving every field not named as it was.

    Must run against a swarm manager. Send only what changes: {"Availability": "drain"} drains the
    node and keeps its role and labels. "Labels" adds or changes the labels named and keeps the rest;
    "RemoveLabels" removes the keys listed; a key in both is refused. Version-guarded, so a node changed
    by someone else since it was read fails the call rather than being overwritten. `node_inspect` shows
    the current `Spec`; to take a node out of the swarm use `node_remove`.

    Args:
        updates: What to change: "Availability", "Role", "Name", "Labels" as {"key": "value"}, and
            "RemoveLabels" as ["key", ...]

    Returns:
        bool: True after the update
    """
    both = sorted(set(updates.get("Labels", {})) & set(updates.get("RemoveLabels", [])))
    if both:
        raise ToolInputError(f"Labels and RemoveLabels both name {both}: set them or remove them, not both.")
    node = _get_client(host).nodes.get(id_or_name)
    # The Engine replaces a node's whole spec on update ("Any values not provided will be removed", in
    # docker-py's own Node.update docstring), so the current spec is resubmitted with the changes laid
    # over it, as `docker node update` does. Node.update sends the version index from the node just
    # fetched, which is what makes a concurrent change fail instead of being silently overwritten.
    spec = dict(node.attrs.get("Spec") or {})
    spec.update({key: value for key, value in updates.items() if key not in ("Labels", "RemoveLabels")})
    if "Labels" in updates or "RemoveLabels" in updates:
        # Removal is a list of keys, as `docker node update --label-rm` takes, rather than a null label value:
        # a null would make the advertised schema nullable, which the schema slimming cannot drop because here
        # it would carry meaning.
        labels = {**(spec.get("Labels") or {}), **updates.get("Labels", {})}
        for key in updates.get("RemoveLabels", []):
            labels.pop(key, None)
        spec["Labels"] = labels
    node.update(spec)
    return True


@tool()
def node_remove(id_or_name: str, force: bool = False, host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Remove a node from the swarm.

    A node should normally be drained (`node_update` with Availability "drain") and have left the
    swarm first, so its tasks reschedule cleanly. Removing an active/reachable node requires `force=True`.

    Args:
        force: Force removal of an active/reachable node

    Returns:
        bool: True after the node is removed
    """
    _get_client(host).nodes.get(id_or_name).remove(force=force)
    return True


def _node_wait_result(
    id_or_name: str,
    until: str,
    *,
    met: bool,
    start: float,
    timed_out: bool = False,
    state: str | None = None,
    availability: str | None = None,
) -> dict:
    """Build the unified node wait result snapshot - the same shape for every `until` mode.

    Args:
        id_or_name: the object waited on
        until: which wait mode was used
        met: whether the condition was satisfied
        start: when the wait began, for the elapsed time
        timed_out: whether the wait hit its deadline
        state: the node's state, where known
        availability: the node's availability, where known

    Returns:
        dict: the unified snapshot - the same shape for every ``until`` mode
    """
    return {
        "node": id_or_name,
        "until": until,
        "met": met,
        "timed_out": timed_out,
        "state": state,
        "availability": availability,
        "waited_seconds": round(time.monotonic() - start, 2),
    }


def _wait_node(
    id_or_name: str,
    until: str,
    timeout_seconds: float,
    poll_interval: float,
    host: str | None = None,
) -> dict:
    """Wait for a swarm node's `Status.State`; `system_wait(kind="node")` is the tool that calls this.

    Never raises on timeout. Does not track where tasks run: whether a drained node's work has moved
    off is a question for the services' tasks.

    Args:
        id_or_name: the node to wait on
        until: the `Status.State` to wait for: "ready", "down", "disconnected" or "unknown"
        timeout_seconds: seconds to wait before returning with timed_out=true
        poll_interval: seconds between re-inspections, capped by the time left
        host: the host label to target, or None for the default

    Returns:
        dict: {"node", "until", "met", "timed_out", "state", "availability", "waited_seconds"}

    Raises:
        ToolInputError: a negative timeout or a non-positive poll interval
    """
    if timeout_seconds < 0:
        raise ToolInputError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
    if poll_interval <= 0:
        raise ToolInputError(f"poll_interval must be > 0, got {poll_interval}.")
    node = _get_client(host).nodes.get(id_or_name)
    start = time.monotonic()
    deadline = start + timeout_seconds
    while True:
        node.reload()
        state = (node.attrs.get("Status") or {}).get("State")
        availability = (node.attrs.get("Spec") or {}).get("Availability")
        if state == until:
            return _node_wait_result(id_or_name, until, met=True, start=start, state=state, availability=availability)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _node_wait_result(
                id_or_name, until, met=False, start=start, timed_out=True, state=state, availability=availability
            )
        # Bound the sleep by the time left so a large poll_interval can't block past the timeout.
        time.sleep(min(poll_interval, remaining))

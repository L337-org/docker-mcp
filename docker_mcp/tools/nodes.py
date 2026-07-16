# library of mcp tools relating to swarm node management

import time
from typing import Literal

from docker_mcp.server import tool
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.system import _get_client


@tool()
def node_inspect(id_or_name: str, host: str | None = None) -> dict:
    """
    Get a swarm node's full inspect payload by id or name.

    Must run against a swarm manager. Shows role, availability, status, and manager reachability —
    use `node_list` to enumerate nodes first, or the `docker://nodes` resource for a fleet
    summary; `service_ps(filters={"node": ...})` shows what a service runs on one node.

    args: id_or_name - The node id or hostname (as shown by `node_list`)
    returns: dict - The node's attrs (Spec{Role, Availability}, Status, ManagerStatus for managers)
    """
    return _get_client(host).nodes.get(id_or_name).attrs


@tool()
def node_list(filters: dict | None = None, host: str | None = None) -> list:
    """
    List swarm nodes.

    Must run against a swarm manager. The fleet view of membership, role, and state; drill into
    one node with `node_inspect`, or read the `docker://nodes` resource for a computed summary.

    args: filters - Filter by attributes (id, name, membership, role)
    returns: list - One full node document per node (Spec, Status, ManagerStatus for managers)
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
    """Build the unified node_wait result snapshot — the same shape for every `until` mode."""
    return {
        "node": id_or_name,
        "until": until,
        "met": met,
        "timed_out": timed_out,
        "state": state,
        "availability": availability,
        "waited_seconds": round(time.monotonic() - start, 2),
    }


@tool()
def node_wait(
    id_or_name: str,
    until: Literal["ready", "down", "disconnected", "unknown"] = "ready",
    timeout_seconds: float = 300.0,
    poll_interval: float = 2.0,
    host: str | None = None,
) -> dict:
    """
    Block until a swarm node's Status.State reaches a target value.

    Never raises on timeout — the result always carries `met` and `timed_out`. Polls
    `Status.State` (one of "unknown"/"down"/"ready"/"disconnected") every `poll_interval`s.
    Common uses: `until="ready"` after a newly joined node, or `until="down"` while draining a
    node before removal. Does not track task placement — for "has this drained node's workload
    fully moved off", inspect the relevant services' tasks directly; no single cheap call spans
    every service in the swarm, so that check isn't built into this tool. `service_wait` covers
    service convergence; `node_list` shows every node's state at once.

    args:
        id_or_name - The node id or name
        until - Target Status.State to wait for: "ready" (default), "down", "disconnected", "unknown"
        timeout_seconds - Max seconds to wait before returning with timed_out=true (default 300)
        poll_interval - Seconds between re-inspections (default 2, > 0); capped by the time left so
                        a large value can't push the total wait past the timeout
    returns: dict - {"node", "until", "met", "timed_out", "state", "availability", "waited_seconds"}
    """
    if timeout_seconds < 0:
        raise ValueError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}.")
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

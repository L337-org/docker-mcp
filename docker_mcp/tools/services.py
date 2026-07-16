# library of mcp tools relating to swarm service management

import time
from collections.abc import Iterable
from typing import Literal, cast

from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import MAX_PAYLOAD_BYTES, drop_none, join_bounded
from docker_mcp.tools.system import _get_client


# --- shared read helpers, also used by the service-logs:// / service-tasks:// resources in
# resources.py, and by service_wait's "running" mode ---

_FAILING_TASK_STATES = frozenset({"failed", "rejected"})


def _read_service_log_tail(id_or_name: str, tail: int = 200, host: str | None = None) -> str:
    """Return a bounded, non-streaming tail of a swarm service's combined stdout/stderr logs."""
    service = _get_client(host).services.get(id_or_name)
    output = service.logs(stdout=True, stderr=True, follow=False, tail=tail)

    def _as_bytes(chunks: Iterable) -> Iterable[bytes]:
        for chunk in chunks:
            yield chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", errors="replace")

    raw = join_bounded(_as_bytes(cast(Iterable, output)), MAX_PAYLOAD_BYTES, f"logs of service {id_or_name}")
    return raw.decode("utf-8", errors="replace")


def _read_service_task_summary(id_or_name: str, host: str | None = None) -> dict:
    """
    Return a computed task/rollout status summary for a swarm service.

    Reproduces what the `audit_swarm_health` prompt already does by hand: counts tasks whose
    *desired* state is "running" by their actual `Status.State`, compares the count against the
    service's desired replica count (Replicated mode) or the total returned tasks (Global mode —
    one task per eligible node, no fixed target), and surfaces any failing tasks' id/node/error.
    Also includes `UpdateStatus.State` from the same service read, so this one summary doubles as
    a rollout-progress view.
    """
    service = _get_client(host).services.get(id_or_name)
    attrs = service.attrs
    mode = (attrs.get("Spec", {}) or {}).get("Mode", {}) or {}
    tasks = service.tasks(filters={"desired-state": "running"})
    running = sum(1 for t in tasks if (t.get("Status") or {}).get("State") == "running")
    # `Replicas` is optional in the daemon's own schema (no documented default), so a Replicated
    # service could in principle omit it — fall back to the observed task count in that case too,
    # the same fallback already used for every non-Replicated mode.
    desired = mode.get("Replicated", {}).get("Replicas") if "Replicated" in mode else None
    if desired is None:
        desired = len(tasks)
    failed_tasks = [
        {
            "id": t.get("ID"),
            "node_id": t.get("NodeID"),
            "state": (t.get("Status") or {}).get("State"),
            "err": (t.get("Status") or {}).get("Err"),
            "message": (t.get("Status") or {}).get("Message"),
        }
        for t in tasks
        if (t.get("Status") or {}).get("State") in _FAILING_TASK_STATES or (t.get("Status") or {}).get("Err")
    ]
    update_status = attrs.get("UpdateStatus") or {}
    return {
        "service": service.name,
        "mode": "replicated" if "Replicated" in mode else ("global" if "Global" in mode else None),
        "running_tasks": running,
        "desired_tasks": desired,
        "failed_tasks": failed_tasks,
        "update_state": update_status.get("State"),
    }


@tool()
def service_create(
    image: str, command: str | list | None = None, extra_kwargs: dict | None = None, host: str | None = None
) -> dict:
    """
    Create a Swarm service; requires a swarm manager node.

    Use this instead of `container_run` when you need replicated or global scheduling,
    rolling updates, or automatic restart across the swarm. Common `extra_kwargs` keys:
    `name` (str), `env` (list of "KEY=VAL"), `mode` ({"Replicated": {"Replicas": N}} or
    {"Global": {}}), `networks` (list of network names/ids), `endpoint_spec`
    ({"Ports": [{"PublishedPort": 80, "TargetPort": 8080}]}), `labels` (dict),
    `restart_policy` ({"Condition": "on-failure", "MaxAttempts": 3}),
    `resources` ({"Limits": {"NanoCPUs": 500000000, "MemoryBytes": 134217728}}). For anything else
    docker-py's `ServiceCollection.create` accepts, call `docs_lookup(section="services")` rather
    than guessing a key name.

    args:
        image - Image to run service tasks from (e.g. "nginx:alpine")
        command - Override the image's default command; string or list of strings
        extra_kwargs - Additional docker-py ServiceCollection.create keyword arguments
    returns: dict - The created service's full document ({"ID", "Version", "Spec", ...})
    """
    kwargs = dict(extra_kwargs or {})
    # Stamp the service-level `labels`; leave any caller `container_labels` untouched.
    labels = with_provenance(kwargs.get("labels"), "service_create")
    if labels is not None:
        kwargs["labels"] = labels
    return _get_client(host).services.create(image, command=command, **kwargs).attrs


@tool()
def service_inspect(id_or_name: str, insert_defaults: bool | None = None, host: str | None = None) -> dict:
    """
    Get a swarm service by id or name.

    Must run against a swarm manager. Returns the desired-state spec and rollout status — for the
    actually-running tasks use `service_ps`, or the `service-tasks://{id_or_name}` resource for a
    computed rollout summary.

    args:
        id_or_name - The service id or name
        insert_defaults - Merge default values into the output
    returns: dict - The full service document ({"ID", "Version", "Spec", "Endpoint", ...};
        "UpdateStatus" during a rolling update)
    """
    return _get_client(host).services.get(id_or_name, insert_defaults=insert_defaults).attrs


@tool()
def service_list(filters: dict | None = None, managed_only: bool = False, host: str | None = None) -> list:
    """
    List swarm services.

    Must run against a swarm manager. One entry per service (the desired state); `service_ps`
    lists a service's tasks, and `stack_services` groups services by stack.

    args:
        filters - Filter by attributes (id, name, label, mode)
        managed_only - Only return services created by this MCP server (filters on the
                             docker-mcp-server.managed label); combines with any `filters` given
    returns: list - One full service document ({"ID", "Spec", ...}) per service
    """
    if managed_only:
        filters = managed_filter(filters)
    return [s.attrs for s in _get_client(host).services.list(**drop_none(filters=filters))]


@tool()
def service_update(id_or_name: str, updates: dict | None = None, force: bool = False, host: str | None = None) -> bool:
    """
    Update a swarm service's configuration, or force a redeploy with no spec change.

    Pass exactly one of `updates` (fields to change, same parameters as `service_create`) or
    `force=True` (the `docker service update --force` equivalent: bumps the ForceUpdate counter so
    the service's tasks redeploy with an unchanged spec — e.g. to reschedule after a node change or
    re-pull a mutable tag).

    args:
        id_or_name - The service id or name
        updates - Fields to update on the service; exactly one of updates/force
        force - Redeploy the service without changing its spec; exactly one of updates/force
    returns: bool - True after the update
    """
    if (updates is None) == (not force):
        raise ValueError("Pass exactly one of `updates` (fields to change) or `force=True` (redeploy unchanged).")
    service = _get_client(host).services.get(id_or_name)
    if force:
        service.force_update()
        return True
    service.update(**cast(dict, updates))
    return True


@tool()
def service_remove(id_or_name: str, host: str | None = None) -> bool:
    """
    Stop and remove a swarm service.

    Requires a swarm manager. Deletes the service definition and shuts down its tasks — no
    confirmation, no undo. To stop work but keep the definition, `service_scale` to 0 replicas.

    args: id_or_name - The service id or name
    returns: bool - True after the service is removed
    """
    _get_client(host).services.get(id_or_name).remove()
    return True


@tool()
def service_ps(id_or_name: str, filters: dict | None = None, host: str | None = None) -> list:
    """
    List a swarm service's tasks (per-replica scheduling units), like `docker service ps`.

    Shows where replicas run and why they fail: each task carries `Status`
    (State/Message/ContainerStatus), `DesiredState`, `NodeID`, and `Slot`. Prefer this over
    `container_list` for services (tasks may run on other nodes), `stack_ps` for a whole stack,
    and the `service-tasks://{id_or_name}` resource for a computed rollout summary. Requires a
    swarm manager.

    args:
        id_or_name - The service id or name
        filters - Filter dict; keys: id, name, node, label, desired-state (running|shutdown|accepted)
    returns: list - Task dicts (ID, Slot, NodeID, Status, DesiredState, Spec)
    """
    service = _get_client(host).services.get(id_or_name)
    return service.tasks(filters=filters)


@tool()
def service_logs(
    id_or_name: str,
    details: bool = False,
    stdout: bool = True,
    stderr: bool = True,
    since: int = 0,
    timestamps: bool = False,
    tail: int | Literal["all"] = 200,
    max_bytes: int = MAX_PAYLOAD_BYTES,
    host: str | None = None,
) -> str:
    """
    Get a bounded snapshot of a swarm service's logs (never follows).

    `follow` is intentionally not exposed: the stream is joined into one string before returning, so
    following would block forever and grow unbounded. Collection is capped at `max_bytes` (ValueError
    if exceeded) so a noisy service can't OOM the server. The default is a bounded `tail=200`;
    `tail="all"` returns the whole buffer, which can be huge on long-running services and exceed
    the agent's context — prefer an integer, or `since`, to constrain output. Logs aggregate
    across all the service's tasks — `container_logs` reads a single container, and the
    `service-logs://{id_or_name}` resource is the resource-flavored equivalent of this tool.

    args:
        id_or_name - The service id or name
        details - Show extra details
        stdout - Include stdout
        stderr - Include stderr
        since - Show logs since this Unix timestamp
        timestamps - Include timestamps
        tail - Number of lines from the end (default 200), or the literal "all" for everything
        max_bytes - Abort with ValueError if the buffered logs exceed this many bytes (default 32 MiB)
    returns: str - Decoded log output
    """
    service = _get_client(host).services.get(id_or_name)
    output = service.logs(
        details=details,
        follow=False,
        stdout=stdout,
        stderr=stderr,
        since=since,
        timestamps=timestamps,
        tail=tail,
    )

    def _as_bytes(chunks: Iterable) -> Iterable[bytes]:
        for chunk in chunks:
            yield chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", errors="replace")

    raw = join_bounded(_as_bytes(cast(Iterable, output)), max_bytes, f"logs of service {id_or_name}")
    return raw.decode("utf-8", errors="replace")


@tool()
def service_scale(id_or_name: str, replicas: int, host: str | None = None) -> bool:
    """
    Set the desired replica count for a Replicated-mode swarm service.

    Only applies to services in `Replicated` mode; a `Global` service runs one task per
    eligible node and has no replica count to set. The swarm scheduler places or removes
    tasks asynchronously to converge on the new count — this call returns once the update
    is accepted, not once every task is running. Check progress with `service_ps` or
    `service_inspect`. For any other spec change (image, env, resources) use
    `service_update` instead.

    args:
        id_or_name - The service id or name
        replicas - The desired number of running task replicas
    returns: bool - True once the scale request is accepted
    """
    return _get_client(host).services.get(id_or_name).scale(replicas)


@tool()
def service_rollback(id_or_name: str, host: str | None = None) -> dict:
    """
    Roll a swarm service back to its previous spec (the docker `service rollback` equivalent).

    Re-applies the service's `PreviousSpec` — the spec from before the most recent `service_update` /
    `service_scale`. Raises ValueError if the service has no PreviousSpec
    (it has never been updated, or was already rolled back). The high-level SDK exposes no rollback,
    so this reads the current version and previous spec via the low-level APIClient and submits them
    with the low-level `update_service` API call.

    args: id_or_name - The service id or name
    returns: dict - The daemon response (a dict with a "Warnings" key)
    """
    api = _get_client(host).api
    info = api.inspect_service(id_or_name)
    previous = info.get("PreviousSpec")
    if not previous:
        raise ValueError(
            f"Service {id_or_name} has no PreviousSpec to roll back to (never updated, or already rolled back)."
        )
    version = info["Version"]["Index"]
    # fetch_current_spec=False is the docker-py default, but pass it explicitly: rollback must *replace*
    # the service with PreviousSpec, not merge PreviousSpec over the current spec. With it False the
    # daemon-side base is empty, so fields absent from PreviousSpec are genuinely unset (the intended
    # rollback), not silently carried over from the spec we're rolling away from.
    return api.update_service(
        id_or_name,
        version,
        task_template=previous.get("TaskTemplate"),
        name=previous.get("Name"),
        labels=previous.get("Labels"),
        mode=previous.get("Mode"),
        update_config=previous.get("UpdateConfig"),
        rollback_config=previous.get("RollbackConfig"),
        networks=previous.get("Networks"),
        endpoint_spec=previous.get("EndpointSpec"),
        fetch_current_spec=False,
    )


def _service_wait_result(
    id_or_name: str,
    until: str,
    *,
    met: bool,
    start: float,
    timed_out: bool = False,
    running_tasks: int | None = None,
    desired_tasks: int | None = None,
    failed_tasks: list | None = None,
    update_state: str | None = None,
) -> dict:
    """Build the unified service_wait result snapshot — the same shape for every `until` mode."""
    return {
        "service": id_or_name,
        "until": until,
        "met": met,
        "timed_out": timed_out,
        "running_tasks": running_tasks,
        "desired_tasks": desired_tasks,
        "failed_tasks": failed_tasks if failed_tasks is not None else [],
        "update_state": update_state,
        "waited_seconds": round(time.monotonic() - start, 2),
    }


@tool()
def service_wait(
    id_or_name: str,
    until: Literal["running", "update-converged"] = "running",
    replicas: int | None = None,
    timeout_seconds: float = 600.0,
    poll_interval: float = 2.0,
    host: str | None = None,
) -> dict:
    """
    Block until a swarm service's tasks converge, or a rolling update finishes.

    One contract for both modes: never raises on timeout — the result always carries `met` and
    `timed_out`. "running" polls task state via the same task-counting logic as
    `service-tasks://{id_or_name}` (not the unconfirmed daemon `ServiceStatus` field) until running
    tasks reach the desired count (Replicated mode) or every returned task is running (Global mode,
    which has no fixed target). "update-converged" polls `UpdateStatus.State` until it reaches a
    terminal value (`completed` or `rollback_completed`); if the service has never been updated (no
    `UpdateStatus` at all), returns promptly with `met=false` — there's nothing to converge to, same
    as `container_wait`'s no-healthcheck case.

    args:
        id_or_name - The service id or name
        until - Condition to wait for: "running" (default) or "update-converged"
        replicas - "running" mode only: override the desired replica count (e.g. right after a
                   same-turn `service_scale` call, before polling reflects the new target)
        timeout_seconds - Max seconds to wait before returning with timed_out=true (default 600)
        poll_interval - Seconds between re-checks (default 2, > 0); capped by the time left so a
                        large value can't push the total wait past the timeout
    returns: dict - {"service", "until", "met", "timed_out", "running_tasks", "desired_tasks",
                     "failed_tasks", "update_state", "waited_seconds"}
    """
    if timeout_seconds < 0:
        raise ValueError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}.")
    if replicas is not None and replicas < 0:
        raise ValueError(f"replicas must be >= 0, got {replicas}.")
    start = time.monotonic()
    deadline = start + timeout_seconds
    while True:
        summary = _read_service_task_summary(id_or_name, host=host)
        desired = replicas if (replicas is not None and until == "running") else summary["desired_tasks"]
        common = {
            "running_tasks": summary["running_tasks"],
            "desired_tasks": desired,
            "failed_tasks": summary["failed_tasks"],
            "update_state": summary["update_state"],
        }
        if until == "running":
            if summary["running_tasks"] >= desired:
                return _service_wait_result(id_or_name, until, met=True, start=start, **common)
        else:  # "update-converged"
            update_state = summary["update_state"]
            if update_state is None:
                # No UpdateStatus at all: nothing to converge to, don't poll to the timeout.
                return _service_wait_result(id_or_name, until, met=False, start=start, **common)
            if update_state in ("completed", "rollback_completed"):
                return _service_wait_result(id_or_name, until, met=True, start=start, **common)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _service_wait_result(id_or_name, until, met=False, start=start, timed_out=True, **common)
        # Bound the sleep by the time left so a large poll_interval can't block past the timeout.
        time.sleep(min(poll_interval, remaining))

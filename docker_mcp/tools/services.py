"""Tools for Swarm services: their lifecycle, scaling, and the tasks they produce."""

# library of mcp tools relating to swarm service management

import time
from typing import Literal, cast

import docker
import docker.models.services

from docker_mcp.exceptions import CapabilityError, RemoteFailureError, ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import (
    MAX_PAYLOAD_BYTES,
    as_byte_chunks,
    close_stream_quietly,
    drop_none,
    join_bounded,
)
from docker_mcp.tools.system import _get_client


# --- shared read helpers, also used by the service-logs:// / service-tasks:// resources in
# resources.py, and by the service and stack waits behind system_wait ---

_FAILING_TASK_STATES = frozenset({"failed", "rejected"})
# `UpdateStatus.State` values after which a rolling update is over, and those where it stopped on a failure
# and will not move again without someone acting (moby `api/types/swarm` UpdateState).
_UPDATE_DONE = frozenset({"completed", "rollback_completed"})
_UPDATE_STUCK = frozenset({"paused", "rollback_paused"})
# Desired states up to and including "running", the ones swarm still means a task to reach (docker/cli's
# `numberedStates` in cli/command/service/progress/progress.go). A replacement waiting out its restart
# delay is desired "ready", so this keeps its node counted between a rejection and the retry.
_LIVE_DESIRED_STATES = frozenset(
    {"new", "allocated", "pending", "assigned", "accepted", "preparing", "ready", "starting", "running"}
)


def _runs_current_spec(task: dict, template: dict) -> bool:
    """Whether a task was created from its service's current task template, i.e. an update left it alone.

    Compares the keys the task's own spec carries: the template also holds `Runtime`, which tasks omit.
    Swarm decides whether a task needs replacing the same way, from its spec against the template. The
    template must be read without the daemon's inserted defaults, as docker-py reads it; tasks carry none.

    Args:
        task: a task document, as `client.api.tasks()` returns it
        template: the service's `Spec.TaskTemplate`

    Returns:
        bool: True when every key of the task's spec equals the template's
    """
    spec = task.get("Spec") or {}
    return all(template.get(key) == value for key, value in spec.items())


def _global_node_counts(service: docker.models.services.Service, client: docker.DockerClient) -> tuple[int, int, int]:
    """How many nodes a global service wants a task on, and on how many one is running, of them on the current spec.

    Counted by node rather than by task, as docker/cli's `globalProgressUpdater` does: a node whose task
    was rejected keeps a replacement swarm still means to run, so it stays wanted rather than dropping out
    of both counts until the retry. A drained node's tasks are shut down and stop counting; a task on a
    node that is down is not counted as running.

    Args:
        service: the docker-py Service model
        client: the docker-py client it came from

    Returns:
        tuple: (nodes wanted, nodes running, nodes running a task made from the current spec)
    """
    tasks = service.tasks()
    template = (service.attrs.get("Spec") or {}).get("TaskTemplate") or {}
    down = {node.id for node in client.nodes.list() if (node.attrs.get("Status") or {}).get("State") == "down"}
    wanted = {t.get("NodeID") for t in tasks if t.get("NodeID") and t.get("DesiredState") in _LIVE_DESIRED_STATES}
    running_tasks = [
        t
        for t in tasks
        if t.get("DesiredState") == "running"
        and (t.get("Status") or {}).get("State") == "running"
        and t.get("NodeID") not in down
    ]
    running = {t.get("NodeID") for t in running_tasks}
    current = {t.get("NodeID") for t in running_tasks if _runs_current_spec(t, template)}
    return len(wanted), len(running), len(current)


def _read_service_log_tail(id_or_name: str, tail: int = 200, host: str | None = None) -> str:
    """Return a bounded, non-streaming tail of a swarm service's combined stdout/stderr logs.

    Args:
        id_or_name: the service to read
        tail: how many lines to return
        host: the host label to target, or None for the default

    Returns:
        str: the combined stdout and stderr tail, bounded and non-streaming
    """
    service = _get_client(host).services.get(id_or_name)
    output = service.logs(stdout=True, stderr=True, follow=False, tail=tail)

    # Fixed cap, as in `_read_bounded_container_logs`: `tail` is the lever the caller has.
    raw = join_bounded(
        as_byte_chunks(output),
        MAX_PAYLOAD_BYTES,
        f"logs of service {id_or_name}",
        remedy="Request fewer lines with `tail`.",
    )
    return raw.decode("utf-8", errors="replace")


def _read_service_task_summary(id_or_name: str, host: str | None = None) -> dict:
    """Return a computed task/rollout status summary for a swarm service.

    Reproduces what the `audit_swarm_health` prompt already does by hand: counts tasks whose
    *desired* state is "running" by their actual `Status.State` and compares the count against the
    service's desired replica count (Replicated mode). A Global service has no fixed target, so it is
    counted by node instead: the nodes swarm still means to run a task on, against those running one.
    Surfaces any failing tasks' id/node/error. `current_tasks` counts only the running tasks made from
    the service's current spec, the same way by node for a Global service: during a rolling update, and
    for a moment after one is requested, the old tasks still run and are in `running_tasks` but not here.
    Also includes `UpdateStatus.State` from the same service read, so this one summary doubles as
    a rollout-progress view.

    Args:
        id_or_name: the service to summarise
        host: the host label to target, or None for the default

    Returns:
        dict: the task and rollout status summary
    """
    client = _get_client(host)
    service = client.services.get(id_or_name)
    attrs = service.attrs
    mode = (attrs.get("Spec", {}) or {}).get("Mode", {}) or {}
    tasks = service.tasks(filters={"desired-state": "running"})
    if "Global" in mode:
        desired, running, current = _global_node_counts(service, client)
    else:
        template = (attrs.get("Spec") or {}).get("TaskTemplate") or {}
        live = [t for t in tasks if (t.get("Status") or {}).get("State") == "running"]
        running = len(live)
        current = sum(1 for t in live if _runs_current_spec(t, template))
        # `Replicas` is optional in the daemon's own schema (no documented default), so a Replicated
        # service could in principle omit it - fall back to the observed task count in that case too,
        # the same fallback already used for every other mode.
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
        "current_tasks": current,
        "desired_tasks": desired,
        "failed_tasks": failed_tasks,
        "update_state": update_status.get("State"),
    }


def _awaiting_global_tasks(summary: dict) -> bool:
    """Whether a global service has no tasks yet, so its desired count says nothing about its nodes.

    A global service's desired count is the nodes it has a live task on, which is zero until the
    orchestrator creates them, so "every node runs one" would be met before anything ran. The
    orchestrator creates every eligible node's task at once, so docker/cli's own `--detach=false` waits
    for the first (cli/command/service/progress/progress.go, `globalProgressUpdater`); the waits do the
    same. A global service whose constraints match no node therefore waits to its timeout, as the CLI
    does.

    Args:
        summary: a `_read_service_task_summary` result

    Returns:
        bool: True for a global service with no task to count yet
    """
    return summary["mode"] == "global" and summary["desired_tasks"] == 0


@tool()
def service_create(  # noqa: DOC101,DOC103
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

    Args:
        image: Image to run service tasks from (e.g. "nginx:alpine")
        command: Override the image's default command; string or list of strings
        extra_kwargs: Additional docker-py ServiceCollection.create keyword arguments

    Returns:
        dict: The created service's full document ({"ID", "Version", "Spec", ...})
    """
    kwargs = dict(extra_kwargs or {})
    # Stamp the service-level `labels`; leave any caller `container_labels` untouched.
    labels = with_provenance(kwargs.get("labels"), "service_create")
    if labels is not None:
        kwargs["labels"] = labels
    return _get_client(host).services.create(image, command=command, **kwargs).attrs


@tool()
def service_inspect(  # noqa: DOC101,DOC103
    id_or_name: str,
    insert_defaults: bool | None = None,
    host: str | None = None,
) -> dict:
    """
    Get a swarm service by id or name.

    Must run against a swarm manager. Returns the desired-state spec and rollout status - for the
    actually-running tasks use `service_ps`, or the `service-tasks://{id_or_name}` resource for a
    computed rollout summary.

    Args:
        insert_defaults: Merge default values into the output

    Returns:
        dict: The full service document ({"ID", "Version", "Spec", "Endpoint", ...}; "UpdateStatus" during a rolling
            update)
    """
    return _get_client(host).services.get(id_or_name, insert_defaults=insert_defaults).attrs


@tool()
def service_list(  # noqa: DOC101,DOC103
    filters: dict | None = None,
    status: bool | None = None,
    managed_only: bool = False,
    host: str | None = None,
) -> list:
    """
    List swarm services.

    Must run against a swarm manager. One entry per service (the desired state); `service_ps`
    lists a service's tasks, and `stack_services` groups services by stack. Pass `status=True`
    for a whole-swarm health sweep: it adds each service's running-versus-desired task counts in
    the one call, where the alternative is a `service_ps` per service. It is a count, not a
    diagnosis - when a service comes back short, `service_ps` on that one name is what names the
    failing task and its error.

    Args:
        filters: Filter by attributes (id, name, label, mode)
        status: Add a `ServiceStatus` entry with the running, desired and completed task counts (needs daemon API
            v1.41+); omit when only the specs are wanted
        managed_only: Only return services created by this MCP server (filters on the docker-mcp-server.managed label);
            combines with any `filters` given

    Returns:
        list: One full service document ({"ID", "Spec", ...}) per service; with `status` each also carries
            "ServiceStatus" ({"RunningTasks", "DesiredTasks", "CompletedTasks"})
    """
    if managed_only:
        filters = managed_filter(filters)
    # `status` is passed only when set: docker-py version-checks it on `is not None`, so a literal
    # status=False would newly raise InvalidVersion against a pre-v1.41 daemon that the default
    # call has always worked on.
    return [s.attrs for s in _get_client(host).services.list(**drop_none(filters=filters, status=status))]


@tool()
def service_update(  # noqa: DOC101,DOC103,DOC501,DOC503
    id_or_name: str,
    updates: dict | None = None,
    force: bool = False,
    host: str | None = None,
) -> bool:
    """
    Update a swarm service's configuration, or force a redeploy with no spec change.

    Pass exactly one of `updates` (fields to change, same parameters as `service_create`) or
    `force=True` (the `docker service update --force` equivalent: bumps the ForceUpdate counter so
    the service's tasks redeploy with an unchanged spec - e.g. to reschedule after a node change or
    re-pull a mutable tag).

    Args:
        updates: Fields to update on the service; exactly one of updates/force
        force: Redeploy the service without changing its spec; exactly one of updates/force

    Returns:
        bool: True after the update
    """
    if (updates is None) == (not force):
        raise ToolInputError("Pass exactly one of `updates` (fields to change) or `force=True` (redeploy unchanged).")
    service = _get_client(host).services.get(id_or_name)
    if force:
        service.force_update()
        return True
    service.update(**cast(dict, updates))
    return True


@tool()
def service_remove(id_or_name: str, host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Stop and remove a swarm service.

    Requires a swarm manager. Deletes the service definition and shuts down its tasks - no
    confirmation, no undo. To stop work but keep the definition, `service_scale` to 0 replicas.

    Returns:
        bool: True after the service is removed
    """
    _get_client(host).services.get(id_or_name).remove()
    return True


@tool()
def service_ps(  # noqa: DOC101,DOC103
    id_or_name: str | None = None,
    filters: dict | None = None,
    host: str | None = None,
) -> list:
    """
    List swarm tasks: one service's, or with no service every task in the swarm, like `docker service ps`.

    Omit `id_or_name` for the cluster-wide view - what is failing anywhere, or with the `node` filter
    one node's workload (the CLI's `docker node ps`) - in one call rather than one per service;
    `stack_ps` covers one stack. Find a single task with the `id` filter (full id or prefix) or
    `name` (the full `<service>.<slot>.<taskid>`, or `<service>.<slot>` for every task that slot has
    run). Both match by prefix, so an ambiguous prefix returns several tasks rather than an error, and
    `web.1` also matches `web.10`. Only container tasks come back unless `runtime` is filtered (e.g.
    `attachment`). Each task carries `Status` (State/Message/ContainerStatus), `DesiredState`,
    `NodeID`, `Slot` and its full `Spec`; `service_logs(task=...)` reads what one printed. Prefer this
    over `container_list` for services, whose tasks may run on other nodes. Read-only. Requires a swarm
    manager: on any other node the daemon refuses, and its refusal is what comes back.

    Args:
        id_or_name: The service whose tasks to list; omit for every task in the swarm
        filters: Filter dict; keys: id, name, service, node, label, desired-state (running|shutdown|accepted), runtime

    Returns:
        list: One full task document per task (ID, ServiceID, NodeID, Slot, Spec, Status, DesiredState)
    """
    client = _get_client(host)
    if id_or_name is None:
        # docker-py has no task collection (no `client.tasks`), so the swarm-wide list is the
        # documented low-level call; `Service.tasks` below is the same call with a service filter.
        return client.api.tasks(filters=filters)
    return client.services.get(id_or_name).tasks(filters=filters)


@tool()
def service_logs(  # noqa: DOC101,DOC103,DOC501,DOC503
    id_or_name: str | None = None,
    task: str | None = None,
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
    Get a bounded snapshot of a swarm service's logs, or of one of its tasks (never follows).

    Pass `id_or_name` for every task of a service interleaved, or `task` for one replica - the one
    that actually failed, found with `service_ps` - just as `docker service logs` takes either.
    `container_logs` is no substitute on a multi-node swarm: a task's container lives on whichever
    node the scheduler chose, and this server talks to one daemon. `follow` is not exposed (the
    stream is joined into one string, so following would never finish). `tail` and `max_bytes` apply
    to what was asked for, so a quiet replica is not crowded out by noisy ones; `tail="all"` can
    exceed the agent's context, so prefer an integer or `since`. docker-py has no public call for a
    task's logs, so that path uses its private request helpers and raises if those internals move.

    Args:
        id_or_name: The service whose logs to read; give this or `task`, not both
        task: One task's id, an unambiguous id prefix, or its full `<service>.<slot>.<taskid>` name; give this or
            `id_or_name`, not both
        details: Include the extra attributes given to the log driver (`--details`)
        since: Show logs since this Unix timestamp
        tail: Number of lines from the end, or the literal "all" for everything
        max_bytes: Abort with ToolInputError if the buffered logs exceed this many bytes (default 32 MiB)

    Returns:
        str: Decoded log output
    """
    if (id_or_name is None) == (task is None):
        raise ToolInputError(
            "service_logs reads either a whole service or one task: pass exactly one of `id_or_name` "
            f"(got {id_or_name!r}) or `task` (got {task!r})"
        )
    params = {
        "details": details,
        "stdout": stdout,
        "stderr": stderr,
        "since": since,
        "timestamps": timestamps,
        "tail": tail,
    }
    if task is not None:
        return _read_task_logs(task, params, max_bytes, host)
    service = _get_client(host).services.get(id_or_name)
    output = service.logs(follow=False, **params)
    raw = join_bounded(as_byte_chunks(output), max_bytes, f"logs of service {id_or_name}")
    return raw.decode("utf-8", errors="replace")


def _read_task_logs(task: str, params: dict, max_bytes: int, host: str | None) -> str:
    """Read one swarm task's logs through `GET /tasks/{id}/logs`, bounded and non-following.

    docker-py has no task collection and no `APIClient.task_logs`, so this drives its private
    request helpers against the published Engine route. Drop the reach-in if docker-py grows a public
    method.

    Args:
        task: the task's id, an unambiguous id prefix, or its full name
        params: the log query options, without `follow`
        max_bytes: abort with ToolInputError if the buffered logs exceed this many bytes
        host: the host label to target, or None for the default

    Returns:
        str: the decoded log output

    Raises:
        CapabilityError: the installed docker-py no longer has the private helpers this needs
        RemoteFailureError: the daemon returned a task document with no ID to address
    """
    api = _get_client(host).api
    # Resolved via getattr so a docker-py that has moved these internals gives the actionable
    # message below rather than an AttributeError from inside the call.
    build_url = getattr(api, "_url", None)
    get = getattr(api, "_get", None)
    raise_for_status = getattr(api, "_raise_for_status", None)
    result_tty = getattr(api, "_get_result_tty", None)
    if build_url is None or get is None or raise_for_status is None or result_tty is None:
        missing = sorted(
            attr
            for attr, fn in (
                ("_url", build_url),
                ("_get", get),
                ("_raise_for_status", raise_for_status),
                ("_get_result_tty", result_tty),
            )
            if fn is None
        )
        raise CapabilityError(
            f"the installed docker-py no longer exposes {', '.join(missing)} on APIClient, which "
            "service_logs(task=...) needs to reach GET /tasks/{id}/logs; read the whole service's "
            "logs with service_logs(id_or_name=...), or run `docker service logs` on a manager"
        )
    # Without a TTY the Engine multiplexes stdout and stderr into framed chunks, so the 8-byte frame
    # headers have to be stripped or they land in the returned text. `_get_result_tty` does that, but
    # only if told which mode applies, and the task's own spec is the only place that records it.
    document = api.inspect_task(task)
    is_tty = document.get("Spec", {}).get("ContainerSpec", {}).get("TTY", False)
    # The URL gets the resolved id, not what the caller passed. `inspect_task` accepts an id prefix
    # or the full `<service>.<slot>.<taskid>` name and `task` advertises both, but whether the
    # logs route resolves them too is undocumented - and there is no need to find out when the
    # canonical id is already in hand. It also removes the chance of inspecting one task and
    # reading another's output if the two endpoints ever disagreed about an ambiguous prefix.
    #
    # Not falling back to `task` when the id is absent: that is the unresolved reference this
    # exists to avoid, so the fallback would quietly reinstate the defect on the one path where the
    # daemon has already behaved unexpectedly. A bare KeyError would be no better - it is not in
    # `_LIBRARY_FAILURES`, so it reaches the client as "Error executing tool" with the text withheld.
    task_id = document.get("ID")
    if not task_id:
        raise RemoteFailureError(
            f"the daemon returned a task document for {task!r} with no 'ID' field, so its "
            'logs endpoint cannot be addressed; `service_ps(filters={"id": ...})` shows what came back'
        )
    response = get(
        build_url("/tasks/{0}/logs", task_id),
        params={**params, "follow": False},
        stream=True,
    )
    # `_get_result_tty` raises for status itself on the multiplexed path but not on the TTY one,
    # where it would otherwise stream an error body back as though it were log output.
    raise_for_status(response)
    try:
        raw = join_bounded(as_byte_chunks(result_tty(True, response, is_tty)), max_bytes, f"logs of task {task}")
    finally:
        # A fully consumed stream releases its pooled connection by itself, but `join_bounded` raises
        # on the max_bytes abort with the body part-read, which would strand that connection for
        # every oversized task. No `CancellableStream` here: there is no watchdog to interrupt a
        # blocked read from, because `follow` is never sent and the daemon closes the stream itself
        # once the tail is written.
        close_stream_quietly(response)
    return raw.decode("utf-8", errors="replace")


@tool()
def service_scale(id_or_name: str, replicas: int, host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Set the desired replica count for a Replicated-mode swarm service.

    Only applies to services in `Replicated` mode; a `Global` service runs one task per
    eligible node and has no replica count to set. The swarm scheduler places or removes
    tasks asynchronously to converge on the new count - this call returns once the update
    is accepted, not once every task is running. Check progress with `service_ps` or
    `service_inspect`. For any other spec change (image, env, resources) use
    `service_update` instead.

    Args:
        replicas: The desired number of running task replicas

    Returns:
        bool: True once the scale request is accepted
    """
    return _get_client(host).services.get(id_or_name).scale(replicas)


@tool()
def service_rollback(id_or_name: str, host: str | None = None) -> dict:  # noqa: DOC101,DOC103,DOC501,DOC503
    """
    Roll a swarm service back to its previous spec (the docker `service rollback` equivalent).

    Re-applies the service's `PreviousSpec` - the spec from before the most recent `service_update` /
    `service_scale`. Raises ToolInputError if the service has no PreviousSpec
    (it has never been updated, or was already rolled back). The high-level SDK exposes no rollback,
    so this reads the current version and previous spec via the low-level APIClient and submits them
    with the low-level `update_service` API call.

    Returns:
        dict: The daemon response (a dict with a "Warnings" key)
    """
    api = _get_client(host).api
    info = api.inspect_service(id_or_name)
    previous = info.get("PreviousSpec")
    if not previous:
        raise ToolInputError(
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
    """Build the unified service wait result snapshot - the same shape for every `until` mode.

    Args:
        id_or_name: the object waited on
        until: which wait mode was used
        met: whether the condition was satisfied
        start: when the wait began, for the elapsed time
        timed_out: whether the wait hit its deadline
        running_tasks: how many tasks are running, where known
        desired_tasks: how many the service wants, where known
        failed_tasks: the tasks that failed, where any did
        update_state: the rollout state, where known

    Returns:
        dict: the unified snapshot - the same shape for every ``until`` mode
    """
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


def _wait_service(
    id_or_name: str,
    until: str,
    timeout_seconds: float,
    poll_interval: float,
    replicas: int | None = None,
    host: str | None = None,
) -> dict:
    """Wait for a swarm service to converge; `system_wait(kind="service")` is the tool that calls this.

    Never raises on timeout. Both modes count only tasks made from the current spec (`current_tasks` in
    `service-tasks://{id_or_name}`): straight after an update is requested swarm clears `UpdateStatus`
    and only later sets it to `updating`, while the old tasks still run, so counting every running task
    would report a rollout that has not started as finished. "running" polls until current-spec tasks
    reach the desired count, or run on every node a global service wants once it has any.
    "update-converged" is met on `completed` or `rollback_completed`, or with no `UpdateStatus` at all
    once current-spec tasks reach the desired count; it polls through `updating`, and returns promptly
    with `met: false` on an update paused on a failure, which will not move again without someone acting.

    Args:
        id_or_name: the service to wait on
        until: "running" or "update-converged"
        timeout_seconds: seconds to wait before returning with timed_out=true
        poll_interval: seconds between re-checks, capped by the time left
        replicas: "running" only: the desired count to wait for, overriding the service's own
        host: the host label to target, or None for the default

    Returns:
        dict: {"service", "until", "met", "timed_out", "running_tasks", "desired_tasks", "failed_tasks",
            "update_state", "waited_seconds"}

    Raises:
        ToolInputError: a negative timeout or replica count, or a non-positive poll interval
    """
    if timeout_seconds < 0:
        raise ToolInputError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
    if poll_interval <= 0:
        raise ToolInputError(f"poll_interval must be > 0, got {poll_interval}.")
    if replicas is not None and replicas < 0:
        raise ToolInputError(f"replicas must be >= 0, got {replicas}.")
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
        rolled_out = summary["current_tasks"] >= desired and not (replicas is None and _awaiting_global_tasks(summary))
        if until == "running":
            if rolled_out:
                return _service_wait_result(id_or_name, until, met=True, start=start, **common)
        else:  # "update-converged"
            update_state = summary["update_state"]
            if update_state in _UPDATE_STUCK:
                return _service_wait_result(id_or_name, until, met=False, start=start, **common)
            if update_state in _UPDATE_DONE or (update_state is None and rolled_out):
                return _service_wait_result(id_or_name, until, met=True, start=start, **common)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _service_wait_result(id_or_name, until, met=False, start=start, timed_out=True, **common)
        # Bound the sleep by the time left so a large poll_interval can't block past the timeout.
        time.sleep(min(poll_interval, remaining))

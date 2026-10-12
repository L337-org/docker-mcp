"""Tools for Swarm stacks: a Compose file deployed as a set of services."""

# library of mcp tools for Docker stacks (Compose-on-Swarm).
#
# `docker stack` is a client-side wrapper: deploying turns a Compose file into swarm services, networks,
# secrets and configs, each stamped with the `com.docker.stack.namespace` label, and everything else the
# CLI does - list, services, ps, rm - is Engine API calls filtered on that label (docker/cli
# `cli/command/stack`). So only `stack_deploy`, which needs the Compose translation, shells out to the
# CLI; the other tools make the same Engine calls through docker-py, which needs no `docker` binary and
# reaches an ssh:// host through docker-py's own transport. Every one needs a swarm manager.
#
# Error convention for `stack_deploy` (see architecture/cli-shell-out.md): it returns the raw CliResult
# dict and never raises on a non-zero exit. With no local `docker` binary and an ssh:// target it runs
# on the far host via the remote-exec fallback in `_cli.py`, staging its working directory.

import time
from typing import Literal, get_args

import docker.errors

from docker_mcp.exceptions import ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    CliResult,
    flag_values,
    remote_stage_and_exec,
    run_docker,
    safe_positional,
    should_remote_exec,
)
from docker_mcp.tools.services import _awaiting_global_tasks, _read_service_task_summary
from docker_mcp.tools.system import _get_client

# deploy resolves image digests against the registry and submits the service specs; the rollout itself
# happens after it returns, so the ceiling only has to cover a slow registry.
_TIMEOUT_DEPLOY = 1800.0

# `docker stack deploy --resolve-image` accepts exactly these values. The alias is the single
# source: it types the parameter (so pydantic advertises the enum and rejects an out-of-set value
# before the body runs) and the frozenset below is derived from it, so the advertised set and the
# defensive check for direct callers cannot drift apart.
ResolveImage = Literal["always", "changed", "never"]
_RESOLVE_IMAGE_CHOICES = frozenset(get_args(ResolveImage))


def _run_stack(
    args: list[str],
    *,
    timeout: float,
    host: str | None = None,
    cwd: str | None = None,
) -> CliResult:
    """Run `docker stack deploy`, locally or - with no local `docker` binary - on the ssh:// host.

    Deploy reads local files, so the remote path always stages the working directory (the server's own
    when `cwd` is None).

    Args:
        args: the full docker argv, beginning with `stack`
        timeout: seconds allowed for the command
        host: configured host label, or None for the default host
        cwd: working directory for resolving relative paths, or None for the server's own

    Returns:
        CliResult: the same shape from either backend
    """
    if should_remote_exec(host, plugin=None):
        return remote_stage_and_exec(
            host,
            args,
            cwd=cwd,
            timeout=timeout,
            # Scanning the whole argv for `-c` recovers exactly `compose_files`, unlike compose's `-f`
            # (see `compose._global_file_values`, where an appended container command can contain one):
            # `stack deploy` appends no user-supplied argv, and its only other caller-controlled token
            # is the stack name, which `safe_positional` refuses if it starts with '-'.
            path_values=flag_values(args, "-c"),
        )
    return run_docker(args, cwd=cwd, timeout=timeout, host=host)


@tool()
def stack_deploy(  # noqa: DOC101,DOC103,DOC501,DOC503
    name: str,
    compose_files: list[str],
    with_registry_auth: bool = False,
    prune: bool = False,
    resolve_image: ResolveImage | None = None,
    cwd: str | None = None,
    timeout_seconds: float = _TIMEOUT_DEPLOY,
    host: str | None = None,
) -> dict:
    """
    Deploy (or update) a stack to the swarm from one or more Compose files.

    Requires the target daemon to be a swarm manager. Re-running with the same `name` updates the
    stack in place. Returns once the service specs are submitted, never waiting for the rollout: wait
    for it with `system_wait(kind="stack", target=name, until="converged")`, or see its state without
    waiting with `stack_services` / `stack_ps`. The swarm analogue of `compose_up`.
    Does not raise on a non-zero CLI exit (a missing `docker` binary or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        name: Name of the stack to create or update
        compose_files: One or more Compose file paths (repeated `-c`; later override earlier). At least one required.
        with_registry_auth: Send registry credentials to swarm agents (needed for private images)
        prune: Remove services no longer defined in the Compose file
        resolve_image: Image-digest resolution; omit for the CLI default ("always")
        cwd: Working directory for resolving relative Compose paths (defaults to the server's cwd; copied to the target
            host if no local docker CLI)
        timeout_seconds: Subprocess timeout (default 1800s)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not compose_files:
        raise ToolInputError("stack_deploy requires at least one entry in compose_files.")
    if resolve_image is not None and resolve_image not in _RESOLVE_IMAGE_CHOICES:
        raise ToolInputError(f"resolve_image must be one of {sorted(_RESOLVE_IMAGE_CHOICES)}, got {resolve_image!r}.")
    args = ["stack", "deploy"]
    for f in compose_files:
        args.extend(["-c", f])
    if with_registry_auth:
        args.append("--with-registry-auth")
    if prune:
        args.append("--prune")
    if resolve_image is not None:
        args.append(f"--resolve-image={resolve_image}")
    # Always detached: a foreground deploy blocks until the rollout converges, which `system_wait` does
    # with a timeout and a result.
    args.append("--detach=true")
    args.append(safe_positional(name, "stack name"))
    return _run_stack(args, cwd=cwd, timeout=timeout_seconds, host=host).to_dict()


def _label_filters(name: str | None, filters: dict | None = None) -> dict:
    """Engine list filters selecting one stack's objects (or every stack's), plus the caller's own.

    Args:
        name: the stack, or None for anything belonging to any stack
        filters: the caller's filters; a `label` there must also match, alongside the stack's

    Returns:
        dict: the filters to pass to the Engine

    Raises:
        ToolInputError: a caller `label` filter on the stack label's own key
    """
    merged = dict(filters or {})
    own = _STACK_LABEL if name is None else f"{_STACK_LABEL}={name}"
    theirs = merged.get("label")
    theirs = [theirs] if isinstance(theirs, str) else list(theirs or [])
    # The Engine turns `label` values into a map keyed by label name (moby `daemon/cluster/utils.go`
    # `convertKVStringsToMap`), so a caller value on the same key would replace the stack's, not narrow it.
    if any(value.partition("=")[0] == _STACK_LABEL for value in theirs):
        raise ToolInputError(f"filters may not set a {_STACK_LABEL!r} label; name the stack instead.")
    merged["label"] = [own, *theirs]
    return merged


@tool()
def stack_list(host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List the stacks deployed to the swarm, with how many services each has.

    Requires a swarm manager. `compose_list` is the non-swarm equivalent; drill into one stack with
    `stack_services`, or wait for one with `system_wait(kind="stack")`.

    Returns:
        list: One {"name", "services"} per stack, sorted by name
    """
    counts: dict[str, int] = {}
    for service in _get_client(host).services.list(filters=_label_filters(None)):
        stack = (service.attrs.get("Spec", {}).get("Labels") or {}).get(_STACK_LABEL)
        if stack is not None:
            counts[stack] = counts.get(stack, 0) + 1
    return [{"name": stack, "services": counts[stack]} for stack in sorted(counts)]


def _service_mode(spec: dict) -> str | None:
    """A service's scheduling mode as the CLI prints it, e.g. "replicated" or "global job".

    Args:
        spec: the service's `Spec`

    Returns:
        str or None: the mode, or None when the spec names none
    """
    mode = next(iter(spec.get("Mode") or {}), None)
    return {"ReplicatedJob": "replicated job", "GlobalJob": "global job"}.get(mode, mode.lower()) if mode else None


@tool()
def stack_services(name: str, filters: dict | None = None, host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List the services of a stack, with how many of their tasks are running against how many are wanted.

    Service-level rollup; use `stack_ps` for the individual tasks, `service_inspect` for one service's
    full spec, and `system_wait(kind="stack", until="converged")` to wait for the stack to settle.
    Requires a swarm manager.

    Args:
        name: The stack to list services for
        filters: Engine service filters, e.g. {"name": "web"}; a list value matches any of them, except
            `label`, where every one must match and the stack's own label key is refused

    Returns:
        list: One {"id", "name", "mode", "running_tasks", "desired_tasks", "image", "ports", "update_state"} per
            service, sorted by name; `ports` lists published ports as "published:target/protocol", and
            `update_state` is a rolling update's state, or null when there has been none
    """
    rows = []
    for model in _get_client(host).services.list(filters=_label_filters(name, filters), status=True):
        service = model.attrs
        spec = service.get("Spec") or {}
        status = service.get("ServiceStatus") or {}
        rows.append(
            {
                "id": service.get("ID"),
                "name": spec.get("Name"),
                "mode": _service_mode(spec),
                "running_tasks": status.get("RunningTasks"),
                "desired_tasks": status.get("DesiredTasks"),
                "image": ((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}).get("Image"),
                "ports": [
                    f"{port.get('PublishedPort')}:{port.get('TargetPort')}/{port.get('Protocol')}"
                    for port in (service.get("Endpoint") or {}).get("Ports") or []
                ],
                "update_state": (service.get("UpdateStatus") or {}).get("State"),
            }
        )
    return sorted(rows, key=lambda row: row["name"] or "")


@tool()
def stack_ps(name: str, filters: dict | None = None, host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List the tasks of a stack: where each runs, its state, and why it failed.

    Task-level view across every service in the stack (`service_ps` covers one service). Includes the
    finished and failed tasks the swarm still keeps, so pass {"desired-state": "running"} for only the
    current ones. Requires a swarm manager.

    Args:
        name: The stack to list tasks for
        filters: Engine task filters, e.g. {"desired-state": "running"}; a list value matches any of them,
            except `label`, where every one must match and the stack's own label key is refused

    Returns:
        list: One {"id", "name", "service", "node", "image", "desired_state", "state", "error", "since"} per task,
            sorted by name; `node` is the hostname, `error` is null unless the task failed, and `since` is
            when it entered its state
    """
    client = _get_client(host)
    services = {s.id: s.name for s in client.services.list(filters=_label_filters(name))}
    nodes = {n.id: (n.attrs.get("Description") or {}).get("Hostname") for n in client.nodes.list()}
    rows = []
    for task in client.api.tasks(filters=_label_filters(name, filters)):
        service = services.get(task.get("ServiceID"), task.get("ServiceID"))
        status = task.get("Status") or {}
        # How the CLI names a task: its slot for a replicated service, its node for a global one.
        suffix = task.get("Slot") or task.get("NodeID")
        rows.append(
            {
                "id": task.get("ID"),
                "name": f"{service}.{suffix}" if suffix else service,
                "service": service,
                "node": nodes.get(task.get("NodeID"), task.get("NodeID")),
                "image": (((task.get("Spec") or {}).get("ContainerSpec") or {}).get("Image")),
                "desired_state": task.get("DesiredState"),
                "state": status.get("State"),
                "error": status.get("Err"),
                "since": status.get("Timestamp"),
            }
        )
    return sorted(rows, key=lambda row: (row["name"] or "", row["since"] or ""))


@tool()
def stack_remove(names: list[str], host: str | None = None) -> list:  # noqa: DOC101,DOC103,DOC501,DOC503
    """
    Remove one or more stacks from the swarm: their services, secrets, configs and networks.

    Destructive: this deletes every service in the named stack(s) - the reverse of `stack_deploy` and the
    swarm analogue of `compose_down`. Returns once removal is requested, never waiting for teardown:
    `system_wait(kind="stack", target=<name>, until="removed")` waits for it. A part that cannot be
    removed is reported under `failed` and the rest still go, as `docker stack rm` does.

    Args:
        names: One or more stack names to remove. At least one is required.

    Returns:
        list: One {"name", "found", "removed", "failed"} per stack; `found` is false when nothing carried the
            stack's label, `removed` maps "services"/"secrets"/"configs"/"networks" to the names removed, and
            `failed` lists {"kind", "name", "error"}
    """
    if not names:
        raise ToolInputError("stack_remove requires at least one entry in names.")
    client = _get_client(host)
    results = []
    for name in names:
        label = _label_filters(name)
        # The CLI's order: services first, then what they used.
        parts = {
            "services": sorted(client.services.list(filters=label), key=lambda o: o.name),
            "secrets": client.secrets.list(filters=label),
            "configs": client.configs.list(filters=label),
            "networks": client.networks.list(filters=label),
        }
        removed: dict[str, list[str]] = {kind: [] for kind in parts}
        failed = []
        for kind, objects in parts.items():
            for obj in objects:
                try:
                    obj.remove()
                except docker.errors.NotFound:
                    pass  # gone already, which is what removing it was for
                except docker.errors.APIError as exc:
                    failed.append({"kind": kind[:-1], "name": obj.name, "error": str(exc)})
                    continue
                removed[kind].append(obj.name)
        results.append({"name": name, "found": any(parts.values()), "removed": removed, "failed": failed})
    return results


# The label `docker stack deploy` puts on every service, network, secret, config and task it creates
# (docker/cli `cli/compose/convert`, LabelNamespace). Filtering on it is how the CLI itself finds a
# stack's parts, so the Engine API alone answers "what belongs to this stack".
_STACK_LABEL = "com.docker.stack.namespace"
# Swarm task states before a task stops for good (moby `api/types/swarm` TaskState). A stack is gone
# only once none of its tasks is in one of these.
_ACTIVE_TASK_STATES = frozenset(
    {"new", "allocated", "pending", "assigned", "accepted", "preparing", "ready", "starting", "running"}
)
# `UpdateStatus.State` values after which a rolling update is over, and those where it stopped on a failure
# and will not move again without someone acting (moby `api/types/swarm` UpdateState).
_UPDATE_DONE = frozenset({"completed", "rollback_completed"})
_UPDATE_STUCK = frozenset({"paused", "rollback_paused"})


def _runs_current_spec(task: dict, template: dict) -> bool:
    """Whether a task was created from its service's current task template, i.e. an update left it alone.

    Compares the keys the task's own spec carries: the template also holds `Runtime`, which tasks omit.
    Swarm decides whether a task needs replacing the same way, from its spec against the template.

    Args:
        task: a task document, as `client.api.tasks()` returns it
        template: the service's `Spec.TaskTemplate`

    Returns:
        bool: True when every key of the task's spec equals the template's
    """
    spec = task.get("Spec") or {}
    return all(template.get(key) == value for key, value in spec.items())


def _wait_stack(
    name: str,
    until: str,
    timeout_seconds: float,
    poll_interval: float,
    host: str | None = None,
) -> dict:
    """Wait for a swarm stack to converge or be removed; `system_wait(kind="stack")` calls this.

    Never raises on timeout. Finds the stack's services and tasks by the label `docker stack deploy`
    stamps, through the Engine API. "converged" is met when every service in the stack has its
    desired number of running tasks on its current spec (a global service, at least one) and no rolling
    update still in progress; it
    returns promptly with `met: false` when no service carries the label, or when an update has paused
    on a failure. "removed" is met when no service carries the label and none of its tasks is still
    starting or running. A service removed while it is being read is treated as gone.

    Args:
        name: the stack name
        until: "converged" or "removed"
        timeout_seconds: seconds to wait before returning with timed_out=true
        poll_interval: seconds between re-checks, capped by the time left
        host: the host label to target, or None for the default

    Returns:
        dict: {"stack", "until", "met", "timed_out", "services", "active_tasks", "waited_seconds"}, where
            `services` has one row per service still present: {"service", "running_tasks", "current_tasks",
            "desired_tasks", "failed_tasks", "update_state"} for "converged", and only {"service"} for "removed"

    Raises:
        ToolInputError: a negative timeout or a non-positive poll interval
    """
    if timeout_seconds < 0:
        raise ToolInputError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
    if poll_interval <= 0:
        raise ToolInputError(f"poll_interval must be > 0, got {poll_interval}.")
    client = _get_client(host)
    label_filter = {"label": f"{_STACK_LABEL}={name}"}
    start = time.monotonic()
    deadline = start + timeout_seconds
    while True:
        services = client.services.list(filters=label_filter)
        rows = []
        awaiting: set[str] = set()
        for service in services:
            if until == "removed":
                # Only whether anything remains matters here, so skip the per-service reads.
                rows.append({"service": service.name})
                continue
            try:
                summary = _read_service_task_summary(service.id, host=host)
                running = client.api.tasks(filters={"service": service.id, "desired-state": "running"})
            except docker.errors.NotFound:
                # Removed between the list and this read (a `stack rm` or a pruning deploy elsewhere).
                continue
            template = (service.attrs.get("Spec") or {}).get("TaskTemplate") or {}
            # Both counts come from this one read rather than the summary's own: a task that starts between
            # two reads would otherwise be met on as current while the row still reports nothing running.
            running = [task for task in running if (task.get("Status") or {}).get("State") == "running"]
            if _awaiting_global_tasks(summary):
                awaiting.add(service.name)
            rows.append(
                {
                    "service": service.name,
                    "running_tasks": len(running),
                    # Running tasks already on the current spec: right after a redeploy the old ones still
                    # run, and swarm may not have recorded the update as started yet.
                    "current_tasks": sum(1 for task in running if _runs_current_spec(task, template)),
                    "desired_tasks": summary["desired_tasks"],
                    "failed_tasks": summary["failed_tasks"],
                    "update_state": summary["update_state"],
                }
            )
        rows.sort(key=lambda row: row["service"])
        active_tasks = sum(
            1
            for task in client.api.tasks(filters=label_filter)
            if (task.get("Status") or {}).get("State") in _ACTIVE_TASK_STATES
        )
        verdict: bool | None
        if until == "converged":
            # No service at all is not "converging": nothing was deployed under that name. A rolling update
            # keeps the old tasks running until each is replaced, so a count of running tasks is met partway
            # through a redeploy, and `UpdateStatus` can still be unset just after the deploy returns. So
            # count only tasks on the current spec, and also wait out an update swarm has recorded.
            if not rows or any(r["update_state"] in _UPDATE_STUCK for r in rows):
                verdict = False
            elif not awaiting and all(
                r["current_tasks"] >= r["desired_tasks"] and r["update_state"] in (None, *_UPDATE_DONE) for r in rows
            ):
                verdict = True
            else:
                verdict = None
        else:  # "removed"
            verdict = True if not rows and active_tasks == 0 else None
        remaining = deadline - time.monotonic()
        if verdict is not None or remaining <= 0:
            return {
                "stack": name,
                "until": until,
                "met": verdict is True,
                "timed_out": verdict is None,
                "services": rows,
                "active_tasks": active_tasks,
                "waited_seconds": round(time.monotonic() - start, 2),
            }
        # Bound the sleep by the time left so a large poll_interval can't push past the timeout.
        time.sleep(min(poll_interval, remaining))

"""Tools for Swarm stacks: a Compose file deployed as a set of services."""

# library of mcp tools for Docker stacks (Compose-on-Swarm).
#
# `docker stack` deploys a Compose file to a swarm as a set of services. It is part of the core
# docker CLI (not a plugin like Compose v2), so unlike compose.py there is no `require_plugin`
# probe - but every subcommand requires the target daemon to be a swarm manager and will fail
# otherwise. These tools shell out via the cross-platform helper in `tools/_cli.py`.
#
# Error convention (see architecture/cli-shell-out.md): action tools (`stack_deploy`, `stack_remove`) return the raw
# CliResult dict and never raise; parsed-query tools (`stack_list`, `stack_ps`, `stack_services`)
# return a parsed list and raise RuntimeError via `raise_on_cli_failure` on a non-zero exit.

# With no local `docker` binary and an ssh:// target, these run on the far host via the remote-exec
# fallback in `_cli.py`. `stack_deploy` is the only one that reads local files, so it is the only one
# that stages its working directory; the queries and `stack rm` name nothing local.

import time
from typing import Literal, get_args

import docker.errors

from docker_mcp.exceptions import ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    CliResult,
    filter_args,
    flag_values,
    parse_json_or_ndjson,
    raise_on_cli_failure,
    remote_exec_cli,
    remote_stage_and_exec,
    run_docker,
    safe_positional,
    should_remote_exec,
)
from docker_mcp.tools.services import _read_service_task_summary
from docker_mcp.tools.system import _get_client

_TIMEOUT_QUERY = 60.0
# deploy pulls images and submits service specs; give it a generous ceiling (it converges
# asynchronously when detached, but a non-detached deploy waits for the rollout).
_TIMEOUT_DEPLOY = 1800.0
_TIMEOUT_RM = 300.0

# `docker stack deploy --resolve-image` accepts exactly these values. The alias is the single
# source: it types the parameter (so pydantic advertises the enum and rejects an out-of-set value
# before the body runs) and the frozenset below is derived from it, so the advertised set and the
# defensive check for direct callers cannot drift apart.
ResolveImage = Literal["always", "changed", "never"]
_RESOLVE_IMAGE_CHOICES = frozenset(get_args(ResolveImage))


# JSON output is requested with the `{{json .}}` Go template rather than the `--format json`
# shorthand: the `json` keyword was only added to the docker CLI formatter in ~v23.0, whereas the
# template renders one JSON object per line (NDJSON) on every version we might run against.
_JSON_FORMAT = "{{json .}}"


def _run_stack(
    args: list[str],
    *,
    timeout: float,
    host: str | None = None,
    cwd: str | None = None,
    stage_cwd: bool = False,
) -> CliResult:
    """Run `docker stack <args...>`, locally or - with no local `docker` binary - on the ssh:// host.

    `stage_cwd` is explicit rather than inferred from `cwd`, because the two questions differ: only
    `stack_deploy` reads local files, and it needs the *server's* working directory staged when `cwd`
    is None, whereas a query with no `cwd` needs no staging at all.

    Args:
        args: the full docker argv, beginning with `stack`
        timeout: seconds allowed for the command
        host: configured host label, or None for the default host
        cwd: working directory for resolving relative paths, or None for the server's own
        stage_cwd: True for a subcommand that reads local files, so that directory is copied over

    Returns:
        CliResult: the same shape from either backend
    """
    if should_remote_exec(host, plugin=None):
        if stage_cwd:
            return remote_stage_and_exec(
                # `stack_deploy` is the only producer of `-c` here, so scanning the argv recovers
                # exactly its `compose_files` list.
                host,
                args,
                cwd=cwd,
                timeout=timeout,
                # Scanning the whole argv is safe *here*, unlike compose's `-f` (see
                # `compose._global_file_values`, where an appended container command can contain one):
                # `stack deploy` appends no user-supplied argv, and its only caller-controlled tokens
                # are the stack name - which `safe_positional` refuses if it starts with '-' - and
                # `--filter key=value` pairs, which always contain '='. Nothing but a real flag can
                # equal "-c".
                path_values=flag_values(args, "-c"),
            )
        return remote_exec_cli(host, args, timeout=timeout)
    return run_docker(args, cwd=cwd, timeout=timeout, host=host)


def _parse_stack_list(stdout: str, *, truncated: bool, what: str) -> list[dict]:
    """Normalise `docker stack <ls|ps|services> --format '{{json .}}'` output to a list of dicts.

    Args:
        stdout: the command's raw output
        truncated: whether the output hit the byte cap
        what: what was listed, for the error message

    Returns:
        list: one dict per row
    """
    parsed = parse_json_or_ndjson(stdout, truncated=truncated, what=what)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []


@tool()
def stack_deploy(  # noqa: DOC101,DOC103,DOC501,DOC503
    name: str,
    compose_files: list[str],
    with_registry_auth: bool = False,
    prune: bool = False,
    resolve_image: ResolveImage | None = None,
    detach: bool = True,
    cwd: str | None = None,
    timeout_seconds: float = _TIMEOUT_DEPLOY,
    host: str | None = None,
) -> dict:
    """
    Deploy (or update) a stack to the swarm from one or more Compose files.

    Requires the target daemon to be a swarm manager. Re-running with the same `name` updates the
    stack in place. Defaults to `detach=True` (returns once specs are submitted, not on
    convergence); set `detach=False` to wait for the rollout (give it a generous
    `timeout_seconds`). The swarm analogue of `compose_up`; watch the rollout with
    `stack_services` / `stack_ps`.
    Does not raise on a non-zero CLI exit (a missing `docker` binary or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        name: Name of the stack to create or update
        compose_files: One or more Compose file paths (repeated `-c`; later override earlier). At least one required.
        with_registry_auth: Send registry credentials to swarm agents (needed for private images)
        prune: Remove services no longer defined in the Compose file
        resolve_image: Image-digest resolution; omit for the CLI default ("always")
        detach: Return immediately after submitting specs (True) vs wait for convergence (False)
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
    args.append(f"--detach={'true' if detach else 'false'}")
    args.append(safe_positional(name, "stack name"))
    return _run_stack(args, cwd=cwd, timeout=timeout_seconds, host=host, stage_cwd=True).to_dict()


@tool()
def stack_list(host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List the stacks deployed to the swarm, parsed from `--format '{{json .}}'`.

    Requires the target daemon to be a swarm manager. `compose_list` is the non-swarm equivalent;
    drill into one stack with `stack_services`.
    Raises RemoteFailureError if the CLI call fails.

    Returns:
        list: One dict per stack (name, services count, orchestrator)
    """
    result = _run_stack(["stack", "ls", "--format", _JSON_FORMAT], timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "stack ls")
    return _parse_stack_list(result.stdout, truncated=result.truncated, what="stack ls output")


@tool()
def stack_ps(  # noqa: DOC101,DOC103
    name: str,
    no_trunc: bool = False,
    filters: dict | None = None,
    host: str | None = None,
) -> list:
    """
    List the tasks of a stack, parsed from `--format '{{json .}}'`.

    Task-level view across every service in the stack (`service_ps` covers one service): where
    each task runs and why it failed. Requires a swarm manager.
    Raises RemoteFailureError if the CLI call fails.

    Args:
        name: The stack to list tasks for
        no_trunc: Do not truncate task IDs / errors in the output
        filters: Filter by attributes, e.g. {"desired-state": "running"}; a list value repeats the filter

    Returns:
        list: One dict per task (id, name, node, image, desired/current state, error)
    """
    args = ["stack", "ps", "--format", _JSON_FORMAT]
    if no_trunc:
        args.append("--no-trunc")
    args.extend(filter_args(filters))
    args.append(safe_positional(name, "stack name"))
    result = _run_stack(args, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "stack ps")
    return _parse_stack_list(result.stdout, truncated=result.truncated, what="stack ps output")


@tool()
def stack_services(name: str, filters: dict | None = None, host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List the services of a stack, parsed from `--format '{{json .}}'`.

    Service-level rollup (replicas ready per service); use `stack_ps` for individual tasks and
    `service_inspect` for one service's full spec. Requires a swarm manager.
    Raises RemoteFailureError if the CLI call fails.

    Args:
        name: The stack to list services for
        filters: Filter by attributes, e.g. {"name": "web"}; a list value repeats the filter

    Returns:
        list: One dict per service (id, name, mode, replicas, image, ports)
    """
    args = ["stack", "services", "--format", _JSON_FORMAT]
    args.extend(filter_args(filters))
    args.append(safe_positional(name, "stack name"))
    result = _run_stack(args, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "stack services")
    return _parse_stack_list(result.stdout, truncated=result.truncated, what="stack services output")


@tool()
def stack_remove(  # noqa: DOC101,DOC103,DOC501,DOC503
    names: list[str], detach: bool = True, timeout_seconds: float = _TIMEOUT_RM, host: str | None = None
) -> dict:
    """
    Remove one or more stacks from the swarm (tears down their services, networks, and secrets).

    Destructive: this stops and deletes every service in the named stack(s) - the reverse of
    `stack_deploy` and the swarm analogue of `compose_down`. Defaults to `detach=True` so the call
    returns once removal is requested rather than waiting for teardown.
    Does not raise on a non-zero CLI exit (a missing `docker` binary or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        names: One or more stack names to remove. At least one is required.
        detach: Return immediately (True) vs wait for the stack(s) to be fully removed (False)
        timeout_seconds: Subprocess timeout (default 300s)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not names:
        raise ToolInputError("stack_remove requires at least one entry in names.")
    args = ["stack", "rm", f"--detach={'true' if detach else 'false'}"]
    args.extend(safe_positional(name, "stack name") for name in names)
    return _run_stack(args, timeout=timeout_seconds, host=host).to_dict()


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
    desired number of running tasks on its current spec and no rolling update still in progress; it
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
            rows.append(
                {
                    "service": service.name,
                    "running_tasks": summary["running_tasks"],
                    # Running tasks already on the current spec: right after a redeploy the old ones still
                    # run, and swarm may not have recorded the update as started yet.
                    "current_tasks": sum(
                        1
                        for task in running
                        if (task.get("Status") or {}).get("State") == "running" and _runs_current_spec(task, template)
                    ),
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
            elif all(
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

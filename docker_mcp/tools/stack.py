# library of mcp tools for Docker stacks (Compose-on-Swarm).
#
# `docker stack` deploys a Compose file to a swarm as a set of services. It is part of the core
# docker CLI (not a plugin like Compose v2), so unlike compose.py there is no `require_plugin`
# probe — but every subcommand requires the target daemon to be a swarm manager and will fail
# otherwise. These tools shell out via the cross-platform helper in `tools/_cli.py`.
#
# Error convention (see CLAUDE.md): action tools (`stack_deploy`, `stack_remove`) return the raw
# CliResult dict and never raise; parsed-query tools (`stack_list`, `stack_ps`, `stack_services`)
# return a parsed list and raise RuntimeError via `raise_on_cli_failure` on a non-zero exit.

# With no local `docker` binary and an ssh:// target, these run on the far host via the remote-exec
# fallback in `_cli.py`. `stack_deploy` is the only one that reads local files, so it is the only one
# that stages its working directory; the queries and `stack rm` name nothing local.

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

_TIMEOUT_QUERY = 60.0
# deploy pulls images and submits service specs; give it a generous ceiling (it converges
# asynchronously when detached, but a non-detached deploy waits for the rollout).
_TIMEOUT_DEPLOY = 1800.0
_TIMEOUT_RM = 300.0

# `docker stack deploy --resolve-image` accepts exactly these values.
_RESOLVE_IMAGE_CHOICES = frozenset({"always", "changed", "never"})


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
    """
    Run `docker stack <args...>`, locally or — with no local `docker` binary — on the ssh:// host.

    `stage_cwd` is explicit rather than inferred from `cwd`, because the two questions differ: only
    `stack_deploy` reads local files, and it needs the *server's* working directory staged when `cwd`
    is None, whereas a query with no `cwd` needs no staging at all.

    args:
        args - the full docker argv, beginning with `stack`
        timeout - seconds allowed for the command
        host - configured host label, or None for the default host
        cwd - working directory for resolving relative paths, or None for the server's own
        stage_cwd - True for a subcommand that reads local files, so that directory is copied over
    returns: CliResult - the same shape from either backend
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
                path_values=flag_values(args, "-c"),
            )
        return remote_exec_cli(host, args, timeout=timeout)
    return run_docker(args, cwd=cwd, timeout=timeout, host=host)


def _parse_stack_list(stdout: str, *, truncated: bool, what: str) -> list[dict]:
    """Normalize `docker stack <ls|ps|services> --format '{{json .}}'` output to a list of dicts."""
    parsed = parse_json_or_ndjson(stdout, truncated=truncated, what=what)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []


@tool()
def stack_deploy(
    name: str,
    compose_files: list[str],
    with_registry_auth: bool = False,
    prune: bool = False,
    resolve_image: str | None = None,
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
    Does not raise on a non-zero CLI exit — inspect `returncode`/`stderr` in the result.

    args:
        name - Name of the stack to create or update
        compose_files - One or more Compose file paths (repeated `-c`; later override earlier). At least one required.
        with_registry_auth - Send registry credentials to swarm agents (needed for private images)
        prune - Remove services no longer defined in the Compose file
        resolve_image - Image-digest resolution: "always" (default), "changed", or "never"
        detach - Return immediately after submitting specs (True) vs wait for convergence (False)
        cwd - Working directory for resolving relative Compose paths (defaults to the server's cwd;
                      copied to the target host if no local docker CLI)
        timeout_seconds - Subprocess timeout (default 1800s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not compose_files:
        raise ValueError("stack_deploy requires at least one entry in compose_files.")
    if resolve_image is not None and resolve_image not in _RESOLVE_IMAGE_CHOICES:
        raise ValueError(f"resolve_image must be one of {sorted(_RESOLVE_IMAGE_CHOICES)}, got {resolve_image!r}.")
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
def stack_list(host: str | None = None) -> list:
    """
    List the stacks deployed to the swarm, parsed from `--format '{{json .}}'`.

    Requires the target daemon to be a swarm manager. `compose_list` is the non-swarm equivalent;
    drill into one stack with `stack_services`.
    Raises RuntimeError if the CLI call fails.

    returns: list - One dict per stack (name, services count, orchestrator)
    """
    result = _run_stack(["stack", "ls", "--format", _JSON_FORMAT], timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "stack ls")
    return _parse_stack_list(result.stdout, truncated=result.truncated, what="stack ls output")


@tool()
def stack_ps(name: str, no_trunc: bool = False, filters: dict | None = None, host: str | None = None) -> list:
    """
    List the tasks of a stack, parsed from `--format '{{json .}}'`.

    Task-level view across every service in the stack (`service_ps` covers one service): where
    each task runs and why it failed. Requires a swarm manager.
    Raises RuntimeError if the CLI call fails.

    args:
        name - The stack to list tasks for
        no_trunc - Do not truncate task IDs / errors in the output
        filters - Filter by attributes, e.g. {"desired-state": "running"}; a list value repeats the filter
    returns: list - One dict per task (id, name, node, image, desired/current state, error)
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
def stack_services(name: str, filters: dict | None = None, host: str | None = None) -> list:
    """
    List the services of a stack, parsed from `--format '{{json .}}'`.

    Service-level rollup (replicas ready per service); use `stack_ps` for individual tasks and
    `service_inspect` for one service's full spec. Requires a swarm manager.
    Raises RuntimeError if the CLI call fails.

    args:
        name - The stack to list services for
        filters - Filter by attributes, e.g. {"name": "web"}; a list value repeats the filter
    returns: list - One dict per service (id, name, mode, replicas, image, ports)
    """
    args = ["stack", "services", "--format", _JSON_FORMAT]
    args.extend(filter_args(filters))
    args.append(safe_positional(name, "stack name"))
    result = _run_stack(args, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "stack services")
    return _parse_stack_list(result.stdout, truncated=result.truncated, what="stack services output")


@tool()
def stack_remove(
    names: list[str], detach: bool = True, timeout_seconds: float = _TIMEOUT_RM, host: str | None = None
) -> dict:
    """
    Remove one or more stacks from the swarm (tears down their services, networks, and secrets).

    Destructive: this stops and deletes every service in the named stack(s) — the reverse of
    `stack_deploy` and the swarm analogue of `compose_down`. Defaults to `detach=True` so the call
    returns once removal is requested rather than waiting for teardown.
    Does not raise on a non-zero CLI exit — inspect `returncode`/`stderr` in the result.

    args:
        names - One or more stack names to remove. At least one is required.
        detach - Return immediately (True) vs wait for the stack(s) to be fully removed (False)
        timeout_seconds - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not names:
        raise ValueError("stack_remove requires at least one entry in names.")
    args = ["stack", "rm", f"--detach={'true' if detach else 'false'}"]
    args.extend(safe_positional(name, "stack name") for name in names)
    return _run_stack(args, timeout=timeout_seconds, host=host).to_dict()

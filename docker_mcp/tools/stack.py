# library of mcp tools for Docker stacks (Compose-on-Swarm).
#
# `docker stack` deploys a Compose file to a swarm as a set of services. It is part of the core
# docker CLI (not a plugin like Compose v2), so unlike compose.py there is no `require_plugin`
# probe — but every subcommand requires the target daemon to be a swarm manager and will fail
# otherwise. These tools shell out via the cross-platform helper in `tools/_cli.py`.
#
# Error convention (see CLAUDE.md): action tools (`stack_deploy`, `stack_rm`) return the raw
# CliResult dict and never raise; parsed-query tools (`stack_ls`, `stack_ps`, `stack_services`)
# return a parsed list and raise RuntimeError via `raise_on_cli_failure` on a non-zero exit.

from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    parse_json_or_ndjson,
    raise_on_cli_failure,
    run_docker,
    safe_positional,
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
    stack_name: str,
    compose_files: list[str],
    with_registry_auth: bool = False,
    prune: bool = False,
    resolve_image: str | None = None,
    detach: bool = True,
    cwd: str | None = None,
    timeout_seconds: float = _TIMEOUT_DEPLOY,
) -> dict:
    """
    Deploy (or update) a stack to the swarm from one or more Compose files.

    Requires the target daemon to be a swarm manager. Re-running with the same `stack_name` updates
    the stack in place. Defaults to `detach=True` so the call returns once the specs are submitted
    rather than blocking on convergence; set `detach=False` to wait for the rollout (give it a
    generous `timeout_seconds`).

    args:
        stack_name: str - Name of the stack to create or update
        compose_files: list[str] - One or more Compose file paths (later files override earlier ones;
                                   passed as repeated `-c`). At least one is required.
        with_registry_auth: bool - Send registry credentials to swarm agents (needed for private images)
        prune: bool - Remove services no longer defined in the Compose file
        resolve_image: str - Image-digest resolution: "always" (default), "changed", or "never"
        detach: bool - Return immediately after submitting specs (True) vs wait for convergence (False)
        cwd: str - Working directory for resolving relative Compose paths (defaults to the server's cwd)
        timeout_seconds: float - Subprocess timeout (default 1800s)
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
    args.append(safe_positional(stack_name, "stack name"))
    return run_docker(args, cwd=cwd, timeout=timeout_seconds).to_dict()


@tool()
def stack_ls() -> list:
    """
    List the stacks deployed to the swarm, parsed from `--format '{{json .}}'`.

    Requires the target daemon to be a swarm manager (raises otherwise).

    returns: list - One dict per stack (name, services count, orchestrator)
    """
    result = run_docker(["stack", "ls", "--format", _JSON_FORMAT], timeout=_TIMEOUT_QUERY)
    raise_on_cli_failure(result, "stack ls")
    return _parse_stack_list(result.stdout, truncated=result.truncated, what="stack ls output")


@tool()
def stack_ps(stack_name: str, no_trunc: bool = False, filters: list[str] | None = None) -> list:
    """
    List the tasks of a stack, parsed from `--format '{{json .}}'`.

    args:
        stack_name: str - The stack to list tasks for
        no_trunc: bool - Do not truncate task IDs / errors in the output
        filters: list[str] - Repeatable `--filter` expressions, e.g. ["desired-state=running"]
    returns: list - One dict per task (id, name, node, image, desired/current state, error)
    """
    args = ["stack", "ps", "--format", _JSON_FORMAT]
    if no_trunc:
        args.append("--no-trunc")
    for f in filters or []:
        args.extend(["--filter", f])
    args.append(safe_positional(stack_name, "stack name"))
    result = run_docker(args, timeout=_TIMEOUT_QUERY)
    raise_on_cli_failure(result, "stack ps")
    return _parse_stack_list(result.stdout, truncated=result.truncated, what="stack ps output")


@tool()
def stack_services(stack_name: str, filters: list[str] | None = None) -> list:
    """
    List the services of a stack, parsed from `--format '{{json .}}'`.

    args:
        stack_name: str - The stack to list services for
        filters: list[str] - Repeatable `--filter` expressions, e.g. ["name=web"]
    returns: list - One dict per service (id, name, mode, replicas, image, ports)
    """
    args = ["stack", "services", "--format", _JSON_FORMAT]
    for f in filters or []:
        args.extend(["--filter", f])
    args.append(safe_positional(stack_name, "stack name"))
    result = run_docker(args, timeout=_TIMEOUT_QUERY)
    raise_on_cli_failure(result, "stack services")
    return _parse_stack_list(result.stdout, truncated=result.truncated, what="stack services output")


@tool()
def stack_rm(stack_names: list[str], detach: bool = True, timeout_seconds: float = _TIMEOUT_RM) -> dict:
    """
    Remove one or more stacks from the swarm (tears down their services, networks, and secrets).

    Destructive: this stops and deletes every service in the named stack(s). Defaults to
    `detach=True` so the call returns once removal is requested rather than waiting for teardown.

    args:
        stack_names: list[str] - One or more stack names to remove. At least one is required.
        detach: bool - Return immediately (True) vs wait for the stack(s) to be fully removed (False)
        timeout_seconds: float - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not stack_names:
        raise ValueError("stack_rm requires at least one entry in stack_names.")
    args = ["stack", "rm", f"--detach={'true' if detach else 'false'}"]
    args.extend(safe_positional(name, "stack name") for name in stack_names)
    return run_docker(args, timeout=timeout_seconds).to_dict()

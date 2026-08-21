# library of mcp tools for managing docker CLI contexts.
#
# Docker contexts are a CLI-only feature - the Docker Engine HTTP API (and
# therefore docker-py) is agnostic to which daemon a CLI invocation targets.
# These tools wrap `docker context ...` via the cross-platform CLI helper.

import json

from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    parse_ndjson,
    raise_on_cli_failure,
    run_docker,
    safe_positional,
    safe_spec_value,
)


@tool()
def context_list() -> list:
    """
    List Docker CLI contexts known to the host running this MCP server.

    Contexts are a CLI concept (stored in the docker config dir) letting one CLI target multiple
    daemons. This server uses whatever DOCKER_HOST / current-context resolved to at startup, so
    changing contexts only affects future subprocess-based tools, not the docker-py SDK client.
    Use `context_inspect` for one context's full config and `context_use` to switch.
    Raises RuntimeError if the CLI call fails.

    returns: list - One dict per context with at least name, description, dockerEndpoint, and current
    """
    result = run_docker(["context", "ls", "--format", "{{json .}}"])
    raise_on_cli_failure(result, "context ls")
    return parse_ndjson(result.stdout, truncated=result.truncated, what="context ls output")


@tool()
def context_inspect(name: str) -> dict:
    """
    Return the full configuration for a single Docker context.

    Full endpoint/TLS detail for one context; `context_list` gives the one-line summary of all.
    Raises RuntimeError if the CLI call fails.

    args: name - Context name (use the `Name` field from `context_list`)
    returns: dict - The parsed `docker context inspect` entry (keys include "Name" and
        "Endpoints" with the daemon URL)
    """
    result = run_docker(["context", "inspect", safe_positional(name, "context name")])
    raise_on_cli_failure(result, "context inspect")
    parsed = json.loads(result.stdout)
    # `docker context inspect` always returns a JSON array, even for a single name.
    if isinstance(parsed, list):
        if not parsed:
            raise RuntimeError(f"`docker context inspect {name}` returned no entries.")
        return parsed[0]
    return parsed


@tool()
def context_create(
    name: str,
    docker_host: str,
    description: str | None = None,
    tls_ca: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    skip_tls_verify: bool = False,
) -> dict:
    """
    Create a new Docker CLI context pointing at a daemon endpoint.

    Registers a named endpoint for the CLI; switch with `context_use`, enumerate with
    `context_list`. It does not retarget this server's docker-py client (pinned at startup).
    Does not raise on a non-zero CLI exit - inspect `returncode`/`stderr` in the result. It does
    raise ValueError before running anything if `docker_host` or a TLS path contains a comma, which
    would inject extra keys (including `skip-tls-verify`) into the endpoint spec.

    args:
        name - Name for the new context (must not already exist)
        docker_host - Daemon URL, e.g. "tcp://10.0.0.5:2376" or "unix:///var/run/docker.sock"; no commas
        description - Optional human description shown in `context ls`
        tls_ca - Path on the local host to the CA cert (for TLS daemons); no commas
        tls_cert - Path on the local host to the client cert; no commas
        tls_key - Path on the local host to the client key; no commas
        skip_tls_verify - Disable TLS verification (insecure; for testing only). The only way to set
            it: it cannot be smuggled through `docker_host`
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    # Every interpolated value is comma-checked: the `--docker` spec separates keys by comma, so a
    # comma in any of these would append a key the caller never passed - `skip-tls-verify=true`
    # being the one that matters, since it would silently contradict `skip_tls_verify=False`.
    docker_spec_parts = [f"host={safe_spec_value(docker_host, 'docker_host')}"]
    if tls_ca:
        docker_spec_parts.append(f"ca={safe_spec_value(tls_ca, 'tls_ca')}")
    if tls_cert:
        docker_spec_parts.append(f"cert={safe_spec_value(tls_cert, 'tls_cert')}")
    if tls_key:
        docker_spec_parts.append(f"key={safe_spec_value(tls_key, 'tls_key')}")
    if skip_tls_verify:
        docker_spec_parts.append("skip-tls-verify=true")
    args = ["context", "create", safe_positional(name, "context name"), "--docker", ",".join(docker_spec_parts)]
    if description is not None:
        args.extend(["--description", description])
    return run_docker(args).to_dict()


@tool()
def context_use(name: str) -> dict:
    """
    Set the active Docker context for the CLI on the host running this MCP server.

    Note: this does not retarget the long-lived docker-py client - SDK-backed tools keep using the
    endpoint they connected to at startup. To retarget those, restart the server with a different
    DOCKER_HOST / DOCKER_CONTEXT. Create contexts with `context_create`; list them with
    `context_list`.

    args: name - Existing context name to set as default
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    return run_docker(["context", "use", safe_positional(name, "context name")]).to_dict()


@tool()
def context_remove(name: str, force: bool = False) -> dict:
    """
    Remove a Docker CLI context.

    Deletes only the CLI's connection metadata - the daemon it pointed at is untouched. The
    current context needs force=True (or `context_use` another first).
    Does not raise on a non-zero CLI exit - inspect `returncode`/`stderr` in the result.

    args:
        name - Context name to remove
        force - Force removal even if the context is the current one
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = ["context", "rm", safe_positional(name, "context name")]
    if force:
        args.append("--force")
    return run_docker(args).to_dict()

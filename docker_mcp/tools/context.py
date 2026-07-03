# library of mcp tools for managing docker CLI contexts.
#
# Docker contexts are a CLI-only feature — the Docker Engine HTTP API (and
# therefore docker-py) is agnostic to which daemon a CLI invocation targets.
# These tools wrap `docker context ...` via the cross-platform CLI helper.

import json

from docker_mcp.server import tool
from docker_mcp.tools._cli import parse_ndjson, raise_on_cli_failure, run_docker, safe_positional


@tool()
def context_list() -> list:
    """
    List Docker CLI contexts known to the host running this MCP server.

    Contexts are a CLI concept (stored in the docker config dir) letting one CLI target multiple
    daemons. This server uses whatever DOCKER_HOST / current-context resolved to at startup, so
    changing contexts only affects future subprocess-based tools, not the docker-py SDK client.

    returns: list - One dict per context with at least name, description, dockerEndpoint, and current
    """
    result = run_docker(["context", "ls", "--format", "{{json .}}"])
    raise_on_cli_failure(result, "context ls")
    return parse_ndjson(result.stdout, truncated=result.truncated, what="context ls output")


@tool()
def context_inspect(name: str) -> dict:
    """
    Return the full configuration for a single Docker context.

    args: name - Context name (use the `Name` field from `context_list`)
    returns: dict - The parsed `docker context inspect` entry for that context
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

    args:
        name - Name for the new context (must not already exist)
        docker_host - Daemon URL, e.g. "tcp://10.0.0.5:2376" or "unix:///var/run/docker.sock"
        description - Optional human description shown in `context ls`
        tls_ca - Path on the local host to the CA cert (for TLS daemons)
        tls_cert - Path on the local host to the client cert
        tls_key - Path on the local host to the client key
        skip_tls_verify - Disable TLS verification (insecure; for testing only)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    docker_spec_parts = [f"host={docker_host}"]
    if tls_ca:
        docker_spec_parts.append(f"ca={tls_ca}")
    if tls_cert:
        docker_spec_parts.append(f"cert={tls_cert}")
    if tls_key:
        docker_spec_parts.append(f"key={tls_key}")
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

    Note: this does not retarget the long-lived docker-py client — SDK-backed tools keep using the
    endpoint they connected to at startup. To retarget those, restart the server with a different
    DOCKER_HOST / DOCKER_CONTEXT.

    args: name - Existing context name to set as default
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    return run_docker(["context", "use", safe_positional(name, "context name")]).to_dict()


@tool()
def context_remove(name: str, force: bool = False) -> dict:
    """
    Remove a Docker CLI context.

    args:
        name - Context name to remove
        force - Force removal even if the context is the current one
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = ["context", "rm", safe_positional(name, "context name")]
    if force:
        args.append("--force")
    return run_docker(args).to_dict()

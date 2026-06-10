# library of mcp tools for managing docker CLI contexts.
#
# Docker contexts are a CLI-only feature — the Docker Engine HTTP API (and
# therefore docker-py) is agnostic to which daemon a CLI invocation targets.
# These tools wrap `docker context ...` via the cross-platform CLI helper.

import json

from docker_mcp.server import mcp
from docker_mcp.tools._cli import parse_ndjson, raise_on_cli_failure, run_docker, safe_positional


@mcp.tool()
def context_ls() -> list:
    """
    List Docker CLI contexts known to the host running this MCP server.

    Contexts are a CLI concept (stored under the user's docker config directory) and let one
    docker CLI target multiple daemons. This MCP server itself uses whatever DOCKER_HOST /
    current-context resolves to at startup, so changing contexts only affects future
    subprocess-based tools, not the long-lived docker-py client used by the SDK-backed tools.

    returns: list - One dict per context with at least name, description, dockerEndpoint, and current
    """
    result = run_docker(["context", "ls", "--format", "{{json .}}"])
    raise_on_cli_failure(result, "context ls")
    return parse_ndjson(result.stdout, truncated=result.truncated, what="context ls output")


@mcp.tool()
def context_inspect(name: str) -> dict:
    """
    Return the full configuration for a single Docker context.

    args: name: str - Context name (use the `Name` field from `context_ls`)
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


@mcp.tool()
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
        name: str - Name for the new context (must not already exist)
        docker_host: str - Daemon URL, e.g. "tcp://10.0.0.5:2376" or "unix:///var/run/docker.sock"
        description: str - Optional human description shown in `context ls`
        tls_ca: str - Path on the local host to the CA cert (for TLS daemons)
        tls_cert: str - Path on the local host to the client cert
        tls_key: str - Path on the local host to the client key
        skip_tls_verify: bool - Disable TLS verification (insecure; for testing only)
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


@mcp.tool()
def context_use(name: str) -> dict:
    """
    Set the active Docker context for the CLI on the host running this MCP server.

    Note: this does not retarget the long-lived docker-py client; SDK-backed tools
    continue to use whatever endpoint they connected to at startup. To retarget those,
    restart the MCP server with a different DOCKER_HOST / DOCKER_CONTEXT environment.

    args: name: str - Existing context name to set as default
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    return run_docker(["context", "use", safe_positional(name, "context name")]).to_dict()


@mcp.tool()
def context_rm(name: str, force: bool = False) -> dict:
    """
    Remove a Docker CLI context.

    args:
        name: str - Context name to remove
        force: bool - Force removal even if the context is the current one
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = ["context", "rm", safe_positional(name, "context name")]
    if force:
        args.append("--force")
    return run_docker(args).to_dict()

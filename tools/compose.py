# library of mcp tools for Docker Compose v2.
#
# Compose v2 is a Go CLI plugin (not part of the Docker Engine HTTP API), so these
# tools shell out via the cross-platform helper in `tools/_cli.py`.
#
# Convention: long-running subcommands run detached (`-d`) and non-interactively
# (`-T`, `--no-follow`) so they can't block the MCP server. To stream logs or
# attach, use the host CLI directly.

import json

from server import mcp
from tools._cli import CliResult, require_plugin, run_docker

# Per-operation timeout ceilings (seconds). Builds and pulls can run for many minutes
# against slow registries / large contexts, so they get longer ceilings than queries.
_TIMEOUT_QUERY = 60.0
_TIMEOUT_UP = 600.0
_TIMEOUT_DOWN = 300.0
_TIMEOUT_BUILD = 1800.0
_TIMEOUT_PULL = 1800.0
_TIMEOUT_RESTART = 300.0
_TIMEOUT_RUN = 600.0


def _global_args(
    files: list[str] | None,
    project_name: str | None,
    profiles: list[str] | None,
) -> list[str]:
    args: list[str] = []
    for f in files or []:
        args.extend(["-f", f])
    if project_name:
        args.extend(["--project-name", project_name])
    for p in profiles or []:
        args.extend(["--profile", p])
    return args


def _run_compose(subcommand_args: list[str], *, cwd: str | None, timeout: float) -> CliResult:
    require_plugin("compose")
    return run_docker(["compose", *subcommand_args], cwd=cwd, timeout=timeout)


def _parse_compose_json(text: str) -> list[dict] | dict | None:
    """
    Parse `docker compose ... --format json` output.

    Compose v2.21+ emits NDJSON (one object per line); older versions emit a single JSON
    array. Returns the parsed structure on success or None if the body is empty.
    """
    stripped = text.strip()
    if not stripped:
        return None
    # Try single-JSON-document parse first (covers `compose config --format json` and the older `ps` format).
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    items: list[dict] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


@mcp.tool()
def compose_up(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    profiles: list[str] | None = None,
    services: list[str] | None = None,
    build: bool = False,
    pull: str | None = None,
    remove_orphans: bool = False,
    wait: bool = False,
    timeout_seconds: float = _TIMEOUT_UP,
) -> dict:
    """
    Bring up a Docker Compose project, detached.

    Always runs with `-d` (detached) so it cannot block the MCP server. Use `compose_ps`
    to confirm services are running, or `wait=True` to block until they're healthy.

    args:
        project_dir: str - Working directory containing the compose file (defaults to the
                           server's cwd). Paths are passed verbatim — no shell expansion.
        files: list[str] - Explicit compose file paths (repeatable; equivalent to `-f`)
        project_name: str - Compose project name override
        profiles: list[str] - Profiles to activate
        services: list[str] - Specific services to bring up (default: all)
        build: bool - Build images before starting
        pull: str - Pull strategy: "always", "missing", "never", or "policy" (compose default)
        remove_orphans: bool - Remove containers for services not in the compose file
        wait: bool - Block until services are healthy (adds `--wait`)
        timeout_seconds: float - Subprocess timeout (default 600s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, profiles), "up", "-d"]
    if build:
        args.append("--build")
    if pull:
        args.extend(["--pull", pull])
    if remove_orphans:
        args.append("--remove-orphans")
    if wait:
        args.append("--wait")
    if services:
        args.extend(services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds).to_dict()


@mcp.tool()
def compose_down(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    profiles: list[str] | None = None,
    volumes: bool = False,
    remove_orphans: bool = False,
    timeout_seconds: float = _TIMEOUT_DOWN,
) -> dict:
    """
    Stop and remove containers, networks (and optionally volumes) for a compose project.

    args:
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        profiles: list[str] - Profiles to consider
        volumes: bool - Also remove named volumes declared by the project (destructive)
        remove_orphans: bool - Remove containers not declared in the compose file
        timeout_seconds: float - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, profiles), "down"]
    if volumes:
        args.append("--volumes")
    if remove_orphans:
        args.append("--remove-orphans")
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds).to_dict()


@mcp.tool()
def compose_ps(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    all: bool = False,
) -> dict:
    """
    List containers in a compose project, parsed from `--format json`.

    args:
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        services: list[str] - Restrict output to these services
        all: bool - Include stopped containers as well
    returns: dict - {"services": list[dict], "raw": <CliResult dict>}; on non-zero exit
                    `services` is an empty list and the caller should inspect `raw.stderr`.
    """
    args = [*_global_args(files, project_name, None), "ps", "--format", "json"]
    if all:
        args.append("--all")
    if services:
        args.extend(services)
    result = _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY)
    parsed = _parse_compose_json(result.stdout) if result.returncode == 0 else None
    if isinstance(parsed, dict):
        # Single-service `compose ps --format json` (older versions) returns one object.
        services_list: list[dict] = [parsed]
    elif isinstance(parsed, list):
        services_list = parsed
    else:
        services_list = []
    return {"services": services_list, "raw": result.to_dict()}


@mcp.tool()
def compose_logs(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    tail: int = 200,
    since: str | None = None,
    until: str | None = None,
    timestamps: bool = False,
) -> dict:
    """
    Fetch a bounded slice of logs from a compose project (never follows).

    args:
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        services: list[str] - Restrict to these services (default: all)
        tail: int - Number of lines per container (default 200; pass 0 for "all", though
                    captured output is still capped at MAX_CLI_OUTPUT_BYTES)
        since: str - Show logs since this timestamp/duration (e.g. "10m", "2024-01-01T00:00:00")
        until: str - Show logs before this timestamp/duration
        timestamps: bool - Include per-line timestamps
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "logs", "--no-color", "--no-log-prefix"]
    if tail and tail > 0:
        args.extend(["--tail", str(tail)])
    elif tail == 0:
        args.extend(["--tail", "all"])
    if since:
        args.extend(["--since", since])
    if until:
        args.extend(["--until", until])
    if timestamps:
        args.append("--timestamps")
    if services:
        args.extend(services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY).to_dict()


@mcp.tool()
def compose_config(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    profiles: list[str] | None = None,
    services_only: bool = False,
    format: str = "yaml",
) -> dict:
    """
    Render the canonical compose configuration after merges, profiles, and variable substitution.

    args:
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        profiles: list[str] - Profiles to activate before rendering
        services_only: bool - List service names only (`--services`)
        format: str - "yaml" (default) or "json"
    returns: dict - {"config": str|dict|None, "raw": <CliResult dict>};
                    `config` is a parsed dict when format="json" and parsing succeeds,
                    otherwise the rendered text from stdout.
    """
    args = [*_global_args(files, project_name, profiles), "config"]
    if services_only:
        args.append("--services")
    elif format == "json":
        args.extend(["--format", "json"])
    result = _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY)
    config: str | dict | list | None
    if result.returncode != 0:
        config = None
    elif format == "json" and not services_only:
        parsed = _parse_compose_json(result.stdout)
        config = parsed if parsed is not None else result.stdout
    else:
        config = result.stdout
    return {"config": config, "raw": result.to_dict()}


@mcp.tool()
def compose_build(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    pull: bool = False,
    no_cache: bool = False,
    timeout_seconds: float = _TIMEOUT_BUILD,
) -> dict:
    """
    Build images for a compose project.

    args:
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        services: list[str] - Specific services to build (default: all)
        pull: bool - Always attempt to pull a newer base image
        no_cache: bool - Do not use cache when building
        timeout_seconds: float - Subprocess timeout (default 1800s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "build"]
    if pull:
        args.append("--pull")
    if no_cache:
        args.append("--no-cache")
    if services:
        args.extend(services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds).to_dict()


@mcp.tool()
def compose_pull(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    ignore_pull_failures: bool = False,
    timeout_seconds: float = _TIMEOUT_PULL,
) -> dict:
    """
    Pull images declared by a compose project.

    args:
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        services: list[str] - Specific services to pull (default: all)
        ignore_pull_failures: bool - Continue past individual pull failures
        timeout_seconds: float - Subprocess timeout (default 1800s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "pull"]
    if ignore_pull_failures:
        args.append("--ignore-pull-failures")
    if services:
        args.extend(services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds).to_dict()


@mcp.tool()
def compose_restart(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    stop_timeout_seconds: int | None = None,
    timeout_seconds: float = _TIMEOUT_RESTART,
) -> dict:
    """
    Restart services in a compose project.

    args:
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        services: list[str] - Specific services to restart (default: all)
        stop_timeout_seconds: int - Grace period before SIGKILL (passed as `--timeout`)
        timeout_seconds: float - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "restart"]
    if stop_timeout_seconds is not None:
        args.extend(["--timeout", str(stop_timeout_seconds)])
    if services:
        args.extend(services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds).to_dict()


@mcp.tool()
def compose_run(
    service: str,
    command: list[str] | None = None,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    detach: bool = True,
    rm: bool = True,
    no_deps: bool = False,
    workdir: str | None = None,
    user: str | None = None,
    env: dict | None = None,
    name: str | None = None,
    timeout_seconds: float = _TIMEOUT_RUN,
) -> dict:
    """
    Run a one-off command against a compose service.

    Always passes `-T` to disable the pseudo-TTY (no terminal is attached when running
    under MCP). Defaults to detached with `--rm` so the call returns promptly.

    args:
        service: str - Service name from the compose file
        command: list[str] - Command and args to run inside the container (exec-form;
                             no shell unless you explicitly invoke one)
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        detach: bool - Run detached (default True)
        rm: bool - Remove the container after the run (default True)
        no_deps: bool - Don't start linked services
        workdir: str - Working directory inside the container
        user: str - User to run as inside the container (uid or name)
        env: dict - Environment variables to set inside the container
        name: str - Optional container name
        timeout_seconds: float - Subprocess timeout (default 600s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "run", "-T"]
    if detach:
        args.append("-d")
    if rm:
        args.append("--rm")
    if no_deps:
        args.append("--no-deps")
    if workdir:
        args.extend(["--workdir", workdir])
    if user:
        args.extend(["--user", user])
    if name:
        args.extend(["--name", name])
    for key, value in (env or {}).items():
        args.extend(["--env", f"{key}={value}"])
    args.append(service)
    if command:
        args.extend(command)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds).to_dict()


@mcp.tool()
def compose_exec(
    service: str,
    command: list[str],
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    index: int = 1,
    workdir: str | None = None,
    user: str | None = None,
    env: dict | None = None,
    timeout_seconds: float = _TIMEOUT_QUERY,
) -> dict:
    """
    Run a command inside an already-running compose service container.

    Always passes `-T` to disable the pseudo-TTY. The agent should pass an exec-form
    argv list (e.g. `["python", "-V"]`) — a `["sh", "-c", "..."]` form will interpret
    shell metacharacters in any untrusted substring.

    args:
        service: str - Service name from the compose file
        command: list[str] - Argv to execute inside the container
        project_dir: str - Working directory containing the compose file
        files: list[str] - Explicit compose file paths
        project_name: str - Compose project name override
        index: int - Container index when the service has multiple replicas (default 1)
        workdir: str - Working directory inside the container
        user: str - User to run as inside the container (uid or name)
        env: dict - Environment variables to set for the exec session
        timeout_seconds: float - Subprocess timeout (default 60s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "exec", "-T"]
    if index != 1:
        args.extend(["--index", str(index)])
    if workdir:
        args.extend(["--workdir", workdir])
    if user:
        args.extend(["--user", user])
    for key, value in (env or {}).items():
        args.extend(["--env", f"{key}={value}"])
    args.append(service)
    args.extend(command)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds).to_dict()


@mcp.tool()
def compose_ls(all: bool = False) -> list:
    """
    List compose projects known to the daemon (across all directories).

    args: all: bool - Include stopped projects
    returns: list - One dict per project (parsed from `--format json`)
    """
    args = ["compose", "ls", "--format", "json"]
    if all:
        args.append("--all")
    require_plugin("compose")
    result = run_docker(args, timeout=_TIMEOUT_QUERY)
    if result.returncode != 0:
        raise RuntimeError(
            f"`docker compose ls` failed with exit code {result.returncode}: {result.stderr.strip() or '<no output>'}"
        )
    parsed = _parse_compose_json(result.stdout)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []

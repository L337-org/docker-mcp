# library of mcp tools for Docker Compose v2.
#
# Compose v2 is a Go CLI plugin (not part of the Docker Engine HTTP API), so these
# tools shell out via the cross-platform helper in `tools/_cli.py`.
#
# Convention: long-running subcommands run detached (`-d`) and non-interactively
# (`-T`, `--no-follow`) so they can't block the MCP server. To stream logs or
# attach, use the host CLI directly.

from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    CliResult,
    parse_json_or_ndjson,
    raise_on_cli_failure,
    require_plugin,
    run_docker,
    safe_positional,
)

# Per-operation timeout ceilings (seconds). Builds and pulls can run for many minutes
# against slow registries / large contexts, so they get longer ceilings than queries.
_TIMEOUT_QUERY = 60.0
_TIMEOUT_UP = 600.0
_TIMEOUT_DOWN = 300.0
_TIMEOUT_BUILD = 1800.0
_TIMEOUT_PULL = 1800.0
_TIMEOUT_RESTART = 300.0
_TIMEOUT_RUN = 600.0
_TIMEOUT_CP = 300.0
# compose_wait blocks until the named service containers stop; bound it so a never-exiting
# service can't pin the call open forever (a timeout surfaces as subprocess.TimeoutExpired).
_TIMEOUT_WAIT = 300.0


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


def _run_compose(subcommand_args: list[str], *, cwd: str | None, timeout: float, host: str | None = None) -> CliResult:
    require_plugin("compose")
    return run_docker(["compose", *subcommand_args], cwd=cwd, timeout=timeout, host=host)


@tool()
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
    host: str | None = None,
) -> dict:
    """
    Bring up a Docker Compose project, detached.

    Always runs detached (`-d`) so it can't block the server. Use `compose_ps` to confirm
    services are running, or `wait=True` to block until they're healthy.

    args:
        project_dir - Dir with the compose file (default: server cwd; paths verbatim, no shell expansion)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        profiles - Profiles to activate
        services - Specific services to bring up (default: all)
        build - Build images before starting
        pull - Pull strategy: "always", "missing", "never", or "policy" (compose default)
        remove_orphans - Remove containers for services not in the compose file
        wait - Block until services are healthy (adds `--wait`)
        timeout_seconds - Subprocess timeout (default 600s)
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
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_down(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    profiles: list[str] | None = None,
    volumes: bool = False,
    remove_orphans: bool = False,
    timeout_seconds: float = _TIMEOUT_DOWN,
    host: str | None = None,
) -> dict:
    """
    Stop and remove containers, networks (and optionally volumes) for a compose project.

    args:
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        profiles - Profiles to consider
        volumes - Also remove named volumes declared by the project (destructive)
        remove_orphans - Remove containers not declared in the compose file
        timeout_seconds - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, profiles), "down"]
    if volumes:
        args.append("--volumes")
    if remove_orphans:
        args.append("--remove-orphans")
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_ps(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    all: bool = False,
    host: str | None = None,
) -> dict:
    """
    List containers in a compose project, parsed from `--format json`.

    args:
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        services - Restrict output to these services
        all - Include stopped containers as well
    returns: dict - {"services": list[dict], "raw": <CliResult dict>}; on non-zero exit
                    `services` is an empty list and the caller should inspect `raw.stderr`.
    """
    args = [*_global_args(files, project_name, None), "ps", "--format", "json"]
    if all:
        args.append("--all")
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    result = _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host)
    parsed = (
        parse_json_or_ndjson(result.stdout, truncated=result.truncated, what="compose ps output")
        if result.returncode == 0
        else None
    )
    if isinstance(parsed, dict):
        # Single-service `compose ps --format json` (older versions) returns one object.
        services_list: list[dict] = [parsed]
    elif isinstance(parsed, list):
        services_list = parsed
    else:
        services_list = []
    return {"services": services_list, "raw": result.to_dict()}


@tool()
def compose_logs(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    tail: int = 200,
    since: str | None = None,
    until: str | None = None,
    timestamps: bool = False,
    host: str | None = None,
) -> dict:
    """
    Fetch a bounded slice of logs from a compose project (never follows).

    args:
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        services - Restrict to these services (default: all)
        tail - Lines per container (default 200; 0 = all, still capped at MAX_CLI_OUTPUT_BYTES)
        since - Show logs since this timestamp/duration (e.g. "10m", "2024-01-01T00:00:00")
        until - Show logs before this timestamp/duration
        timestamps - Include per-line timestamps
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
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def compose_config(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    profiles: list[str] | None = None,
    services_only: bool = False,
    format: str = "yaml",
    host: str | None = None,
) -> dict:
    """
    Render the canonical compose configuration after merges, profiles, and variable substitution.

    args:
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        profiles - Profiles to activate before rendering
        services_only - List service names only (`--services`)
        format - "yaml" (default) or "json"
    returns: dict - {"config": str|dict|None, "raw": <CliResult dict>};
                    `config` is a parsed dict when format="json" and parsing succeeds,
                    otherwise the rendered text from stdout.
    """
    args = [*_global_args(files, project_name, profiles), "config"]
    if services_only:
        args.append("--services")
    elif format == "json":
        args.extend(["--format", "json"])
    result = _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host)
    config: str | dict | list | None
    if result.returncode != 0:
        config = None
    elif format == "json" and not services_only:
        parsed = parse_json_or_ndjson(result.stdout, truncated=result.truncated, what="compose config output")
        config = parsed if parsed is not None else result.stdout
    else:
        config = result.stdout
    return {"config": config, "raw": result.to_dict()}


@tool()
def compose_build(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    pull: bool = False,
    no_cache: bool = False,
    timeout_seconds: float = _TIMEOUT_BUILD,
    host: str | None = None,
) -> dict:
    """
    Build images for a compose project.

    args:
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        services - Specific services to build (default: all)
        pull - Always attempt to pull a newer base image
        no_cache - Do not use cache when building
        timeout_seconds - Subprocess timeout (default 1800s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "build"]
    if pull:
        args.append("--pull")
    if no_cache:
        args.append("--no-cache")
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_pull(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    ignore_pull_failures: bool = False,
    timeout_seconds: float = _TIMEOUT_PULL,
    host: str | None = None,
) -> dict:
    """
    Pre-fetch images for a compose project's services without starting them.

    Use this to stage images before an outage window, to refresh cached images before
    `compose_up`, or to verify images are accessible without starting containers. For
    registry-authenticated pulls ensure the daemon is logged in first with `login`.
    `compose_up --pull always` does the same as part of startup; use this tool when you
    want to separate the pull step.

    args:
        project_dir - Dir containing the compose file (default: server cwd)
        files - Explicit compose file paths, passed as `-f` (overrides auto-discovery)
        project_name - Override the compose project name
        services - Pull only these services; omit to pull all
        ignore_pull_failures - Continue if an individual image pull fails
        timeout_seconds - Subprocess timeout (default 1800s for large image pulls)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "pull"]
    if ignore_pull_failures:
        args.append("--ignore-pull-failures")
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_restart(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    stop_timeout_seconds: int | None = None,
    timeout_seconds: float = _TIMEOUT_RESTART,
    host: str | None = None,
) -> dict:
    """
    Stop then start services without recreating containers or applying config changes.

    Use this to bounce a service (e.g. to pick up a runtime file change or clear an
    in-memory state). If the compose file has changed (new image, environment, volumes,
    ports) use `compose_up` instead — it recreates affected containers to apply the diff.
    `stop_timeout_seconds` controls the SIGTERM grace period before Docker sends SIGKILL.

    args:
        project_dir - Dir containing the compose file (default: server cwd)
        files - Explicit compose file paths, passed as `-f`
        project_name - Override the compose project name
        services - Restart only these services; omit to restart all
        stop_timeout_seconds - Seconds to wait for graceful stop before SIGKILL
        timeout_seconds - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "restart"]
    if stop_timeout_seconds is not None:
        args.extend(["--timeout", str(stop_timeout_seconds)])
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_stop(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    stop_timeout_seconds: int | None = None,
    timeout_seconds: float = _TIMEOUT_DOWN,
    host: str | None = None,
) -> dict:
    """
    Stop services in a compose project without removing their containers.

    Unlike `compose_down`, containers/networks/volumes survive — use `compose_start` to bring them back.

    args:
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        services - Specific services to stop (default: all)
        stop_timeout_seconds - Grace period before SIGKILL (passed as `--timeout`)
        timeout_seconds - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "stop"]
    if stop_timeout_seconds is not None:
        args.extend(["--timeout", str(stop_timeout_seconds)])
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_start(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    timeout_seconds: float = _TIMEOUT_UP,
    host: str | None = None,
) -> dict:
    """
    Start existing (stopped) containers of a compose project.

    Counterpart to `compose_stop`: starts existing containers without recreating them. Use
    `compose_up` to (re)create containers from the compose file.

    args:
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        services - Specific services to start (default: all)
        timeout_seconds - Subprocess timeout (default 600s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "start"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
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
    host: str | None = None,
) -> dict:
    """
    Run a one-off command against a compose service.

    Always passes `-T` (no TTY under MCP). Defaults to detached with `--rm` so the call returns promptly.

    args:
        service - Service name from the compose file
        command - Command + args to run (exec-form; no shell unless you invoke one)
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        detach - Run detached (default True)
        rm - Remove the container after the run (default True)
        no_deps - Don't start linked services
        workdir - Working directory inside the container
        user - User to run as inside the container (uid or name)
        env - Environment variables to set inside the container
        name - Optional container name
        timeout_seconds - Subprocess timeout (default 600s)
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
    args.append(safe_positional(service, "service"))
    if command:
        args.extend(command)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
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
    host: str | None = None,
) -> dict:
    """
    Run a command inside an already-running compose service container.

    Always passes `-T` (no TTY). Pass an exec-form argv (e.g. `["python", "-V"]`); a
    `["sh", "-c", "..."]` form interprets shell metacharacters in untrusted substrings.

    args:
        service - Service name from the compose file
        command - Argv to execute inside the container
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        index - Container index when the service has multiple replicas (default 1)
        workdir - Working directory inside the container
        user - User to run as inside the container (uid or name)
        env - Environment variables to set for the exec session
        timeout_seconds - Subprocess timeout (default 60s)
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
    args.append(safe_positional(service, "service"))
    args.extend(command)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_images(
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    host: str | None = None,
) -> list:
    """
    List the images used by a compose project's services, parsed from `--format json`.

    args:
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        services - Restrict to these services (default: all)
    returns: list - One dict per container image (service, container, repository, tag, id, size)
    """
    args = [*_global_args(files, project_name, None), "images", "--format", "json"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    result = _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "compose images")
    parsed = parse_json_or_ndjson(result.stdout, truncated=result.truncated, what="compose images output")
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []


@tool()
def compose_port(
    service: str,
    private_port: int,
    protocol: str = "tcp",
    index: int = 1,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Resolve the host binding for a service's container port.

    The compose equivalent of `docker port`: which host address/port a service's private port is
    published on. `published` is None when the port isn't published.

    args:
        service - Service name from the compose file
        private_port - The container-internal port to look up
        protocol - "tcp" (default) or "udp"
        index - Container index when the service has multiple replicas (default 1)
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
    returns: dict - {"service", "private_port", "protocol", "published": "host:port"|None,
                     "host": str|None, "port": int|None, "bindings": list[str]}.
                     `published`/`host`/`port` describe the first binding; `bindings` lists every
                     line (a port can be published on more than one address, e.g. IPv4 and IPv6).
    """
    args = [*_global_args(files, project_name, None), "port", "--protocol", protocol]
    if index != 1:
        args.extend(["--index", str(index)])
    args.append(safe_positional(service, "service"))
    args.append(str(private_port))
    result = _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "compose port")
    # `compose port` may print several bindings, one per line (e.g. an IPv4 and an IPv6 address).
    # Parse the first non-empty line deterministically — splitting on the *last* colon keeps the
    # port intact even for a bracketed IPv6 host like "[::]:8080" — and surface the rest in `bindings`.
    bindings = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    first = bindings[0] if bindings else ""
    host, sep, port = first.rpartition(":")
    return {
        "service": service,
        "private_port": private_port,
        "protocol": protocol,
        "published": first or None,
        "host": host if (sep and host) else None,
        "port": int(port) if (sep and port.isdigit()) else None,
        "bindings": bindings,
    }


@tool()
def compose_wait(
    services: list[str],
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    timeout_seconds: float = _TIMEOUT_WAIT,
    host: str | None = None,
) -> dict:
    """
    Block until the named service containers stop, then return their exit codes.

    For one-shot / batch services. A long-running service that never exits blocks until
    `timeout_seconds`, then the subprocess is killed (TimeoutExpired) — bound it sensibly.
    Exit codes are on stdout.

    args:
        services - One or more services to wait on. At least one is required.
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        timeout_seconds - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not services:
        raise ValueError("compose_wait requires at least one service.")
    args = [*_global_args(files, project_name, None), "wait"]
    args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_top(
    services: list[str] | None = None,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Show the running processes of a compose project's containers.

    Output is the `ps`-style process table per service (not JSON); read it from `stdout`.

    args:
        services - Restrict to these services (default: all)
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "top"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def compose_cp(
    source: str,
    dest: str,
    index: int = 1,
    all_containers: bool = False,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    timeout_seconds: float = _TIMEOUT_CP,
    host: str | None = None,
) -> dict:
    """
    Copy files/folders between a service container and the server host's filesystem.

    Exactly one of `source`/`dest` is `SERVICE:PATH`; the other is a path on the host running this MCP
    server, read/written as the server's user (same host exposure as the file-path archive tools — see
    SECURITY.md). Copying to stdout (`dest="-"`) is unsupported; use the container-archive tools.

    args:
        source - `SERVICE:SRC_PATH` or a host path
        dest - `SERVICE:DEST_PATH` or a host path (not "-")
        index - Container index when the service has multiple replicas (default 1)
        all_containers - Copy to/from all containers of the service (`--all`)
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
        timeout_seconds - Subprocess timeout (default 300s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "cp"]
    if index != 1:
        args.extend(["--index", str(index)])
    if all_containers:
        args.append("--all")
    args.append(safe_positional(source, "source"))
    args.append(safe_positional(dest, "dest"))
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_kill(
    services: list[str] | None = None,
    signal: str = "SIGKILL",
    remove_orphans: bool = False,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Send a signal to a compose project's containers (default SIGKILL).

    args:
        services - Restrict to these services (default: all)
        signal - Signal to send (default "SIGKILL"; e.g. "SIGTERM", "SIGHUP")
        remove_orphans - Also remove containers for services not in the compose file
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "kill"]
    if signal and signal != "SIGKILL":
        args.extend(["--signal", signal])
    if remove_orphans:
        args.append("--remove-orphans")
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def compose_pause(
    services: list[str] | None = None,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Pause the containers of a compose project (freezes their processes).

    args:
        services - Restrict to these services (default: all)
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "pause"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def compose_unpause(
    services: list[str] | None = None,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Unpause the containers of a compose project (resumes paused processes).

    args:
        services - Restrict to these services (default: all)
        project_dir - Dir with the compose file (default: server cwd)
        files - Explicit compose file paths (repeatable, `-f`)
        project_name - Compose project name override
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "unpause"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def compose_ls(all: bool = False, host: str | None = None) -> list:
    """
    List compose projects known to the daemon (across all directories).

    args: all - Include stopped projects
    returns: list - One dict per project (parsed from `--format json`)
    """
    args = ["compose", "ls", "--format", "json"]
    if all:
        args.append("--all")
    require_plugin("compose")
    result = run_docker(args, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "compose ls")
    parsed = parse_json_or_ndjson(result.stdout, truncated=result.truncated, what="compose ls output")
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []

"""Tools for Compose projects: their lifecycle, and inspecting what one is running."""

# library of mcp tools for Docker Compose v2.
#
# Compose v2 is a Go CLI plugin (not part of the Docker Engine HTTP API), so these
# tools shell out via the cross-platform helper in `tools/_cli.py`.
#
# Convention: long-running subcommands run detached (`-d`) and non-interactively
# (`-T`, `--no-follow`) so they can't block the MCP server. To stream logs or
# attach, use the host CLI directly.
#
# Every subcommand but `compose_list` reads its Compose file from a working directory (that one asks
# the daemon), so with no local compose plugin and an ssh:// target the remote-exec fallback has to
# *stage* that directory on the far host before running there (`_cli.py:remote_stage_and_exec`);
# `compose_list` takes the exec-only path. Consequences worth knowing: the whole directory is copied
# (nothing can tell which files a Compose file references, so an oversized one is refused with a limit
# error), and registry credentials come from the remote user's `~/.docker/config.json`. `compose_cp` is
# bespoke rather than going through `remote_stage_and_exec` like the rest of this module: one side of
# the copy is a local path outside the compose file's working directory, which that helper has no
# concept of relaying (see `_remote_compose_cp`).

import os
import time
from pathlib import Path
from typing import Literal

from docker_mcp.exceptions import ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    CliResult,
    parse_json_or_ndjson,
    raise_on_cli_failure,
    remote_cli_session,
    remote_exec_cli,
    remote_stage_and_exec,
    require_plugin,
    run_docker,
    run_in_session,
    safe_positional,
    should_remote_exec,
)
from docker_mcp.tools.system import _get_client

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


# The flags `_global_args` emits, all of which take a separate value token. Used to walk exactly that
# prefix back off an argv and no further.
_GLOBAL_FLAGS = ("-f", "--project-name", "--profile")


def _global_file_values(subcommand_args: list[str]) -> list[str]:
    """The `-f` values in the argv's *global prefix* - the local paths a compose call names.

    Scanning the whole argv for `-f` would be wrong, not merely loose: `compose_run` and `compose_exec`
    append an arbitrary container command, so `command=["python", "-f", "script.py"]` would present
    `script.py` as a compose file, and on the remote path an absolute one would be uploaded and the
    caller's argument rewritten. Only `_global_args` produces genuine `-f` values, always as a
    flag/value prefix, so walking pairs from the start and stopping at the first token that is not a
    global flag recovers exactly that list - the subcommand name terminates it.

    Args:
        subcommand_args: the argv built by a tool, without the leading `compose`

    Returns:
        list[str]: the values following each `-f` in the global prefix
    """
    values: list[str] = []
    index = 0
    while index + 1 < len(subcommand_args) and subcommand_args[index] in _GLOBAL_FLAGS:
        if subcommand_args[index] == "-f":
            values.append(subcommand_args[index + 1])
        index += 2
    return values


def _run_compose(subcommand_args: list[str], *, cwd: str | None, timeout: float, host: str | None = None) -> CliResult:
    """Run `docker compose <args...>`, staging the working directory when the CLI has to run remotely.

    Args:
        subcommand_args: the compose argv, without the leading `compose`
        cwd: the project directory, or None for the server's own working directory (which is what
              gets staged in the remote case, matching what the local subprocess would use)
        timeout: seconds allowed for the command
        host: configured host label, or None for the default host

    Returns:
        CliResult: the same shape from either backend
    """
    if should_remote_exec(host, plugin="compose"):
        return remote_stage_and_exec(
            host,
            ["compose", *subcommand_args],
            cwd=cwd,
            timeout=timeout,
            path_values=_global_file_values(subcommand_args),
        )
    require_plugin("compose")
    return run_docker(["compose", *subcommand_args], cwd=cwd, timeout=timeout, host=host)


@tool()
def compose_up(  # noqa: DOC101,DOC103
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    profiles: list[str] | None = None,
    services: list[str] | None = None,
    build: bool = False,
    pull: Literal["always", "missing", "never"] | None = None,
    remove_orphans: bool = False,
    timeout_seconds: float = _TIMEOUT_UP,
    host: str | None = None,
) -> dict:
    """
    Bring up a Docker Compose project, detached.

    Always runs detached (`-d`), so it returns once the containers are started. To wait for them, call
    `system_wait(kind="compose", target=<project name>, until="healthy")` (or `until="running"`);
    `compose_ps` shows their state without waiting.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        project_dir: Dir with the compose file (default: server cwd, copied to the target host if no local plugin; paths
            verbatim, no shell expansion)
        files: Explicit compose file paths (repeatable, `-f`)
        services: Specific services to bring up (default: all)
        build: Build images before starting
        pull: Pull strategy; omit to use each service's own `pull_policy`
        remove_orphans: Remove containers for services not in the compose file

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, profiles), "up", "-d"]
    if build:
        args.append("--build")
    if pull:
        args.extend(["--pull", pull])
    if remove_orphans:
        args.append("--remove-orphans")
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_down(  # noqa: DOC101,DOC103
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

    Inverse of `compose_up`. Images are kept; named volumes go only with volumes=True
    (destructive). Use `compose_stop` to stop without removing anything.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        volumes: Also remove named volumes declared by the project (destructive)
        remove_orphans: Remove containers not declared in the compose file

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, profiles), "down"]
    if volumes:
        args.append("--volumes")
    if remove_orphans:
        args.append("--remove-orphans")
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_ps(  # noqa: DOC101,DOC103
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    all: bool = False,
    host: str | None = None,
) -> dict:
    """
    List containers in a compose project, parsed from `--format json`.

    Container-level view of one project (state, health, publishers); `compose_list` enumerates
    projects, and `container_list` covers non-compose containers.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises): `services`
    comes back empty - inspect `raw.stderr`.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        services: Restrict output to these services
        all: Include stopped containers as well

    Returns:
        dict: {"services": list[dict], "raw": <CliResult dict>}; on non-zero exit `services` is an empty list and the
            caller should inspect `raw.stderr`.
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
def compose_logs(  # noqa: DOC101,DOC103
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    tail: int | Literal["all"] = 200,
    since: str | None = None,
    until: str | None = None,
    timestamps: bool = False,
    host: str | None = None,
) -> dict:
    """
    Fetch a bounded slice of logs from a compose project (never follows).

    Bounded and non-following by design, so it always returns promptly. For one container's logs
    use `container_logs`; for a swarm service use `service_logs`. Log text arrives on `stdout`.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        services: Restrict to these services (default: all)
        tail: Lines per container, or the literal "all" (still capped at MAX_CLI_OUTPUT_BYTES)
        since: Show logs since this timestamp/duration (e.g. "10m", "2024-01-01T00:00:00")
        until: Show logs before this timestamp/duration
        timestamps: Include per-line timestamps

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "logs", "--no-color", "--no-log-prefix"]
    args.extend(["--tail", str(tail)])
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
def compose_config(  # noqa: DOC101,DOC103
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    profiles: list[str] | None = None,
    services_only: bool = False,
    format: Literal["yaml", "json"] = "yaml",
    host: str | None = None,
) -> dict:
    """
    Render the canonical compose configuration after merges, profiles, and variable substitution.

    Use it to validate compose files and see exactly what the CLI will run before `compose_up`.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises): on a failed
    render `config` may be None - inspect `raw.stderr`.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        profiles: Profiles to activate before rendering
        services_only: List service names only (`--services`)
        format: Render as YAML (default) or JSON

    Returns:
        dict: {"config": str|dict|None, "raw": <CliResult dict>}; `config` is a parsed dict when format="json" and
            parsing succeeds, otherwise the rendered text from stdout.
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
def compose_build(  # noqa: DOC101,DOC103
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

    Builds the images declared by the project's `build:` sections without starting anything -
    `compose_up(build=True)` builds and starts in one step.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        services: Specific services to build (default: all)
        pull: Always attempt to pull a newer base image
        no_cache: Do not use cache when building

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
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
def compose_pull(  # noqa: DOC101,DOC103
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
    registry-authenticated pulls ensure the daemon is logged in first with `system_login`.
    `compose_up --pull always` does the same as part of startup; use this tool when you
    want to separate the pull step.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`; overrides auto-discovery)
        services: Pull only these services; omit to pull all
        ignore_pull_failures: Continue if an individual image pull fails
        timeout_seconds: Subprocess timeout (default 1800s for large image pulls)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "pull"]
    if ignore_pull_failures:
        args.append("--ignore-pull-failures")
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_restart(  # noqa: DOC101,DOC103
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
    ports) use `compose_up` instead - it recreates affected containers to apply the diff.
    `stop_timeout_seconds` controls the SIGTERM grace period before Docker sends SIGKILL.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        services: Restart only these services; omit to restart all
        stop_timeout_seconds: Seconds to wait for graceful stop before SIGKILL

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "restart"]
    if stop_timeout_seconds is not None:
        args.extend(["--timeout", str(stop_timeout_seconds)])
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_stop(  # noqa: DOC101,DOC103
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

    Unlike `compose_down`, containers/networks/volumes survive - use `compose_start` to bring them back.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        services: Specific services to stop (default: all)
        stop_timeout_seconds: Grace period before SIGKILL (passed as `--timeout`)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "stop"]
    if stop_timeout_seconds is not None:
        args.extend(["--timeout", str(stop_timeout_seconds)])
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_start(  # noqa: DOC101,DOC103
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
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        services: Specific services to start (default: all)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "start"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_run(  # noqa: DOC101,DOC103
    service: str,
    command: list[str] | None = None,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
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

    Always detached (`-d`) and `-T` (no TTY under MCP): it returns once the container starts, with the
    container's id as `stdout`, newline-terminated (strip it). For its result,
    `system_wait(kind="container", target=<that id>, until="not-running")` gives the exit code,
    `container_logs` the output, and `container_remove` the cleanup; the container is kept until then,
    because removing it on exit would delete its logs. Unlike `compose_exec`, this starts a NEW
    container for the service rather than running inside the existing one.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        service: Service name from the compose file
        command: Command + args to run (exec-form; no shell unless you invoke one)
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        no_deps: Don't start linked services
        workdir: Working directory inside the container
        user: User to run as inside the container (uid or name)
        env: Environment variables to set inside the container

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    # Detached and kept: a foreground run blocks for as long as the command runs, and `--rm` would delete
    # the logs and exit code before `system_wait` and `container_logs` could read them.
    args = [*_global_args(files, project_name, None), "run", "-T", "-d"]
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
def compose_exec(  # noqa: DOC101,DOC103
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
    Run a command inside an already-running compose service container (see also `container_exec`).

    Always passes `-T` (no TTY). Pass an exec-form argv (e.g. `["python", "-V"]`); a
    `["sh", "-c", "..."]` form interprets shell metacharacters in untrusted substrings.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        service: Service name from the compose file
        command: Argv to execute inside the container
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        index: Container index when the service has multiple replicas
        workdir: Working directory inside the container
        user: User to run as inside the container (uid or name)
        env: Environment variables to set for the exec session

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
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
def compose_images(  # noqa: DOC101,DOC103
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    services: list[str] | None = None,
    host: str | None = None,
) -> list:
    """
    List the images used by a compose project's services, parsed from `--format json`.

    Answers "what image and tag does each service container actually run?" - the containers must
    exist (`compose_up` first). Use `compose_ps` for container state and
    `image_list` for daemon-wide images.
    Raises RemoteFailureError if the CLI call fails.

    Args:
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)
        services: Restrict to these services (default: all)

    Returns:
        list: One dict per container image (service, container, repository, tag, id, size)
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
def compose_port(  # noqa: DOC101,DOC103
    service: str,
    private_port: int,
    # Not an enum: compose performs no validation on this flag at all (tcp/udp/sctp and a bogus
    # value are indistinguishable in its output), so the accepted set cannot be established from
    # the tool itself, and guessing one risks refusing a value compose would have honoured.
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
    published on. `published` is None when the port isn't published. For non-compose containers
    read `container_inspect`'s NetworkSettings.Ports instead.
    Raises RemoteFailureError if the CLI call fails.

    Args:
        service: Service name from the compose file
        private_port: The container-internal port to look up
        protocol: "tcp" (default) or "udp"
        index: Container index when the service has multiple replicas
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)

    Returns:
        dict: {"service", "private_port", "protocol", "published": "host:port"|None, "host": str|None, "port": int|None,
            "bindings": list[str]}. `published`/`host`/`port` describe the first binding; `bindings` lists every line (a
            port can be published on more than one address, e.g. IPv4 and IPv6).
    """
    args = [*_global_args(files, project_name, None), "port", "--protocol", protocol]
    if index != 1:
        args.extend(["--index", str(index)])
    args.append(safe_positional(service, "service"))
    args.append(str(private_port))
    result = _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "compose port")
    # `compose port` may print several bindings, one per line (e.g. an IPv4 and an IPv6 address).
    # Parse the first non-empty line deterministically - splitting on the *last* colon keeps the
    # port intact even for a bracketed IPv6 host like "[::]:8080" - and surface the rest in `bindings`.
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


# Compose stamps these on every container it creates; reading them through the Engine API lets a
# wait follow a project without the compose plugin, on any host the server reaches.
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
# `compose run` containers carry this as the string "True"; they are not part of the project's state.
_COMPOSE_ONEOFF_LABEL = "com.docker.compose.oneoff"
# On a dependent container: "<service>:<condition>:<restart>" per dependency, comma-separated, e.g.
# "migrate:service_completed_successfully:false". An integration test holds the format to a real project.
_COMPOSE_DEPENDS_ON_LABEL = "com.docker.compose.depends_on"
_STOPPED = ("exited", "dead")


def _compose_containers(project: str, services: list[str] | None, host: str | None) -> tuple[list[dict], set[str]]:
    """Read the state of a compose project's service containers through the Engine API.

    Args:
        project: the compose project name, as `compose_list` reports it
        services: only these services' containers, or None for all of them
        host: the host label to target, or None for the default

    Returns:
        tuple: one {"name", "service", "status", "health", "exit_code"} per container, sorted by name and
            with `exit_code` set only once it has stopped; and the services some other service depends on
            with `condition: service_completed_successfully`, which are meant to exit
    """
    # `ignore_removed`: a container can go between the list and docker-py's inspect of it (a `compose
    # run --rm` one-off, or a recreate), which would otherwise fail the whole wait with NotFound.
    found = _get_client(host).containers.list(
        all=True, ignore_removed=True, filters={"label": f"{_COMPOSE_PROJECT_LABEL}={project}"}
    )
    rows, completing = [], set()
    for container in found:
        labels = container.labels or {}
        if labels.get(_COMPOSE_ONEOFF_LABEL) == "True":
            continue
        # Read before the `services` filter: the dependency is declared on the dependent's container.
        for dependency in filter(None, (labels.get(_COMPOSE_DEPENDS_ON_LABEL) or "").split(",")):
            name, _, rest = dependency.partition(":")
            if rest.split(":", 1)[0] == "service_completed_successfully":
                completing.add(name)
        service = labels.get(_COMPOSE_SERVICE_LABEL)
        if services and service not in services:
            continue
        state = container.attrs.get("State") or {}
        status = state.get("Status")
        rows.append(
            {
                "name": container.name,
                "service": service,
                "status": status,
                "health": (state.get("Health") or {}).get("Status"),
                "exit_code": state.get("ExitCode") if status in _STOPPED else None,
            }
        )
    return sorted(rows, key=lambda row: row["name"]), completing


def _compose_condition(until: str, rows: list[dict], completing: set[str]) -> bool | None:
    """Decide a compose wait from one reading: met, cannot be met, or not yet.

    Args:
        until: "running", "healthy" or "exited"
        rows: the project's containers, from `_compose_containers`
        completing: services another service waits on to complete successfully

    Returns:
        bool or None: True when met, False when it can no longer be met (nothing to wait on, a container
            stopped that was not meant to, a job that exited non-zero, or an unhealthy container), None
            while it is still settling
    """
    if not rows:
        return False
    if until == "exited":
        return True if all(row["status"] in _STOPPED for row in rows) else None
    # What `docker compose up --wait` accepts (docker/compose `getDependencyCondition`): a service another
    # depends on with `service_completed_successfully` only has to exit 0; any other stop is a failure.
    stopped = [row for row in rows if row["status"] in _STOPPED]
    if any(row["service"] not in completing or row["exit_code"] != 0 for row in stopped):
        return False
    if any(row["health"] == "unhealthy" for row in rows):
        return False
    live = [row for row in rows if row["status"] not in _STOPPED]
    if until == "running":
        return True if all(row["status"] == "running" for row in live) else None
    # "healthy": a container with a HEALTHCHECK must report healthy, one without must be running.
    ready = all(row["health"] == "healthy" or (row["health"] is None and row["status"] == "running") for row in live)
    return True if ready else None


def _wait_compose(
    project: str,
    until: str,
    timeout_seconds: float,
    poll_interval: float,
    services: list[str] | None = None,
    host: str | None = None,
) -> dict:
    """Wait for a compose project's containers; `system_wait(kind="compose")` is the tool that calls this.

    Never raises on timeout. Reads the containers by their compose labels through the Engine API, so
    no compose plugin is needed, and leaves out `compose run` one-off containers. "running" and
    "healthy" accept what `docker compose up --wait` accepts: a service another depends on with
    `service_completed_successfully` may have exited 0, and for "healthy" a container without a
    HEALTHCHECK counts once it runs. Returns promptly with `met: false` when the condition can no
    longer be met: no container carries the project label, a container stopped that was not meant to,
    a job exited non-zero, or a container reports unhealthy.

    Args:
        project: the compose project name, as `compose_list` reports it
        until: "running", "healthy" or "exited"
        timeout_seconds: seconds to wait before returning with timed_out=true
        poll_interval: seconds between re-checks, capped by the time left
        services: only these services' containers, or None for every service in the project
        host: the host label to target, or None for the default

    Returns:
        dict: {"project", "until", "met", "timed_out", "containers", "waited_seconds"}, where
            `containers` is the last reading, with each stopped container's exit code

    Raises:
        ToolInputError: a negative timeout or a non-positive poll interval
    """
    if timeout_seconds < 0:
        raise ToolInputError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
    if poll_interval <= 0:
        raise ToolInputError(f"poll_interval must be > 0, got {poll_interval}.")
    start = time.monotonic()
    deadline = start + timeout_seconds
    while True:
        rows, completing = _compose_containers(project, services, host)
        verdict = _compose_condition(until, rows, completing)
        remaining = deadline - time.monotonic()
        if verdict is not None or remaining <= 0:
            return {
                "project": project,
                "until": until,
                "met": verdict is True,
                "timed_out": verdict is None,
                "containers": rows,
                "waited_seconds": round(time.monotonic() - start, 2),
            }
        # Bound the sleep by the time left so a large poll_interval can't push past the timeout.
        time.sleep(min(poll_interval, remaining))


@tool()
def compose_top(  # noqa: DOC101,DOC103
    services: list[str] | None = None,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Show the running processes of a compose project's containers.

    Output is the `ps`-style process table per service (not JSON); read it from `stdout`. The
    per-container equivalent is `container_top`.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        services: Restrict to these services (default: all)
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "top"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host).to_dict()


def _split_cp_arg(arg: str) -> tuple[str, str]:
    """Split a `compose cp` SRC/DEST argument into `(service, path)`.

    Mirrors `docker compose cp`'s own `splitCpArg`, verified against docker/compose's
    `pkg/compose/cp.go`, which is byte-for-byte the same algorithm plain `docker cp` uses: an
    absolute local path is never a container reference; otherwise the text before the first `:` is
    the service name, unless it starts with `.` (an explicit relative local path like
    `./file:name.txt`). `os.path.isabs` - rather than hand-rolling the Windows drive-letter check
    `splitCpArg` needs - already reflects *this* host's own platform, which is exactly what "is this
    a local path" needs to mean here.

    Used only to decide which side needs staging/fetching for the remote-exec fallback below; the
    real CLI, local or remote, still does its own parsing of the literal argv either way.

    Args:
        arg: one of `compose_cp`'s `source`/`dest` values

    Returns:
        tuple[str, str]: `(service, path)` if `arg` names a container path, else `("", arg)`
    """
    if os.path.isabs(arg):
        return "", arg
    service, sep, path = arg.partition(":")
    if not sep or service.startswith("."):
        return "", arg
    return service, path


def _remote_compose_cp(
    source: str,
    dest: str,
    *,
    index: int,
    all_containers: bool,
    project_dir: str | None,
    files: list[str] | None,
    project_name: str | None,
    timeout_seconds: float,
    host: str | None,
) -> CliResult:
    """Run `compose cp` on the target `ssh://` host, relaying whichever side of the copy is local.

    Always stages `project_dir` first - compose needs to resolve the project/service the same way
    every other compose subcommand does - then branches on which side `_split_cp_arg` identifies as
    the container reference: a local source is staged like any other input (host->container); a local
    destination gets a fresh scratch path reserved for the remote command to write into, fetched back
    once it succeeds (container->host). When neither or both sides look like `SERVICE:PATH`, the call
    is passed through with no staging on either side - the real remote CLI gives the same validation
    error `docker compose cp` would locally (e.g. "copying between services is not supported").

    Because the actual copy always runs through the real remote CLI, every documented parameter
    behaves exactly as it does locally - `--all`, `--index`, `project_dir`/`files` and the result
    shape all come along for free. The one behaviour with no remote equivalent: a container->host copy
    is refused up front if the local destination already exists, since only this host - not the
    remote one - knows that, and `reserve_path` guarantees the remote command starts from a path that
    does not exist yet (matching what a fresh local destination would look like).

    Args:
        source: the copy's source, container-side or local
        dest: the copy's destination, container-side or local
        index: which replica to address when the service has several
        all_containers: whether to copy to every replica
        project_dir: the compose project directory, or None
        files: the compose files to use, or None
        project_name: the project name, or None
        timeout_seconds: the wall-clock limit
        host: the host label to target, or None for the default

    Returns:
        dict: the copy's outcome, naming the side that was relayed

    Raises:
        ToolInputError: neither side of the copy is local, or the local side is
            unusable.
    """
    ctr_src, _ = _split_cp_arg(source)
    ctr_dst, _ = _split_cp_arg(dest)
    local_dest: Path | None = None
    if ctr_src and not ctr_dst:
        local_dest = Path(dest).expanduser()
        if local_dest.exists():
            raise ToolInputError(
                f"compose_cp: refusing to fetch {source!r} to {dest!r}: the destination already exists on "
                f"this host. The remote-exec fallback only creates a new path there, matching the state the "
                f"remote command starts from - remove the existing path first, or choose a different one."
            )

    local_cwd = Path(project_dir).expanduser() if project_dir else Path.cwd()
    if not local_cwd.is_dir():
        detail = "it exists but is not a directory" if local_cwd.exists() else "nothing exists at that path"
        raise ToolInputError(
            f"Cannot run `docker compose cp` on the remote host: {str(local_cwd)!r} is not a usable project "
            f"directory on this host ({detail}), and it is what would be copied over."
        )

    subcommand_args = [*_global_args(files, project_name, None), "cp"]
    if index != 1:
        subcommand_args.extend(["--index", str(index)])
    if all_containers:
        subcommand_args.append("--all")

    with remote_cli_session(host, timeout=timeout_seconds) as session:
        staged_tree = session.stage_tree(local_cwd)
        if ctr_dst and not ctr_src:
            local_source = Path(source).expanduser()
            if not local_source.exists():
                raise ToolInputError(f"compose_cp: {source!r} does not exist on this host.")
            staged_source = (
                session.stage_tree(local_source) if local_source.is_dir() else session.stage_file(local_source)
            )
            full_args = ["compose", *subcommand_args, staged_source, dest]
            result = run_in_session(session, full_args, cwd=staged_tree, timeout=timeout_seconds)
        elif local_dest is not None:
            scratch = session.reserve_path()
            full_args = ["compose", *subcommand_args, source, scratch]
            result = run_in_session(session, full_args, cwd=staged_tree, timeout=timeout_seconds)
            if result.returncode == 0:
                session.fetch_path(scratch, local_dest)
        else:
            full_args = ["compose", *subcommand_args, source, dest]
            result = run_in_session(session, full_args, cwd=staged_tree, timeout=timeout_seconds)

    return result


@tool()
def compose_cp(  # noqa: DOC101,DOC103
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

    Exactly one of `source`/`dest` is `SERVICE:PATH`; the other is a path on the host running this
    MCP server, read/written as the server's user (same host exposure as the file-path archive
    tools - see SECURITY.md). Copying to stdout (`dest="-"`) is unsupported; use
    `container_archive_get`.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result. With no local compose plugin and an `ssh://` target, runs the real
    `docker compose cp` on that host instead and relays whichever side of the copy is local over the same SSH
    connection - every parameter above behaves the same either way, since the actual copy always runs through
    the real CLI. The one difference: a container->host copy is refused if the local destination already
    exists, since only this host (not the remote one) knows that. `unix://`/`tcp://`+TLS hosts with no local
    plugin are not covered by this fallback (no shell to run the CLI on) and still raise `CapabilityError` -
    use `container_archive_put` (host to container) or `container_archive_get_to_file` (container to host)
    there instead; both talk to the daemon directly and need no local CLI (`compose_ps` gives you the
    container name).

    Args:
        source: `SERVICE:SRC_PATH` or a host path
        dest: `SERVICE:DEST_PATH` or a host path (not "-")
        index: Container index when the service has multiple replicas
        all_containers: Copy to/from all containers of the service (`--all`)
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    source = safe_positional(source, "source")
    dest = safe_positional(dest, "dest")
    if should_remote_exec(host, plugin="compose"):
        return _remote_compose_cp(
            source,
            dest,
            index=index,
            all_containers=all_containers,
            project_dir=project_dir,
            files=files,
            project_name=project_name,
            timeout_seconds=timeout_seconds,
            host=host,
        ).to_dict()
    args = [*_global_args(files, project_name, None), "cp"]
    if index != 1:
        args.extend(["--index", str(index)])
    if all_containers:
        args.append("--all")
    args.append(source)
    args.append(dest)
    return _run_compose(args, cwd=project_dir, timeout=timeout_seconds, host=host).to_dict()


@tool()
def compose_kill(  # noqa: DOC101,DOC103
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

    Immediate, with no grace period - prefer `compose_stop` for a clean shutdown (stop signal,
    then kill after a timeout).
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        services: Restrict to these services (default: all)
        signal: Signal to send (default "SIGKILL"; e.g. "SIGTERM", "SIGHUP")
        remove_orphans: Also remove containers for services not in the compose file
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
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
def compose_pause(  # noqa: DOC101,DOC103
    services: list[str] | None = None,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Pause the containers of a compose project (freezes their processes in place).

    Paused containers stop consuming CPU but keep memory, network endpoints, and state; resume
    with `compose_unpause`. To actually stop containers (each one's configured stop signal,
    freeing resources) use `compose_stop`; to stop and delete them use `compose_down`.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        services: Restrict to these services (default: all)
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "pause"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def compose_unpause(  # noqa: DOC101,DOC103
    services: list[str] | None = None,
    project_dir: str | None = None,
    files: list[str] | None = None,
    project_name: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Unpause the containers of a compose project (resumes paused processes).

    Reverse of `compose_pause`: processes continue from where they were frozen (no restart).
    `compose_start` is the counterpart for stopped containers.
    Does not raise on a non-zero CLI exit (a missing compose plugin or a timeout still raises) - inspect
    `returncode`/`stderr` in the result.

    Args:
        services: Restrict to these services (default: all)
        project_dir: Dir with the compose file (default: server cwd; copied to the target host if no local plugin)
        files: Explicit compose file paths (repeatable, `-f`)

    Returns:
        dict: {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args = [*_global_args(files, project_name, None), "unpause"]
    if services:
        args.extend(safe_positional(s, "service") for s in services)
    return _run_compose(args, cwd=project_dir, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def compose_list(all: bool = False, host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List compose projects known to the daemon (across all directories).

    Project-level view (one entry per project); `compose_ps` lists the containers of a single
    project.
    Raises RemoteFailureError if the CLI call fails.

    Args:
        all: Include stopped projects

    Returns:
        list: One dict per project (parsed from `--format json`)
    """
    args = ["compose", "ls", "--format", "json"]
    if all:
        args.append("--all")
    # The only compose tool that reads nothing from a working directory (it asks the daemon), so the
    # remote path needs no staging.
    if should_remote_exec(host, plugin="compose"):
        result = remote_exec_cli(host, args, timeout=_TIMEOUT_QUERY)
    else:
        require_plugin("compose")
        result = run_docker(args, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "compose ls")
    parsed = parse_json_or_ndjson(result.stdout, truncated=result.truncated, what="compose ls output")
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []

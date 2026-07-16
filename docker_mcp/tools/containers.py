# library of mcp tools relating to container management

import math
import re
import threading
import time
from collections.abc import Iterable
from typing import Literal, TypedDict, cast

import requests.exceptions

from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import (
    MAX_PAYLOAD_BYTES,
    close_stream_quietly,
    drop_none,
    host_read_path,
    join_bounded,
    stream_to_file,
)
from docker_mcp.tools.system import _get_client, guard_not_self


class RestartPolicy(TypedDict, total=False):
    """Restart policy for container_run, mirroring the `docker` module's expected dict shape."""

    Name: Literal["no", "always", "on-failure", "unless-stopped"]
    MaximumRetryCount: int  # only meaningful with Name="on-failure"


@tool()
def container_run(
    image: str,
    command: str | list | None = None,
    name: str | None = None,
    detach: bool = True,
    environment: dict | list | None = None,
    ports: dict | None = None,
    volumes: dict | list | None = None,
    network: str | None = None,
    hostname: str | None = None,
    user: str | None = None,
    working_dir: str | None = None,
    entrypoint: str | list | None = None,
    restart_policy: RestartPolicy | None = None,
    labels: dict | list | None = None,
    remove: bool = False,
    auto_remove: bool = False,
    privileged: bool = False,
    tty: bool = False,
    stdin_open: bool = False,
    mem_limit: int | str | None = None,
    cpu_count: int | None = None,
    extra_kwargs: dict | None = None,
    host: str | None = None,
) -> dict | str:
    """
    Run a container from an image (create and start in one call, like `docker run`).

    Use `container_create` to prepare a container without starting it, or `container_exec` to run
    a command in a container that already exists. With detach=False the call blocks until the
    container exits and returns its output, so long-running images need detach=True. Created
    containers are stamped with provenance labels.

    args:
        image - The image to run
        command - The command to run in the container
        name - Name to assign to the container
        detach - Run in the background and return container info
        environment - Environment variables to set
        ports - Port mappings, e.g. {'2222/tcp': 3333}
        volumes - Volumes to mount
        network - Name of the network to attach
        hostname - Optional hostname for the container
        user - Username or UID to run as
        working_dir - Working directory inside the container
        entrypoint - Entrypoint to override the image default
        restart_policy - Restart policy, e.g. {'Name': 'on-failure', 'MaximumRetryCount': 3}
        labels - Labels to set on the container
        remove - Remove the container when it exits (only with detach=False)
        auto_remove - Enable auto-removal of the container on daemon side
        privileged - Give extended privileges to the container
        tty - Allocate a pseudo-TTY
        stdin_open - Keep STDIN open
        mem_limit - Memory limit
        cpu_count - Number of CPUs
        extra_kwargs - Additional keyword arguments forwarded to ContainerCollection.run (call
                       `docs_lookup(section="containers")` for the full accepted set)
    returns: dict | str - Container attrs when detach=True, otherwise stdout/stderr as a string
    """
    kwargs: dict = {
        "detach": detach,
        **drop_none(
            command=command,
            name=name,
            environment=environment,
            ports=ports,
            volumes=volumes,
            network=network,
            hostname=hostname,
            user=user,
            working_dir=working_dir,
            entrypoint=entrypoint,
            restart_policy=restart_policy,
            labels=with_provenance(labels, "container_run"),
            mem_limit=mem_limit,
            cpu_count=cpu_count,
        ),
    }
    for key, value in {
        "remove": remove,
        "auto_remove": auto_remove,
        "privileged": privileged,
        "tty": tty,
        "stdin_open": stdin_open,
    }.items():
        if value:
            kwargs[key] = value
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    result = _get_client(host).containers.run(image, **kwargs)
    if detach:
        return result.attrs
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    return str(result)


@tool()
def container_create(
    image: str, command: str | list | None = None, extra_kwargs: dict | None = None, host: str | None = None
) -> dict:
    """
    Create a container from an image without starting it.

    Use this when you need to configure a container (with `extra_kwargs`) before its first
    start, or want creation and start as separate observable steps. For the common case of
    create-then-start-immediately use `container_run` instead — it does both in one call.
    Start the created container with `container_start`. Common `extra_kwargs` keys: `name`
    (str), `environment` (list of "KEY=VAL" or dict), `ports` (dict, e.g.
    `{"80/tcp": 8080}`), `volumes` (dict, e.g. `{"/host/path": {"bind": "/container/path",
    "mode": "rw"}}`), `labels` (dict). For anything else docker-py's `ContainerCollection.create`
    accepts, call `docs_lookup(section="containers")` rather than guessing a key name.

    args:
        image - Image to create the container from, e.g. "nginx:alpine"
        command - Override the image's default command; string or list of strings
        extra_kwargs - Additional docker-py ContainerCollection.create keyword arguments
    returns: dict - The created container's attrs (not yet running)
    """
    kwargs = dict(extra_kwargs or {})
    labels = with_provenance(kwargs.get("labels"), "container_create")
    if labels is not None:
        kwargs["labels"] = labels
    container = _get_client(host).containers.create(image, command=command, **kwargs)
    return container.attrs


@tool()
def container_inspect(id_or_name: str, host: str | None = None) -> dict:
    """
    Return the full inspect detail for a single container.

    Use this when you need complete information about one container — config, state,
    network settings, mounts, environment variables, and resource limits. To enumerate many
    containers use `container_list` instead (same payload per container by default; abridged
    with sparse=True). For just logs or stats use `container_logs` / `container_stats`.

    args: id_or_name - Container id (full or short) or name
    returns: dict - Full container inspect attrs (equivalent to `docker inspect`)
    """
    return _get_client(host).containers.get(id_or_name).attrs


@tool()
def container_list(
    all: bool = False,
    since: str | None = None,
    before: str | None = None,
    limit: int | None = None,
    filters: dict | None = None,
    sparse: bool = False,
    ignore_removed: bool = False,
    managed_only: bool = False,
    host: str | None = None,
) -> list:
    """
    List containers on the daemon (running only by default).

    Pass all=True to include stopped containers. For a compose project `compose_ps` groups
    containers by service; for swarm services use `service_ps` (tasks may live on other nodes).

    args:
        all - Show all containers, including stopped ones (default False: running only)
        since - Only show containers created after this id or name
        before - Only show containers created before this id or name
        limit - Maximum number of results
        filters - Filter by attributes (e.g. status, label)
        sparse - Skip inspect calls and return less detail
        ignore_removed - Ignore containers removed during listing
        managed_only - Only return containers created by this MCP server (filters on the
                             docker-mcp-server.managed label); combines with any `filters` given
    returns: list - One dict per container: full inspect payloads by default (each match is
        inspected, like `container_inspect`); sparse=True skips the per-container inspect calls
        and returns the daemon's abridged list entries instead
    """
    if managed_only:
        filters = managed_filter(filters)
    kwargs: dict = {
        "all": all,
        "sparse": sparse,
        "ignore_removed": ignore_removed,
        **drop_none(since=since, before=before, limit=limit, filters=filters),
    }
    return [c.attrs for c in _get_client(host).containers.list(**kwargs)]


@tool()
def container_prune(filters: dict | None = None, host: str | None = None) -> dict:
    """
    Remove all stopped containers to reclaim disk space.

    Only removes containers that are not running — running containers are never affected.
    Use `container_list(all=True)` to preview what would be removed before calling this.
    Valid filter keys: `until` (RFC3339 timestamp or duration like "24h" — removes containers
    stopped before that point), `label` (key or key=value). For a broader cleanup of
    containers plus unused images, networks, and volumes see the `prune_managed` prompt.

    args: filters - Narrow which stopped containers to remove; omit to remove all stopped
    returns: dict - {"ContainersDeleted": [...], "SpaceReclaimed": <bytes>}
    """
    return _get_client(host).containers.prune(filters=filters)


@tool()
def container_start(id_or_name: str, host: str | None = None) -> dict:
    """
    Start an existing stopped container.

    Use this to restart a container that was previously created or stopped without removing it.
    To create and start a new container in one step use `container_run` instead. Calling on
    an already-running container has no effect (the daemon returns 304 and no error is
    raised). To stop then start a running container use `container_restart`.

    args: id_or_name - Container id (full or short) or name
    returns: dict - The container's full inspect payload after starting
    """
    container = _get_client(host).containers.get(id_or_name)
    container.start()
    container.reload()
    return container.attrs


@tool()
def container_stop(id_or_name: str, stop_timeout_seconds: int = 10, host: str | None = None) -> dict:
    """
    Gracefully stop a running container (its configured stop signal, then SIGKILL after a timeout).

    Prefer this over `container_kill` for a clean shutdown: the main process receives the
    container's stop signal (`STOPSIGNAL`, default SIGTERM) and has stop_timeout_seconds to exit
    before the daemon force-kills it. Use `container_restart` to stop and start again in one call,
    or `container_pause` to freeze processes without stopping. When the server runs containerized
    it refuses to stop its own container.

    args:
        id_or_name - The container id or name
        stop_timeout_seconds - Seconds between the stop signal and SIGKILL (default 10)
    returns: dict - The container's attrs after the stop (exit code under State.ExitCode)
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.stop(timeout=stop_timeout_seconds)
    container.reload()
    return container.attrs


@tool()
def container_restart(id_or_name: str, stop_timeout_seconds: int = 10, host: str | None = None) -> dict:
    """
    Restart a container: stop then start again in one call.

    The container receives its configured stop signal (`STOPSIGNAL`, default SIGTERM), SIGKILL
    after stop_timeout_seconds, and is then started. Use `container_stop`/`container_start` to do
    the halves separately. When the server runs containerized it refuses to restart its own
    container.

    args:
        id_or_name - The container id or name
        stop_timeout_seconds - Seconds between the stop signal and SIGKILL (default 10)
    returns: dict - The container's full inspect payload after the restart
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.restart(timeout=stop_timeout_seconds)
    container.reload()
    return container.attrs


@tool()
def container_kill(id_or_name: str, signal: str | None = None, host: str | None = None) -> dict:
    """
    Send a signal to a running container (default SIGKILL — immediate, no graceful shutdown).

    Use it to force-kill a container that ignores `container_stop`, or with `signal` to poke a
    process without stopping it (e.g. SIGHUP for a config reload). For a normal shutdown prefer
    `container_stop`, which sends the container's configured stop signal first. Fails with a
    conflict error if the container is not running. When the server runs containerized it refuses
    to signal its own container.

    args:
        id_or_name - The container id or name
        signal - Signal name or number as a string (e.g. "SIGHUP", "9"); default SIGKILL
    returns: dict - The container's full inspect payload after the signal
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.kill(signal=signal)
    container.reload()
    return container.attrs


@tool()
def container_pause(id_or_name: str, host: str | None = None) -> dict:
    """
    Suspend all processes in a container using the kernel freezer cgroup.

    Unlike sending SIGSTOP, the freezer cgroup suspends processes without their being able
    to observe or intercept the suspension. A paused container keeps its resources (memory,
    open file descriptors) but consumes no CPU. Resume with `container_unpause` —
    `container_exec` fails against a paused container until it is unpaused.

    args: id_or_name - The container id or name
    returns: dict - The container's full inspect payload after pause (State.Paused true)
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.pause()
    container.reload()
    return container.attrs


@tool()
def container_unpause(id_or_name: str, host: str | None = None) -> dict:
    """
    Resume all processes in a paused container (the reverse of `container_pause`).

    Only valid on a paused container — it fails if the container is merely stopped; use
    `container_start` for stopped containers. Processes continue from where they were frozen.

    args: id_or_name - The container id or name
    returns: dict - The container's attrs after unpause (State.Paused becomes false)
    """
    container = _get_client(host).containers.get(id_or_name)
    container.unpause()
    container.reload()
    return container.attrs


@tool()
def container_remove(
    id_or_name: str, volumes: bool = False, link: bool = False, force: bool = False, host: str | None = None
) -> bool:
    """
    Remove a container, deleting its writable layer.

    The image is untouched (`image_remove` deletes images); named volumes are never removed —
    volumes=True only covers anonymous ones. A running container is refused unless force=True,
    which kills it first. When the server runs containerized it refuses to remove its own
    container.

    args:
        id_or_name - The container id or name
        volumes - Also remove anonymous volumes (the CLI's `--volumes`); named volumes persist
        link - Remove the specified link
        force - Kill a running container before removing it (default False: running is an error)
    returns: bool - True after removal completes
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.remove(v=volumes, link=link, force=force)
    return True


@tool()
def container_logs(
    id_or_name: str,
    stdout: bool = True,
    stderr: bool = True,
    timestamps: bool = False,
    tail: int | Literal["all"] = 200,
    since: float | None = None,
    until: float | None = None,
    follow: bool = False,
    limit_lines: int = 200,
    timeout_seconds: float = 30.0,
    host: str | None = None,
) -> str:
    """
    Get the logs of a container: a one-shot snapshot by default, or a bounded live tail with `follow=True`.

    Follow mode returns when `limit_lines` lines are collected, `timeout_seconds` elapses, or the
    container exits, whichever comes first — so the agent can watch live output without blocking
    forever. `limit_lines`/`timeout_seconds` apply only in follow mode; `until` only in snapshot mode.

    Caveat for `ssh://` daemons: docker-py can't cancel an SSH stream, so in follow mode the
    `timeout_seconds` watchdog can't interrupt a fully silent container — use the snapshot mode
    there if you need a hard time bound.

    args:
        id_or_name - The container id or name
        stdout - Include stdout
        stderr - Include stderr
        timestamps - Include timestamps
        tail - Number of lines from the end (default 200), or the literal "all" for everything
        since - Only return logs created after this unix timestamp
        until - Only return logs created before this unix timestamp (snapshot mode only)
        follow - Follow the live log stream instead of returning a snapshot
        limit_lines - Follow mode: max lines to collect before returning (default 200)
        timeout_seconds - Follow mode: max wall-clock seconds before returning what was collected (default 30)
    returns: str - Decoded log output (up to `limit_lines` lines in follow mode)
    """
    container = _get_client(host).containers.get(id_or_name)
    if not follow:
        output = container.logs(
            stdout=stdout,
            stderr=stderr,
            stream=False,
            timestamps=timestamps,
            tail=tail,
            since=since,
            until=until,
        )
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace")
        return str(output)
    stream = container.logs(
        stdout=stdout,
        stderr=stderr,
        stream=True,
        follow=True,
        timestamps=timestamps,
        tail=tail,
        since=since,
    )
    collected: list[str] = []
    # container.logs(stream=True) returns a CancellableStream; a watchdog timer closes its socket
    # on the deadline, which unblocks the iteration even when the container emits nothing.
    timer = threading.Timer(timeout_seconds, lambda: close_stream_quietly(stream))
    timer.start()
    try:
        for chunk in stream:
            text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            for line in text.splitlines():
                collected.append(line)
                if len(collected) >= limit_lines:
                    return "\n".join(collected)
    finally:
        timer.cancel()
        close_stream_quietly(stream)
    return "\n".join(collected)


@tool()
def container_stats(id_or_name: str, host: str | None = None) -> dict:
    """
    Get one point-in-time resource-usage snapshot for a container (non-streaming).

    Returns the raw engine stats payload; CPU percent must be computed from the delta between
    `cpu_stats` and `precpu_stats`. For a pre-computed human-readable summary prefer the
    `docker-stats://{id_or_name}` resource; for a process listing use `container_top`.

    args: id_or_name - The container id or name
    returns: dict - Engine stats payload (read, cpu_stats, precpu_stats, memory_stats, networks,
        pids_stats, ...)
    """
    container = _get_client(host).containers.get(id_or_name)
    # `decode` is only valid with stream=True; a one-shot stream=False read already returns a dict.
    return cast(dict, container.stats(stream=False))


# --- shared read helpers, also used by the docker-logs:// / docker-stats:// resources in resources.py ---

# Default line cap for a one-shot log read so a resource read can't flood the agent's context.
_LOG_TAIL_LINES = 200


def _read_log_tail(id_or_name: str, tail: int = _LOG_TAIL_LINES, host: str | None = None) -> str:
    """Return a bounded, non-streaming tail of a container's combined stdout/stderr logs."""
    container = _get_client(host).containers.get(id_or_name)
    output = container.logs(stdout=True, stderr=True, stream=False, timestamps=False, tail=tail)
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


def _div_mb(value: float) -> float:
    """Bytes -> MiB."""
    return value / (1024 * 1024)


def _summarize_stats(name: str | None, snapshot: dict) -> dict:
    """
    Reduce a one-shot `container.stats` snapshot to a small human-readable summary.

    CPU% is computed from the snapshot's own `cpu_stats`/`precpu_stats` delta (a single stream=False
    read already carries both), matching how `docker stats` derives it. Every field is read
    defensively because the stats shape varies across cgroup v1/v2 and platforms; anything missing
    degrades to 0 rather than raising.
    """
    cpu = snapshot.get("cpu_stats", {}) or {}
    precpu = snapshot.get("precpu_stats", {}) or {}
    cpu_total = (cpu.get("cpu_usage", {}) or {}).get("total_usage", 0) or 0
    precpu_total = (precpu.get("cpu_usage", {}) or {}).get("total_usage", 0) or 0
    cpu_delta = cpu_total - precpu_total
    system_delta = (cpu.get("system_cpu_usage", 0) or 0) - (precpu.get("system_cpu_usage", 0) or 0)
    online = cpu.get("online_cpus") or len((cpu.get("cpu_usage", {}) or {}).get("percpu_usage") or []) or 1
    cpu_percent = (cpu_delta / system_delta) * online * 100.0 if system_delta > 0 and cpu_delta > 0 else 0.0

    mem = snapshot.get("memory_stats", {}) or {}
    usage = mem.get("usage", 0) or 0
    # Match `docker stats`: subtract reclaimable page cache (cgroup v2 inactive_file, v1 cache).
    detail = mem.get("stats", {}) or {}
    cache = detail.get("inactive_file", detail.get("cache", 0)) or 0
    mem_used = max(usage - cache, 0)
    mem_limit = mem.get("limit", 0) or 0
    mem_percent = (mem_used / mem_limit * 100.0) if mem_limit > 0 else 0.0

    nets = snapshot.get("networks", {}) or {}
    net_rx = sum((n.get("rx_bytes", 0) or 0) for n in nets.values())
    net_tx = sum((n.get("tx_bytes", 0) or 0) for n in nets.values())

    blk = (snapshot.get("blkio_stats", {}) or {}).get("io_service_bytes_recursive") or []
    blk_read = sum((e.get("value", 0) or 0) for e in blk if str(e.get("op", "")).lower() == "read")
    blk_write = sum((e.get("value", 0) or 0) for e in blk if str(e.get("op", "")).lower() == "write")

    return {
        "container": name,
        "cpu_percent": round(cpu_percent, 2),
        "mem_used_mb": round(_div_mb(mem_used), 1),
        "mem_limit_mb": round(_div_mb(mem_limit), 1),
        "mem_percent": round(mem_percent, 1),
        "net_rx_mb": round(_div_mb(net_rx), 2),
        "net_tx_mb": round(_div_mb(net_tx), 2),
        "blk_read_mb": round(_div_mb(blk_read), 2),
        "blk_write_mb": round(_div_mb(blk_write), 2),
    }


def _read_stats_summary(id_or_name: str, host: str | None = None) -> dict:
    """
    Return a computed resource-usage summary for a running container.

    Raises RuntimeError if the container isn't running — there is no live cgroup to sample on a
    stopped container, so the `docker-stats://` resource surfaces a clean message instead of a raw
    daemon error.
    """
    container = _get_client(host).containers.get(id_or_name)
    container.reload()
    status = (container.attrs.get("State", {}) or {}).get("Status")
    if status != "running":
        raise RuntimeError(
            f"Container {id_or_name!r} is not running (status: {status or 'unknown'}); "
            f"resource-usage stats require a running container."
        )
    snapshot = cast(dict, container.stats(stream=False))
    return _summarize_stats(container.name, snapshot)


@tool()
def container_top(id_or_name: str, ps_args: str | None = None, host: str | None = None) -> dict:
    """
    List the processes running inside a container (the daemon runs `ps` on the host).

    Works on any running container without executing anything in it, so it needs no shell or `ps`
    binary in the image — unlike `container_exec` with `ps`. Use `container_stats` for resource
    usage rather than process lists. Fails if the container is not running.

    args:
        id_or_name - The container id or name
        ps_args - Extra ps arguments (e.g. "aux"); default is the daemon's standard ps invocation
    returns: dict - {"Titles": [ps column names], "Processes": [[one row of values per process]]}
    """
    container = _get_client(host).containers.get(id_or_name)
    return cast(dict, container.top(ps_args=ps_args))


@tool()
def container_exec(
    id_or_name: str,
    cmd: str | list,
    stdout: bool = True,
    stderr: bool = True,
    stdin: bool = False,
    tty: bool = False,
    privileged: bool = False,
    user: str = "",
    detach: bool = False,
    environment: dict | list | None = None,
    workdir: str | None = None,
    demux: bool = False,
    host: str | None = None,
) -> dict:
    """
    Run a command inside a running container (for a compose service, prefer `compose_exec`).

    Security: when any element of `cmd` is agent-controlled, use an exec-form argv list that does not
    invoke a shell (e.g. `["python", "-V"]`, `["ls", path]`). A string `cmd`, or a shell form like
    `["sh", "-c", template]`, interprets shell metacharacters in the untrusted parts.

    args:
        id_or_name - The container id or name
        cmd - Command to execute (prefer exec-form argv, no shell, when any element is agent-controlled)
        stdout - Attach to stdout
        stderr - Attach to stderr
        stdin - Attach to stdin
        tty - Allocate a pseudo-TTY
        privileged - Run with extended privileges
        user - User to run the command as
        detach - Detach from the exec
        environment - Environment variables
        workdir - Working directory inside the container
        demux - Return stdout and stderr separately
    returns: dict - {"exit_code", "output"}; output is combined stdout+stderr, or a
        [stdout, stderr] pair with demux=True
    """
    container = _get_client(host).containers.get(id_or_name)
    result = container.exec_run(
        cmd,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        tty=tty,
        privileged=privileged,
        user=user,
        detach=detach,
        stream=False,
        socket=False,
        environment=environment,
        workdir=workdir,
        demux=demux,
    )
    output = result.output
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return {"exit_code": result.exit_code, "output": output}


@tool()
def container_commit(
    id_or_name: str,
    repository: str | None = None,
    tag: str | None = None,
    message: str | None = None,
    author: str | None = None,
    pause: bool = True,
    changes: str | list | None = None,
    conf: dict | None = None,
    host: str | None = None,
) -> dict:
    """
    Snapshot a container's current filesystem state as a new image.

    Useful for capturing a debugging state or saving manual changes made inside a container.
    For repeatable builds use `image_build` with a Dockerfile instead; publish the result with
    `image_tag` + `image_push`. The container is paused by default during
    the snapshot to ensure filesystem consistency — set `pause=False` only if the container
    cannot be paused. `changes` accepts Dockerfile instructions to apply on top of the
    snapshot, e.g. `["CMD [\"python\", \"app.py\"]", "ENV FOO=bar"]`.

    args:
        id_or_name - Container id or name to snapshot
        repository - Repository name for the new image, e.g. "myorg/myimage"
        tag - Tag for the new image (default: "latest")
        message - Commit message stored in the image metadata
        author - Author string stored in the image metadata
        pause - Pause the container during commit for consistency (default True)
        changes - Dockerfile instructions (CMD, ENV, EXPOSE, etc.) to apply to the image
        conf - Additional image configuration overrides as a dict
    returns: dict - The new image's full inspect payload (Id is the new image id)
    """
    container = _get_client(host).containers.get(id_or_name)
    image = container.commit(
        repository=repository,
        tag=tag,
        message=message,
        author=author,
        pause=pause,
        changes=changes,
        conf=conf,
    )
    return image.attrs


@tool()
def container_diff(id_or_name: str, host: str | None = None) -> list:
    """
    List filesystem changes a container has made relative to its image.

    Use it to audit what a container wrote before `container_commit` or `container_archive_get`,
    or to debug unexpected writes. Only the writable container layer is compared — files in
    volumes and bind mounts never show up.

    args: id_or_name - The container id or name
    returns: list - Dicts of {"Path", "Kind"}; Kind 0=modified, 1=added, 2=deleted
    """
    container = _get_client(host).containers.get(id_or_name)
    return container.diff()


@tool()
def container_rename(id_or_name: str, name: str, host: str | None = None) -> dict:
    """
    Rename a container in place; its id, state, and configuration are unchanged.

    Use it to free up or claim a container name (names are unique per daemon) — e.g. before
    starting a replacement under the old name. Fails with a conflict error if the new name is
    already taken. Not related to `image_tag`, which names images.

    args:
        id_or_name - The container id or name
        name - The new name; must not be in use by any other container
    returns: dict - The container's full inspect payload after the rename
    """
    container = _get_client(host).containers.get(id_or_name)
    container.rename(name)
    container.reload()
    return container.attrs


@tool()
def container_update(id_or_name: str, updates: dict, host: str | None = None) -> dict:
    """
    Update resource limits on a container without recreating it.

    Changes take effect immediately on Linux (cgroups); not all fields are updatable on
    every platform. Common `updates` keys: `mem_limit` (bytes, e.g. 134217728 for 128 MB),
    `memswap_limit` (memory+swap in bytes; -1 = unlimited), `cpu_shares` (relative weight,
    default 1024), `cpu_period` / `cpu_quota` (microseconds for CFS throttling),
    `cpuset_cpus` (e.g. "0-1"), `restart_policy` (dict with `Name` such as
    "on-failure"/"always"/"unless-stopped" and optional `MaximumRetryCount`). To change
    image, env, or volumes the container must be recreated (`container_remove` +
    `container_run`).

    args:
        id_or_name - Container id or name to update
        updates - Resource fields to update; see description for valid keys
    returns: dict - The container's full inspect payload after the update
    """
    container = _get_client(host).containers.get(id_or_name)
    container.update(**updates)
    container.reload()
    return container.attrs


def _wait_result(
    id_or_name: str,
    until: str,
    *,
    met: bool,
    start: float,
    timed_out: bool = False,
    status_code: int | None = None,
    error: str | None = None,
    health: str | None = None,
    status: str | None = None,
    matched_line: str | None = None,
) -> dict:
    """Build the unified container_wait result snapshot — the same shape for every `until` mode."""
    return {
        "container": id_or_name,
        "until": until,
        "met": met,
        "timed_out": timed_out,
        "status_code": status_code,
        "error": error,
        "health": health,
        "status": status,
        "matched_line": matched_line,
        "waited_seconds": round(time.monotonic() - start, 2),
    }


# Cap each log-match poll's read so a chatty container can't grow the buffer we search unbounded.
_LOG_MATCH_TAIL_LINES = 1000


@tool()
def container_wait(
    id_or_name: str,
    until: Literal["not-running", "next-exit", "removed", "healthy", "log-match"] = "not-running",
    timeout_seconds: float = 600.0,
    poll_interval: float = 2.0,
    pattern: str | None = None,
    regex: bool = False,
    host: str | None = None,
) -> dict:
    """
    Block until a container reaches a condition: stopped, "healthy", or its logs contain a pattern.

    One contract for every mode: never raises on timeout — the result always carries `met` (condition
    reached) and `timed_out`. The stop conditions ("not-running"/"next-exit"/"removed") use the
    daemon's blocking wait and fill `status_code`/`error` (the container's exit info); "healthy" polls
    the container's HEALTHCHECK every `poll_interval`s and fills `health`/`status`; "log-match" polls
    recent logs every `poll_interval`s for `pattern` and fills `matched_line`. For a compose
    project use `compose_wait`; for swarm services use `service_wait`.

    Health semantics: with no HEALTHCHECK defined, once the container is `running` the tool returns
    promptly with `health: null` and `met: false` (false = "not confirmed healthy", not "unhealthy" —
    check `health` to tell them apart). A container that exits before becoming healthy returns its
    terminal `status` and `met: false`.

    Log-match semantics: `pattern` is matched as a **plain substring** by default — safe against any
    input, including adversarial ones. Pass `regex=True` to match `pattern` as a regular expression
    (via `re.search`) instead; only do this with patterns you trust, since a regex with catastrophic
    backtracking run against attacker-influenced log content can exhaust CPU (ReDoS). Checks stdout
    and stderr, most recent lines first within each poll. If the container exits/dies before the
    pattern ever appears, returns promptly with `met=false` (not `timed_out`) — no further logs can
    arrive, so there's nothing to keep polling for.

    args:
        id_or_name - The container id or name
        until - Condition to wait for: "not-running" (default), "next-exit", "removed", "healthy",
                or "log-match" (requires `pattern`)
        timeout_seconds - Max seconds to wait before returning with timed_out=true (default 600)
        poll_interval - "healthy"/"log-match" only: seconds between re-checks (default 2, > 0);
                        capped by the time left so a large value can't push the total wait past the
                        timeout
        pattern - "log-match" only: substring (or, with `regex=True`, a regular expression) to look
                  for in the container's logs
        regex - "log-match" only: treat `pattern` as a regular expression instead of a plain substring
    returns: dict - {"container", "until", "met", "timed_out", "status_code", "error", "health",
                     "status", "matched_line", "waited_seconds"}; stop modes fill status_code/error,
                     "healthy" fills health ("starting"/"healthy"/"unhealthy", or null with no
                     healthcheck) and status, "log-match" fills matched_line when met and status if
                     the container exited without matching.
    """
    if timeout_seconds < 0:
        raise ValueError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
    if until in ("healthy", "log-match") and poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}.")
    if until == "log-match" and not pattern:
        raise ValueError("`pattern` is required when until='log-match'.")
    container = _get_client(host).containers.get(id_or_name)
    start = time.monotonic()
    if until not in ("healthy", "log-match"):
        try:
            # The daemon wait takes whole seconds; round up so a small fractional timeout still
            # waits (int() would truncate 0.5 to an immediate 0s timeout).
            result = cast(dict, container.wait(timeout=math.ceil(timeout_seconds), condition=until))
        except requests.exceptions.ReadTimeout:
            return _wait_result(id_or_name, until, met=False, start=start, timed_out=True)
        return _wait_result(
            id_or_name,
            until,
            met=True,
            start=start,
            status_code=result.get("StatusCode"),
            error=(result.get("Error") or {}).get("Message") if isinstance(result.get("Error"), dict) else None,
        )
    deadline = start + timeout_seconds
    if until == "healthy":
        while True:
            container.reload()
            state = container.attrs.get("State", {}) or {}
            status = state.get("Status")  # created / running / exited / dead / paused / restarting
            health = (state.get("Health") or {}).get("Status")  # starting / healthy / unhealthy, or None

            if health == "healthy":
                return _wait_result(id_or_name, until, met=True, start=start, health=health, status=status)
            if health == "unhealthy":
                return _wait_result(id_or_name, until, met=False, start=start, health=health, status=status)
            if status in ("exited", "dead"):
                # Stopped before ever becoming healthy.
                return _wait_result(id_or_name, until, met=False, start=start, health=health, status=status)
            if health is None and status == "running":
                # No HEALTHCHECK defined: there's nothing to converge to, so don't poll to the timeout.
                return _wait_result(id_or_name, until, met=False, start=start, health=health, status=status)
            # Otherwise still settling (health "starting", or status created/restarting/paused): keep polling.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _wait_result(
                    id_or_name, until, met=False, start=start, timed_out=True, health=health, status=status
                )
            # Bound the sleep by the time left so a large poll_interval can't push past the timeout.
            time.sleep(min(poll_interval, remaining))
    # "log-match"
    matcher = (lambda line: re.search(cast(str, pattern), line)) if regex else (lambda line: pattern in line)
    while True:
        output = container.logs(stdout=True, stderr=True, stream=False, tail=_LOG_MATCH_TAIL_LINES)
        text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        for line in reversed(text.splitlines()):
            if matcher(line):
                return _wait_result(id_or_name, until, met=True, start=start, matched_line=line)
        container.reload()
        status = (container.attrs.get("State", {}) or {}).get("Status")
        if status in ("exited", "dead"):
            # Stopped without ever matching — no further logs will arrive, don't poll to the timeout.
            return _wait_result(id_or_name, until, met=False, start=start, status=status)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _wait_result(id_or_name, until, met=False, start=start, timed_out=True, status=status)
        # Bound the sleep by the time left so a large poll_interval can't push past the timeout.
        time.sleep(min(poll_interval, remaining))


@tool()
def container_export(
    id_or_name: str,
    dest_path: str | None = None,
    overwrite: bool = False,
    max_bytes: int = MAX_PAYLOAD_BYTES,
    host: str | None = None,
) -> bytes | dict:
    """
    Export a container's filesystem as a tar archive: to a file on the server host, or in band.

    The tar is a flat filesystem snapshot with no image metadata or layers — use `image_save` for
    an archive `image_load` can restore, and `container_archive_get` for a single file or
    directory. With `dest_path` the archive streams straight to disk (no byte cap), so it handles large
    containers — the file is written by the server's user, `~` is expanded, and an existing file is
    refused unless `overwrite=True`. Without `dest_path` the tar bytes are returned in band, capped
    at `max_bytes` (default 32 MiB) because MCP base64-encodes them — a fallback for when no
    writable host path exists (e.g. a containerized server without a bind mount).

    args:
        id_or_name - The container id or name
        dest_path - Destination path on the server host; omit to return the bytes in band
        overwrite - Replace dest_path if it already exists (default False)
        max_bytes - In-band mode: abort with ValueError beyond this many bytes (default 32 MiB)
    returns: bytes | dict - the tar bytes (in band), or {"path": <resolved path>, "bytes_written": int}
    """
    container = _get_client(host).containers.get(id_or_name)
    if dest_path is None:
        return join_bounded(cast(Iterable[bytes], container.export()), max_bytes, f"export of {id_or_name}")
    path, written = stream_to_file(cast(Iterable[bytes], container.export()), dest_path, overwrite=overwrite)
    return {"path": str(path), "bytes_written": written}


@tool()
def container_archive_get(
    id_or_name: str, path: str, max_bytes: int = MAX_PAYLOAD_BYTES, host: str | None = None
) -> dict:
    """
    Retrieve a file or directory from a container as a tar archive, returned in band.

    For large paths prefer `container_archive_get_to_file`, which streams to a host path; the in-band
    bytes here are capped (default 32 MiB) because MCP base64-encodes them.

    args:
        id_or_name - The container id or name
        path - Path inside the container
        max_bytes - Abort with ValueError if the archive exceeds this many bytes (defaults to 32 MiB)
    returns: dict - Mapping with archive (bytes) and stat (dict) keys
    """
    container = _get_client(host).containers.get(id_or_name)
    stream, stat = container.get_archive(path)
    return {"archive": join_bounded(stream, max_bytes, f"archive of {id_or_name}:{path}"), "stat": stat}


@tool()
def container_archive_get_to_file(
    id_or_name: str, path: str, dest_path: str, overwrite: bool = False, host: str | None = None
) -> dict:
    """
    Retrieve a file or directory from a container as a tar archive written to a file on the server host.

    File-writing variant of `container_archive_get` — prefer it for anything large, since in-band
    bytes are base64-encoded by MCP. For the whole filesystem use `container_export`. Streams
    straight to disk (no in-band byte cap). The file is written by the server's user; `~` is
    expanded and an existing file is refused unless `overwrite=True`.

    args:
        id_or_name - The container id or name
        path - Path inside the container
        dest_path - Destination path on the server host for the tarball
        overwrite - Replace dest_path if it already exists (default False)
    returns: dict - {"path": <resolved path>, "bytes_written": int, "stat": dict}
    """
    container = _get_client(host).containers.get(id_or_name)
    stream, stat = container.get_archive(path)
    written_path, written = stream_to_file(stream, dest_path, overwrite=overwrite)
    return {"path": str(written_path), "bytes_written": written, "stat": stat}


@tool()
def container_archive_put(
    id_or_name: str,
    path: str,
    data: bytes | None = None,
    from_file: str | None = None,
    host: str | None = None,
) -> bool:
    """
    Upload a tar archive to a path inside a container, from in-band bytes or a file on the server host.

    Inverse of `container_archive_get`: the archive is extracted at `path` inside the container.
    Pass exactly one of `data` (tar bytes in band) or `from_file` (a path on the server host, streamed
    straight to the daemon — preferred for large archives, since in-band bytes are base64-encoded by
    MCP). `from_file` is read by the server's user; `~` is expanded.

    args:
        id_or_name - The container id or name
        path - Destination path inside the container (must already exist)
        data - Tar archive bytes; exactly one of data/from_file
        from_file - Path on the server host to the tar archive to upload; exactly one of data/from_file
    returns: bool - True if the upload succeeded
    """
    if (data is None) == (from_file is None):
        raise ValueError("Pass exactly one of `data` (in-band tar bytes) or `from_file` (a server-host path).")
    container = _get_client(host).containers.get(id_or_name)
    if data is not None:
        return container.put_archive(path, data)
    source = host_read_path(cast(str, from_file))
    with source.open("rb") as handle:
        return container.put_archive(path, handle)

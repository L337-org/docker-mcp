# library of mcp tools relating to container management

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
from docker_mcp.tools.client import _get_client, guard_not_self


class RestartPolicy(TypedDict, total=False):
    """Restart policy for run_container, mirroring the `docker` module's expected dict shape."""

    Name: Literal["no", "always", "on-failure", "unless-stopped"]
    MaximumRetryCount: int  # only meaningful with Name="on-failure"


@tool()
def run_container(
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
) -> dict | str:
    """
    Run a container from an image.

    args:
        image: str - The image to run
        command: str | list - The command to run in the container
        name: str - Name to assign to the container
        detach: bool - Run in the background and return container info
        environment: dict | list - Environment variables to set
        ports: dict - Port mappings, e.g. {'2222/tcp': 3333}
        volumes: dict | list - Volumes to mount
        network: str - Name of the network to attach
        hostname: str - Optional hostname for the container
        user: str - Username or UID to run as
        working_dir: str - Working directory inside the container
        entrypoint: str | list - Entrypoint to override the image default
        restart_policy: RestartPolicy - Restart policy, e.g. {'Name': 'on-failure', 'MaximumRetryCount': 3}
        labels: dict | list - Labels to set on the container
        remove: bool - Remove the container when it exits (only with detach=False)
        auto_remove: bool - Enable auto-removal of the container on daemon side
        privileged: bool - Give extended privileges to the container
        tty: bool - Allocate a pseudo-TTY
        stdin_open: bool - Keep STDIN open
        mem_limit: int | str - Memory limit
        cpu_count: int - Number of CPUs
        extra_kwargs: dict - Additional keyword arguments forwarded to ContainerCollection.run
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
            labels=with_provenance(labels, "run_container"),
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
    result = _get_client().containers.run(image, **kwargs)
    if detach:
        return result.attrs
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    return str(result)


@tool()
def create_container(image: str, command: str | list | None = None, extra_kwargs: dict | None = None) -> dict:
    """
    Create a container without starting it.

    args:
        image: str - The image to use
        command: str | list - The command to run when started
        extra_kwargs: dict - Additional keyword arguments forwarded to ContainerCollection.create
    returns: dict - The created container's attrs
    """
    kwargs = dict(extra_kwargs or {})
    labels = with_provenance(kwargs.get("labels"), "create_container")
    if labels is not None:
        kwargs["labels"] = labels
    container = _get_client().containers.create(image, command=command, **kwargs)
    return container.attrs


@tool()
def get_container(id_or_name: str) -> dict:
    """
    Get a container by id or name.

    args: id_or_name: str - The container id or name
    returns: dict - The container's attrs
    """
    return _get_client().containers.get(id_or_name).attrs


@tool()
def list_containers(
    all: bool = False,
    since: str | None = None,
    before: str | None = None,
    limit: int | None = None,
    filters: dict | None = None,
    sparse: bool = False,
    ignore_removed: bool = False,
    managed_only: bool = False,
) -> list:
    """
    List containers.

    args:
        all: bool - Show all containers, including stopped ones
        since: str - Only show containers created after this id or name
        before: str - Only show containers created before this id or name
        limit: int - Maximum number of results
        filters: dict - Filter by attributes (e.g. status, label)
        sparse: bool - Skip inspect calls and return less detail
        ignore_removed: bool - Ignore containers removed during listing
        managed_only: bool - Only return containers created by this MCP server (filters on the
                             docker-mcp-server.managed label); combines with any `filters` given
    returns: list - A list of container attrs dicts
    """
    if managed_only:
        filters = managed_filter(filters)
    kwargs: dict = {
        "all": all,
        "sparse": sparse,
        "ignore_removed": ignore_removed,
        **drop_none(since=since, before=before, limit=limit, filters=filters),
    }
    return [c.attrs for c in _get_client().containers.list(**kwargs)]


@tool()
def prune_containers(filters: dict | None = None) -> dict:
    """
    Remove stopped containers.

    args: filters: dict - Filters to apply to the prune operation
    returns: dict - Information on deleted containers and reclaimed space
    """
    return _get_client().containers.prune(filters=filters)


@tool()
def start_container(id_or_name: str) -> dict:
    """
    Start a container.

    args: id_or_name: str - The container id or name
    returns: dict - The container's attrs after start
    """
    container = _get_client().containers.get(id_or_name)
    container.start()
    container.reload()
    return container.attrs


@tool()
def stop_container(id_or_name: str, timeout: int = 10) -> dict:
    """
    Stop a container.

    args:
        id_or_name: str - The container id or name
        timeout: int - Seconds to wait before forcing termination
    returns: dict - The container's attrs after stop
    """
    container = _get_client().containers.get(id_or_name)
    guard_not_self(container)
    container.stop(timeout=timeout)
    container.reload()
    return container.attrs


@tool()
def restart_container(id_or_name: str, timeout: int = 10) -> dict:
    """
    Restart a container.

    args:
        id_or_name: str - The container id or name
        timeout: int - Seconds to wait before forcing restart
    returns: dict - The container's attrs after restart
    """
    container = _get_client().containers.get(id_or_name)
    guard_not_self(container)
    container.restart(timeout=timeout)
    container.reload()
    return container.attrs


@tool()
def kill_container(id_or_name: str, signal: str | None = None) -> dict:
    """
    Send a signal to a container.

    args:
        id_or_name: str - The container id or name
        signal: str - Signal to send (defaults to SIGKILL)
    returns: dict - The container's attrs after kill
    """
    container = _get_client().containers.get(id_or_name)
    guard_not_self(container)
    container.kill(signal=signal)
    container.reload()
    return container.attrs


@tool()
def pause_container(id_or_name: str) -> dict:
    """
    Pause all processes in a container.

    args: id_or_name: str - The container id or name
    returns: dict - The container's attrs after pause
    """
    container = _get_client().containers.get(id_or_name)
    guard_not_self(container)
    container.pause()
    container.reload()
    return container.attrs


@tool()
def unpause_container(id_or_name: str) -> dict:
    """
    Resume all processes in a paused container.

    args: id_or_name: str - The container id or name
    returns: dict - The container's attrs after unpause
    """
    container = _get_client().containers.get(id_or_name)
    container.unpause()
    container.reload()
    return container.attrs


@tool()
def remove_container(id_or_name: str, v: bool = False, link: bool = False, force: bool = False) -> bool:
    """
    Remove a container.

    args:
        id_or_name: str - The container id or name
        v: bool - Also remove anonymous volumes
        link: bool - Remove the specified link
        force: bool - Force remove a running container
    returns: bool - True after removal completes
    """
    container = _get_client().containers.get(id_or_name)
    guard_not_self(container)
    container.remove(v=v, link=link, force=force)
    return True


@tool()
def container_logs(
    id_or_name: str,
    stdout: bool = True,
    stderr: bool = True,
    timestamps: bool = False,
    tail: int | Literal["all"] = "all",
    since: float | None = None,
    until: float | None = None,
) -> str:
    """
    Get the logs of a container.

    args:
        id_or_name: str - The container id or name
        stdout: bool - Include stdout
        stderr: bool - Include stderr
        timestamps: bool - Include timestamps
        tail: int | "all" - Number of lines from the end, or the literal "all"
        since: float - Only return logs created after this unix timestamp
        until: float - Only return logs created before this unix timestamp
    returns: str - Decoded log output
    """
    container = _get_client().containers.get(id_or_name)
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


@tool()
def follow_container_logs(
    id_or_name: str,
    limit_lines: int = 200,
    stdout: bool = True,
    stderr: bool = True,
    timestamps: bool = False,
    since: float | None = None,
    timeout_seconds: float = 30.0,
) -> str:
    """
    Tail a container's log stream, bounded by `limit_lines`, `timeout_seconds`, or container exit.

    The call returns when `limit_lines` lines have been collected, `timeout_seconds` elapses, or the
    container exits, whichever comes first. Wraps the streaming logs API of the `docker` module so
    the agent can watch live output without blocking forever. `limit_lines` bounds memory;
    `timeout_seconds` bounds wall-clock time, which matters for a quiet but long-lived container
    that would otherwise never emit the line that lets the call return.

    Caveat for `ssh://` daemons: docker-py cannot cancel an SSH stream, so the `timeout_seconds`
    watchdog can't interrupt a fully silent container there — use `container_logs` (a one-shot,
    non-streaming read) against an SSH daemon if you need a hard time bound.

    args:
        id_or_name: str - The container id or name
        limit_lines: int - Maximum number of lines to collect before returning (default 200)
        stdout: bool - Include stdout
        stderr: bool - Include stderr
        timestamps: bool - Include timestamps
        since: float - Only return logs created after this unix timestamp
        timeout_seconds: float - Maximum wall-clock seconds to follow before returning what was
                                 collected so far (defaults to 30)
    returns: str - Decoded log output containing up to `limit_lines` lines
    """
    container = _get_client().containers.get(id_or_name)
    stream = container.logs(
        stdout=stdout,
        stderr=stderr,
        stream=True,
        follow=True,
        timestamps=timestamps,
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
def container_stats(id_or_name: str) -> dict:
    """
    Get a single resource usage stats snapshot for a container.

    args: id_or_name: str - The container id or name
    returns: dict - Decoded stats snapshot
    """
    container = _get_client().containers.get(id_or_name)
    # `decode` is only valid with stream=True; a one-shot stream=False read already returns a dict.
    return cast(dict, container.stats(stream=False))


# --- shared read helpers, also used by the docker-logs:// / docker-stats:// resources in resources.py ---

# Default line cap for a one-shot log read so a resource read can't flood the agent's context.
_LOG_TAIL_LINES = 200


def _read_log_tail(id_or_name: str, tail: int = _LOG_TAIL_LINES) -> str:
    """Return a bounded, non-streaming tail of a container's combined stdout/stderr logs."""
    container = _get_client().containers.get(id_or_name)
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


def _read_stats_summary(id_or_name: str) -> dict:
    """
    Return a computed resource-usage summary for a running container.

    Raises RuntimeError if the container isn't running — there is no live cgroup to sample on a
    stopped container, so the `docker-stats://` resource surfaces a clean message instead of a raw
    daemon error.
    """
    container = _get_client().containers.get(id_or_name)
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
def container_top(id_or_name: str, ps_args: str | None = None) -> dict:
    """
    Show the running processes inside a container.

    args:
        id_or_name: str - The container id or name
        ps_args: str - Arguments to pass to ps inside the container
    returns: dict - Output of the top command
    """
    container = _get_client().containers.get(id_or_name)
    return cast(dict, container.top(ps_args=ps_args))


@tool()
def exec_in_container(
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
) -> dict:
    """
    Run a command inside a running container.

    Security: when any element of `cmd` is derived from agent-controlled input,
    use an exec-form argv list that does not invoke a shell — e.g. ["python", "-V"]
    or ["ls", path]. A string `cmd`, or a list like ["sh", "-c", template] that
    invokes a shell, will interpret shell metacharacters in the untrusted parts
    and can run unintended commands.

    args:
        id_or_name: str - The container id or name
        cmd: str | list - The command to execute (prefer an exec-form argv list
                          that does not invoke a shell when any element is
                          agent-controlled)
        stdout: bool - Attach to stdout
        stderr: bool - Attach to stderr
        stdin: bool - Attach to stdin
        tty: bool - Allocate a pseudo-TTY
        privileged: bool - Run with extended privileges
        user: str - User to run the command as
        detach: bool - Detach from the exec
        environment: dict | list - Environment variables
        workdir: str - Working directory inside the container
        demux: bool - Return stdout and stderr separately
    returns: dict - Mapping with exit_code and output keys
    """
    container = _get_client().containers.get(id_or_name)
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
def commit_container(
    id_or_name: str,
    repository: str | None = None,
    tag: str | None = None,
    message: str | None = None,
    author: str | None = None,
    pause: bool = True,
    changes: str | list | None = None,
    conf: dict | None = None,
) -> dict:
    """
    Commit a container to an image.

    args:
        id_or_name: str - The container id or name
        repository: str - Repository for the new image
        tag: str - Tag for the new image
        message: str - Commit message
        author: str - Author of the commit
        pause: bool - Pause container during commit
        changes: str | list - Dockerfile instructions to apply
        conf: dict - Configuration overrides
    returns: dict - The new image's attrs
    """
    container = _get_client().containers.get(id_or_name)
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
def container_diff(id_or_name: str) -> list:
    """
    Inspect changes on a container's filesystem.

    args: id_or_name: str - The container id or name
    returns: list - Filesystem changes since the image was created
    """
    container = _get_client().containers.get(id_or_name)
    return container.diff()


@tool()
def rename_container(id_or_name: str, name: str) -> dict:
    """
    Rename a container.

    args:
        id_or_name: str - The container id or name
        name: str - The new name
    returns: dict - The container's attrs after rename
    """
    container = _get_client().containers.get(id_or_name)
    container.rename(name)
    container.reload()
    return container.attrs


@tool()
def resize_container(id_or_name: str, height: int, width: int) -> bool:
    """
    Resize the tty session of a container.

    args:
        id_or_name: str - The container id or name
        height: int - New tty height in characters
        width: int - New tty width in characters
    returns: bool - True after the resize completes
    """
    container = _get_client().containers.get(id_or_name)
    container.resize(height, width)
    return True


@tool()
def update_container(id_or_name: str, updates: dict) -> dict:
    """
    Update resource limits on a running container.

    args:
        id_or_name: str - The container id or name
        updates: dict - Resource fields to update (cpu_shares, mem_limit, restart_policy, etc.)
    returns: dict - The container's attrs after the update
    """
    container = _get_client().containers.get(id_or_name)
    container.update(**updates)
    container.reload()
    return container.attrs


@tool()
def wait_container(
    id_or_name: str,
    timeout: int | None = 600,
    condition: Literal["not-running", "next-exit", "removed"] = "not-running",
) -> dict:
    """
    Block until a container stops, then return its exit info.

    The default `timeout` is finite (600s) so the call can't block the MCP server indefinitely on
    a container that never reaches `condition`. When the timeout is exceeded a RuntimeError is
    raised (poll `get_container` instead, or pass a larger `timeout`). Pass `timeout=None` to
    restore the old unbounded behavior — only do so if you are sure the wait will complete.

    args:
        id_or_name: str - The container id or name
        timeout: int | None - Maximum seconds to wait before raising (default 600; None waits forever)
        condition: "not-running" | "next-exit" | "removed" - State to wait for
    returns: dict - The wait result with StatusCode and Error keys
    """
    container = _get_client().containers.get(id_or_name)
    try:
        return cast(dict, container.wait(timeout=timeout, condition=condition))
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(
            f"Container {id_or_name!r} did not reach condition {condition!r} within {timeout}s. "
            f"Poll `get_container` for its current state, or call `wait_container` with a larger "
            f"`timeout` (or `timeout=None` to wait indefinitely)."
        ) from exc


def _health_result(
    id_or_name: str, *, healthy: bool, health: str | None, status: str | None, start: float, timed_out: bool = False
) -> dict:
    """Build the wait_for_container_healthy result snapshot from the current poll observation."""
    return {
        "container": id_or_name,
        "healthy": healthy,
        "health": health,
        "status": status,
        "waited_seconds": round(time.monotonic() - start, 2),
        "timed_out": timed_out,
    }


@tool()
def wait_for_container_healthy(
    id_or_name: str,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
) -> dict:
    """
    Poll a container until its healthcheck reports `healthy` (or it stops, or the timeout elapses).

    Complements `wait_container`, which waits for a container to *exit*: this waits for a running
    container to become *healthy*. It re-inspects every `poll_interval` seconds and never blocks
    longer than `timeout` (no exception on timeout — the result carries `timed_out: true`).

    Health comes from the container's HEALTHCHECK. If the image/container defines none there is no
    health state to wait for, so once the container is `running` the tool returns promptly with
    `health: null` and `healthy: false` (false meaning "not confirmed healthy", not "unhealthy" —
    check the `health` field to tell them apart). A container that exits before becoming healthy
    returns with its terminal `status` and `healthy: false`.

    args:
        id_or_name: str - The container id or name
        timeout: float - Maximum seconds to wait before returning timed_out (default 120)
        poll_interval: float - Seconds between health re-inspections (default 2; must be > 0). The
                              wait between polls is also capped by the time left, so a large
                              poll_interval can't push the total wait past `timeout`.
    returns: dict - {"container": str, "healthy": bool, "health": str|None, "status": str|None,
                     "waited_seconds": float, "timed_out": bool}; `health` is one of
                     "starting"/"healthy"/"unhealthy" or null when no healthcheck is defined.
    """
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be > 0, got {poll_interval}.")
    container = _get_client().containers.get(id_or_name)
    start = time.monotonic()
    deadline = start + timeout
    while True:
        container.reload()
        state = container.attrs.get("State", {}) or {}
        status = state.get("Status")  # created / running / exited / dead / paused / restarting
        health = (state.get("Health") or {}).get("Status")  # starting / healthy / unhealthy, or None

        if health == "healthy":
            return _health_result(id_or_name, healthy=True, health=health, status=status, start=start)
        if health == "unhealthy":
            return _health_result(id_or_name, healthy=False, health=health, status=status, start=start)
        if status in ("exited", "dead"):
            # Stopped before ever becoming healthy.
            return _health_result(id_or_name, healthy=False, health=health, status=status, start=start)
        if health is None and status == "running":
            # No HEALTHCHECK defined: there's nothing to converge to, so don't poll to the timeout.
            return _health_result(id_or_name, healthy=False, health=health, status=status, start=start)
        # Otherwise still settling (health "starting", or status created/restarting/paused): keep polling.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _health_result(id_or_name, healthy=False, health=health, status=status, start=start, timed_out=True)
        # Bound the sleep by the time left so a large poll_interval can't block past `timeout`.
        time.sleep(min(poll_interval, remaining))


@tool()
def export_container(id_or_name: str, max_bytes: int = MAX_PAYLOAD_BYTES) -> bytes:
    """
    Export a container's filesystem as a tar archive, returned in band.

    For anything but a small container prefer `export_container_to_file`, which streams to a host
    path; the in-band bytes here are capped (default 32 MiB) because MCP base64-encodes them.

    args:
        id_or_name: str - The container id or name
        max_bytes: int - Abort with ValueError if the export exceeds this many bytes (defaults to 32 MiB)
    returns: bytes - The tar archive contents
    """
    container = _get_client().containers.get(id_or_name)
    return join_bounded(cast(Iterable[bytes], container.export()), max_bytes, f"export of {id_or_name}")


@tool()
def export_container_to_file(id_or_name: str, dest_path: str, overwrite: bool = False) -> dict:
    """
    Export a container's filesystem as a tar archive written to a file on the server host.

    Streams straight to disk (no in-band byte cap), so it handles large containers. The file is
    written by the server's user; `~` is expanded and an existing file is refused unless `overwrite=True`.

    args:
        id_or_name: str - The container id or name
        dest_path: str - Destination path on the server host for the tarball
        overwrite: bool - Replace dest_path if it already exists (default False)
    returns: dict - {"path": <resolved path>, "bytes_written": int}
    """
    container = _get_client().containers.get(id_or_name)
    path, written = stream_to_file(cast(Iterable[bytes], container.export()), dest_path, overwrite=overwrite)
    return {"path": str(path), "bytes_written": written}


@tool()
def get_container_archive(id_or_name: str, path: str, max_bytes: int = MAX_PAYLOAD_BYTES) -> dict:
    """
    Retrieve a file or directory from a container as a tar archive, returned in band.

    For large paths prefer `get_container_archive_to_file`, which streams to a host path; the in-band
    bytes here are capped (default 32 MiB) because MCP base64-encodes them.

    args:
        id_or_name: str - The container id or name
        path: str - Path inside the container
        max_bytes: int - Abort with ValueError if the archive exceeds this many bytes (defaults to 32 MiB)
    returns: dict - Mapping with archive (bytes) and stat (dict) keys
    """
    container = _get_client().containers.get(id_or_name)
    stream, stat = container.get_archive(path)
    return {"archive": join_bounded(stream, max_bytes, f"archive of {id_or_name}:{path}"), "stat": stat}


@tool()
def get_container_archive_to_file(id_or_name: str, path: str, dest_path: str, overwrite: bool = False) -> dict:
    """
    Retrieve a file or directory from a container as a tar archive written to a file on the server host.

    Streams straight to disk (no in-band byte cap). The file is written by the server's user; `~` is
    expanded and an existing file is refused unless `overwrite=True`.

    args:
        id_or_name: str - The container id or name
        path: str - Path inside the container
        dest_path: str - Destination path on the server host for the tarball
        overwrite: bool - Replace dest_path if it already exists (default False)
    returns: dict - {"path": <resolved path>, "bytes_written": int, "stat": dict}
    """
    container = _get_client().containers.get(id_or_name)
    stream, stat = container.get_archive(path)
    written_path, written = stream_to_file(stream, dest_path, overwrite=overwrite)
    return {"path": str(written_path), "bytes_written": written, "stat": stat}


@tool()
def put_container_archive(id_or_name: str, path: str, data: bytes) -> bool:
    """
    Upload a tar archive to a path inside a container.

    For a tarball already on the server host, prefer `put_container_archive_from_file` — it streams
    from disk instead of carrying the (base64-encoded) bytes through the MCP protocol.

    args:
        id_or_name: str - The container id or name
        path: str - Destination path inside the container
        data: bytes - Tar archive bytes
    returns: bool - True if the upload succeeded
    """
    container = _get_client().containers.get(id_or_name)
    return container.put_archive(path, data)


@tool()
def put_container_archive_from_file(id_or_name: str, path: str, file_path: str) -> bool:
    """
    Upload a tar archive from a file on the server host to a path inside a container.

    Streams the file straight to the daemon, so it handles large archives that would be impractical
    to pass in band via `put_container_archive`. `file_path` is read by the server's user; `~` is expanded.

    args:
        id_or_name: str - The container id or name
        path: str - Destination path inside the container (must already exist)
        file_path: str - Path on the server host to the tar archive to upload
    returns: bool - True if the upload succeeded
    """
    container = _get_client().containers.get(id_or_name)
    source = host_read_path(file_path)
    with source.open("rb") as handle:
        return container.put_archive(path, handle)

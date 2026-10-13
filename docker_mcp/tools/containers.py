"""Tools for containers.

Their lifecycle, and reaching inside a running one for logs, execution, stats and files.
"""

# library of mcp tools relating to container management

import math
import re
import threading
import time
from collections.abc import Iterable
from typing import Any, Literal, TypedDict, cast

import docker.errors
import docker.models.containers
import requests.exceptions

from docker_mcp.exceptions import ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import (
    MAX_PAYLOAD_BYTES,
    as_byte_chunks,
    close_stream_quietly,
    drop_none,
    open_host_read_file,
    join_bounded,
    stream_to_file,
)
from docker_mcp.tools.system import _get_client, guard_not_self


class RestartPolicy(TypedDict, total=False):
    """Restart policy for container_run, mirroring the `docker` module's expected dict shape.

    Attributes:
        Name: the policy name the docker module expects.
        MaximumRetryCount: how many restarts to attempt, where the policy uses one.
    """

    Name: Literal["no", "always", "on-failure", "unless-stopped"]
    MaximumRetryCount: int  # only meaningful with Name="on-failure"


def _reread(container: docker.models.containers.Container) -> dict:
    """Re-read a container after acting on it, allowing for one that `auto_remove` has already deleted.

    Docker deletes an `auto_remove` container as soon as it stops, so a re-read straight after starting,
    stopping or signalling one can find it gone although the action worked. Reporting `NotFound` then
    would tell the agent the action failed and invite it to try again.

    Args:
        container: the docker-py Container acted on, as fetched before the action

    Returns:
        dict: the inspect payload after the action; for an `auto_remove` container already gone, the one
            fetched before it, with `removed_on_exit: true`

    Raises:
        docker.errors.NotFound: the container is gone and was not `auto_remove`, so something else removed it
    """
    try:
        container.reload()
    except docker.errors.NotFound:
        if not (container.attrs.get("HostConfig") or {}).get("AutoRemove"):
            raise
        return {**container.attrs, "removed_on_exit": True}
    return container.attrs


@tool()
def container_run(  # noqa: DOC101,DOC103,DOC501,DOC503
    image: str,
    command: str | list | None = None,
    name: str | None = None,
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
    auto_remove: bool = False,
    privileged: bool = False,
    tty: bool = False,
    stdin_open: bool = False,
    mem_limit: int | str | None = None,
    cpu_count: int | None = None,
    extra_kwargs: dict | None = None,
    host: str | None = None,
) -> dict:
    """
    Run a container from an image (create and start in one call, like `docker run`).

    Returns once the container has started; it never waits for it to finish. For a one-shot command,
    `system_wait(kind="container", until="not-running")` gives the exit code, `container_logs` the
    output and `container_remove` the cleanup. Use `container_create` to prepare a container without
    starting it, or `container_exec` to run a command in one that already exists. Created containers
    are stamped with provenance labels.

    Args:
        environment: Environment variables, as `{"KEY": "value"}` or a list of "KEY=value" strings
        ports: Port mappings, e.g. {'2222/tcp': 3333}
        volumes: Volumes to mount, as `{"/host/path": {"bind": "/in/container", "mode": "rw"}}`
            or a list of "host:container:mode" strings
        user: Username or UID to run as
        restart_policy: Restart policy, e.g. {'Name': 'on-failure', 'MaximumRetryCount': 3}
        labels: Labels to set on the container
        auto_remove: Have the daemon delete the container when it exits, which also deletes its logs and exit
            code: leave it off when either is wanted, and call `container_remove` afterwards
        privileged: Give extended privileges to the container
        mem_limit: Memory limit: bytes as an int, or a units string ("100000b", "1000k", "128m", "1g")
        extra_kwargs: Additional keyword arguments forwarded to ContainerCollection.run (call
            `docs_lookup(section="containers")` for the full accepted set)

    Returns:
        dict: The started container's full inspect payload; an `auto_remove` container already gone returns
            the payload from before the start, with `removed_on_exit: true`
    """
    # Always detached: a blocking run waits for the image to finish, with no bound, and an image that
    # never exits would hang the call for good. `system_wait` is how a caller waits, with a timeout.
    kwargs: dict = {
        "detach": True,
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
            labels=labels,
            mem_limit=mem_limit,
            cpu_count=cpu_count,
        ),
    }
    for key, value in {
        "auto_remove": auto_remove,
        "privileged": privileged,
        "tty": tty,
        "stdin_open": stdin_open,
    }.items():
        if value:
            kwargs[key] = value
    if extra_kwargs:
        # Forwarded after the defaults, so these two would bring back the blocking run by the side door.
        blocked = sorted({"detach", "remove"} & set(extra_kwargs))
        if blocked:
            raise ToolInputError(
                f"container_run does not take {blocked} in extra_kwargs: it always returns once the container "
                "starts. Wait with system_wait(kind='container'), read the output with container_logs, and clean "
                "up with container_remove or auto_remove=True."
            )
        kwargs.update(extra_kwargs)
    # Stamped after extra_kwargs, matching container_create/service_create. Stamping before the
    # update let extra_kwargs={"labels": ...} replace the whole dict and drop the managed labels,
    # which silently hid the container from managed_only listings and prune_managed. A caller still
    # wins on an individual key collision, which is the documented behaviour of with_provenance.
    provenance = with_provenance(kwargs.get("labels"), "container_run")
    if provenance is not None:
        kwargs["labels"] = provenance
    container = _get_client(host).containers.run(image, **kwargs)
    # docker-py's run returns the object it fetched before starting it, so its attrs still say "created";
    # re-read it, as container_start does, so the payload describes the started container.
    return _reread(container)


@tool()
def container_create(  # noqa: DOC101,DOC103
    image: str, command: str | list | None = None, extra_kwargs: dict | None = None, host: str | None = None
) -> dict:
    """
    Create a container from an image without starting it.

    Use this when you need to configure a container (with `extra_kwargs`) before its first
    start, or want creation and start as separate observable steps. For the common case of
    create-then-start-immediately use `container_run` instead - it does both in one call.
    Start the created container with `container_start`. Common `extra_kwargs` keys: `name`
    (str), `environment` (list of "KEY=VAL" or dict), `ports` (dict, e.g. `{"80/tcp": 8080}`),
    `volumes` (dict, e.g. `{"/host/path": {"bind": "/container/path", "mode": "rw"}}`), `labels`
    (dict). For anything else docker-py's `ContainerCollection.create` accepts, call
    `docs_lookup(section="containers")` rather than guessing a key name.

    Args:
        image: Image to create the container from, e.g. "nginx:alpine"
        command: Override the image's default command; string or list of strings
        extra_kwargs: Additional docker-py ContainerCollection.create keyword arguments

    Returns:
        dict: The created container's full inspect payload (not yet running)
    """
    kwargs = dict(extra_kwargs or {})
    labels = with_provenance(kwargs.get("labels"), "container_create")
    if labels is not None:
        kwargs["labels"] = labels
    container = _get_client(host).containers.create(image, command=command, **kwargs)
    return container.attrs


@tool()
def container_inspect(id_or_name: str, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Return the full inspect detail for a single container.

    Use this when you need complete information about one container - config, state,
    network settings, mounts, environment variables, and resource limits. To enumerate many
    containers use `container_list` instead (same payload per container by default; abridged
    with sparse=True). For just logs or stats use `container_logs` / `container_stats`.

    Returns:
        dict: Full container inspect attrs (equivalent to `docker inspect`)
    """
    return _get_client(host).containers.get(id_or_name).attrs


@tool()
def container_list(  # noqa: DOC101,DOC103,DOC501,DOC503
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

    Args:
        all: Show all containers, including stopped ones (default False: running only)
        since: Only show containers created after this id or name
        before: Only show containers created before this id or name
        limit: Maximum number of results
        filters: Filter by attributes (e.g. status, label)
        sparse: Skip inspect calls and return less detail
        ignore_removed: Ignore containers removed during listing; inert when `sparse=True`, which
            skips the inspect calls that would fail
        managed_only: Only return containers created by this MCP server (filters on the docker-mcp-server.managed
            label); combines with any `filters` given

    Returns:
        list: One dict per container: full inspect payloads by default (each match is inspected, like
            `container_inspect`); sparse=True skips the per-container inspect calls and returns the daemon's abridged
            list entries instead
    """
    if managed_only:
        filters = managed_filter(filters)
    # `since`/`before` go in `filters`, not in the top-level kwargs docker-py still forwards to
    # `GET /containers/json` as query params of the same name. moby dropped those query params from
    # that handler: `getContainersJSON` now builds its ContainerListOptions from `all`, `size`,
    # `limit` and `filters` only, with no version gate, and the Engine API spec has documented
    # exactly those four since v1.51. So the top-level spelling is on its way to being silently
    # ignored - the caller asks for a window and gets the whole list back, with no error to notice.
    # The filter spelling is the documented one and has been in moby's `acceptedPsFilterTags` since
    # 1.13 (API v1.25), below anything this server can reach, so it is safe on every daemon. Do not
    # pass them as top-level kwargs again.
    for key, value in (("since", since), ("before", before)):
        if value is None:
            continue
        clash = (filters or {}).get(key)
        if clash is not None and clash != value:
            raise ToolInputError(
                f"`{key}` was given twice and the two disagree: {value!r} as an argument, "
                f"{clash!r} in `filters`. Pass one or the other."
            )
        filters = {**(filters or {}), key: value}
    kwargs: dict = {
        "all": all,
        "sparse": sparse,
        "ignore_removed": ignore_removed,
        **drop_none(limit=limit, filters=filters),
    }
    return [c.attrs for c in _get_client(host).containers.list(**kwargs)]


@tool()
def container_prune(filters: dict | None = None, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Remove all stopped containers to reclaim disk space.

    Only removes containers that are not running - running containers are never affected.
    Use `container_list(all=True)` to preview what would be removed before calling this.
    Valid filter keys: `until` (RFC3339 timestamp or duration like "24h" - removes containers
    stopped before that point), `label` (key or key=value). For a broader cleanup of
    containers plus unused images, networks, and volumes see the `prune_managed` prompt.

    Args:
        filters: Narrow which stopped containers to remove; omit to remove all stopped

    Returns:
        dict: {"ContainersDeleted": [...], "SpaceReclaimed": <bytes>}
    """
    return _get_client(host).containers.prune(filters=filters)


@tool()
def container_start(id_or_name: str, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Start an existing stopped container.

    Use this to restart a container that was previously created or stopped without removing it.
    To create and start a new container in one step use `container_run` instead. Calling on
    an already-running container has no effect (the daemon returns 304 and no error is
    raised). To stop then start a running container use `container_restart`.

    Returns:
        dict: The container's full inspect payload after starting; for an `auto_remove` container already
            gone, the one from before, with `removed_on_exit: true`
    """
    container = _get_client(host).containers.get(id_or_name)
    container.start()
    return _reread(container)


@tool()
def container_stop(  # noqa: DOC101,DOC103
    id_or_name: str,
    stop_timeout_seconds: int = 10,
    host: str | None = None,
) -> dict:
    """
    Gracefully stop a running container (its configured stop signal, then SIGKILL after a timeout).

    Prefer this over `container_kill` for a clean shutdown: the main process receives the
    container's stop signal (`STOPSIGNAL`, default SIGTERM) and has stop_timeout_seconds to exit
    before the daemon force-kills it. Use `container_restart` to stop and start again in one call,
    or `container_pause` to freeze processes without stopping. When the server runs containerized
    it refuses to stop its own container.

    Args:
        stop_timeout_seconds: Seconds between the stop signal and SIGKILL

    Returns:
        dict: The container's full inspect payload after the stop (exit code under State.ExitCode); for an
            `auto_remove` container, which the stop deletes, the one from before, with `removed_on_exit: true`
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.stop(timeout=stop_timeout_seconds)
    return _reread(container)


@tool()
def container_restart(  # noqa: DOC101,DOC103
    id_or_name: str,
    stop_timeout_seconds: int = 10,
    host: str | None = None,
) -> dict:
    """
    Restart a container: stop then start again in one call.

    The container receives its configured stop signal (`STOPSIGNAL`, default SIGTERM), SIGKILL
    after stop_timeout_seconds, and is then started. Use `container_stop`/`container_start` to do
    the halves separately. When the server runs containerized it refuses to restart its own
    container.

    Args:
        stop_timeout_seconds: Seconds between the stop signal and SIGKILL

    Returns:
        dict: The container's full inspect payload after the restart; for an `auto_remove` container already
            gone, the one from before, with `removed_on_exit: true`
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.restart(timeout=stop_timeout_seconds)
    return _reread(container)


@tool()
def container_kill(id_or_name: str, signal: str | None = None, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Send a signal to a running container (default SIGKILL - immediate, no graceful shutdown).

    Use it to force-kill a container that ignores `container_stop`, or with `signal` to poke a
    process without stopping it (e.g. SIGHUP for a config reload). For a normal shutdown prefer
    `container_stop`, which sends the container's configured stop signal first. Fails with a
    conflict error if the container is not running. When the server runs containerized it refuses
    to signal its own container.

    Args:
        signal: Signal name or number as a string (e.g. "SIGHUP", "9"); default SIGKILL

    Returns:
        dict: The container's full inspect payload after the signal; for an `auto_remove` container already
            gone, the one from before, with `removed_on_exit: true`
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.kill(signal=signal)
    return _reread(container)


@tool()
def container_pause(id_or_name: str, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Suspend all processes in a container using the kernel freezer cgroup.

    Unlike sending SIGSTOP, the freezer cgroup suspends processes without their being able
    to observe or intercept the suspension. A paused container keeps its resources (memory,
    open file descriptors) but consumes no CPU. Resume with `container_unpause` -
    `container_exec` fails against a paused container until it is unpaused.

    Returns:
        dict: The container's full inspect payload after pause (State.Paused true)
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.pause()
    container.reload()
    return container.attrs


@tool()
def container_unpause(id_or_name: str, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Resume all processes in a paused container (the reverse of `container_pause`).

    Only valid on a paused container - it fails if the container is merely stopped; use
    `container_start` for stopped containers. Processes continue from where they were frozen.

    Returns:
        dict: The container's full inspect payload after unpause (State.Paused becomes false)
    """
    container = _get_client(host).containers.get(id_or_name)
    container.unpause()
    container.reload()
    return container.attrs


@tool()
def container_remove(  # noqa: DOC101,DOC103
    id_or_name: str, volumes: bool = False, link: bool = False, force: bool = False, host: str | None = None
) -> bool:
    """
    Remove a container, deleting its writable layer.

    The image is untouched (`image_remove` deletes images); named volumes are never removed -
    volumes=True only covers anonymous ones. A running container is refused unless force=True,
    which kills it first. When the server runs containerized it refuses to remove its own
    container.

    Args:
        volumes: Also remove anonymous volumes (the CLI's `--volumes`); named volumes persist
        link: Remove the legacy link named `id_or_name`, not the container
        force: Kill a running container before removing it (default False: running is an error)

    Returns:
        bool: True after removal completes
    """
    container = _get_client(host).containers.get(id_or_name)
    guard_not_self(container, host=host)
    container.remove(v=volumes, link=link, force=force)
    return True


def _read_bounded_container_logs(container: Any, what: str, **log_kwargs: Any) -> str:
    """Read a container's logs incrementally and return them decoded, capped at MAX_PAYLOAD_BYTES.

    `stream=True` with `follow=False` is what makes the cap effective: docker-py returns a
    generator that terminates at EOF, so `join_bounded` can abort part-way through. Passing
    `stream=False` instead would have docker-py buffer the whole payload before returning, so any
    cap applied afterwards would measure memory that had already been committed. Raises ToolInputError
    when the cap is hit, matching `service_logs`.

    Args:
        container: the container to read from
        what: what is being read, for the error message
        **log_kwargs: forwarded to ``container.logs``

    Returns:
        str: the decoded logs, capped at MAX_PAYLOAD_BYTES
    """
    # as_byte_chunks handles both shapes: docker-py returns a stream for stream=True today, and a
    # whole-payload return would be yielded as one chunk rather than iterated into digit soup.
    output = container.logs(stream=True, follow=False, **log_kwargs)
    # The cap here is fixed, so the caller cannot raise it; what they can do is ask for less.
    raw = join_bounded(
        as_byte_chunks(output),
        MAX_PAYLOAD_BYTES,
        what,
        remedy="Request fewer lines with `tail`, or narrow the window with `since`/`until`.",
    )
    return raw.decode("utf-8", errors="replace")


@tool()
def container_logs(  # noqa: DOC101,DOC103
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
    container exits, whichever comes first - so the agent can watch live output without blocking
    forever. `limit_lines`/`timeout_seconds` apply only in follow mode; `until` only in snapshot mode.

    Snapshot mode is capped at 32 MiB and raises ToolInputError past it, so a noisy container can't
    exhaust the server's memory; `service_logs` caps the same way and lets the caller raise it.
    Prefer an integer `tail`, or `since`, over `tail="all"` on a long-running container: "all" is
    safe but will abort on the cap rather than returning a partial answer, and a large result can
    still exceed the agent's context.

    Caveat for `ssh://` daemons: docker-py can't cancel an SSH stream, so in follow mode the
    `timeout_seconds` watchdog can't interrupt a fully silent container - use the snapshot mode
    there if you need a hard time bound.

    Args:
        tail: Number of lines from the end, or the literal "all" for everything
        since: Only return logs created after this unix timestamp
        until: Only return logs created before this unix timestamp (snapshot mode only)
        follow: Follow the live log stream instead of returning a snapshot
        limit_lines: Follow mode: max lines to collect before returning
        timeout_seconds: Follow mode: max wall-clock seconds before returning what was collected

    Returns:
        str: Decoded log output (up to `limit_lines` lines in follow mode). Raises ToolInputError in snapshot mode if
            the logs exceed 32 MiB.
    """
    container = _get_client(host).containers.get(id_or_name)
    if not follow:
        return _read_bounded_container_logs(
            container,
            f"logs of container {id_or_name}",
            stdout=stdout,
            stderr=stderr,
            timestamps=timestamps,
            tail=tail,
            since=since,
            until=until,
        )
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
def container_stats(id_or_name: str, one_shot: bool = False, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Get one point-in-time resource-usage snapshot for a container (non-streaming).

    Returns the raw engine stats payload; CPU percent must be computed from the delta between
    `cpu_stats` and `precpu_stats`. By default the daemon collects two cycles before answering,
    which is what fills `precpu_stats` - so the call takes about a second. `one_shot=True` returns
    after a single collection instead, leaving `precpu_stats` zeroed and CPU percent uncomputable;
    use it when only `memory_stats`/`pids_stats` matter. For a pre-computed human-readable summary
    prefer the `docker-stats://{id_or_name}` resource; for a process listing use `container_top`.

    Args:
        one_shot: Skip the second collection cycle for a faster answer, at the cost of an empty
            `precpu_stats` (so no CPU percent); needs daemon API v1.41+

    Returns:
        dict: Engine stats payload (read, cpu_stats, precpu_stats, memory_stats, networks, pids_stats, ...)
    """
    container = _get_client(host).containers.get(id_or_name)
    # `decode` is only valid with stream=True; a one-shot stream=False read already returns a dict.
    # `one_shot` is passed only when set: docker-py version-checks the parameter on `is not None`,
    # so a literal one_shot=False would newly raise InvalidVersion against a pre-v1.41 daemon that
    # the default call has always worked on.
    extra = {"one_shot": True} if one_shot else {}
    return cast(dict, container.stats(stream=False, **extra))


# --- shared read helpers, also used by the docker-logs:// / docker-stats:// resources in resources.py ---

# Default line cap for a one-shot log read so a resource read can't flood the agent's context.
_LOG_TAIL_LINES = 200


def _read_log_tail(id_or_name: str, tail: int = _LOG_TAIL_LINES, host: str | None = None) -> str:
    """Return a bounded tail of a container's combined stdout/stderr logs.

    `tail` bounds the line count, but a single pathological line is unbounded on its own, so the
    read also goes through the same MAX_PAYLOAD_BYTES cap as `container_logs`.

    Args:
        id_or_name: the container to read
        tail: how many lines to return
        host: the host label to target, or None for the default

    Returns:
        str: the combined stdout and stderr tail, bounded
    """
    container = _get_client(host).containers.get(id_or_name)
    return _read_bounded_container_logs(
        container, f"logs of container {id_or_name}", stdout=True, stderr=True, timestamps=False, tail=tail
    )


def _div_mb(value: float) -> float:
    """Bytes -> MiB.

    Args:
        value: a size in bytes

    Returns:
        float: the same size in MiB
    """
    return value / (1024 * 1024)


def _summarize_stats(name: str | None, snapshot: dict) -> dict:
    """Reduce a one-shot `container.stats` snapshot to a small human-readable summary.

    CPU% is computed from the snapshot's own `cpu_stats`/`precpu_stats` delta (a single stream=False
    read already carries both), matching how `docker stats` derives it. Every field is read
    defensively because the stats shape varies across cgroup v1/v2 and platforms; anything missing
    degrades to 0 rather than raising.

    Args:
        name: the container's name, or None
        snapshot: one ``container.stats`` sample

    Returns:
        dict: a small human-readable summary of that sample
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
    """Return a computed resource-usage summary for a running container.

    Raises ToolInputError if the container isn't running - there is no live cgroup to sample on a
    stopped container, so the `docker-stats://` resource surfaces a clean message instead of a raw
    daemon error.

    Args:
        id_or_name: the container to sample
        host: the host label to target, or None for the default

    Returns:
        dict: the computed resource-usage summary

    Raises:
        ToolInputError: the container is not running, so there is nothing to sample.
    """
    container = _get_client(host).containers.get(id_or_name)
    container.reload()
    status = (container.attrs.get("State", {}) or {}).get("Status")
    if status != "running":
        raise ToolInputError(
            f"Container {id_or_name!r} is not running (status: {status or 'unknown'}); "
            f"resource-usage stats require a running container."
        )
    snapshot = cast(dict, container.stats(stream=False))
    return _summarize_stats(container.name, snapshot)


@tool()
def container_top(id_or_name: str, ps_args: str | None = None, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    List the processes running inside a container (the daemon runs `ps` on the host).

    Works on any running container without executing anything in it, so it needs no shell or `ps`
    binary in the image - unlike `container_exec` with `ps`. Use `container_stats` for resource
    usage rather than process lists. Fails if the container is not running.

    Args:
        ps_args: Extra ps arguments (e.g. "aux"); default is the daemon's standard ps invocation

    Returns:
        dict: {"Titles": [ps column names], "Processes": [[one row of values per process]]}
    """
    container = _get_client(host).containers.get(id_or_name)
    return cast(dict, container.top(ps_args=ps_args))


# container_exec's default bound. A command run this way is expected to finish; anything longer passes its
# own `timeout_seconds`.
_EXEC_TIMEOUT_SECONDS = 120.0


@tool()
def container_exec(  # noqa: DOC101,DOC103,DOC501,DOC503
    id_or_name: str,
    cmd: str | list,
    stdout: bool = True,
    stderr: bool = True,
    stdin: bool = False,
    tty: bool = False,
    privileged: bool = False,
    user: str = "",
    environment: dict | list | None = None,
    workdir: str | None = None,
    demux: bool = False,
    timeout_seconds: float = _EXEC_TIMEOUT_SECONDS,
    max_bytes: int = MAX_PAYLOAD_BYTES,
    host: str | None = None,
) -> dict:
    """
    Run a command inside a running container and return its output (for a compose service, prefer `compose_exec`).

    Waits for the command, bounded by `timeout_seconds` (default 120). A command still running then is
    reported with `timed_out: true` and whatever output arrived. Output past `max_bytes` (default 32 MiB)
    is cut off with `truncated: true`, and the call returns at once rather than waiting for the command.
    A command still running then has a null `exit_code` and keeps running in the container, because the
    Engine has no way to stop an exec: `container_top` shows it, and another `container_exec` can kill it. For a
    long-running or background job, start a container with `container_run` and wait with `system_wait`.

    Security: when any element of `cmd` is agent-controlled, use an exec-form argv list that does not
    invoke a shell (e.g. `["python", "-V"]`, `["ls", path]`). A string `cmd`, or a shell form like
    `["sh", "-c", template]`, interprets shell metacharacters in the untrusted parts.

    Args:
        cmd: Command to execute (prefer exec-form argv, no shell, when any element is agent-controlled)
        privileged: Run with extended privileges
        environment: Environment variables, as `{"KEY": "value"}` or a list of "KEY=value" strings
        workdir: Working directory inside the container
        demux: Return stdout and stderr separately
        timeout_seconds: Wall-clock seconds to wait for the command before returning with timed_out=true
        max_bytes: Most bytes of output to keep, stdout and stderr together

    Returns:
        dict: {"exit_code", "output", "timed_out", "truncated", "exec_id"}; output is combined stdout+stderr, or a
            [stdout, stderr] pair with demux=True; exit_code is null when the command had not finished
    """
    # Thread.join rejects NaN and anything past threading.TIMEOUT_MAX, and it runs only once the command has
    # started, so a bound it would refuse is refused here instead, before anything runs.
    if not (math.isfinite(timeout_seconds) and 0 < timeout_seconds <= threading.TIMEOUT_MAX):
        raise ToolInputError(
            f"timeout_seconds must be a finite number above 0 and at most {threading.TIMEOUT_MAX:.0f}, "
            f"got {timeout_seconds}."
        )
    if max_bytes <= 0:
        raise ToolInputError(f"max_bytes must be > 0, got {max_bytes}.")
    client = _get_client(host)
    container = client.containers.get(id_or_name)
    # Not Container.exec_run: it has no timeout, and its non-blocking mode discards the exec id and returns
    # neither output nor exit code. The documented APIClient calls give a stream this can bound.
    exec_id = client.api.exec_create(
        container.id,
        cmd,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        tty=tty,
        privileged=privileged,
        user=user,
        environment=environment,
        workdir=workdir,
    )["Id"]
    # With stream=True, exec_start returns a CancellableStream of chunks (or of (stdout, stderr) pairs).
    stream = cast(Iterable[Any], client.api.exec_start(exec_id, tty=tty, stream=True, demux=demux))
    buffers = (bytearray(), bytearray())
    lock = threading.Lock()
    state: dict[str, Any] = {"truncated": False, "error": None}

    def read() -> None:
        try:
            for chunk in stream:
                parts = zip(buffers, chunk, strict=True) if demux else ((buffers[0], chunk),)
                with lock:
                    for buffer, data in parts:
                        if not data:
                            continue
                        room = max_bytes - len(buffers[0]) - len(buffers[1])
                        buffer += data[: max(room, 0)]
                        if len(data) > room:
                            state["truncated"] = True
                            return
        except Exception as exc:  # noqa: BLE001 - handed to the caller below, unless the close caused it
            state["error"] = exc

    # The read runs in a thread the call waits on for at most `timeout_seconds`, so the bound holds on every
    # transport: closing the stream ends the read on a local or TCP daemon, but docker-py cannot close an
    # ssh:// stream, where the thread instead stays blocked until the command ends or the connection drops.
    reader = threading.Thread(target=read, name=f"container_exec {exec_id[:12]}", daemon=True)
    reader.start()
    reader.join(timeout_seconds)
    timed_out = reader.is_alive()
    close_stream_quietly(stream)
    if not timed_out and state["error"] is not None:
        raise state["error"]
    with lock:
        out = buffers[0].decode("utf-8", errors="replace")
        err = buffers[1].decode("utf-8", errors="replace")
    # Still running after a timeout or a cut-off read, in which case the Engine reports no exit code yet.
    exit_code = None if timed_out else client.api.exec_inspect(exec_id).get("ExitCode")
    return {
        "exit_code": exit_code,
        "output": [out, err] if demux else out,
        "timed_out": timed_out,
        "truncated": state["truncated"],
        "exec_id": exec_id,
    }


@tool()
def container_commit(  # noqa: DOC101,DOC103
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
    the snapshot to ensure filesystem consistency - set `pause=False` only if the container
    cannot be paused. `changes` accepts Dockerfile instructions to apply on top of the
    snapshot, e.g. `["CMD [\"python\", \"app.py\"]", "ENV FOO=bar"]`.

    Args:
        repository: Repository name for the new image, e.g. "myorg/myimage"
        tag: Tag for the new image (default: "latest")
        message: Commit message stored in the image metadata
        pause: Pause the container during commit for consistency
        changes: Dockerfile instructions (CMD, ENV, EXPOSE, etc.) to apply to the image
        conf: Additional image configuration overrides as a dict

    Returns:
        dict: The new image's full inspect payload (Id is the new image id)
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
def container_diff(id_or_name: str, host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List filesystem changes a container has made relative to its image.

    Use it to audit what a container wrote before `container_commit` or `container_archive_get`,
    or to debug unexpected writes. Only the writable container layer is compared - files in
    volumes and bind mounts never show up.

    Returns:
        list: Dicts of {"Path", "Kind"}; Kind 0=modified, 1=added, 2=deleted
    """
    container = _get_client(host).containers.get(id_or_name)
    return container.diff()


@tool()
def container_rename(id_or_name: str, name: str, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Rename a container in place; its id, state, and configuration are unchanged.

    Use it to free up or claim a container name (names are unique per daemon) - e.g. before
    starting a replacement under the old name. Fails with a conflict error if the new name is
    already taken. Not related to `image_tag`, which names images.

    Args:
        name: The new name; must not be in use by any other container

    Returns:
        dict: The container's full inspect payload after the rename
    """
    container = _get_client(host).containers.get(id_or_name)
    container.rename(name)
    container.reload()
    return container.attrs


@tool()
def container_update(id_or_name: str, updates: dict, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
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

    Args:
        updates: Resource fields to update; see description for valid keys

    Returns:
        dict: The container's full inspect payload after the update
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
    """Build the unified container wait result snapshot - the same shape for every `until` mode.

    Args:
        id_or_name: the container waited on
        until: which wait mode was used
        met: whether the condition was satisfied
        start: when the wait began, for the elapsed time
        timed_out: whether the wait hit its deadline
        status_code: the container's exit status, where the mode produces one
        error: the failure text, where the wait failed
        health: the health state, for the health mode
        status: the container's status, where the mode reports one
        matched_line: the log line that matched, for the log mode

    Returns:
        dict: the unified snapshot - the same shape for every ``until`` mode
    """
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


def _wait_container(
    id_or_name: str,
    until: str,
    timeout_seconds: float,
    poll_interval: float,
    pattern: str | None = None,
    regex: bool = False,
    host: str | None = None,
) -> dict:
    """Wait for a container condition; `system_wait(kind="container")` is the tool that calls this.

    Never raises on timeout: the result always carries `met` and `timed_out`. The stop conditions
    ("not-running"/"next-exit"/"removed") use the daemon's blocking wait and fill `status_code`/`error`;
    "healthy" polls the HEALTHCHECK and fills `health`/`status`, returning promptly with `health: null`
    and `met: false` when no HEALTHCHECK is defined; "log-match" polls recent logs for `pattern`, as a
    plain substring unless `regex` is set, since a hostile regex run over attacker-influenced logs can
    exhaust CPU. A container that exits before the condition holds returns promptly with `met: false`.

    Args:
        id_or_name: the container to wait on
        until: "not-running", "next-exit", "removed", "healthy" or "log-match"
        timeout_seconds: seconds to wait before returning with timed_out=true
        poll_interval: seconds between re-checks for "healthy"/"log-match", capped by the time left
        pattern: "log-match" only: the substring, or with `regex` the regular expression, to look for
        regex: "log-match" only: treat `pattern` as a regular expression
        host: the host label to target, or None for the default

    Returns:
        dict: {"container", "until", "met", "timed_out", "status_code", "error", "health", "status",
            "matched_line", "waited_seconds"}

    Raises:
        ToolInputError: a negative timeout, a non-positive poll interval, or "log-match" without a pattern
    """
    if timeout_seconds < 0:
        raise ToolInputError(f"timeout_seconds must be >= 0, got {timeout_seconds}.")
    if until in ("healthy", "log-match") and poll_interval <= 0:
        raise ToolInputError(f"poll_interval must be > 0, got {poll_interval}.")
    if until == "log-match" and not pattern:
        raise ToolInputError("`pattern` is required when until='log-match'.")
    container = _get_client(host).containers.get(id_or_name)
    start = time.monotonic()
    if until not in ("healthy", "log-match") and math.ceil(timeout_seconds) == 0:
        # A zero timeout means check once, as for every other kind. The daemon's wait cannot express it:
        # requests refuses a zero timeout with a builtin ValueError. A next exit has not happened yet, and
        # a container just fetched has not been removed, so only "not-running" can already be met.
        state = container.attrs.get("State") or {}
        if until == "not-running" and not state.get("Running"):
            return _wait_result(
                id_or_name,
                until,
                met=True,
                start=start,
                status_code=state.get("ExitCode"),
                error=state.get("Error") or None,
            )
        return _wait_result(id_or_name, until, met=False, start=start, timed_out=True)
    if until not in ("healthy", "log-match"):
        try:
            # The daemon wait takes whole seconds; round up so a small fractional timeout still
            # waits (int() would truncate 0.5 to an immediate 0s timeout).
            result = cast(
                dict,
                container.wait(
                    timeout=math.ceil(timeout_seconds),
                    condition=cast(Literal["not-running", "next-exit", "removed"], until),
                ),
            )
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
            # Stopped without ever matching - no further logs will arrive, don't poll to the timeout.
            return _wait_result(id_or_name, until, met=False, start=start, status=status)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _wait_result(id_or_name, until, met=False, start=start, timed_out=True, status=status)
        # Bound the sleep by the time left so a large poll_interval can't push past the timeout.
        time.sleep(min(poll_interval, remaining))


@tool()
def container_export(  # noqa: DOC101,DOC103
    id_or_name: str,
    dest_path: str | None = None,
    overwrite: bool = False,
    max_bytes: int = MAX_PAYLOAD_BYTES,
    host: str | None = None,
) -> bytes | dict:
    """
    Export a container's filesystem as a tar archive: to a file on the server host, or in band.

    The tar is a flat filesystem snapshot with no image metadata or layers - use `image_save` for
    an archive that `image_load` can restore, and `container_archive_get` for a single file or
    directory. With `dest_path` the archive streams straight to disk (no byte cap), so it handles large
    containers - the file is written by the server's user, `~` is expanded, and an existing file is
    refused unless `overwrite=True`. Without `dest_path` the tar bytes are returned in band, capped
    at `max_bytes` (default 32 MiB) because MCP base64-encodes them - a fallback for when no
    writable host path exists (e.g. a containerized server without a bind mount).

    Args:
        dest_path: Destination path on the server host; omit to return the bytes in band
        overwrite: Replace dest_path if it already exists
        max_bytes: In-band mode: abort with ToolInputError beyond this many bytes (default 32 MiB)

    Returns:
        bytes | dict: the tar bytes (in band), or {"path": <resolved path>, "bytes_written": int}
    """
    container = _get_client(host).containers.get(id_or_name)
    if dest_path is None:
        return join_bounded(cast(Iterable[bytes], container.export()), max_bytes, f"export of {id_or_name}")
    path, written = stream_to_file(cast(Iterable[bytes], container.export()), dest_path, overwrite=overwrite)
    return {"path": str(path), "bytes_written": written}


@tool()
def container_archive_get(  # noqa: DOC101,DOC103
    id_or_name: str, path: str, max_bytes: int = MAX_PAYLOAD_BYTES, host: str | None = None
) -> dict:
    """
    Retrieve a file or directory from a container as a tar archive, returned in band.

    In-band bytes are capped (default 32 MiB) because MCP base64-encodes them;
    `container_archive_get_to_file` streams to a host path instead.

    Args:
        path: Path inside the container
        max_bytes: Abort with ToolInputError if the archive exceeds this many bytes (defaults to 32 MiB)

    Returns:
        dict: Mapping with archive (bytes) and stat (dict) keys
    """
    container = _get_client(host).containers.get(id_or_name)
    stream, stat = container.get_archive(path)
    return {"archive": join_bounded(stream, max_bytes, f"archive of {id_or_name}:{path}"), "stat": stat}


@tool()
def container_archive_get_to_file(  # noqa: DOC101,DOC103
    id_or_name: str, path: str, dest_path: str, overwrite: bool = False, host: str | None = None
) -> dict:
    """
    Retrieve a file or directory from a container as a tar archive written to a file on the server host.

    File-writing variant of `container_archive_get` - prefer it for anything large, since in-band
    bytes are base64-encoded by MCP. For the whole filesystem use `container_export`. Streams
    straight to disk (no in-band byte cap). The file is written by the server's user; `~` is
    expanded and an existing file is refused unless `overwrite=True`.

    Args:
        path: Path inside the container
        dest_path: Destination path on the server host for the tarball
        overwrite: Replace dest_path if it already exists

    Returns:
        dict: {"path": <resolved path>, "bytes_written": int, "stat": dict}
    """
    container = _get_client(host).containers.get(id_or_name)
    stream, stat = container.get_archive(path)
    written_path, written = stream_to_file(stream, dest_path, overwrite=overwrite)
    return {"path": str(written_path), "bytes_written": written, "stat": stat}


@tool()
def container_archive_put(  # noqa: DOC101,DOC103,DOC501,DOC503
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
    straight to the daemon - preferred for large archives, since in-band bytes are base64-encoded by
    MCP). `from_file` is read by the server's user; `~` is expanded.

    Args:
        path: Destination path inside the container (must already exist)
        data: Tar archive bytes; exactly one of data/from_file
        from_file: Path on the server host to the tar archive to upload; exactly one of data/from_file

    Returns:
        bool: True if the upload succeeded
    """
    if (data is None) == (from_file is None):
        raise ToolInputError("Pass exactly one of `data` (in-band tar bytes) or `from_file` (a server-host path).")
    container = _get_client(host).containers.get(id_or_name)
    if data is not None:
        return container.put_archive(path, data)
    with open_host_read_file(cast(str, from_file)) as handle:
        return container.put_archive(path, handle)

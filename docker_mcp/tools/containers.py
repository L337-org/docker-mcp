# library of mcp tools relating to container management

import threading
from collections.abc import Iterable
from typing import Literal, cast

import requests.exceptions

from docker_mcp.server import mcp
from docker_mcp.tools._utils import MAX_PAYLOAD_BYTES, close_stream_quietly, drop_none, join_bounded
from docker_mcp.tools.client import _get_client


@mcp.tool()
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
    restart_policy: dict | None = None,
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
        restart_policy: dict - Restart policy, e.g. {'Name': 'on-failure'}
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
            labels=labels,
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


@mcp.tool()
def create_container(image: str, command: str | list | None = None, extra_kwargs: dict | None = None) -> dict:
    """
    Create a container without starting it.

    args:
        image: str - The image to use
        command: str | list - The command to run when started
        extra_kwargs: dict - Additional keyword arguments forwarded to ContainerCollection.create
    returns: dict - The created container's attrs
    """
    kwargs = extra_kwargs or {}
    container = _get_client().containers.create(image, command=command, **kwargs)
    return container.attrs


@mcp.tool()
def get_container(id_or_name: str) -> dict:
    """
    Get a container by id or name.

    args: id_or_name: str - The container id or name
    returns: dict - The container's attrs
    """
    return _get_client().containers.get(id_or_name).attrs


@mcp.tool()
def list_containers(
    all: bool = False,
    since: str | None = None,
    before: str | None = None,
    limit: int | None = None,
    filters: dict | None = None,
    sparse: bool = False,
    ignore_removed: bool = False,
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
    returns: list - A list of container attrs dicts
    """
    kwargs: dict = {
        "all": all,
        "sparse": sparse,
        "ignore_removed": ignore_removed,
        **drop_none(since=since, before=before, limit=limit, filters=filters),
    }
    return [c.attrs for c in _get_client().containers.list(**kwargs)]


@mcp.tool()
def prune_containers(filters: dict | None = None) -> dict:
    """
    Remove stopped containers.

    args: filters: dict - Filters to apply to the prune operation
    returns: dict - Information on deleted containers and reclaimed space
    """
    return _get_client().containers.prune(filters=filters)


@mcp.tool()
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


@mcp.tool()
def stop_container(id_or_name: str, timeout: int = 10) -> dict:
    """
    Stop a container.

    args:
        id_or_name: str - The container id or name
        timeout: int - Seconds to wait before forcing termination
    returns: dict - The container's attrs after stop
    """
    container = _get_client().containers.get(id_or_name)
    container.stop(timeout=timeout)
    container.reload()
    return container.attrs


@mcp.tool()
def restart_container(id_or_name: str, timeout: int = 10) -> dict:
    """
    Restart a container.

    args:
        id_or_name: str - The container id or name
        timeout: int - Seconds to wait before forcing restart
    returns: dict - The container's attrs after restart
    """
    container = _get_client().containers.get(id_or_name)
    container.restart(timeout=timeout)
    container.reload()
    return container.attrs


@mcp.tool()
def kill_container(id_or_name: str, signal: str | None = None) -> dict:
    """
    Send a signal to a container.

    args:
        id_or_name: str - The container id or name
        signal: str - Signal to send (defaults to SIGKILL)
    returns: dict - The container's attrs after kill
    """
    container = _get_client().containers.get(id_or_name)
    container.kill(signal=signal)
    container.reload()
    return container.attrs


@mcp.tool()
def pause_container(id_or_name: str) -> dict:
    """
    Pause all processes in a container.

    args: id_or_name: str - The container id or name
    returns: dict - The container's attrs after pause
    """
    container = _get_client().containers.get(id_or_name)
    container.pause()
    container.reload()
    return container.attrs


@mcp.tool()
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


@mcp.tool()
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
    container.remove(v=v, link=link, force=force)
    return True


@mcp.tool()
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


@mcp.tool()
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
    Tail a container's log stream, returning when `limit_lines` lines have been collected,
    `timeout_seconds` elapses, or the container exits — whichever comes first.

    Wraps the streaming logs API of the `docker` module so the agent can watch live output
    without blocking forever. `limit_lines` bounds memory; `timeout_seconds` bounds wall-clock
    time, which matters for a quiet but long-lived container that would otherwise never emit the
    line that lets the call return.

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


@mcp.tool()
def container_stats(id_or_name: str) -> dict:
    """
    Get a single resource usage stats snapshot for a container.

    args: id_or_name: str - The container id or name
    returns: dict - Decoded stats snapshot
    """
    container = _get_client().containers.get(id_or_name)
    return cast(dict, container.stats(decode=True, stream=False))


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def container_diff(id_or_name: str) -> list:
    """
    Inspect changes on a container's filesystem.

    args: id_or_name: str - The container id or name
    returns: list - Filesystem changes since the image was created
    """
    container = _get_client().containers.get(id_or_name)
    return container.diff()


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def export_container(id_or_name: str, max_bytes: int = MAX_PAYLOAD_BYTES) -> bytes:
    """
    Export a container's filesystem as a tar archive.

    args:
        id_or_name: str - The container id or name
        max_bytes: int - Abort with ValueError if the export exceeds this many bytes (defaults to 1 GiB)
    returns: bytes - The tar archive contents
    """
    container = _get_client().containers.get(id_or_name)
    return join_bounded(cast(Iterable[bytes], container.export()), max_bytes, f"export of {id_or_name}")


@mcp.tool()
def get_container_archive(id_or_name: str, path: str, max_bytes: int = MAX_PAYLOAD_BYTES) -> dict:
    """
    Retrieve a file or directory from a container as a tar archive.

    args:
        id_or_name: str - The container id or name
        path: str - Path inside the container
        max_bytes: int - Abort with ValueError if the archive exceeds this many bytes (defaults to 1 GiB)
    returns: dict - Mapping with archive (bytes) and stat (dict) keys
    """
    container = _get_client().containers.get(id_or_name)
    stream, stat = container.get_archive(path)
    return {"archive": join_bounded(stream, max_bytes, f"archive of {id_or_name}:{path}"), "stat": stat}


@mcp.tool()
def put_container_archive(id_or_name: str, path: str, data: bytes) -> bool:
    """
    Upload a tar archive to a path inside a container.

    args:
        id_or_name: str - The container id or name
        path: str - Destination path inside the container
        data: bytes - Tar archive bytes
    returns: bool - True if the upload succeeded
    """
    container = _get_client().containers.get(id_or_name)
    return container.put_archive(path, data)

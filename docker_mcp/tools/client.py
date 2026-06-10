# library of mcp tools relating to client management

import os
import threading

import docker
import requests.exceptions
from docker.errors import DockerException

from docker_mcp.server import tool
from docker_mcp.tools._utils import close_stream_quietly

_client: docker.DockerClient | None = None
# Guards every read/swap of `_client`. FastMCP runs sync tools concurrently in a worker
# threadpool, so lazy init in `_get_client`, teardown in `close`, and the swap in `reconnect`
# must not race (e.g. two threads each building a client, or one using a client another closed).
_client_lock = threading.Lock()

# Daemon errors that can surface either when building a client or on the first request to it.
# docker-py raises DockerException for protocol/config problems and lets requests' own
# connection/timeout errors through for an unreachable endpoint.
_CONNECT_ERRORS: tuple[type[BaseException], ...] = (DockerException, requests.exceptions.RequestException)


def _close_client_quietly(client: docker.DockerClient) -> None:
    """Best-effort close of a discarded client; a failed teardown must not block a reconnect."""
    try:
        client.close()
    except Exception:  # noqa: S110, BLE001 — teardown of an already-discarded client is best-effort
        # The usual reason to discard a client is that it's already broken; swallow whatever
        # its close() raises so the caller can proceed to build a fresh one.
        pass


def _get_client() -> docker.DockerClient:
    global _client
    with _client_lock:
        if _client is None:
            try:
                _client = docker.from_env()
            except _CONNECT_ERRORS as exc:
                host = os.environ.get("DOCKER_HOST", "default unix socket")
                raise RuntimeError(
                    f"Cannot reach the Docker daemon at {host}. Is Docker running, "
                    f"and is DOCKER_HOST set correctly? Underlying error: {exc}"
                ) from exc
        return _client


@tool()
def ping() -> bool:
    """
    Check that the Docker server is responsive.

    returns: bool - True if the daemon responded successfully
    """
    return _get_client().ping()


@tool()
def version() -> dict:
    """
    Return Docker server version information.

    returns: dict - Version information from the Docker daemon
    """
    return _get_client().version()


@tool()
def info() -> dict:
    """
    Return system-wide Docker information.

    returns: dict - System information from the Docker daemon
    """
    return _get_client().info()


@tool()
def df() -> dict:
    """
    Return Docker disk usage information.

    returns: dict - Data usage information for images, containers and volumes
    """
    return _get_client().df()


@tool()
def login(
    username: str,
    password: str,
    email: str | None = None,
    registry: str | None = None,
    reauth: bool = False,
    dockercfg_path: str | None = None,
) -> dict:
    """
    Authenticate with a Docker registry.

    Security: the password is sent as a tool argument, which many MCP clients log
    verbatim. Prefer running `docker login` once on the host running this MCP
    server so the `docker` module can reuse the credentials cached in that host's
    Docker config (typically `~/.docker/config.json`), and avoid calling this
    tool from an agent loop.

    args:
        username: str - Registry username
        password: str - Registry password or token
        email: str - Registry account email
        registry: str - URL to the registry (defaults to Docker Hub)
        reauth: bool - Force re-authentication even if valid credentials exist
        dockercfg_path: str - Path to a custom dockercfg file
    returns: dict - The server response from the login request
    """
    return _get_client().login(
        username=username,
        password=password,
        email=email,
        registry=registry,
        reauth=reauth,
        dockercfg_path=dockercfg_path,
    )


@tool()
def events(
    since: str | None = None,
    until: str | None = None,
    filters: dict | None = None,
    limit: int = 100,
    timeout_seconds: float = 30.0,
) -> list:
    """
    Stream real-time events from the Docker server, bounded by `limit` events or `timeout_seconds`.

    The call returns when `limit` events have been collected or `timeout_seconds` elapses, whichever
    comes first. Both bounds matter: `limit` caps how many events accumulate in memory, while
    `timeout_seconds` caps how long the call blocks. Without the time bound a quiet daemon (fewer
    than `limit` events, no `until`) would block the tool call indefinitely, since the event stream
    only yields when an event actually occurs.

    args:
        since: str - Show events created since this timestamp
        until: str - Show events created until this timestamp
        filters: dict - Filters to apply to the event stream
        limit: int - Maximum number of events to return (defaults to 100)
        timeout_seconds: float - Maximum wall-clock seconds to wait before returning what was
                                 collected so far (defaults to 30)
    returns: list - A list of decoded event dicts (length <= limit)
    """
    stream = _get_client().events(since=since, until=until, filters=filters, decode=True)
    collected: list = []
    # The event stream is a CancellableStream; closing its socket from a watchdog timer unblocks
    # the iteration below (the blocked read surfaces as StopIteration), giving a hard time bound
    # even when no events ever arrive.
    timer = threading.Timer(timeout_seconds, lambda: close_stream_quietly(stream))
    timer.start()
    try:
        for event in stream:
            collected.append(event)
            if len(collected) >= limit:
                break
    finally:
        timer.cancel()
        close_stream_quietly(stream)
    return collected


@tool()
def close() -> bool:
    """
    Close the Docker client session and reset the cached client.

    returns: bool - True once the client has been closed
    """
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None
    return True


@tool()
def reconnect(docker_host: str | None = None) -> dict:
    """
    Rebuild the shared Docker SDK client, optionally retargeting it at a different daemon.

    Validates the new endpoint before swapping out and closing the old client, so a bad target
    leaves the working one in place. Security: retargeting moves a root-equivalent trust boundary
    and `docker_host` is logged like any argument — see README "Security considerations".

    args: docker_host: str | None - Daemon URL to connect to, or None to rebuild from the environment
    returns: dict - The new daemon's version info (same shape as `version`), confirming connectivity
    """
    global _client
    target = docker_host or os.environ.get("DOCKER_HOST", "the default Docker socket")
    try:
        new_client = docker.DockerClient(base_url=docker_host) if docker_host else docker.from_env()
    except _CONNECT_ERRORS as exc:
        raise RuntimeError(f"Could not build a Docker client for {target!r}: {exc}") from exc
    try:
        version_info = new_client.version()
    except _CONNECT_ERRORS as exc:
        _close_client_quietly(new_client)
        raise RuntimeError(
            f"Built a Docker client for {target!r} but the daemon is unreachable: {exc}. "
            f"Kept the previous client; check the endpoint and try again."
        ) from exc
    with _client_lock:
        old_client = _client
        _client = new_client
    if old_client is not None and old_client is not new_client:
        _close_client_quietly(old_client)
    return version_info

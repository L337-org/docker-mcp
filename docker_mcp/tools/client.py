# library of mcp tools relating to client management

import os
import threading

import docker
from docker.errors import DockerException

from docker_mcp.server import mcp
from docker_mcp.tools._utils import close_stream_quietly

_client: docker.DockerClient | None = None


def _get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        try:
            _client = docker.from_env()
        except DockerException as exc:
            host = os.environ.get("DOCKER_HOST", "default unix socket")
            raise RuntimeError(
                f"Cannot reach the Docker daemon at {host}. Is Docker running, "
                f"and is DOCKER_HOST set correctly? Underlying error: {exc}"
            ) from exc
    return _client


@mcp.tool()
def ping() -> bool:
    """
    Check that the Docker server is responsive.

    returns: bool - True if the daemon responded successfully
    """
    return _get_client().ping()


@mcp.tool()
def version() -> dict:
    """
    Return Docker server version information.

    returns: dict - Version information from the Docker daemon
    """
    return _get_client().version()


@mcp.tool()
def info() -> dict:
    """
    Return system-wide Docker information.

    returns: dict - System information from the Docker daemon
    """
    return _get_client().info()


@mcp.tool()
def df() -> dict:
    """
    Return Docker disk usage information.

    returns: dict - Data usage information for images, containers and volumes
    """
    return _get_client().df()


@mcp.tool()
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


@mcp.tool()
def events(
    since: str | None = None,
    until: str | None = None,
    filters: dict | None = None,
    limit: int = 100,
    timeout_seconds: float = 30.0,
) -> list:
    """
    Stream real-time events from the Docker server, returning when `limit` events have been
    collected or `timeout_seconds` elapses — whichever comes first.

    Both bounds matter: `limit` caps how many events accumulate in memory, while `timeout_seconds`
    caps how long the call blocks. Without the time bound a quiet daemon (fewer than `limit` events,
    no `until`) would block the tool call indefinitely, since the event stream only yields when an
    event actually occurs.

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


@mcp.tool()
def close() -> bool:
    """
    Close the Docker client session and reset the cached client.

    returns: bool - True once the client has been closed
    """
    global _client
    if _client is not None:
        _client.close()
        _client = None
    return True

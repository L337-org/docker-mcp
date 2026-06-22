# library of mcp tools relating to client management

import os
import sys
import threading
from pathlib import Path

import docker
import requests.exceptions
from docker.errors import DockerException
from docker.models.containers import Container

from docker_mcp._env import scrub_unresolved_env
from docker_mcp._hosts import default as _default_host, registry as _host_registry, resolve_auto
from docker_mcp.server import tool
from docker_mcp.tools._utils import classify_host_kernel, close_stream_quietly, env_flag, in_container

_client: docker.DockerClient | None = None
# Guards every read/swap of `_client`. FastMCP runs sync tools concurrently in a worker
# threadpool, so lazy init in `_get_client`, teardown in `close`, and the swap in `reconnect`
# must not race (e.g. two threads each building a client, or one using a client another closed).
_client_lock = threading.Lock()

# Daemon errors that can surface either when building a client or on the first request to it.
# docker-py raises DockerException for protocol/config problems and lets requests' own
# connection/timeout errors through for an unreachable endpoint.
_CONNECT_ERRORS: tuple[type[BaseException], ...] = (DockerException, requests.exceptions.RequestException)

# The full id of the container this server runs in, pinned once at startup (see startup_preflight).
# Stays None on the host install or whenever we can't identify ourselves, which leaves the
# self-termination guard inert. Env var lets an operator who really means it bypass the guard.
_self_container_id: str | None = None
_SELF_TERMINATE_OVERRIDE_ENV = "DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE"
_LEGACY_SELF_TERMINATE_OVERRIDE_ENV = "DOCKER_MCP_ALLOW_SELF_TERMINATE"  # deprecated alias, still honored


def _detect_self_container_id(client: docker.DockerClient) -> str | None:
    """
    Resolve the full id of the container this server runs in, or None if it can't be determined.

    Docker sets the container's short id as its hostname by default, so we look that up via the
    daemon. Returns None if the hostname was overridden (`--hostname`) or the lookup fails — the
    self-termination guard then stays inert rather than guessing.
    """
    hostname = (os.environ.get("HOSTNAME") or "").strip()
    if not hostname:
        try:
            hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        except OSError:
            hostname = ""
    if not hostname:
        return None
    try:
        return client.containers.get(hostname).id
    except _CONNECT_ERRORS:
        return None


def guard_not_self(container: Container) -> None:
    """
    Refuse a destructive lifecycle action against this server's own container.

    An accident guard, not a security boundary: it only constrains calls made through this server's
    tools. A human recovering a wedged server runs `docker rm -f` from their own shell, which never
    touches this server. Inert when we aren't containerized or couldn't identify ourselves, and
    bypassable with DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE=1.
    """
    if _self_container_id is None or container.id != _self_container_id:
        return
    if env_flag(_SELF_TERMINATE_OVERRIDE_ENV, _LEGACY_SELF_TERMINATE_OVERRIDE_ENV):
        return
    raise RuntimeError(
        f"Refusing to operate on the docker-mcp-server's own container ({container.short_id} "
        f"{container.name}) — this would terminate the MCP session mid-call. Set "
        f"{_SELF_TERMINATE_OVERRIDE_ENV}=1 to override, or run the action from the host shell "
        f"(e.g. `docker rm -f`), which bypasses this server entirely."
    )


def _close_client_quietly(client: docker.DockerClient) -> None:
    """Best-effort close of a discarded client; a failed teardown must not block a reconnect."""
    try:
        client.close()
    except Exception:  # noqa: S110, BLE001 — teardown of an already-discarded client is best-effort
        # The usual reason to discard a client is that it's already broken; swallow whatever
        # its close() raises so the caller can proceed to build a fresh one.
        pass


def _build_default_client() -> docker.DockerClient:
    """Client for DOCKER_HOST when set (via from_env, honoring its TLS env), else the resolved endpoint.

    auto/local resolution lives in docker_mcp._hosts now (the pure registry layer); resolve_auto() is
    the relocated _resolve_default_base_url().
    """
    if os.environ.get("DOCKER_HOST"):
        return docker.from_env()
    base_url = resolve_auto()
    return docker.DockerClient(base_url=base_url) if base_url else docker.from_env()


def _get_client() -> docker.DockerClient:
    global _client
    with _client_lock:
        if _client is None:
            try:
                _client = _build_default_client()
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
def list_hosts() -> list[dict]:
    """
    List the Docker hosts configured via DOCKER_MCP_SERVER_HOSTS.

    With a single host (or the var unset) this is the one resolved daemon; with several it is the set the
    `host` argument selects from. The `default` entry is the one used when `host` is omitted.

    returns: list[dict] - one per host: name; url (resolved daemon URL, null = docker-py platform
        default); read_only; tls (whether a per-host cert dir is configured); default (the omitted-host fallback)
    """
    hosts = _host_registry()
    default_label = _default_host().label if hosts else None
    return [
        {
            "name": host.label,
            "url": host.url,
            "read_only": host.read_only,
            "tls": host.cert_dir is not None,
            "default": host.label == default_label,
        }
        for host in hosts.values()
    ]


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

    Security: the password is sent as a tool argument, which many MCP clients log verbatim. Prefer
    running `docker login` once on the host so the `docker` module reuses the credentials cached in
    `~/.docker/config.json`, and avoid calling this tool from an agent loop.

    args:
        username - Registry username
        password - Registry password or token
        email - Registry account email
        registry - URL to the registry (defaults to Docker Hub)
        reauth - Force re-authentication even if valid credentials exist
        dockercfg_path - Path to a custom dockercfg file
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
def logout(registry: str | None = None) -> dict:
    """
    Clear cached registry credentials from this server's in-memory Docker client.

    docker-py / the Engine have no true logout: `login` validates against the registry (the daemon's
    `/auth` is stateless) and caches credentials in-process. This drops that in-memory cache; it does
    NOT contact the daemon or touch the host's `~/.docker/config.json`. With no `registry`, clears
    every cached credential; pass one to clear just that entry (key must match `login`; Docker Hub is
    cached under "docker.io"). `close`/`reconnect` also clear it by discarding the client.

    Reaches into a private docker-py attribute (`api._auth_configs`); degrades to clearing nothing if
    that internal shape changes.

    args: registry - Registry key to clear, or None to clear every cached credential
    returns: dict - {"cleared": [<registry keys removed>]}
    """
    api = _get_client().api
    # _auth_configs is a private docker-py attribute: an AuthConfig (dict subclass) whose "auths" key
    # maps registry -> credential. Guard its presence/shape instead of assuming, so a docker-py change
    # downgrades to a no-op rather than an AttributeError mid-tool.
    auth_configs = getattr(api, "_auth_configs", None)
    auths = auth_configs.get("auths") if isinstance(auth_configs, dict) else None
    if not isinstance(auths, dict) or not auths:
        return {"cleared": []}
    if registry is None:
        cleared = list(auths.keys())
        auths.clear()
    else:
        cleared = [registry] if auths.pop(registry, None) is not None else []
    return {"cleared": cleared}


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

    Returns when `limit` events are collected or `timeout_seconds` elapses, whichever comes first
    (`limit` caps memory; `timeout_seconds` caps how long the call blocks — without it a quiet daemon
    would block indefinitely, since the stream only yields on an actual event).

    Caveat for `ssh://` daemons: docker-py can't cancel an SSH stream, so the `timeout_seconds`
    watchdog can't interrupt a fully idle stream — bound with `until`/`limit` (or a non-SSH endpoint).

    args:
        since - Show events created since this timestamp
        until - Show events created until this timestamp
        filters - Filters to apply to the event stream
        limit - Max events to return (default 100)
        timeout_seconds - Max wall-clock seconds before returning what was collected (default 30)
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

    args: docker_host - Daemon URL to connect to, or None to rebuild from the environment
    returns: dict - The new daemon's version info (same shape as `version`), confirming connectivity
    """
    global _client
    # Name the endpoint we'll actually use, mirroring _build_default_client's precedence so a failure
    # message points at the real target (context/socket resolution, not a misleading default).
    target = docker_host or os.environ.get("DOCKER_HOST") or resolve_auto() or "the default Docker socket"
    try:
        new_client = docker.DockerClient(base_url=docker_host) if docker_host else _build_default_client()
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


def _connection_help(exc: BaseException) -> str:
    """OS-aware guidance, emitted when the startup ping fails, for getting the daemon reachable."""
    lines = [f"docker-mcp-server: cannot reach the Docker daemon ({exc})."]
    host = os.environ.get("DOCKER_HOST")
    if host:
        lines.append(f"  DOCKER_HOST is set to {host} — verify that endpoint is reachable.")
    if host and host.startswith("ssh://"):
        # ssh:// uses the pure-Python paramiko transport. Its failure modes are auth/host-key, not
        # the socket-mount issues the rest of this function covers, so give targeted hints and stop.
        lines.append(
            "  This is an ssh:// endpoint (paramiko transport). Common causes: the SSH key isn't "
            "loaded (run `ssh-add`, and forward SSH_AUTH_SOCK), or the host key isn't in "
            "known_hosts — paramiko rejects unknown hosts. Add the host key only after verifying its "
            "fingerprint out of band: connect once with `ssh <host>` and confirm the prompt, or "
            "compare `ssh-keyscan <host> | ssh-keygen -lf -` against a trusted fingerprint before "
            "trusting it (don't blind-append `ssh-keyscan` output — that trusts any key returned, "
            "MITM included). In a container, mount your `~/.ssh` (key + known_hosts) read-only; no "
            "`ssh` binary is needed."
        )
        return "\n".join(lines)
    if not in_container():
        lines.append("  Is Docker running, and is DOCKER_HOST set correctly?")
        return "\n".join(lines)
    lines.append("  Running in a container: the daemon socket must be bind-mounted in, or DOCKER_HOST set.")
    kind = classify_host_kernel()
    if kind == "wsl2":
        lines.append(
            "  Host looks like Windows/WSL2 — the engine listens on a named pipe, not a unix socket. "
            "Pass `-e DOCKER_HOST=tcp://host.docker.internal:2375` (enable the TCP endpoint in Docker "
            "Desktop) or mount the WSL-side socket."
        )
    elif kind == "docker-desktop":
        lines.append(
            "  Host looks like Docker Desktop (macOS) — mount the Desktop socket: "
            "`-v $HOME/.docker/run/docker.sock:/var/run/docker.sock` (or enable 'Allow the default "
            "Docker socket' in Settings and mount `/var/run/docker.sock`)."
        )
    else:
        lines.append(
            "  Mount the daemon socket: `-v /var/run/docker.sock:/var/run/docker.sock` "
            "(rootless: `-v $XDG_RUNTIME_DIR/docker.sock:/var/run/docker.sock`)."
        )
    return "\n".join(lines)


def _connection_summary(client: docker.DockerClient) -> str:
    """One-line confirmation of which daemon we reached, plus self-guard status when containerized."""
    try:
        details = client.info()
    except _CONNECT_ERRORS:
        details = {}
    os_name = details.get("OperatingSystem") or "unknown"
    security_options = details.get("SecurityOptions") or []
    rootless = any(isinstance(opt, str) and "name=rootless" in opt for opt in security_options)
    suffix = " (rootless)" if rootless else ""
    note = ""
    if in_container():
        note = (
            f"; self-termination guard active for container {_self_container_id[:12]}"
            if _self_container_id
            else "; self-termination guard inactive (could not identify own container)"
        )
    return f"docker-mcp-server: connected to Docker daemon — {os_name}{suffix}{note}."


def startup_preflight() -> None:
    """
    Best-effort startup diagnostics, written only to stderr (stdout is the MCP stdio channel).

    Pings the daemon; on failure prints OS-aware connection guidance and returns without raising, so
    a client that only wants the tool list still starts. On success, pins this server's own container
    id for the self-termination guard (when containerized) and prints a one-line confirmation of the
    daemon it reached. Never raises — diagnostics must not crash startup.
    """
    global _self_container_id
    scrub_unresolved_env()
    try:
        client = _get_client()
        client.ping()
    except Exception as exc:  # noqa: BLE001 — startup diagnostics must never abort the server
        print(_connection_help(exc), file=sys.stderr, flush=True)
        return
    if in_container():
        _self_container_id = _detect_self_container_id(client)
    print(_connection_summary(client), file=sys.stderr, flush=True)

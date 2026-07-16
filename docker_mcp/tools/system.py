# library of mcp tools relating to the system domain: daemon info/auth and client connection control

import os
import sys
import threading
import urllib.parse
from pathlib import Path

import docker
import requests.exceptions
from docker.errors import DockerException
from docker.models.containers import Container

from docker_mcp._env import scrub_unresolved_env
from docker_mcp._hosts import (
    Host,
    default as _default_host,
    is_multi as _is_multi,
    registry as _host_registry,
    resolve as _resolve_host,
    resolve_auto,
)
from docker_mcp.server import tool
from docker_mcp.tools._ssh_proxy import parse_ssh_url
from docker_mcp.tools._utils import classify_host_kernel, close_stream_quietly, env_flag, in_container

# One lazily-built docker-py client per configured host label (the pool). FastMCP runs sync tools
# concurrently in a worker threadpool, so building in `_get_client`, teardown in `close`, and the swap
# in `reconnect` must not race (e.g. two threads each building a client, or one using a client another
# closed) — `_client_lock` guards every read/mutation of `_clients`.
_clients: dict[str, docker.DockerClient] = {}
_client_lock = threading.Lock()

# Daemon errors that can surface either when building a client or on the first request to it.
# docker-py raises DockerException for protocol/config problems and lets requests' own
# connection/timeout errors through for an unreachable endpoint.
_CONNECT_ERRORS: tuple[type[BaseException], ...] = (DockerException, requests.exceptions.RequestException)

# The full id of the container this server runs in, pinned once at startup (see startup_preflight).
# Stays None on the host install or whenever we can't identify ourselves, which leaves the
# self-termination guard inert. Env var lets an operator who really means it bypass the guard.
_self_container_id: str | None = None
# Label of the host the server's own container runs on (the self host). The self-termination guard only
# fires when a call targets this host — our own container can't exist on any other daemon.
_self_host_label: str | None = None
_SELF_TERMINATE_OVERRIDE_ENV = "DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE"


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


def _self_host() -> Host | None:
    """
    The host the server's own container runs on = the first configured host on a local transport
    (unix:// / npipe:// or the platform default). Self-detection and the self-termination guard key off
    this; a remote-only config returns None, leaving the guard inert (our container can't be on a remote).
    """
    for host in _host_registry().values():
        if host.url is None or host.url.startswith(("unix://", "npipe://")):
            return host
    return None


def guard_not_self(container: Container, host: str | None = None) -> None:
    """
    Refuse a destructive lifecycle action against this server's own container.

    An accident guard, not a security boundary: it only constrains calls made through this server's
    tools. A human recovering a wedged server runs `docker rm -f` from their own shell, which never
    touches this server. Inert when we aren't containerized or couldn't identify ourselves, and
    bypassable with DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE=1.

    `host` is the host the call targets: the guard only fires on the self host, since our own container
    can only exist on the daemon the server runs on.
    """
    if _self_container_id is None or container.id != _self_container_id:
        return
    if _self_host_label is not None and _resolve_host(host).label != _self_host_label:
        return
    if env_flag(_SELF_TERMINATE_OVERRIDE_ENV):
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


def _ensure_ssh_port(url: str) -> str:
    """
    Work around a docker-py bug for an `ssh://` URL with no explicit port: `docker.utils.parse_host()`
    hardcodes port 22 into the URL *before* `SSHHTTPAdapter._create_paramiko_client` ever runs, so that
    adapter's own `~/.ssh/config` `Port` fallback (which only fires while the port is still unset) never
    triggers — a non-22 `Port` in `~/.ssh/config` is silently ignored. We splice in the configured port
    ourselves first, reusing the exact `~/.ssh/config` lookup `_ssh_proxy.parse_ssh_url` already does for
    the CLI-backed tools, so both tool families honor a non-default SSH port the same way.

    args: url: str - a DOCKER_HOST/host URL; only `ssh://` URLs with no explicit port are affected
    returns: str - `url` unchanged, or with the `~/.ssh/config` port spliced into the netloc
    """
    if not url.startswith("ssh://"):
        return url
    parsed = urllib.parse.urlparse(url)
    try:
        has_port = parsed.port is not None
    except ValueError:
        # A malformed port (e.g. "ssh://host:abc") — leave url untouched so docker-py's own
        # validation raises its clearer error instead of this helper failing first.
        return url
    if has_port:
        return url
    try:
        # parse_ssh_url raises ValueError on a missing hostname (e.g. "ssh://" or "ssh://@") or a
        # non-integer `Port` in ~/.ssh/config — leave url untouched in either case rather than
        # raising a bare ValueError here instead of the connection error docker-py would produce.
        target = parse_ssh_url(url)
    except ValueError:
        return url
    if target.port is None:
        return url
    return urllib.parse.urlunparse(parsed._replace(netloc=f"{parsed.netloc}:{target.port}"))


def _build_default_client() -> docker.DockerClient:
    """Client for DOCKER_HOST when set (via from_env, honoring its TLS env), else the resolved endpoint.

    auto/local resolution lives in docker_mcp._hosts now (the pure registry layer); resolve_auto() is
    the relocated _resolve_default_base_url().
    """
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host:
        fixed = _ensure_ssh_port(docker_host)
        if fixed != docker_host:
            return docker.from_env(environment={**os.environ, "DOCKER_HOST": fixed})
        return docker.from_env()
    base_url = resolve_auto()
    return docker.DockerClient(base_url=base_url) if base_url else docker.from_env()


def _tls_from_dir(cert_dir: str) -> docker.TLSConfig:
    """
    A TLSConfig from Docker's conventional certs in `cert_dir`. Always verifies the daemon against
    `ca.pem`; presents a client cert (mutual TLS) only when both `cert.pem` and `key.pem` are present,
    else verifies the daemon only (e.g. a self-signed daemon pinned via `ca.pem`, with no client auth).
    """
    directory = Path(cert_dir)
    cert, key = directory / "cert.pem", directory / "key.pem"
    client_cert = (str(cert), str(key)) if cert.exists() and key.exists() else None
    return docker.TLSConfig(client_cert=client_cert, ca_cert=str(directory / "ca.pem"), verify=True)


def _tls_config_for(host: Host) -> docker.TLSConfig | None:
    """Per-host TLS, tiered: the host's own `(tls=<dir>)` cert dir, else the global DOCKER_CERT_PATH /
    DOCKER_TLS_VERIFY env (mirroring from_env), else plaintext (None)."""
    if host.cert_dir:
        return _tls_from_dir(host.cert_dir)
    if (os.environ.get("DOCKER_TLS_VERIFY") or "").strip():
        return _tls_from_dir(os.environ.get("DOCKER_CERT_PATH") or str(Path.home() / ".docker"))
    return None


def _build_client(host: Host) -> docker.DockerClient:
    """
    Build the docker-py client for one configured host.

    The legacy single host (DOCKER_MCP_SERVER_HOSTS unset) goes through _build_default_client so the
    existing DOCKER_HOST / from_env behavior (and its TLS env / API-version negotiation) is preserved
    exactly — this is the ONLY path that reads DOCKER_HOST. An explicitly-configured host is built from
    its resolved URL with per-host TLS; one that resolved to the platform default (url=None, e.g. `local`
    on Windows) is built WITHOUT a base_url so it uses the platform socket/npipe and never re-reads the
    ambient DOCKER_HOST (which is ignored when DOCKER_MCP_SERVER_HOSTS is set).
    """
    if not _is_multi() and not (os.environ.get("DOCKER_MCP_SERVER_HOSTS") or "").strip():
        return _build_default_client()
    tls = _tls_config_for(host)
    if host.url is None:
        return docker.DockerClient(tls=tls) if tls is not None else docker.DockerClient()
    url = _ensure_ssh_port(host.url)
    return docker.DockerClient(base_url=url, tls=tls) if tls is not None else docker.DockerClient(base_url=url)


def _get_client(host: str | None = None) -> docker.DockerClient:
    """The pooled docker-py client for `host` (the default host when None), lazily built and cached."""
    resolved = _resolve_host(host)
    label = resolved.label
    with _client_lock:
        client = _clients.get(label)
        if client is None:
            try:
                client = _build_client(resolved)
            except _CONNECT_ERRORS as exc:
                where = resolved.url or os.environ.get("DOCKER_HOST") or "the default Docker socket"
                raise RuntimeError(
                    f"Cannot reach the Docker daemon for host {label!r} at {where}. Is Docker running, "
                    f"and is the endpoint correct? Underlying error: {exc}"
                ) from exc
            _clients[label] = client
        return client


@tool()
def system_ping(host: str | None = None) -> bool:
    """
    Check that the Docker server is responsive.

    The cheapest daemon health check. A failure here usually means connection config rather than
    daemon load — `system_reconnect` rebuilds a wedged client, `host_list` shows the configured
    endpoints. For daemon details use `system_version` / `system_info`.

    returns: bool - True if the daemon responded successfully
    """
    return _get_client(host).ping()


@tool()
def system_version(host: str | None = None) -> dict:
    """
    Return Docker server version information.

    Engine version, API level, and per-component versions — the first thing to check for feature
    availability. `system_info` reports runtime state (counts, drivers, swarm role) instead.

    returns: dict - {"Version", "ApiVersion", "MinAPIVersion", "Os", "Arch", "Components", ...}
    """
    return _get_client(host).version()


@tool()
def system_info(host: str | None = None) -> dict:
    """
    Return system-wide Docker information, like `docker info`.

    Daemon runtime state: container/image counts, storage and logging drivers, swarm role, and
    daemon warnings. Use `system_version` for version/API level and `system_df` for disk usage.

    returns: dict - {"Containers", "Images", "Driver", "ServerVersion", "Swarm", "Warnings", ...}
    """
    return _get_client(host).info()


@tool()
def system_df(host: str | None = None) -> dict:
    """
    Summarize Docker disk usage: layer storage plus per-object sizes for images, containers, volumes, build cache.

    Equivalent to `docker system df`. Use it to find what to reclaim before `image_prune` /
    `container_prune` / `volume_prune` / `buildx_prune`; use `system_info` for daemon config and
    counts rather than sizes. The reply enumerates every object on the daemon, so expect a large
    payload on busy hosts.

    returns: dict - {"LayersSize", "Images", "Containers", "Volumes", "BuildCache"} with per-object
        size fields
    """
    return _get_client(host).df()


@tool()
def host_list() -> list[dict]:
    """
    List the Docker hosts configured via DOCKER_MCP_SERVER_HOSTS.

    With a single host (or the var unset) this is the one resolved daemon; with several it is
    the set that the `host` argument selects from. The `default` entry is the one used when
    `host` is omitted; pass a `name` as the `host` argument of daemon-backed tools
    (`system_ping(host=...)` checks one entry). The `docker-mcp://hosts` resource mirrors this
    tool.

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
def system_login(
    username: str,
    password: str,
    email: str | None = None,
    registry: str | None = None,
    reauth: bool = False,
    dockercfg_path: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Authenticate with a Docker registry.

    Security: the password is sent as a tool argument, which many MCP clients log verbatim. Prefer
    running `docker login` once on the host so the `docker` module reuses the credentials cached in
    `~/.docker/config.json`, and avoid calling this tool from an agent loop. Credentials let
    `image_pull` / `image_push` reach private repositories; `system_logout` clears them.

    args:
        username - Registry username
        password - Registry password or token
        email - Registry account email
        registry - URL to the registry (defaults to Docker Hub)
        reauth - Force re-authentication even if valid credentials exist
        dockercfg_path - Path to a custom dockercfg file
    returns: dict - The login response: {"Status"} always; "IdentityToken" only when the registry issues one
    """
    return _get_client(host).login(
        username=username,
        password=password,
        email=email,
        registry=registry,
        reauth=reauth,
        dockercfg_path=dockercfg_path,
    )


@tool()
def system_logout(registry: str | None = None, host: str | None = None) -> dict:
    """
    Clear cached registry credentials from this server's in-memory Docker client.

    docker-py / the Engine have no true logout: `system_login` validates against the registry (the daemon's
    `/auth` is stateless) and caches credentials in-process. This drops that in-memory cache; it does
    NOT contact the daemon or touch the host's `~/.docker/config.json`. With no `registry`, clears
    every cached credential; pass one to clear just that entry (key must match `system_login`; Docker Hub
    is cached under "docker.io"). `system_close`/`system_reconnect` also clear it by discarding the client.

    Reaches into a private docker-py attribute (`api._auth_configs`); degrades to clearing nothing if
    that internal shape changes.

    args:
        registry - Registry key to clear, or None to clear every cached credential
    returns: dict - {"cleared": [<registry keys removed>]}
    """
    api = _get_client(host).api
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
def system_events(
    since: str | None = None,
    until: str | None = None,
    filters: dict | None = None,
    limit: int = 100,
    timeout_seconds: float = 30.0,
    host: str | None = None,
) -> list:
    """
    Stream real-time events from the Docker server, bounded by `limit` events or `timeout_seconds`.

    Returns when `limit` events are collected or `timeout_seconds` elapses, whichever comes first
    (`limit` caps memory; `timeout_seconds` caps how long the call blocks — without it a quiet daemon
    would block indefinitely, since the stream only yields on an actual event).

    Caveat for `ssh://` daemons: docker-py can't cancel an SSH stream, so the `timeout_seconds`
    watchdog can't interrupt a fully idle stream — bound with `until`/`limit` (or a non-SSH endpoint).

    "Wait for the next matching event" idiom: pass `limit=1` with `filters` narrowed to what you
    care about (e.g. `{"type": "container", "event": "health_status"}`) and a generous
    `timeout_seconds`. This blocks until that one event arrives (or the timeout elapses, returning an
    empty list) instead of re-polling a snapshot on a timer — there's no separate wait tool for this
    since the filtering this call already does covers it.

    args:
        since - Show events created since this timestamp
        until - Show events created until this timestamp
        filters - Filters to apply to the event stream
        limit - Max events to return (default 100)
        timeout_seconds - Max wall-clock seconds before returning what was collected (default 30)
    returns: list - A list of decoded event dicts (length <= limit)
    """
    stream = _get_client(host).events(since=since, until=until, filters=filters, decode=True)
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
def system_close(host: str | None = None) -> bool:
    """
    Close and drop pooled Docker client connection(s); each is rebuilt lazily on next use.

    Use this to force a stale or errored connection to be discarded. Prefer `system_reconnect` when you
    want to immediately re-establish the connection rather than wait for the next tool call to
    trigger a lazy rebuild. With `host` omitted every pooled client is closed (unlike other tools,
    where omitting it means the default host). Closing clients does not affect running containers.

    returns: bool - True once closed
    """
    with _client_lock:
        labels = list(_clients) if host is None else [_resolve_host(host).label]
        for label in labels:
            client = _clients.pop(label, None)
            if client is not None:
                _close_client_quietly(client)
    return True


@tool()
def system_reconnect(host: str | None = None) -> dict:
    """
    Rebuild a pooled Docker client from its configured endpoint, to recover a wedged connection.

    Validates the rebuilt client before swapping in (and only then closes the old one), so a failed
    rebuild leaves the working client in place. Rebuilds the default host's client when `host` is
    omitted. It CANNOT retarget to a different daemon — to add or change a daemon, edit
    DOCKER_MCP_SERVER_HOSTS and restart. `system_close` closes pooled clients without rebuilding;
    `host_list` shows the configured endpoints.

    returns: dict - the rebuilt host's version info (same shape as `system_version`), confirming connectivity
    """
    resolved = _resolve_host(host)
    label = resolved.label
    try:
        new_client = _build_client(resolved)
    except _CONNECT_ERRORS as exc:
        raise RuntimeError(f"Could not build a Docker client for host {label!r}: {exc}") from exc
    try:
        version_info = new_client.version()
    except _CONNECT_ERRORS as exc:
        _close_client_quietly(new_client)
        raise RuntimeError(
            f"Built a Docker client for host {label!r} but the daemon is unreachable: {exc}. "
            f"Kept the previous client; check the endpoint and try again."
        ) from exc
    with _client_lock:
        old_client = _clients.get(label)
        _clients[label] = new_client
    if old_client is not None and old_client is not new_client:
        _close_client_quietly(old_client)
    return version_info


def _connection_help(exc: BaseException, host: Host | None) -> str:
    """OS-aware guidance, emitted when the startup ping of the default host fails, for getting it reachable."""
    lines = [f"docker-mcp-server: cannot reach the Docker daemon ({exc})."]
    url = host.url if host is not None else None
    if host is not None and url:
        lines.append(f"  Default host {host.label!r} resolves to {url} — verify that endpoint is reachable.")
    if url and url.startswith("ssh://"):
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


def _host_tag(host: Host) -> str:
    """A host's label with brief `(ro, remote)` annotations, for the boot roster of the other hosts."""
    tags = []
    if host.read_only:
        tags.append("ro")
    if host.url and not host.url.startswith(("unix://", "npipe://")):
        tags.append("remote")
    return host.label + (f" ({', '.join(tags)})" if tags else "")


def _connection_summary(client: docker.DockerClient, host: Host) -> str:
    """One-line confirmation of the default daemon reached, the self-guard status, and a no-connect
    roster of the other configured hosts (so boot shows the topology without dialing them)."""
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
            f"; self-termination guard active for container {_self_container_id[:12]} on host {_self_host_label!r}"
            if _self_container_id
            else "; self-termination guard inactive (could not identify own container)"
        )
    others = [_host_tag(h) for h in _host_registry().values() if h.label != host.label]
    roster = f" | other hosts (lazy): {', '.join(others)}" if others else ""
    return f"docker-mcp-server: connected to default host {host.label!r} — {os_name}{suffix}{note}.{roster}"


def startup_preflight() -> None:
    """
    Best-effort startup diagnostics, written only to stderr (stdout is the MCP stdio channel).

    Pings the default host; on failure prints OS-aware connection guidance and returns without raising,
    so a client that only wants the tool list still starts (other hosts connect lazily). On success,
    pins this server's own container id + host for the self-termination guard (when containerized,
    detected against the self host — the local one, which may differ from the default) and prints a
    one-line confirmation plus a roster of the other configured hosts. Never raises.
    """
    global _self_container_id, _self_host_label
    scrub_unresolved_env()
    default = _default_host()
    try:
        client = _get_client()
        client.ping()
    except Exception as exc:  # noqa: BLE001 — startup diagnostics must never abort the server
        print(_connection_help(exc, default), file=sys.stderr, flush=True)
        return
    if in_container():
        self_host = _self_host()
        if self_host is not None:
            try:
                detected = _detect_self_container_id(_get_client(self_host.label))
            except _CONNECT_ERRORS:
                detected = None
            if detected is not None:
                _self_container_id = detected
                _self_host_label = self_host.label
    print(_connection_summary(client, default), file=sys.stderr, flush=True)

"""Tools for the daemon itself, and for this server's view of it.

What the daemon is and what it holds, the registry credentials it uses, and which of the
configured hosts are reachable from here.
"""

# library of mcp tools relating to the system domain: daemon info/auth and client connection control

import inspect
import ipaddress
import math
import os
import re
import socket
import sys
import threading
import urllib.parse
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

import docker
import requests.exceptions
from docker.utils import parse_repository_tag
from docker.errors import DockerException
from docker.models.containers import Container

from docker_mcp.exceptions import RemoteFailureError, ToolInputError, ToolRefusalError
from docker_mcp._env import scrub_unresolved_env
from docker_mcp._hosts import (
    Host,
    default as _default_host,
    is_multi as _is_multi,
    is_ssh_url,
    registry as _host_registry,
    resolve as _resolve_host,
    resolve_auto,
)
from docker_mcp.server import disable_switch_warnings, is_domain_disabled, tool
from docker_mcp.tools._ssh_proxy import _CONNECT_TIMEOUT_CAP_SECONDS, connect_socket_with_family_fallback, parse_ssh_url
from docker_mcp.tools._utils import classify_host_kernel, close_stream_quietly, env_flag, in_container

# One lazily-built docker-py client per configured host label (the pool). FastMCP runs sync tools
# concurrently in a worker threadpool, so building in `_get_client`, teardown in `close`, and the swap
# in `reconnect` must not race (e.g. two threads each building a client, or one using a client another
# closed) - `_client_lock` guards every read/mutation of `_clients`.
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
# fires when a call targets this host - our own container can't exist on any other daemon.
_self_host_label: str | None = None
_SELF_TERMINATE_OVERRIDE_ENV = "DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE"


def _detect_self_container_id(client: docker.DockerClient) -> str | None:
    """Resolve the full id of the container this server runs in, or None if it can't be determined.

    Docker sets the container's short id as its hostname by default, so we look that up via the
    daemon. Returns None if the hostname was overridden (`--hostname`) or the lookup fails - the
    self-termination guard then stays inert rather than guessing.

    Args:
        client: a connected client for the host to look on

    Returns:
        str or None: this container's full id, or None when it cannot be determined
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
    """The host the server's own container runs on.

    The first configured host on a local transport (unix://, npipe://, or the platform default).
    Self-detection and the self-termination guard key off this; a remote-only config returns None,
    leaving the guard inert, because our container cannot be on a remote.

    Returns:
        Host or None: the host the server's own container runs on, or None when it cannot be
            determined
    """
    for host in _host_registry().values():
        if host.url is None or host.url.startswith(("unix://", "npipe://")):
            return host
    return None


def guard_not_self(container: Container, host: str | None = None) -> None:
    """Refuse a destructive lifecycle action against this server's own container.

    An accident guard, not a security boundary: it only constrains calls made through this server's
    tools. A human recovering a wedged server runs `docker rm -f` from their own shell, which never
    touches this server. Inert when we aren't containerized or couldn't identify ourselves, and
    bypassable with DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE=1.

    `host` is the host the call targets: the guard only fires on the self host, since our own container
    can only exist on the daemon the server runs on.

    Args:
        container: the container the action would affect
        host: the host label the action targets, or None for the default

    Raises:
        ToolRefusalError: the container is this server's own, so the action would kill
            the process performing it.
    """
    if _self_container_id is None or container.id != _self_container_id:
        return
    if _self_host_label is not None and _resolve_host(host).label != _self_host_label:
        return
    if env_flag(_SELF_TERMINATE_OVERRIDE_ENV):
        return
    raise ToolRefusalError(
        f"Refusing to operate on the docker-mcp-server's own container ({container.short_id} "
        f"{container.name}) - this would terminate the MCP session mid-call. Set "
        f"{_SELF_TERMINATE_OVERRIDE_ENV}=1 to override, or run the action from the host shell "
        f"(e.g. `docker rm -f`), which bypasses this server entirely."
    )


def _close_client_quietly(client: docker.DockerClient) -> None:
    """Best-effort close of a discarded client; a failed teardown must not block a reconnect.

    Args:
        client: the discarded client to close
    """
    try:
        client.close()
    except Exception:  # noqa: S110, BLE001 - teardown of an already-discarded client is best-effort
        # The usual reason to discard a client is that it's already broken; swallow whatever
        # its close() raises so the caller can proceed to build a fresh one.
        pass


def _ensure_ssh_port(url: str) -> str:
    """Work around a docker-py bug for an `ssh://` URL with no explicit port.

    `docker.utils.parse_host()` hardcodes port 22 into the URL *before*
    `SSHHTTPAdapter._create_paramiko_client` ever runs, so that adapter's own `~/.ssh/config` `Port`
    fallback (which only fires while the port is still unset) never triggers - a non-22 `Port` in
    `~/.ssh/config` is silently ignored. We splice in the configured port ourselves first, reusing
    the exact `~/.ssh/config` lookup `_ssh_proxy.parse_ssh_url` already does for the CLI-backed
    tools, so both tool families honor a non-default SSH port the same way.

    Args:
        url: a DOCKER_HOST/host URL; only `ssh://` URLs with no explicit port are affected

    Returns:
        str: `url` unchanged, or with the `~/.ssh/config` port spliced into the netloc
    """
    if not is_ssh_url(url):
        return url
    parsed = urllib.parse.urlparse(url)
    try:
        has_port = parsed.port is not None
    except ValueError:
        # A malformed port (e.g. "ssh://host:abc") - leave url untouched so docker-py's own
        # validation raises its clearer error instead of this helper failing first.
        return url
    if has_port:
        return url
    try:
        # parse_ssh_url raises ValueError on a missing hostname (e.g. "ssh://" or "ssh://@") or a
        # non-integer `Port` in ~/.ssh/config - leave url untouched in either case rather than
        # raising a bare ValueError here instead of the connection error docker-py would produce.
        target = parse_ssh_url(url)
    except ValueError:
        return url
    if target.port is None:
        return url
    return urllib.parse.urlunparse(parsed._replace(netloc=f"{parsed.netloc}:{target.port}"))


def _ensure_reachable_family(url: str) -> str:
    """Work around paramiko's narrow IPv4/IPv6 fallback for an `ssh://` docker-py connection.

    `paramiko.SSHClient.connect()` (used internally by docker-py's `SSHHTTPAdapter`) resolves both
    address families but only retries the next one on `ECONNREFUSED`/`EHOSTUNREACH` - a timed-out or
    black-holed IPv6 route (`ETIMEDOUT`, what a broken IPv6 path actually produces) is never retried,
    so a host that's perfectly reachable over IPv4 fails outright. `tcp://` doesn't have this problem
    (`urllib3.util.connection.create_connection` catches any `OSError` per attempt) - only `ssh://`
    needs this. See `_ssh_proxy.connect_socket_with_family_fallback`'s docstring for the full story.

    Rather than reaching into docker-py's `SSHHTTPAdapter` internals (undocumented, and it always
    builds its own fresh `paramiko.SSHClient` with no hook to accept a pre-connected socket), this
    probes every resolved address itself with that same broad-fallback helper, then splices whichever
    address actually answered into the URL as a literal IP - so docker-py's own (unmodified) connect
    resolves trivially to the one address already proven reachable, the same trick `_ensure_ssh_port`
    above already uses to work around a different docker-py quirk. One extra short-lived probe
    connection per client build; `_get_client`'s pool means that happens once per host, not per call.

    A URL that isn't `ssh://`, has no hostname, or is already a literal address (no family ambiguity
    to resolve) is returned unchanged, as is one whose first resolved address answers - paramiko would
    connect there itself, and keeping the hostname keeps its known_hosts lookup by name - and one where
    every resolved address fails to connect, where the normal (paramiko-native) connection attempt
    still runs and produces its own error, rather than this helper raising first. Only a fallback past
    a failed address splices in a literal, and then known_hosts needs an entry for that address.

    Args:
        url: a DOCKER_HOST/host URL; only `ssh://` URLs are affected

    Returns:
        str: `url` unchanged, or with its hostname replaced by the literal address that answered after
            an earlier one failed
    """
    if not is_ssh_url(url):
        return url
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return url
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return url  # already a literal address; no family ambiguity to resolve
    port = parsed.port or 22
    try:
        sock = connect_socket_with_family_fallback(hostname, port, _CONNECT_TIMEOUT_CAP_SECONDS)
    except OSError:
        return url
    try:
        peer_host = sock.getpeername()[0]
    finally:
        sock.close()
    # paramiko checks the host key against whatever name the URL carries, so splicing in an address makes
    # it look up `[address]:port` in known_hosts, where many entries are only by name. Splice only when an
    # earlier address failed - the case this exists for - and leave the hostname when the first answered.
    try:
        first = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        # The name resolved a moment ago for the probe; if it fails now, splice as before rather than
        # hand paramiko a name it may not resolve either.
        first = None
    if peer_host == first:
        return url
    literal = f"[{peer_host}]" if ":" in peer_host else peer_host
    userinfo = f"{parsed.username}@" if parsed.username else ""
    return urllib.parse.urlunparse(parsed._replace(netloc=f"{userinfo}{literal}:{port}"))


def _from_env_no_context(**kwargs: Any) -> docker.DockerClient:
    """`docker.from_env` with docker-py's own Docker-context resolution switched off.

    **Every `from_env` call in this package must go through here** (a single choke point rather than a
    convention to remember). docker-py 7.2.0 made `from_env` resolve the active Docker CLI context
    whenever the environment yields no `base_url` - which is *our* job, not its: `_hosts.resolve_auto`
    already reads `DOCKER_CONTEXT` / config.json `currentContext`, and the answer is pinned at
    `load()` so a mid-session `docker context use` cannot silently move a label (restart to
    re-resolve). A second, independent resolution would reintroduce that drift and could disagree with
    the endpoint the CLI shell-out targets for the same label. This is why `docker[ssh]>=7.2.0` is a
    hard floor: on 7.1.0 the kwarg is not popped and reaches `kwargs_from_env` as an unexpected
    argument.

    Args:
        **kwargs: forwarded to `docker.from_env` (e.g. `environment=`)

    Returns:
        docker.DockerClient: a client that never consults a CLI context on its own
    """
    # pyright's bundled typeshed stub for `docker` predates 7.2.0 and so lacks `use_context`, while the
    # runtime signature is `**kwargs` and accepts it. tests/test_pyproject_pins.py::
    # test_the_declared_docker_floor_supports_the_kwarg_the_code_passes is the real check.
    return docker.from_env(use_context=False, **kwargs)  # pyright: ignore[reportCallIssue]


def _build_default_client() -> docker.DockerClient:
    """Client for DOCKER_HOST when set (via from_env, honoring its TLS env), else the resolved endpoint.

    auto/local resolution lives in docker_mcp._hosts now (the pure registry layer); resolve_auto() is
    the relocated _resolve_default_base_url().

    Goes through `_from_env_no_context` on all three paths - see there for why. Two of them are safe
    only incidentally (an env-derived `base_url` short-circuits docker-py's context lookup), so the
    guarantee deliberately doesn't rest on that.

    Returns:
        docker.DockerClient: a client for DOCKER_HOST, or for the resolved endpoint
    """
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host:
        fixed = _ensure_reachable_family(_ensure_ssh_port(docker_host))
        if fixed != docker_host:
            return _from_env_no_context(environment={**os.environ, "DOCKER_HOST": fixed})
        return _from_env_no_context()
    base_url = resolve_auto()
    return docker.DockerClient(base_url=base_url) if base_url else _from_env_no_context()


def _tls_from_dir(cert_dir: str) -> docker.TLSConfig:
    """A TLSConfig from Docker's conventional certs in `cert_dir`.

    Always verifies the daemon against `ca.pem`; presents a client cert (mutual TLS) only when both
    `cert.pem` and `key.pem` are present, else verifies the daemon only (e.g. a self-signed daemon
    pinned via `ca.pem`, with no client auth).

    Args:
        cert_dir: a directory holding Docker's conventional certificate names

    Returns:
        docker.tls.TLSConfig: the config built from it
    """
    directory = Path(cert_dir)
    cert, key = directory / "cert.pem", directory / "key.pem"
    client_cert = (str(cert), str(key)) if cert.exists() and key.exists() else None
    return docker.TLSConfig(client_cert=client_cert, ca_cert=str(directory / "ca.pem"), verify=True)


def _tls_config_for(host: Host) -> docker.TLSConfig | None:
    """Per-host TLS, tiered.

    The host's own `(tls=<dir>)` cert dir, else the global DOCKER_CERT_PATH / DOCKER_TLS_VERIFY env
    (mirroring from_env), else plaintext (None).

    Args:
        host: the configured host to build TLS for

    Returns:
        docker.tls.TLSConfig or None: the config, or None when the host needs no TLS
    """
    if host.cert_dir:
        return _tls_from_dir(host.cert_dir)
    if (os.environ.get("DOCKER_TLS_VERIFY") or "").strip():
        return _tls_from_dir(os.environ.get("DOCKER_CERT_PATH") or str(Path.home() / ".docker"))
    return None


def _build_client(host: Host) -> docker.DockerClient:
    """Build the docker-py client for one configured host.

    The legacy single host (DOCKER_MCP_SERVER_HOSTS unset) goes through _build_default_client so the
    existing DOCKER_HOST / from_env behaviour (and its TLS env / API-version negotiation) is preserved
    exactly - this is the ONLY path that reads DOCKER_HOST. An explicitly-configured host is built from
    its resolved URL with per-host TLS; one that resolved to the platform default (url=None, e.g. `local`
    on Windows) is built WITHOUT a base_url so it uses the platform socket/npipe and never re-reads the
    ambient DOCKER_HOST (which is ignored when DOCKER_MCP_SERVER_HOSTS is set).

    Args:
        host: the configured host to connect to

    Returns:
        docker.DockerClient: a client for that host
    """
    if not _is_multi() and not (os.environ.get("DOCKER_MCP_SERVER_HOSTS") or "").strip():
        return _build_default_client()
    tls = _tls_config_for(host)
    if host.url is None:
        return docker.DockerClient(tls=tls) if tls is not None else docker.DockerClient()
    url = _ensure_reachable_family(_ensure_ssh_port(host.url))
    return docker.DockerClient(base_url=url, tls=tls) if tls is not None else docker.DockerClient(base_url=url)


def _get_client(host: str | None = None) -> docker.DockerClient:  # noqa: DOC503
    """The pooled docker-py client for `host` (the default host when None), lazily built and cached.

    Args:
        host: the host label, or None for the default

    Returns:
        docker.DockerClient: the pooled client, built on first use

    Raises:
        RemoteFailureError: a client was built but the daemon is unreachable.
        HostGuardError: propagated from _hosts.resolve when the label is not configured.
    """
    resolved = _resolve_host(host)
    label = resolved.label
    with _client_lock:
        client = _clients.get(label)
        if client is None:
            try:
                client = _build_client(resolved)
            except _CONNECT_ERRORS as exc:
                where = resolved.url or os.environ.get("DOCKER_HOST") or "the default Docker socket"
                raise RemoteFailureError(
                    f"Cannot reach the Docker daemon for host {label!r} at {where}. Is Docker running, "
                    f"and is the endpoint correct? Underlying error: {exc}"
                ) from exc
            _clients[label] = client
        return client


@tool()
def system_ping(host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Check that the Docker server is responsive.

    The cheapest daemon health check. A failure here usually means connection config rather than
    daemon load - `system_reconnect` rebuilds a wedged client, `host_list` shows the configured
    endpoints. For daemon details use `system_version` / `system_info`.

    Returns:
        bool: True if the daemon responded successfully
    """
    return _get_client(host).ping()


@tool()
def system_version(host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Return Docker server version information.

    Engine version, API level, and per-component versions - the first thing to check for feature
    availability. `system_info` reports runtime state (counts, drivers, swarm role) instead.

    Returns:
        dict: {"Version", "ApiVersion", "MinAPIVersion", "Os", "Arch", "Components", ...}
    """
    return _get_client(host).version()


@tool()
def system_info(host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Return system-wide Docker information, like `docker info`.

    Daemon runtime state: container/image counts, storage and logging drivers, swarm role, and
    daemon warnings. Use `system_version` for version/API level and `system_df` for disk usage.

    Returns:
        dict: {"Containers", "Images", "Driver", "ServerVersion", "Swarm", "Warnings", ...}
    """
    return _get_client(host).info()


@tool()
def system_df(host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Summarize Docker disk usage: layer storage plus per-object sizes for images, containers, volumes, build cache.

    Equivalent to `docker system df`. Use it to find what to reclaim before `image_prune` /
    `container_prune` / `volume_prune` / `buildx_prune`; use `system_info` for daemon config and
    counts rather than sizes. The reply enumerates every object on the daemon, so expect a large
    payload on busy hosts.

    Returns:
        dict: {"LayersSize", "Images", "Containers", "Volumes", "BuildCache"} with per-object size fields
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

    Returns:
        list[dict]: one per host: name; url (resolved daemon URL, null = docker-py platform default); read_only;
            non_destructive (blocks destructive calls only); tls (whether a per-host cert dir is configured); default
            (the omitted-host fallback)
    """
    hosts = _host_registry()
    default_label = _default_host().label if hosts else None
    return [
        {
            "name": host.label,
            "url": host.url,
            "read_only": host.read_only,
            "non_destructive": host.non_destructive,
            "tls": host.cert_dir is not None,
            "default": host.label == default_label,
        }
        for host in hosts.values()
    ]


@tool()
def system_login(  # noqa: DOC101,DOC103
    username: str,
    password: str,
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

    Args:
        password: Registry password or token
        registry: URL to the registry (defaults to Docker Hub)
        reauth: Force re-authentication even if valid credentials exist
        dockercfg_path: A dockercfg file on the server host to use instead of `~/.docker/config.json`

    Returns:
        dict: The login response: {"Status"} always; "IdentityToken" only when the registry issues one
    """
    # No `email`: the Engine dropped it from `AuthConfig` at API v1.56 (moby's
    # `registry.AuthConfig` no longer declares the field), and nothing had read it since
    # `docker login --email` was removed in 17.06 - `/auth` decodes the body without
    # DisallowUnknownFields, so the value is discarded rather than rejected. docker-py still puts
    # `email` in the request body and in its credential cache, so exposing it advertised a
    # parameter no daemon acts on. Do not re-add it.
    return _get_client(host).login(
        username=username,
        password=password,
        registry=registry,
        reauth=reauth,
        dockercfg_path=dockercfg_path,
    )


@tool()
def system_logout(registry: str | None = None, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Clear cached registry credentials from this server's in-memory Docker client.

    docker-py / the Engine have no true logout: `system_login` validates against the registry (the daemon's
    `/auth` is stateless) and caches credentials in-process. This drops that in-memory cache; it does
    NOT contact the daemon or touch the host's `~/.docker/config.json`. With no `registry`, clears
    every cached credential; pass one to clear just that entry (key must match `system_login`; Docker Hub
    is cached under "docker.io"). `system_close`/`system_reconnect` also clear it by discarding the client.

    Reaches into a private docker-py attribute (`api._auth_configs`); degrades to clearing nothing if
    that internal shape changes.

    Args:
        registry: Registry key to clear, or None to clear every cached credential

    Returns:
        dict: {"cleared": [<registry keys removed>]}
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
def system_events(  # noqa: DOC101,DOC103
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
    (`limit` caps memory; `timeout_seconds` caps how long the call blocks - without it a quiet daemon
    would block indefinitely, since the stream only yields on an actual event).

    Caveat for `ssh://` daemons: docker-py can't cancel an SSH stream, so the `timeout_seconds`
    watchdog can't interrupt a fully idle stream - bound with `until`/`limit` (or a non-SSH endpoint).

    "Wait for the next matching event" idiom: pass `limit=1` with `filters` narrowed to what you
    care about (e.g. `{"type": "container", "event": "health_status"}`) and a generous
    `timeout_seconds`. This blocks until that one event arrives (or the timeout elapses, returning an
    empty list) instead of re-polling a snapshot on a timer - there's no separate wait tool for this
    since the filtering this call already does covers it.

    Args:
        since: Show events created since this timestamp
        until: Show events created until this timestamp
        filters: Filters to apply to the event stream
        limit: Max events to return
        timeout_seconds: Max wall-clock seconds before returning what was collected

    Returns:
        list: A list of decoded event dicts (length <= limit)
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


# --- system_wait: one wait for everything worth waiting for ---------------------------------------
#
# Each kind is answered by a helper in the module of the domain it belongs to, imported when called:
# those modules import `_get_client` from here, so importing them at the top would be circular. A kind
# is offered only while its domain is enabled, so `kind` and `until` are built from the enabled set at
# import: a disabled domain's waits are never advertised, rather than advertised and refused (SU.8.1).
_WAIT_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "container": ("containers", ("not-running", "next-exit", "removed", "healthy", "log-match")),
    "service": ("services", ("running", "update-converged")),
    "node": ("nodes", ("ready", "down", "disconnected", "unknown")),
    "stack": ("stack", ("converged", "removed")),
    "compose": ("compose", ("running", "healthy", "exited")),
    "registry_tag": ("registry", ("present",)),
}
# Never empty: `containers` cannot be disabled (ALWAYS_ON_DOMAINS), so the container kind is always here.
_WAIT_ENABLED = tuple(kind for kind, (domain, _) in _WAIT_KINDS.items() if not is_domain_disabled(domain))
WaitKind = Literal[_WAIT_ENABLED]  # pyright: ignore[reportInvalidTypeForm]
WaitUntil = Literal[  # pyright: ignore[reportInvalidTypeForm]
    tuple(dict.fromkeys(until for kind in _WAIT_ENABLED for until in _WAIT_KINDS[kind][1]))
]
# The registry is polled over HTTPS and may rate-limit; everything else is a local daemon read.
_WAIT_DEFAULT_POLL = {"registry_tag": 5.0}
# A socket timeout ends up in a `timeval`, whose seconds are a 32-bit `long` on Windows (Winsock), so this is
# the largest wait every platform can pass to the daemon: about 68 years.
_MAX_WAIT_SECONDS = 2**31 - 1
# Parameters only one kind uses: they leave the schema with their kind, so nothing disabled is advertised.
_WAIT_KIND_PARAMS: dict[str, tuple[str, ...]] = {
    "container": ("pattern", "regex"),
    "service": ("replicas",),
    "compose": ("services",),
    "registry_tag": ("username", "password", "limit"),
}
# The summary when some kind is disabled; with every kind enabled the hand-written first line stands,
# because it is tuned for what `tool_list` matches within one source line.
_WAIT_SUMMARY_CLAUSES = {
    "container": "a container exits or is healthy",
    "service": "a service converges",
    "node": "a node is ready",
    "stack": "a stack converges or is removed",
    "compose": "a compose project is up or done",
    "registry_tag": "a tag lands in a registry",
}
# What each kind's helper names its target, so the result can carry it once as `target`.
_WAIT_TARGET_KEYS = frozenset({"container", "service", "node", "stack", "project", "repository", "tag"})


def _wait_registry_target(target: str) -> tuple[str, str]:
    """Split a registry_tag wait's target into repository and tag.

    Args:
        target: "repository:tag", e.g. "ghcr.io/org/app:1.2" or "localhost:5000/app:dev"

    Returns:
        tuple: the repository and the tag

    Raises:
        ToolInputError: the target names a digest or no tag
    """
    repository, tag = parse_repository_tag(target)
    if "@" in target or not tag:
        raise ToolInputError(
            f"system_wait(kind='registry_tag') needs target as 'repository:tag', got {target!r}: "
            "a digest is already immutable, so there is nothing to wait for, and with no tag there is "
            "nothing to look for"
        )
    return repository, tag


def _wait_check_options(kind: str, until: str, given: dict[str, bool]) -> None:
    """Refuse an `until` or a kind-specific option that does not belong to `kind`.

    Args:
        kind: the kind asked for
        until: the condition asked for
        given: which kind-specific options the caller set, by name

    Raises:
        ToolInputError: `kind` is not enabled, `until` is not one of its conditions, or an option
            given belongs to another kind or condition
    """
    if kind not in _WAIT_ENABLED:
        raise ToolInputError(f"system_wait cannot wait on kind={kind!r} here; enabled kinds: {list(_WAIT_ENABLED)}")
    untils = _WAIT_KINDS[kind][1]
    if until not in untils:
        raise ToolInputError(f"system_wait(kind={kind!r}) waits until one of {list(untils)}, not {until!r}")
    belongs = {
        "pattern": kind == "container" and until == "log-match",
        "regex": kind == "container" and until == "log-match",
        "replicas": kind == "service" and until == "running",
        "services": kind == "compose",
        "username": kind == "registry_tag",
        "password": kind == "registry_tag",
        "limit": kind == "registry_tag",
        # registry_tag talks HTTPS to the registry; no daemon is involved, so a host would mean nothing.
        "host": kind != "registry_tag",
    }
    stray = [option for option, set_ in given.items() if set_ and not belongs[option]]
    if stray:
        raise ToolInputError(f"system_wait(kind={kind!r}, until={until!r}) does not take {stray}")


def _fit_to_enabled_kinds[F: Callable[..., Any]](func: F) -> F:
    """Take a disabled kind out of the wait tool's description and schema, so neither offers it.

    Drops the kind's bullet, the `Args:` entries and parameters only it uses, and rebuilds the summary
    from the enabled kinds. Applied beneath `@tool` so it runs first: the registered description and
    schema are the trimmed ones, while the source keeps every line for the linters that read it. With
    every kind enabled it changes nothing.

    Args:
        func: the wait tool

    Returns:
        Callable: the same function, its `__doc__` and `__signature__` fitted to the enabled kinds
    """
    disabled = [kind for kind in _WAIT_KINDS if kind not in _WAIT_ENABLED]
    if not disabled:
        return func
    dropped = {param for kind in disabled for param in _WAIT_KIND_PARAMS.get(kind, ())}
    kept, skipping, item_indent, in_args = [], False, 0, False
    for line in (func.__doc__ or "").splitlines(keepends=True):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.endswith(":") and " " not in stripped:
            in_args = stripped == "Args:"
        bullet = re.match(r"^(\s*)- (\w+):", line)
        entry = re.match(r"^(\s*)(\w+): ", line) if in_args else None
        if bullet:
            item_indent = len(bullet.group(1))
            skipping = bullet.group(2) in disabled
        elif entry and entry.group(2) in func.__code__.co_varnames:
            item_indent = len(entry.group(1))
            skipping = entry.group(2) in dropped
        elif not stripped or indent <= item_indent:
            # A continuation is indented past its item. Measured relative to the item, because the compiler
            # dedents docstrings (Python 3.13+), so absolute columns differ by context.
            skipping = False
        if not skipping:
            kept.append(line)
    clauses = [_WAIT_SUMMARY_CLAUSES[kind] for kind in _WAIT_ENABLED]
    summary = clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + " or " + clauses[-1]
    body = "".join(kept).lstrip("\n").split("\n", 1)[1]
    func.__doc__ = f"Wait until {summary}.\n{body}"
    signature = inspect.signature(func)
    func.__signature__ = signature.replace(  # pyright: ignore[reportFunctionMemberAccess]
        parameters=[param for name, param in signature.parameters.items() if name not in dropped]
    )
    return func


@tool()
@_fit_to_enabled_kinds
def system_wait(  # noqa: DOC101,DOC103,DOC501,DOC503
    kind: WaitKind,  # pyright: ignore[reportInvalidTypeForm] - built from the enabled kinds at import
    target: str,
    until: WaitUntil,  # pyright: ignore[reportInvalidTypeForm]
    timeout_seconds: float = 600.0,
    poll_interval: float | None = None,
    replicas: int | None = None,
    pattern: str | None = None,
    regex: bool = False,
    services: list[str] | None = None,
    username: str | None = None,
    password: str | None = None,
    limit: int = 1000,
    host: str | None = None,
) -> dict:
    """
    Wait until a container/compose project exits or is healthy, a service/stack converges, a node/registry tag is ready.

    The one wait tool: action tools return once their work is started, and this waits for the result. For
    a status read without waiting use `container_inspect`, `service_ps` or `compose_ps`; to follow changes
    as they happen, `system_events`. Never raises on timeout. `met: false` without `timed_out` means the
    condition can no longer be met (the target stopped, has nothing to wait for, or is gone): `detail`
    says why.

    `kind`: `target` - `until` values:
    - container: id or name - not-running (exit code in `detail.status_code`), next-exit, removed,
      healthy (no HEALTHCHECK: not met), log-match (finds `pattern` in recent logs)
    - service: swarm manager only; id or name - running (all desired tasks, or `replicas`), update-converged
    - node: swarm manager only; id or hostname - ready, down, disconnected, unknown
    - stack: swarm manager only; stack name - converged (every service's tasks running, updates finished), removed
    - compose: project name from `compose_list` - running, healthy (no HEALTHCHECK: running is enough;
      a job others wait on may have exited 0), exited (exit codes in `detail`); `services` narrows it
    - registry_tag: "repository:tag" - present (polls the registry over HTTPS every 5 s by default; no
      daemon, no `host`)

    Args:
        poll_interval: Seconds between checks (default 2, unless the kind says otherwise), capped by the time left
        pattern: log-match: a plain substring, or with `regex=True` a regular expression (trusted patterns
            only: a hostile one run over attacker-influenced logs can exhaust CPU)
        username: registry_tag: overrides DOCKER_MCP_SERVER_REGISTRY_USERNAME
        password: registry_tag: overrides DOCKER_MCP_SERVER_REGISTRY_PASSWORD
        limit: registry_tag: most tags read per poll; a tag beyond it is never found

    Returns:
        dict: {"kind", "target", "until", "met", "timed_out", "waited_seconds", "detail"}
    """
    _wait_check_options(
        kind,
        until,
        {
            "pattern": pattern is not None,
            "regex": regex,
            "replicas": replicas is not None,
            "services": bool(services),
            "username": username is not None,
            "password": password is not None,
            "limit": limit != 1000,
            "host": host is not None,
        },
    )
    if until == "log-match" and not pattern:
        raise ToolInputError("system_wait(until='log-match') needs `pattern`")
    # There is no ceiling on how long a caller may wait, but the container stop conditions hand the timeout
    # to a socket, and a NaN deadline never passes. _MAX_WAIT_SECONDS is the most every supported
    # platform's socket timeout can hold; past it the stop conditions fail with a builtin OverflowError.
    for label, value in (("timeout_seconds", timeout_seconds), ("poll_interval", poll_interval)):
        if value is not None and not (math.isfinite(value) and value <= _MAX_WAIT_SECONDS):
            raise ToolInputError(
                f"system_wait {label} must be a finite number of seconds, at most {_MAX_WAIT_SECONDS}; got {value!r}."
            )
    interval = poll_interval if poll_interval is not None else _WAIT_DEFAULT_POLL.get(kind, 2.0)
    if kind == "container":
        from docker_mcp.tools.containers import _wait_container

        result = _wait_container(target, until, timeout_seconds, interval, pattern=pattern, regex=regex, host=host)
    elif kind == "service":
        from docker_mcp.tools.services import _wait_service

        result = _wait_service(target, until, timeout_seconds, interval, replicas=replicas, host=host)
    elif kind == "node":
        from docker_mcp.tools.nodes import _wait_node

        result = _wait_node(target, until, timeout_seconds, interval, host=host)
    elif kind == "stack":
        from docker_mcp.tools.stack import _wait_stack

        result = _wait_stack(target, until, timeout_seconds, interval, host=host)
    elif kind == "compose":
        from docker_mcp.tools.compose import _wait_compose

        result = _wait_compose(target, until, timeout_seconds, interval, services=services, host=host)
    else:  # "registry_tag"
        from docker_mcp.tools.registry import _wait_registry_tag

        repository, tag = _wait_registry_target(target)
        result = _wait_registry_tag(
            repository, tag, timeout_seconds, interval, username=username, password=password, limit=limit
        )
    shared = {"until", "met", "timed_out", "waited_seconds"}
    return {
        "kind": kind,
        "target": target,
        "until": until,
        "met": result["met"],
        "timed_out": result["timed_out"],
        "waited_seconds": result["waited_seconds"],
        "detail": {k: v for k, v in result.items() if k not in shared and k not in _WAIT_TARGET_KEYS},
    }


@tool()
def system_close(host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Close and drop pooled Docker client connection(s); each is rebuilt lazily on next use.

    Use this to force a stale or errored connection to be discarded. Prefer `system_reconnect` when you
    want to immediately re-establish the connection rather than wait for the next tool call to
    trigger a lazy rebuild. With `host` omitted every pooled client is closed (unlike other tools,
    where omitting it means the default host). Closing clients does not affect running containers.

    Returns:
        bool: True once closed
    """
    with _client_lock:
        labels = list(_clients) if host is None else [_resolve_host(host).label]
        for label in labels:
            client = _clients.pop(label, None)
            if client is not None:
                _close_client_quietly(client)
    return True


@tool()
def system_reconnect(host: str | None = None) -> dict:  # noqa: DOC101,DOC103,DOC501,DOC503
    """
    Rebuild a pooled Docker client from its configured endpoint, to recover a wedged connection.

    Validates the rebuilt client before swapping in (and only then closes the old one), so a failed
    rebuild leaves the working client in place. Rebuilds the default host's client when `host` is
    omitted. It CANNOT retarget to a different daemon - to add or change a daemon, edit
    DOCKER_MCP_SERVER_HOSTS and restart. `system_close` closes pooled clients without rebuilding;
    `host_list` shows the configured endpoints.

    Returns:
        dict: the rebuilt host's version info (same shape as `system_version`), confirming connectivity
    """
    resolved = _resolve_host(host)
    label = resolved.label
    try:
        new_client = _build_client(resolved)
    except _CONNECT_ERRORS as exc:
        raise RemoteFailureError(f"Could not build a Docker client for host {label!r}: {exc}") from exc
    try:
        version_info = new_client.version()
    except _CONNECT_ERRORS as exc:
        _close_client_quietly(new_client)
        raise RemoteFailureError(
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
    """OS-aware guidance, emitted when the startup ping of the default host fails, for getting it reachable.

    Args:
        exc: the failure the startup ping raised
        host: the host it was pinging, or None when none is resolved

    Returns:
        str: OS-aware guidance for getting the daemon reachable
    """
    lines = [f"docker-mcp-server: cannot reach the Docker daemon ({exc})."]
    url = host.url if host is not None else None
    if host is not None and url:
        lines.append(f"  Default host {host.label!r} resolves to {url} - verify that endpoint is reachable.")
    if is_ssh_url(url):
        # ssh:// uses the pure-Python paramiko transport. Its failure modes are auth/host-key, not
        # the socket-mount issues the rest of this function covers, so give targeted hints and stop.
        lines.append(
            "  This is an ssh:// endpoint (paramiko transport). Common causes: the SSH key isn't "
            "loaded (run `ssh-add`, and forward SSH_AUTH_SOCK), or the host key isn't in "
            "known_hosts - paramiko rejects unknown hosts. Add the host key only after verifying its "
            "fingerprint out of band: connect once with `ssh <host>` and confirm the prompt, or "
            "compare `ssh-keyscan <host> | ssh-keygen -lf -` against a trusted fingerprint before "
            "trusting it (don't blind-append `ssh-keyscan` output - that trusts any key returned, "
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
            "  Host looks like Windows/WSL2 - the engine listens on a named pipe, not a unix socket. "
            "Pass `-e DOCKER_HOST=tcp://host.docker.internal:2375` (enable the TCP endpoint in Docker "
            "Desktop) or mount the WSL-side socket."
        )
    elif kind == "docker-desktop":
        lines.append(
            "  Host looks like Docker Desktop (macOS) - mount the Desktop socket: "
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
    """A host's label with brief `(ro, remote)` / `(nd, remote)` annotations.

    For the boot roster of the other hosts. `nd` only shows when `ro` is absent, since `ro` already
    implies it.

    Args:
        host: the configured host to label

    Returns:
        str: its label, annotated with ``(ro, remote)`` or ``(nd, remote)`` where they apply
    """
    tags = []
    if host.read_only:
        tags.append("ro")
    elif host.non_destructive:
        tags.append("nd")
    if host.url and not host.url.startswith(("unix://", "npipe://")):
        tags.append("remote")
    return host.label + (f" ({', '.join(tags)})" if tags else "")


def _connection_summary(client: docker.DockerClient, host: Host) -> str:
    """One-line confirmation of the daemon reached and the hosts configured.

    Names the default daemon, the self-guard status, and a no-connect roster of the other configured
    hosts, so boot shows the topology without dialing them.

    Args:
        client: the connected client
        host: the host it reached

    Returns:
        str: a one-line confirmation of the daemon reached and the hosts configured
    """
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
    return f"docker-mcp-server: connected to default host {host.label!r} - {os_name}{suffix}{note}.{roster}"


def startup_preflight() -> None:
    """Best-effort startup diagnostics, written only to stderr (stdout is the MCP stdio channel).

    Pings the default host; on failure prints OS-aware connection guidance and returns without raising,
    so a client that only wants the tool list still starts (other hosts connect lazily). On success,
    pins this server's own container id + host for the self-termination guard (when containerized,
    detected against the self host - the local one, which may differ from the default) and prints a
    one-line confirmation plus a roster of the other configured hosts. Never raises.
    """
    global _self_container_id, _self_host_label
    # Before the ping, so it is shown even when the daemon cannot be reached.
    for warning in disable_switch_warnings():
        print(f"docker-mcp-server: WARNING: {warning}", file=sys.stderr, flush=True)
    scrub_unresolved_env()
    default = _default_host()
    try:
        client = _get_client()
        client.ping()
    except Exception as exc:  # noqa: BLE001 - startup diagnostics must never abort the server
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

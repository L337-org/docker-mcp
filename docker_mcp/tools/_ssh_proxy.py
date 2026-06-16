# Per-call localhost TCP proxy that bridges CLI-backed tools (Compose, Buildx, Context, Scout) to
# an ssh:// daemon through paramiko, the same pure-Python transport docker-py already uses for the
# SDK-backed tools. This lets the `docker` CLI authenticate with our paramiko credentials instead
# of shelling out to the system `ssh` client, so both tool families behave identically over SSH.
#
# Mechanism (see docker-py's docker/transport/sshconn.py): both the docker CLI and docker-py run
# `docker system dial-stdio` over an SSH session channel — that command bridges the remote
# /var/run/docker.sock to stdin/stdout, one channel per API connection. docker-py opens those
# channels directly on its own paramiko transport; here we accept plain TCP connections from the
# `docker` CLI on 127.0.0.1 and bridge each one to its own `dial-stdio` channel on a single shared
# paramiko connection, full-duplex, until either side closes.

import contextlib
import logging
import os
import socket
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

import paramiko

logger = logging.getLogger(__name__)

_RECV_BUFFER_SIZE = 32_768
_ACCEPT_POLL_SECONDS = 0.5
_JOIN_TIMEOUT_SECONDS = 5.0


class BidirectionalStream(Protocol):
    """Minimal duplex-stream shape a channel factory must return.

    Both `socket.socket` and `paramiko.Channel` satisfy this already, which is what lets tests
    inject a plain socket (e.g. one end of `socket.socketpair()`) in place of a real SSH channel.
    """

    def recv(self, n: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def shutdown(self, how: int, /) -> None: ...

    def close(self) -> None: ...


ChannelFactory = Callable[[], BidirectionalStream]


@dataclass(frozen=True)
class SshTarget:
    """Resolved connection parameters for an ssh:// DOCKER_HOST, after ~/.ssh/config lookup."""

    hostname: str
    port: int | None
    username: str | None
    key_filename: str | None
    proxycommand: str | None


def parse_ssh_url(url: str) -> SshTarget:
    """
    Parse a DOCKER_HOST=ssh://... URL into paramiko connection parameters.

    Applies the same ~/.ssh/config lookups (Hostname, Port, User, IdentityFile, ProxyCommand)
    that docker-py's `SSHHTTPAdapter._create_paramiko_client` performs, so this proxy resolves the
    same target docker-py (and the system `ssh` client) would for the same URL.

    args: url: str - a DOCKER_HOST value starting with 'ssh://'
    returns: SshTarget - hostname/port/username/key_filename/proxycommand after config-file lookup
    """
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Could not parse a hostname from ssh URL: {url!r}")
    port = parsed.port
    username = parsed.username
    key_filename: str | None = None
    proxycommand: str | None = None

    ssh_config_file = os.path.expanduser("~/.ssh/config")
    if os.path.exists(ssh_config_file):
        conf = paramiko.SSHConfig()
        with open(ssh_config_file) as f:
            conf.parse(f)
        host_config = conf.lookup(hostname)
        if "proxycommand" in host_config:
            proxycommand = host_config["proxycommand"]
        if "hostname" in host_config:
            hostname = host_config["hostname"]
        if port is None and "port" in host_config:
            port = int(host_config["port"])
        if username is None and "user" in host_config:
            username = host_config["user"]
        if "identityfile" in host_config:
            identity = host_config["identityfile"]
            # paramiko's SSHConfig.lookup() already tokenizes `~` to the home dir itself; this
            # expanduser() call is a no-op backstop in case a future value still has a literal `~`.
            key_filename = os.path.expanduser(identity[0] if isinstance(identity, list) else identity)

    return SshTarget(
        hostname=hostname, port=port, username=username, key_filename=key_filename, proxycommand=proxycommand
    )


def connect_ssh_client(docker_host: str, *, timeout: float | None = None) -> paramiko.SSHClient:
    """
    Build and connect a paramiko SSHClient for a DOCKER_HOST=ssh://... URL.

    Mirrors docker-py's `SSHHTTPAdapter._create_paramiko_client` defaults: system host keys are
    loaded and an unknown host key is rejected (`RejectPolicy`, not auto-add); `allow_agent` and
    `look_for_keys` are left at paramiko's own defaults (both True) rather than overridden, exactly
    as docker-py leaves them, so this proxy authenticates with the same credentials docker-py would
    pick for the same URL. Unlike docker-py, `port` is omitted from the connect kwargs entirely when
    unresolved rather than passed through as `None` — paramiko's own default (22) only applies when
    the kwarg is absent, and an explicit `None` instead resolves to port 0, which always refuses.

    `timeout`, when given, bounds the raw socket connect *and* the banner/auth handshake phases
    (paramiko tracks these as separate phases with separate, otherwise-unbounded defaults) so a
    slow or filtered host can't hang past the caller's own deadline — see `run_docker`, whose
    `timeout` argument only wraps `subprocess.run` and would otherwise leave this paramiko connect
    (which runs beforehand, to set up the local proxy) unbounded.

    args:
        docker_host: str - a DOCKER_HOST value starting with 'ssh://'
        timeout: float | None - seconds to bound the connect/banner/auth phases; None means
                 paramiko's own (unbounded) defaults
    returns: paramiko.SSHClient - already connected; caller is responsible for closing it
    """
    target = parse_ssh_url(docker_host)
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    connect_kwargs: dict = {"hostname": target.hostname, "username": target.username}
    if target.port is not None:
        connect_kwargs["port"] = target.port
    if target.key_filename:
        connect_kwargs["key_filename"] = target.key_filename
    if target.proxycommand:
        connect_kwargs["sock"] = paramiko.ProxyCommand(target.proxycommand)
    if timeout is not None:
        connect_kwargs["timeout"] = timeout
        connect_kwargs["banner_timeout"] = timeout
        connect_kwargs["auth_timeout"] = timeout
    client.connect(**connect_kwargs)
    return client


def paramiko_dial_stdio_factory(ssh_client: paramiko.SSHClient) -> ChannelFactory:
    """
    Build a channel factory that opens a fresh `docker system dial-stdio` channel on `ssh_client`.

    This is the production `ChannelFactory` for `SshDialStdioProxy`: one already-connected SSH
    transport is shared for the lifetime of a single CLI invocation, and a new session channel is
    opened per accepted local connection (the docker CLI may open more than one).

    args: ssh_client: paramiko.SSHClient - an already-connected client (see `connect_ssh_client`)
    returns: ChannelFactory - a zero-arg callable returning a new exec channel on each call
    """

    def factory() -> BidirectionalStream:
        transport = ssh_client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport is not connected.")
        channel = transport.open_session()
        channel.exec_command("docker system dial-stdio")
        return channel

    return factory


def _close_quietly(closable: BidirectionalStream) -> None:
    """Best-effort shutdown+close; shutdown first reliably unblocks a peer thread's blocking recv().

    Catches broadly on purpose: `closable` may be a `socket.socket` (raises `OSError`) or a
    `paramiko.Channel` (can raise `paramiko.SSHException` or `EOFError` on an already-torn-down
    transport) — either way this is teardown-path cleanup that must never leak out and abandon
    the caller's pump threads unjoined.
    """
    try:
        closable.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: S110, BLE001 — best-effort close; see docstring for why it's broad
        pass
    try:
        closable.close()
    except Exception:  # noqa: S110, BLE001 — best-effort close; see docstring for why it's broad
        pass


class SshDialStdioProxy:
    """
    Localhost TCP listener that bridges each accepted connection to a stream from `channel_factory`.

    Meant to be used per-call (one instance per `run_docker` invocation), not as a long-lived
    session proxy: `start()` binds an ephemeral port, `stop()` tears the listener and every pumped
    connection down. The channel factory is injectable so tests can exercise accept/pump/teardown
    with a fake duplex stream (e.g. one end of `socket.socketpair()`) instead of a real SSH session.
    """

    def __init__(self, channel_factory: ChannelFactory) -> None:
        self._channel_factory = channel_factory
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._pump_threads: list[threading.Thread] = []
        self._connections: list[socket.socket] = []
        self._state_lock = threading.Lock()
        self._stopped = threading.Event()
        self.port: int | None = None

    def start(self) -> int:
        """Bind an ephemeral 127.0.0.1 port, start accepting connections, and return the port."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(_ACCEPT_POLL_SECONDS)
        self._listener = listener
        port: int = listener.getsockname()[1]
        self.port = port
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return port

    def stop(self) -> None:
        """Stop accepting new connections, force-close in-flight ones, and wait (bounded) for pumps to drain."""
        self._stopped.set()
        if self._listener is not None:
            _close_quietly(self._listener)
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        with self._state_lock:
            connections = list(self._connections)
            pump_threads = list(self._pump_threads)
        # Closing each accepted connection unblocks its pump threads' recv() calls even if the
        # CLI/test client never closed its end — `_pump_duplex`'s finally then cascades the close
        # to the paired stream, so stop() never just sits waiting out the join timeout.
        for conn in connections:
            _close_quietly(conn)
        for thread in pump_threads:
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    def __enter__(self) -> SshDialStdioProxy:
        self.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        assert self._listener is not None  # noqa: S101 — invariant: set by start() before this thread runs
        while not self._stopped.is_set():
            try:
                conn, _addr = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._handle_connection, args=(conn,), daemon=True)
            with self._state_lock:
                self._connections.append(conn)
                self._pump_threads.append(thread)
            thread.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            stream = self._channel_factory()
        except Exception:
            logger.exception("ssh proxy: channel factory failed; dropping connection")
            _close_quietly(conn)
            return
        _pump_duplex(conn, stream)


def _pump_duplex(conn: socket.socket, stream: BidirectionalStream) -> None:
    """Relay bytes both ways between `conn` and `stream` until either side closes, then close both."""

    def relay(src: BidirectionalStream, dst: BidirectionalStream) -> None:
        try:
            while True:
                data = src.recv(_RECV_BUFFER_SIZE)
                if not data:
                    return
                dst.sendall(data)
        except Exception:  # noqa: BLE001 — any stream/transport error just ends this relay direction
            # A socket OSError or a paramiko channel error (e.g. EOFError / SSHException) mid-pump
            # means the connection is over; log and fall through to close the peer. Letting it escape
            # would surface as an unhandled exception in this daemon thread (and abandon teardown).
            logger.debug("ssh proxy: relay ended on a stream error", exc_info=True)
            return
        finally:
            _close_quietly(dst)

    forward = threading.Thread(target=relay, args=(conn, stream), daemon=True)
    backward = threading.Thread(target=relay, args=(stream, conn), daemon=True)
    forward.start()
    backward.start()
    forward.join()
    backward.join()


@contextlib.contextmanager
def ssh_proxy_for_docker_host(docker_host: str, *, timeout: float | None = None) -> Iterator[SshDialStdioProxy]:
    """
    Connect to an ssh:// DOCKER_HOST via paramiko and run a per-call local TCP proxy for the
    `with` block's duration.

    Intended for `_cli.py:run_docker`: point the CLI subprocess's DOCKER_HOST at
    `tcp://127.0.0.1:<proxy.port>` for the duration of the `with` block so it authenticates through
    this same paramiko connection instead of shelling out to the system `ssh` client. Both the SSH
    connection and the local listener are guaranteed to be torn down on the way out, success or not.

    args:
        docker_host: str - a DOCKER_HOST value starting with 'ssh://'
        timeout: float | None - forwarded to `connect_ssh_client` to bound the connect/banner/auth
                 phases; see that function's docstring
    returns: Iterator[SshDialStdioProxy] - yields the started proxy; read `proxy.port` for the URL
    """
    ssh_client = connect_ssh_client(docker_host, timeout=timeout)
    try:
        proxy = SshDialStdioProxy(paramiko_dial_stdio_factory(ssh_client))
        with proxy:
            yield proxy
    finally:
        ssh_client.close()

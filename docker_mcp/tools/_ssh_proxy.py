# SSH plumbing shared by every CLI-backed tool (Compose, Stack, Buildx, Context, Scout), built on
# paramiko - the same pure-Python transport docker-py already uses for the SDK-backed tools, so both
# tool families authenticate identically over SSH with no system `ssh` client involved.
#
# Two distinct mechanisms live here, both on top of `connect_ssh_client`:
#
# 1. `ssh_proxy_for_docker_host` - a per-call localhost TCP proxy letting a *local* `docker` CLI
#    drive a remote daemon. Mechanism (see docker-py's docker/transport/sshconn.py): both the docker
#    CLI and docker-py run `docker system dial-stdio` over an SSH session channel, which bridges the
#    remote /var/run/docker.sock to stdin/stdout, one channel per API connection. docker-py opens
#    those channels on its own paramiko transport; here we accept plain TCP connections from the
#    `docker` CLI on 127.0.0.1 and bridge each to its own `dial-stdio` channel over one shared
#    paramiko connection, full-duplex, until either side closes.
#
# 2. `run_remote_exec` - runs the `docker` CLI *on the remote host itself*, for the fallback where
#    there is no local `docker` binary (or plugin) to point at a daemon in the first place. Used
#    only when the local CLI is genuinely unavailable; when it is present, mechanism 1 is unchanged.

import contextlib
import enum
import logging
import math
import os
import posixpath
import shlex
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Protocol, cast

import paramiko

from docker_mcp.exceptions import CapabilityError, RemoteFailureError, ToolInputError
from docker_mcp._hosts import is_ssh_url
from docker_mcp.tools._utils import assert_host_writable, stream_to_file

logger = logging.getLogger(__name__)

_RECV_BUFFER_SIZE = 32_768
_ACCEPT_POLL_SECONDS = 0.5
_JOIN_TIMEOUT_SECONDS = 5.0
# Upper bound on the SSH handshake (connect/banner/auth) regardless of a caller's larger operation
# timeout: an unreachable or packet-filtered host must fail fast, not hang for a build-sized timeout.
_CONNECT_TIMEOUT_CAP_SECONDS = 30.0


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


def connect_socket_with_family_fallback(hostname: str, port: int, timeout: float | None) -> socket.socket:
    """Connect a plain TCP socket to hostname:port, trying every resolved address family in turn.

    `paramiko.SSHClient.connect()` already resolves both address families (`getaddrinfo(..., AF_UNSPEC,
    ...)`) and loops over the results, but only advances to the next address when the connect attempt
    raises `ECONNREFUSED` or `EHOSTUNREACH` - verified by reading the installed paramiko's
    `client.py:_families_and_addresses`/`connect`. A timed-out or black-holed IPv6 route (`ETIMEDOUT`,
    what a broken IPv6 path actually produces - packets vanish, nothing rejects) is re-raised
    immediately instead, so a host that's perfectly reachable over IPv4 fails outright. This mirrors
    `urllib3.util.connection.create_connection` instead (a bare `except OSError` per attempt, no errno
    filtering - the same reason `tcp://` connections don't have this problem), and is meant to be
    handed to `paramiko.SSHClient.connect(sock=..., ...)`, which skips its own resolution/connect loop
    entirely once a socket is already supplied.

    Args:
        hostname: str - the target to resolve; a literal IP is accepted too (single result, no fallback)
        port: int - the target port
        timeout: float | None - per-attempt connect timeout in seconds; None waits indefinitely
    returns: socket.socket - already connected to the first address that accepted
    raises: OSError - every resolved address failed to connect (the last error is re-raised); a
        `socket.gaierror` (a subclass of OSError) if `hostname` cannot be resolved at all
    """
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not None:
                sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError(f"getaddrinfo({hostname!r}, {port}) returned no usable addresses")


def parse_ssh_url(url: str) -> SshTarget:
    """Parse a DOCKER_HOST=ssh://... URL into paramiko connection parameters.

    Applies the same ~/.ssh/config lookups (Hostname, Port, User, IdentityFile, ProxyCommand)
    that docker-py's `SSHHTTPAdapter._create_paramiko_client` performs, so this proxy resolves the
    same target docker-py (and the system `ssh` client) would for the same URL.

    The scheme is checked rather than assumed. Both callers (the dial-stdio proxy and the remote-exec
    fallback) only ever pass an ssh:// host, but a wrong scheme would otherwise be parsed as if it
    were one - `tcp://10.0.0.5:2375` yields a plausible target and gets attempted as SSH on port 2375,
    failing with advice about keys and known_hosts for what is really a caller bug. Validating here
    covers both callers at the one point that already validates the URL.

    args: url: str - a DOCKER_HOST value starting with 'ssh://'
    returns: SshTarget - hostname/port/username/key_filename/proxycommand after config-file lookup
    raises: ValueError - the URL is not ssh://, or carries no hostname
    """
    if not is_ssh_url(url):
        raise ValueError(f"Expected an ssh:// URL for an SSH connection, got {url!r}")
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
    """Build and connect a paramiko SSHClient for a DOCKER_HOST=ssh://... URL.

    Mirrors docker-py's `SSHHTTPAdapter._create_paramiko_client` defaults: system host keys are
    loaded and an unknown host key is rejected (`RejectPolicy`, not auto-add); `allow_agent` and
    `look_for_keys` are left at paramiko's own defaults (both True) rather than overridden, exactly
    as docker-py leaves them, so this proxy authenticates with the same credentials docker-py would
    pick for the same URL. Unlike docker-py, `port` is omitted from the connect kwargs entirely when
    unresolved rather than passed through as `None` - paramiko's own default (22) only applies when
    the kwarg is absent, and an explicit `None` instead resolves to port 0, which always refuses.

    `timeout`, when given, bounds the raw socket connect *and* the banner/auth handshake phases
    (paramiko tracks these as separate phases with separate, otherwise-unbounded defaults) so a
    slow or filtered host can't hang past the caller's own deadline - see `run_docker`, whose
    `timeout` argument only wraps `subprocess.run` and would otherwise leave this paramiko connect
    (which runs beforehand, to set up the local proxy) unbounded. The bound is itself capped at
    `_CONNECT_TIMEOUT_CAP_SECONDS` so a large operation timeout (e.g. an 1800s build) still fails an
    unreachable host fast rather than hanging for the whole operation budget.

    Unless a `~/.ssh/config` `ProxyCommand` is in play (which already supplies its own `sock` - a
    bastion/jump-host connection has no hostname:port of ours to resolve), the raw TCP connect is
    made via `connect_socket_with_family_fallback` and handed to paramiko as `sock=`, so this falls
    back from IPv6 to IPv4 on any connect failure rather than paramiko's own narrower
    ECONNREFUSED/EHOSTUNREACH-only retry (see that function's docstring).

    A connection failure (auth, unknown host key, unreachable host) is re-raised as a `RemoteFailureError`
    with actionable guidance rather than a bare paramiko/socket exception.

    Args:
        docker_host: str - a DOCKER_HOST value starting with 'ssh://'
        timeout: float | None - seconds to bound the connect/banner/auth phases (capped at
                 _CONNECT_TIMEOUT_CAP_SECONDS); None means paramiko's own (unbounded) defaults
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
    bounded: float | None = None
    if timeout is not None:
        bounded = min(timeout, _CONNECT_TIMEOUT_CAP_SECONDS)
        connect_kwargs["timeout"] = bounded
        connect_kwargs["banner_timeout"] = bounded
        connect_kwargs["auth_timeout"] = bounded
    if target.proxycommand:
        connect_kwargs["sock"] = paramiko.ProxyCommand(target.proxycommand)
    try:
        if not target.proxycommand:
            connect_kwargs["sock"] = connect_socket_with_family_fallback(
                target.hostname, target.port if target.port is not None else 22, bounded
            )
        client.connect(**connect_kwargs)
    except (paramiko.SSHException, OSError) as exc:
        client.close()
        raise RemoteFailureError(
            f"Could not establish the SSH connection to {docker_host!r} for the docker CLI: {exc}. "
            f"Check that your key is loaded (run `ssh-add`, and forward SSH_AUTH_SOCK), that the host "
            f"key is in ~/.ssh/known_hosts (paramiko rejects unknown hosts - connect once with `ssh` "
            f"after verifying its fingerprint), and that the host is reachable."
        ) from exc
    return client


def paramiko_dial_stdio_factory(ssh_client: paramiko.SSHClient) -> ChannelFactory:
    """Build a channel factory that opens a fresh `docker system dial-stdio` channel on `ssh_client`.

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
    transport) - either way this is teardown-path cleanup that must never leak out and abandon
    the caller's pump threads unjoined.
    """
    try:
        closable.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: S110, BLE001 - best-effort close; see docstring for why it's broad
        pass
    try:
        closable.close()
    except Exception:  # noqa: S110, BLE001 - best-effort close; see docstring for why it's broad
        pass


class SshDialStdioProxy:
    """Localhost TCP listener that bridges each accepted connection to a stream from `channel_factory`.

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
        # CLI/test client never closed its end - `_pump_duplex`'s finally then cascades the close
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
        assert self._listener is not None  # noqa: S101 - invariant: set by start() before this thread runs
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
        except Exception:  # noqa: BLE001 - any stream/transport error just ends this relay direction
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
    """Connect to an ssh:// DOCKER_HOST and run a local TCP proxy for the `with` block.

    Uses paramiko for the connection and starts the proxy per call.

    Intended for `_cli.py:run_docker`: point the CLI subprocess's DOCKER_HOST at
    `tcp://127.0.0.1:<proxy.port>` for the duration of the `with` block so it authenticates through
    this same paramiko connection instead of shelling out to the system `ssh` client. Both the SSH
    connection and the local listener are guaranteed to be torn down on the way out, success or not.

    Args:
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


# --- remote exec: run the `docker` CLI on the far side of the SSH connection ----------------------
#
# The fallback for a machine with no local `docker` binary (or a missing plugin) but SSH access to a
# real Docker host: instead of driving a local CLI at a remote daemon (the proxy above), run the CLI
# on the remote host, which - being a Docker host - plausibly already has it.

# Exit status the remote wrapper reports when *it* killed the command for exceeding its timeout.
# 124 is GNU `timeout`'s convention. It must be distinguishable from the command's own statuses:
# reporting the killed process's 143 (128+SIGTERM) instead would be indistinguishable from any other
# SIGTERM death, and would surface a timeout as an ordinary failure while the local subprocess path
# raises TimeoutExpired for the same event.
#
# No exit code is collision-proof, because `docker run`/`compose run` propagate the *container's* own
# status and a container may legitimately exit 124. So the code alone does not decide it: a timeout is
# only attributed when the call also actually ran for its full timeout (see `_is_remote_timeout`),
# which a command exiting 124 early cannot satisfy.
_REMOTE_TIMEOUT_EXIT_CODE = 124

# Tolerance when comparing elapsed time against the watchdog's deadline, for float/latency noise only.
# A genuine remote timeout always elapses at least the watchdog's sleep, so this need not be generous.
_TIMEOUT_ATTRIBUTION_SLACK_SECONDS = 0.25


def _validate_exec_args(argv: Sequence[str], timeout: float, max_output_bytes: int) -> None:
    """Reject arguments the remote path cannot honour, before anything connects or runs.

    A non-positive timeout raises `subprocess.TimeoutExpired` rather than `ValueError`, to match the
    local path exactly: `subprocess.run` raises that same exception immediately for `0`, `-1` and
    `-0.5` (verified), and the command never completes. Remotely the watchdog's one-second floor would
    otherwise quietly grant a whole second of runtime, so a mutating command - `compose down`, say -
    would actually execute where the local backend refused it. Silently running more than asked is a
    worse failure than a clear error.

    Args:
        argv - the remote command, used only for the exception's message
        timeout - the caller's timeout; must be positive
        max_output_bytes - the retention cap; must not be negative
    raises:
        subprocess.TimeoutExpired - `timeout` is zero or negative
        ValueError - `max_output_bytes` is negative (a caller bug with no local analogue)
    """
    if not argv:
        # The wrapper interpolates the joined argv, so an empty one emits a bare `& pid=$!` and the
        # remote shell dies with "syntax error near unexpected token &" (verified). Fail here instead:
        # a caller bug should not look like a broken wrapper script.
        raise ValueError("argv must contain at least the binary to run, got an empty sequence")
    if timeout <= 0:
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)
    if max_output_bytes < 0:
        raise ValueError(f"max_output_bytes must not be negative, got {max_output_bytes!r}")


def _watchdog_sleep_seconds(timeout: float) -> int:
    """Whole seconds the remote watchdog sleeps before killing the command.

    Shared by the wrapper that emits the `sleep` and the attribution check that reasons about when it
    can have fired, so the two cannot drift apart. `sleep` takes whole seconds portably, hence the
    round up; the floor of 1 keeps a sub-second timeout from degenerating into `sleep 0`.

    args: timeout - the caller's timeout in seconds
    returns: int - the watchdog's sleep duration, at least 1
    """
    return max(1, math.ceil(timeout))


def _is_remote_timeout(returncode: int, elapsed: float, timeout: float) -> bool:
    """Whether a finished remote command should be reported as having timed out.

    Requires both the wrapper's sentinel status *and* corroborating elapsed time, so a command that
    legitimately exits with the sentinel code well inside its budget - a container propagating 124
    through `docker run`, say - is reported as the ordinary failure it is rather than as a timeout.

    The comparison is against the watchdog's *effective* sleep, not the caller's raw timeout. Those
    differ whenever the timeout is not a whole number of seconds, and the difference is not benign: at
    `timeout=30.2` the watchdog cannot fire before 31s, so testing against 30.2 would misattribute a
    sentinel exit at 30.5s, and at `timeout=0.1` the raw threshold goes negative and would misattribute
    *every* sentinel exit. Comparing against the sleep the watchdog actually performs closes both.

    Args:
        returncode - the exit status the remote wrapper reported
        elapsed - seconds from issuing the command to it completing
        timeout - the caller's timeout for the command
    returns: bool - True only when the status is the sentinel and the watchdog could actually have fired
    """
    if returncode != _REMOTE_TIMEOUT_EXIT_CODE:
        return False
    return elapsed >= _watchdog_sleep_seconds(timeout) - _TIMEOUT_ATTRIBUTION_SLACK_SECONDS


# Extra local slack past the caller's timeout before we give up on the channel ourselves. The remote
# watchdog should have killed the command and exited by then; this only covers the case where the
# wrapper never ran or the remote is wedged, so the call can't hang indefinitely.
_REMOTE_KILL_GRACE_SECONDS = 10.0

# How long the remote watchdog waits after SIGTERM before escalating to SIGKILL. SIGTERM goes first so
# the docker CLI can shut down cleanly; the escalation is what makes the timeout an actual guarantee,
# since SIGTERM alone can be trapped or ignored. Kept well inside `_REMOTE_KILL_GRACE_SECONDS` so the
# escalation has time to land before `exec_remote`'s own local deadline gives up on the channel.
_REMOTE_TERM_GRACE_SECONDS = 3

# Idle poll interval while draining a remote command's output. Deliberately a poll rather than
# select(): paramiko Channels are select-able only via fileno(), which both couples this loop to a
# real channel object and allocates an OS pipe per call - a plain readiness poll keeps the loop
# trivially fake-able in tests, and at these call rates the wakeups cost nothing measurable.
_EXEC_POLL_SECONDS = 0.01

# How many extra readiness polls to make after the exit status appears, before accepting that the
# command's output is finished. Guards against a final chunk surfacing from paramiko's transport
# thread just after both streams last read as quiet, which would otherwise be dropped silently.
# Costs at most `_EXIT_SETTLE_POLLS * _EXEC_POLL_SECONDS` once per call, only at the very end.
_EXIT_SETTLE_POLLS = 5

# --- staging limits and bookkeeping ----------------------------------------------------------------
#
# Naming the staging root after the project makes an abandoned directory attributable on a shared
# host (only a hard transport drop can leave one - see `remote_staging_session`). The `stage.` infix
# then separates it from the watchdog's own marker files, which share the project prefix: a stray
# marker is an empty file and harmless, whereas a stray staging directory holds copied content
# (possibly a build secret) and is worth an operator's attention. Confirmed worth having by mistaking
# one for the other while verifying this on a real host - a `docker-mcp-server.*` glob matched the
# listing command's own live marker file and read as a leaked staging directory.
_STAGE_ROOT_PREFIX = "docker-mcp-server.stage."

# Ceiling on one staged tree. Whole-tree staging cannot narrow itself to "only the files the command
# will actually read" without parsing Compose YAML (`build:`, `env_file:` and `include:` all name
# arbitrary relative paths), so a caller who points a compose tool at a directory that also holds a
# large dataset would otherwise silently push all of it over SSH and fill the remote /tmp. The cap
# turns that into an immediate, explained refusal naming the offending directory. Generous enough
# that a real project tree never meets it.
_MAX_STAGE_BYTES = 200 * 1024 * 1024  # 200 MiB
# A second cap on entry count: a huge `node_modules` or `.git` can sit far under the byte ceiling and
# still make both the tar and the remote extraction pathologically slow.
_MAX_STAGE_FILES = 50_000

# Bounds for the session's own bookkeeping commands (mktemp / rm / tar), which are quick and quiet.
# Extraction gets its own, larger bound because it scales with the staged tree, not with the network.
_STAGING_CONTROL_TIMEOUT_SECONDS = 60.0
_STAGING_EXTRACT_TIMEOUT_SECONDS = 300.0
# Bookkeeping commands print a path at most; anything beyond this is an error message we want to see
# in full but need not retain megabytes of.
_STAGING_OUTPUT_CAP_BYTES = 65_536
# Read size for streaming a fetched file through `stream_to_file` - matches `_RECV_BUFFER_SIZE`'s
# order of magnitude for the proxy's own socket reads, comfortably larger than SFTP's own packet size.
_FETCH_CHUNK_BYTES = 32_768


class RemoteDialectKind(enum.Enum):
    """Which command-wrapping dialect a remote host needs.

    Only POSIX is implemented. WINDOWS exists so detection can *name* what it found and refuse
    precisely, rather than mis-running a POSIX script against cmd/PowerShell - and so adding Windows
    later is one new dialect implementation rather than a redesign.
    """

    POSIX = "posix"
    WINDOWS = "windows"


# `uname -s` values we accept as POSIX. Matched exactly (lowercased) rather than by "did uname exit
# 0", because exit status alone has a real false positive: a Windows host whose sshd shell is
# cmd/PowerShell but which has Git Bash or Cygwin on PATH answers `uname -s` successfully with
# MINGW64_NT-... , which would classify as POSIX and drop us into a half-working MSYS environment with
# translated paths. Note WSL reports plain "Linux" and so is (correctly) accepted: sshd running
# inside a WSL distro is a genuine Linux target, not a Windows one.
_POSIX_UNAME_VALUES = frozenset({"linux", "darwin", "freebsd", "openbsd", "netbsd", "dragonfly", "sunos", "aix"})


class RemoteDialect(Protocol):
    """Everything platform-specific about driving a remote shell: command wrapping and path handling.

    A dialect exists so the pieces that cannot be written portably are stated once per platform
    rather than assumed. The staging members are argv lists rather than command strings because they
    are handed to `wrap_with_timeout` like any other command, so they inherit the same watchdog and
    quoting; only `temp_dir_argv` needs a shell of its own, for the `${TMPDIR}` expansion.
    """

    def wrap_with_timeout(self, argv: Sequence[str], *, timeout: float, cwd: str | None = None) -> str: ...

    def temp_dir_argv(self) -> list[str]: ...

    def remove_tree_argv(self, path: str) -> list[str]: ...

    def extract_tar_argv(self, archive: str, dest: str) -> list[str]: ...

    def create_tar_argv(self, source: str, archive: str) -> list[str]: ...

    def join_path(self, *parts: str) -> str: ...


class PosixDialect:
    """Command wrapper for a POSIX remote shell, needing only `sh`, `sleep`, `kill` and `mktemp`.

    Deliberately not GNU coreutils `timeout`, which is absent on macOS/BSD; this runs anywhere with
    a POSIX shell. Termination is the *remote* side's own responsibility because closing an SSH
    channel does not portably kill what it started.
    """

    def wrap_with_timeout(self, argv: Sequence[str], *, timeout: float, cwd: str | None = None) -> str:
        """Build the remote `sh -c` command that runs `argv` under a self-killing watchdog.

        Two things here are load-bearing and easy to get wrong:

        `cd` is emitted as its **own statement**, never joined to the command with `&&`. In POSIX
        shell `&` binds looser than `&&`, so `cd X && cmd &` makes the whole AND-list one async job
        and `$!` becomes the *subshell's* pid - killing that leaves the real `docker` process alive
        and orphaned to init on every single timeout. Keeping `cd` separate means `$!` is the
        command itself.

        The watchdog reports `_REMOTE_TIMEOUT_EXIT_CODE` via a marker file, written only when the
        command was still alive at the deadline, so a timeout is distinguishable from any other
        non-zero exit. Testing whether the *watchdog* is still alive with `kill -0` would instead be
        wrong: a watchdog that has fired but not yet been reaped is a zombie whose pid still answers,
        so real timeouts would be missed. (`kill -0` on the *command* below is a different question -
        asked before signalling, about a live child - and is sound.)

        The marker is written **before** the kill, which is what makes it race-free. The kill is what
        releases the main shell from `wait $pid`, and the main shell then kills the watchdog - so
        writing the marker afterwards leaves a window where the watchdog is killed before the `printf`
        lands, and a genuine timeout is misreported as an ordinary SIGTERM death (observed:
        intermittent 143 instead of the sentinel). Ordering it first guarantees the main shell is
        still blocked when the file is written.

        Both `wait`s run inside `{ ... } 2>/dev/null` groups to swallow the shell's own asynchronous
        job-reap notices ("Terminated: 15 ( sleep 30; ...)"), which otherwise land in the *command's*
        captured stderr and corrupt it - on every fast command, since killing the still-sleeping
        watchdog is the normal path. The redirect only hides the shell's notice, not the command's own
        output: the command inherited fd 2 when it started, so its writes are unaffected. A brace
        group is required rather than a subshell so `ec=$?` assigns in the current shell.

        The watchdog's own stdio is sent to /dev/null so it never holds the command's streams. Killing
        the watchdog subshell does not reliably kill the `sleep` inside it (same reason `cd X && cmd &`
        was wrong above), and an orphaned `sleep` still holding those descriptors keeps the stream open
        for the remainder of the timeout window - which a consumer waiting for EOF experiences as a
        fast command hanging for its full timeout. The watchdog has no legitimate use for them anyway.

        Termination targets the direct child only, never its process group. That much *is* parity with
        the local path: `subprocess.run(timeout=...)` calls `Popen.kill()`, which is
        `os.kill(self.pid, ...)` on POSIX - the child, not the group - so a process the command itself
        forked survives a timeout there too (see `_drain_exec_channel` for why that shapes completion
        detection).

        The signal is *not* parity, and this used to claim it was. Locally the timeout sends SIGKILL,
        which cannot be caught; here SIGTERM goes first so the docker CLI can clean up, then SIGKILL
        after `_REMOTE_TERM_GRACE_SECONDS`. Without that escalation a command which traps or ignores
        SIGTERM would outlive its own timeout - the caller would still be released, by the local
        deadline in `exec_remote`, while the remote process kept running.

        A command that exits on its own at the exact instant the watchdog fires may be attributed
        either way; the window is microseconds wide.

        Args:
            argv - the remote command as an argv list; joined with shell quoting, never concatenated
            timeout - seconds before the remote watchdog kills the command (rounded up, floor 1s)
            cwd - remote directory to run in; a failure to enter it exits 127 without running argv
        returns: str - a complete `sh -c '...'` command string for `Channel.exec_command`
        """
        seconds = _watchdog_sleep_seconds(timeout)
        # An explicit template, because the bare `mktemp` form is not portable: macOS accepts it, but
        # FreeBSD/OpenBSD/NetBSD require a template argument and would fail here - before running argv
        # at all - on hosts this dialect claims to support.
        lines = [
            # Fail fast if the marker cannot be created (absent `mktemp`, unwritable or read-only
            # TMPDIR). Continuing leaves `m` empty, and the marker write then fails silently while the
            # kill still happens - so a genuine timeout comes back as a plain SIGTERM exit and is
            # reported as an ordinary failure (verified: rc=143 instead of the sentinel). Exit 125
            # follows GNU `timeout`'s convention for "the wrapper itself could not run".
            'm=$(mktemp "${TMPDIR:-/tmp}/docker-mcp-server.XXXXXXXX") || {'
            ' echo "docker-mcp-server: cannot create a temp file on the remote host'
            ' (is ${TMPDIR:-/tmp} writable?)" >&2; exit 125; }',
            # Remove the marker on any exit rather than only the happy path: the early `cd` failure
            # below returns without reaching the end of the script, and an outer timeout or dropped
            # channel can kill this shell outright - both of which otherwise strand the file in the
            # remote temp dir. A SIGKILL still can't be trapped, so this narrows the leak rather than
            # eliminating it.
            "trap 'rm -f \"$m\"' EXIT HUP INT TERM",
        ]
        if cwd is not None:
            lines.append(f"cd {shlex.quote(cwd)} || exit 127")
        lines.extend(
            [
                f"{shlex.join(argv)} & pid=$!",
                # The watchdog counts in one-second sleeps and gives up as soon as the command is gone,
                # so nobody has to kill it: it self-terminates within a second of the command finishing.
                # A single `sleep <timeout>` cannot be cleaned up portably - killing the subshell that
                # owns it leaves the `sleep` orphaned (verified on Linux/dash: one stray per call, alive
                # for the rest of the timeout window), and putting each background job in its own process
                # group with `set -m` so `kill -- -$wpid` reaches the child hangs outright on a shell with
                # no controlling terminal, which is exactly what an SSH exec channel provides.
                f'(i=0; while [ "$i" -lt {seconds} ]; do sleep 1;'
                " kill -0 $pid 2>/dev/null || exit 0; i=$((i+1)); done;"
                ' printf t >"$m"; kill $pid 2>/dev/null;'
                # SIGTERM first so the docker CLI can clean up, then SIGKILL, so a command that traps
                # or ignores SIGTERM cannot outlive its own timeout. `kill -9` on an already-dead pid
                # is a harmless no-op, so this needs no liveness re-check.
                f" sleep {_REMOTE_TERM_GRACE_SECONDS}; kill -9 $pid 2>/dev/null)"
                " >/dev/null 2>&1 &",
                "{ wait $pid; ec=$?; } 2>/dev/null",
                f'[ -s "$m" ] && ec={_REMOTE_TIMEOUT_EXIT_CODE}',
                "exit $ec",
            ]
        )
        return f"sh -c {shlex.quote(chr(10).join(lines))}"

    def temp_dir_argv(self) -> list[str]:
        """Argv creating a private staging directory and printing its path.

        `mktemp -d` is used rather than a name we compose ourselves because it is atomic and creates
        the directory mode 0700 - on a shared host, a predictable path under /tmp would be a symlink
        and content-tampering opportunity for any other user on the box. An explicit template is
        required for the same portability reason as in `wrap_with_timeout`: bare `mktemp -d` works on
        macOS but not on the BSDs. Nesting `sh -c` inside the watchdog wrapper is intentional - the
        `${TMPDIR:-/tmp}` expansion needs a shell, and going through the wrapper means this command is
        bounded and quoted exactly like every other.

        returns: list[str] - argv whose stdout is the new directory's absolute path
        """
        return ["sh", "-c", f'mktemp -d "${{TMPDIR:-/tmp}}/{_STAGE_ROOT_PREFIX}XXXXXXXX"']

    def remove_tree_argv(self, path: str) -> list[str]:
        """Argv removing a staged tree and everything under it.

        args: path - absolute remote path to remove; passed as an argv element, never interpolated
        returns: list[str] - argv that succeeds whether or not the path still exists
        """
        return ["rm", "-rf", path]

    def extract_tar_argv(self, archive: str, dest: str) -> list[str]:
        """Argv unpacking a staged tar archive into an existing directory.

        Uncompressed tar only: `-z` autodetection is a GNU/bsdtar extension rather than something
        every POSIX `tar` offers, so the uploader does not compress (see `_upload_and_extract`).

        Args:
            archive - absolute remote path of the uploaded tar
            dest - absolute remote directory to unpack into; must already exist
        returns: list[str] - argv for the extraction
        """
        return ["tar", "-xf", archive, "-C", dest]

    def create_tar_argv(self, source: str, archive: str) -> list[str]:
        """Argv packing a remote path into an uncompressed tar, for fetching it back over SFTP.

        The inverse of `extract_tar_argv`: `source`'s parent directory becomes `tar`'s `-C` base and
        only its basename is added, so the archive's sole top-level member is that basename - whether
        `source` is a file or a directory - matching what local extraction expects to recreate under
        the caller's real destination. Uncompressed for the same reason `_tar_local_tree` is: nothing
        on the fetching side assumes `gzip`.

        Args:
            source - absolute remote path (file or directory) to pack
            archive - absolute remote path to write the tar to
        returns: list[str] - argv for the archive creation
        """
        parent, name = posixpath.split(source)
        return ["tar", "-cf", archive, "-C", parent or "/", name]

    def join_path(self, *parts: str) -> str:
        r"""Join remote path components with the remote separator.

        `posixpath`, not `os.path`: the separator belongs to the *remote* host, and a server running
        on Windows would otherwise compose `\\`-joined paths for a Linux target.

        args: parts - path components, the first of which should be absolute
        returns: str - the joined remote path
        """
        return posixpath.join(*parts)


_DIALECTS: dict[RemoteDialectKind, RemoteDialect] = {RemoteDialectKind.POSIX: PosixDialect()}


def get_dialect(kind: RemoteDialectKind) -> RemoteDialect:
    """Return the wrapper implementation for a dialect, or refuse if it isn't implemented yet.

    args: kind - the dialect a host was detected as
    returns: RemoteDialect - the implementation to wrap commands with
    raises: CapabilityError - for a detected-but-unimplemented dialect (today: WINDOWS)
    """
    dialect = _DIALECTS.get(kind)
    if dialect is None:
        # Deliberately not phrased as "this is a Windows host": `detect_remote_dialect` also routes a
        # failed probe and an unrecognized `uname -s` here, so a restricted shell or an uncommon Unix
        # kernel reaches this message too, and calling those Windows would be wrong.
        raise CapabilityError(
            "Remote-exec fallback: no supported POSIX shell was detected on this host, so the docker "
            "CLI cannot be run on it. Any host presenting a POSIX shell is supported - including "
            f"{', '.join(sorted(_POSIX_UNAME_VALUES))} - as is sshd running inside a WSL distro. Common "
            "causes of this refusal: an sshd whose shell is Windows cmd/PowerShell (a Windows dialect "
            "is architected but not implemented yet); a `uname` from MSYS/MinGW/Cygwin; a restricted "
            "shell; a probe that never answered; or a kernel not on that list. Preceding log lines "
            "record what the host actually reported. Either install the docker CLI locally to use the "
            "local-CLI path against this host, or expose the host over a POSIX shell (on Windows, run "
            "sshd inside the WSL distro)."
        )
    return dialect


_DIALECT_CACHE_TTL_SECONDS = 60.0
_dialect_cache: dict[str, tuple[float, RemoteDialectKind]] = {}
_dialect_cache_lock = threading.Lock()


def _clear_dialect_cache() -> None:
    """Drop all cached dialect detections (used by tests; also valid after a remote OS change)."""
    with _dialect_cache_lock:
        _dialect_cache.clear()


def detect_remote_dialect(
    ssh_client: paramiko.SSHClient, cache_key: str, *, timeout: float | None = None
) -> RemoteDialectKind:
    """Detect which command dialect a remote host needs, by probing `uname -s`.

    This is a *behavioural* probe - "is there a POSIX shell here that will run my script?" - not an
    OS fingerprint, which is why sshd inside a WSL distro is correctly accepted (it answers "Linux"
    and has real `sh`/`sleep`/`kill`). Only an allow-listed kernel name counts as POSIX; matching on
    "did `uname` exit 0" would wrongly accept a Windows host with Git Bash or Cygwin on PATH, which
    answers successfully with `MINGW64_NT-...`.

    Everything else - an MSYS/Cygwin-flavoured value, an unrecognised kernel, a restricted shell, a
    failed probe - is reported as WINDOWS, which here means only "no supported POSIX shell answered"
    rather than a claim about the OS. Since `get_dialect`'s refusal therefore cannot name a cause, both
    routes to WINDOWS log a warning before returning, so the refusal is diagnosable on first
    occurrence: the observed exit status and output when the probe ran, or the underlying error when it
    could not be completed at all. A successful POSIX detection logs nothing - there is nothing to
    explain, and this runs on every call whose host is not already cached.

    Cached per host with a short TTL (mirroring `_cli.has_plugin`), so a long-lived server neither
    re-probes on every call nor needs a restart after a remote change.

    Args:
        ssh_client - an already-connected client for the host being probed
        cache_key - identity to cache under; pass the host's DOCKER_HOST URL
        timeout - seconds to bound the probe (channel reads and the exit-status wait alike), capped
                  at _CONNECT_TIMEOUT_CAP_SECONDS; None falls back to that cap rather than being
                  unbounded, since an unbounded probe can hang detection outright
    returns: RemoteDialectKind - POSIX when `uname -s` names a known POSIX kernel, else WINDOWS
    """
    now = time.monotonic()
    with _dialect_cache_lock:
        entry = _dialect_cache.get(cache_key)
        if entry is not None and now - entry[0] < _DIALECT_CACHE_TTL_SECONDS:
            return entry[1]

    kind = RemoteDialectKind.WINDOWS
    try:
        transport = ssh_client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport is not connected.")
        channel = transport.open_session()
        try:
            # Bound the channel unconditionally. With `timeout=None` there would be no bound at all, and
            # the `recv` below blocks first - before the exit-status deadline further down can help - so
            # a remote that wedges without writing anything would hang detection forever. Falling back
            # to the connect cap keeps a probe bounded even when the caller expressed no preference.
            probe_bound = (
                _CONNECT_TIMEOUT_CAP_SECONDS if timeout is None else min(timeout, _CONNECT_TIMEOUT_CAP_SECONDS)
            )
            channel.settimeout(probe_bound)
            channel.exec_command("uname -s")
            output = channel.recv(_RECV_BUFFER_SIZE).decode("utf-8", errors="replace").strip().lower()
            # Poll for the exit status rather than calling recv_exit_status() straight off.
            # `Channel.settimeout` above bounds reads and writes only; recv_exit_status() waits on an
            # Event with no timeout at all (paramiko's own docstring warns it "will hang
            # indefinitely"), so a wedged remote shell would hang detection - and with it
            # run_remote_exec - regardless of the caller's timeout. A probe that never answers is
            # exactly the "no POSIX shell here" case, so expiry falls through to WINDOWS.
            deadline = time.monotonic() + probe_bound
            while not channel.exit_status_ready():
                if time.monotonic() >= deadline:
                    raise TimeoutError("the remote host did not report an exit status for `uname -s`")
                time.sleep(_EXEC_POLL_SECONDS)
            status = channel.recv_exit_status()
            if status == 0 and output in _POSIX_UNAME_VALUES:
                kind = RemoteDialectKind.POSIX
            else:
                # Log at warning, not debug: `get_dialect` can only refuse generically (a failed probe,
                # an MSYS/Cygwin `uname`, a restricted shell and an unlisted kernel all land here), so
                # this line is the only place recording *what the host actually reported* - without it
                # the refusal is not diagnosable on first occurrence.
                logger.warning(
                    "remote-exec: host %s is not a supported POSIX remote - `uname -s` exited %d and "
                    "reported %r; remote-exec will be refused for this host",
                    cache_key,
                    status,
                    output,
                )
        finally:
            channel.close()
    except OSError, EOFError, paramiko.SSHException, RuntimeError:
        # Probe failure is itself the signal ("no POSIX shell answered"), never a hard error here -
        # get_dialect() is what turns a non-POSIX result into an actionable refusal.
        # Warning, not debug, for the same reason as the branch above: `get_dialect` can only refuse
        # generically, so this is the only record of *why*. This path is the least self-evident of the
        # lot - an unreachable host, a dead transport, a shell that never answered - so leaving it at
        # debug would mean the refusal could not be explained without reproducing it.
        logger.warning(
            "remote-exec: host %s is not a supported POSIX remote - the `uname -s` probe could not be "
            "completed; remote-exec will be refused for this host",
            cache_key,
            exc_info=True,
        )

    with _dialect_cache_lock:
        _dialect_cache[cache_key] = (time.monotonic(), kind)
    return kind


@dataclass(frozen=True)
class RemoteExecResult:
    """Outcome of one remote command: raw captured bytes plus whether the cap truncated them.

    Bytes rather than str so decoding stays the caller's concern, matching how `_cli.run_docker`
    captures a local subprocess and decodes once at the boundary.
    """

    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool


def _drain_exec_channel(
    channel: paramiko.Channel, *, max_output_bytes: int, deadline: float, argv: Sequence[str], timeout: float
) -> tuple[bytes, bytes, bool]:
    """Pump a channel's stdout and stderr until the command ends, capping what we keep.

    Both streams must be drained *concurrently*: paramiko stops advertising window space for a
    stream nobody reads, so draining only stdout lets a chatty stderr fill the window and block the
    remote command until our own deadline - the classic pipe deadlock. `subprocess.run` avoids this
    locally via `communicate()`; this is the equivalent. For the same reason, once the cap is hit we
    keep reading and discard rather than stopping, since an unread stream would hang the remote
    instead of merely truncating its output.

    Completion is decided by `exit_status_ready()`, deliberately not by EOF on the streams. A command
    that spawns its own children leaves those children holding the inherited stdout/stderr after the
    watchdog SIGTERMs their parent, so waiting for EOF would block for the rest of the timeout window
    even though the command itself has already exited (measured: `subprocess.run(capture_output=True)`
    blocks exactly this way locally, since it *does* wait for EOF). Keying on the exit status makes
    this path return as soon as the command is genuinely done.

    Args:
        channel - a channel with the command already exec'd
        max_output_bytes - per-stream cap on retained bytes; excess is read and dropped
        deadline - monotonic time after which we abandon the channel
        argv - the remote argv, for the TimeoutExpired message
        timeout - the caller's timeout, for the TimeoutExpired message
    returns: tuple[bytes, bytes, bool] - (stdout, stderr, truncated)
    raises: subprocess.TimeoutExpired - if `deadline` passes before the command ends
    """
    stdout = bytearray()
    stderr = bytearray()
    truncated = False

    def keep(buffer: bytearray, chunk: bytes) -> bool:
        room = max_output_bytes - len(buffer)
        if room <= 0:
            return True
        buffer.extend(chunk[:room])
        return len(chunk) > room

    while True:
        moved = False
        if channel.recv_ready():
            truncated = keep(stdout, channel.recv(_RECV_BUFFER_SIZE)) or truncated
            moved = True
        if channel.recv_stderr_ready():
            truncated = keep(stderr, channel.recv_stderr(_RECV_BUFFER_SIZE)) or truncated
            moved = True
        if moved:
            continue  # drain greedily before re-checking for completion
        if channel.exit_status_ready():
            # Do not break on the first quiet poll. paramiko surfaces data from its transport thread,
            # so a final chunk can land in the window between the readiness checks above and this one
            # - the greedy drain cannot help, having already given up - and breaking here loses it
            # silently, which is worse than truncating loudly because the caller cannot tell. Settle
            # for a few short polls instead and resume draining if anything appears. This narrows the
            # window rather than closing it; closing it entirely would mean waiting for EOF, which is
            # exactly what a command's surviving children make unsafe (see above).
            settled = False
            for _ in range(_EXIT_SETTLE_POLLS):
                if channel.recv_ready() or channel.recv_stderr_ready():
                    settled = True
                    break
                time.sleep(_EXEC_POLL_SECONDS)
            if settled:
                continue
            break
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout, output=bytes(stdout), stderr=bytes(stderr))
        time.sleep(_EXEC_POLL_SECONDS)

    return bytes(stdout), bytes(stderr), truncated


def exec_remote(
    ssh_client: paramiko.SSHClient,
    argv: Sequence[str],
    *,
    max_output_bytes: int,
    timeout: float,
    cwd: str | None = None,
    dialect: RemoteDialectKind = RemoteDialectKind.POSIX,
) -> RemoteExecResult:
    """Run one command on an already-connected host, under the dialect's own timeout watchdog.

    Prefer `run_remote_exec` unless you already hold a connection you intend to reuse (staging a
    file and then running against it, say) - this takes a client rather than opening one.

    A timeout raises `subprocess.TimeoutExpired`, the same exception `run_docker`'s local path
    raises, so callers see one contract regardless of which backend ran. That covers both the
    remote watchdog's own kill (reported as `_REMOTE_TIMEOUT_EXIT_CODE`) and the local grace
    deadline, which only fires if the watchdog never ran at all.

    Args:
        ssh_client - an already-connected client for the target host
        argv - the remote command as an argv list, including the binary (e.g. ["docker", "ps"])
        max_output_bytes - per-stream cap on retained output; the rest is drained and dropped
        timeout - seconds the remote watchdog allows the command before killing it
        cwd - remote directory to run in; entering it is part of the wrapped command
        dialect - the host's detected dialect; a non-POSIX one is refused by `get_dialect`
    returns: RemoteExecResult - exit status plus captured (possibly truncated) stdout/stderr bytes
    raises:
        RuntimeError - the transport is gone
        CapabilityError - the dialect isn't implemented
        subprocess.TimeoutExpired - the command exceeded `timeout`
    """
    _validate_exec_args(argv, timeout, max_output_bytes)
    command = get_dialect(dialect).wrap_with_timeout(argv, timeout=timeout, cwd=cwd)
    transport = ssh_client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport is not connected.")
    channel = transport.open_session()
    started = time.monotonic()
    try:
        channel.exec_command(command)
        stdout, stderr, truncated = _drain_exec_channel(
            channel,
            max_output_bytes=max_output_bytes,
            deadline=started + timeout + _REMOTE_KILL_GRACE_SECONDS,
            argv=argv,
            timeout=timeout,
        )
        # Safe to call unguarded here, unlike in detect_remote_dialect: _drain_exec_channel only
        # returns once it has seen exit_status_ready(), so the Event this waits on is already set.
        returncode = channel.recv_exit_status()
    finally:
        channel.close()

    if _is_remote_timeout(returncode, time.monotonic() - started, timeout):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout, output=stdout, stderr=stderr)
    return RemoteExecResult(returncode=returncode, stdout=stdout, stderr=stderr, truncated=truncated)


def run_remote_exec(
    docker_host: str,
    argv: Sequence[str],
    *,
    max_output_bytes: int,
    timeout: float,
    cwd: str | None = None,
) -> RemoteExecResult:
    """Connect to an ssh:// host, run one command on it, and close the connection.

    A fresh connection per call, which suits commands with no local inputs to stage (`scout_*`, and
    the reference-only buildx/stack subcommands): there is nothing to keep a session open for, and
    per-call teardown matches how `ssh_proxy_for_docker_host` already behaves.

    Args:
        docker_host - the host's resolved DOCKER_HOST value, starting with 'ssh://'
        argv - the remote command as an argv list, including the binary
        max_output_bytes - per-stream cap on retained output
        timeout - seconds the remote watchdog allows the command; also bounds the SSH handshake
        cwd - remote directory to run in
    returns: RemoteExecResult - exit status plus captured (possibly truncated) stdout/stderr bytes
    raises:
        RemoteFailureError - the connection could not be opened (with guidance)
        CapabilityError - the remote is not POSIX
        subprocess.TimeoutExpired - the command exceeded `timeout`
    """
    # Validate before connecting: opening (and authenticating) an SSH session only to reject the
    # caller's own arguments wastes a handshake against a possibly-remote host.
    _validate_exec_args(argv, timeout, max_output_bytes)
    ssh_client = connect_ssh_client(docker_host, timeout=timeout)
    try:
        dialect = detect_remote_dialect(ssh_client, docker_host, timeout=timeout)
        return exec_remote(
            ssh_client,
            argv,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            cwd=cwd,
            dialect=dialect,
        )
    finally:
        ssh_client.close()


# --- staging: putting local files where a remote docker command can read them -----------------------
#
# `run_remote_exec` covers commands whose arguments are all references (an image, a service, a stack).
# The rest - `compose -f docker-compose.yml`, `stack deploy -c`, `buildx bake -f`, a build context -
# name files that exist *here* and would mean something else, or nothing, on the target host. Those
# need the files copied over first, which is one logical operation with the command that reads them:
# hence a session holding one connection, one private remote directory, and a guaranteed teardown,
# rather than an exec primitive with a file-copy bolted on.


def _walk_relative(root: Path) -> Iterator[str]:
    """Yield every entry under `root` as a path relative to it, directories included.

    Symlinks are not followed (`followlinks=False`), so a link into a huge tree costs one entry rather
    than recursing through it - and matches how the tar records them.

    args: root - directory to walk
    returns: Iterator[str] - relative paths, in os.walk order
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            yield os.path.relpath(os.path.join(dirpath, name), root)


def _enforce_stage_limits(root: Path, entries: Iterator[str] | Sequence[str], *, what: str) -> None:
    """Refuse an oversized staging payload before any of it is read, tarred, or uploaded.

    Checks as it consumes `entries`, so a pathologically large tree is refused after a few thousand
    entries instead of being walked, tarred to local disk and pushed over SSH first. An entry that
    cannot be stat'd is counted but contributes no bytes: it is not this function's job to report a
    read error, and the tar step will surface the real one.

    Args:
        root - the directory the entries are relative to
        entries - relative paths to account for; a generator is consumed lazily, which is the point
        what - noun for the message, e.g. "directory" or "build context"
    raises: ToolInputError - the payload exceeds `_MAX_STAGE_BYTES` or `_MAX_STAGE_FILES`
    """
    total = 0
    count = 0
    for relative in entries:
        count += 1
        with contextlib.suppress(OSError):
            total += os.lstat(root / relative).st_size
        if count > _MAX_STAGE_FILES or total > _MAX_STAGE_BYTES:
            raise ToolInputError(
                f"Refusing to stage the {what} {str(root)!r} onto the remote host: it holds at least "
                f"{count} entries totalling {total // (1024 * 1024)} MiB, past the staging limit of "
                f"{_MAX_STAGE_FILES} entries / {_MAX_STAGE_BYTES // (1024 * 1024)} MiB. With no local docker "
                f"CLI the whole {what} has to be copied to the target host, and there is no way to tell "
                f"which files the command will actually read. Point the call at a smaller directory (for a "
                f"Compose tool, an explicit project_dir holding just the Compose files and what they "
                f"reference), exclude bulk directories via .dockerignore for a build, or install the docker "
                f"CLI on this host so the files can stay where they are."
            )


def _staged_member(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Keep regular files, directories and symlinks; drop anything else from a staged tar.

    FIFOs and device nodes cannot be meaningfully recreated on another host, and a staged copy of one
    would be a surprise rather than a service. (Sockets never reach this filter - `gettarinfo` returns
    None for them and `TarFile.add` skips them itself.)

    args: info - the member tarfile is about to add
    returns: tarfile.TarInfo | None - the member to keep, or None to skip it
    """
    if info.isfile() or info.isdir() or info.issym():
        return info
    logger.debug("remote-exec staging: skipping unsupported entry %r (type %r)", info.name, info.type)
    return None


def _tar_local_tree(root: Path) -> IO[bytes]:
    """Pack a directory's contents into an uncompressed tar in a local temp file, rewound for upload.

    No `.dockerignore` handling: this is a plain directory copy for tools that read files from a
    working directory (Compose, stack, bake), not a build context - see `stage_build_context` for the
    filtered variant. Uncompressed because the remote unpacks with plain `tar -xf`, whose compression
    autodetection is a GNU/bsdtar extension rather than a POSIX guarantee.

    A symlink is staged as a symlink, so one pointing inside the tree still resolves remotely while an
    absolute or escaping one will not - the same outcome as copying the tree by any other means.

    args: root - directory whose contents become the archive's top level
    returns: IO[bytes] - the archive, positioned at 0; the caller closes it
    """
    archive = tempfile.TemporaryFile()  # deleted on close, never linked into a directory
    try:
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            bundle.add(str(root), arcname=".", filter=_staged_member)
    except BaseException:
        archive.close()
        raise
    archive.seek(0)
    return archive


def _load_context_tar_helpers():
    """Import docker-py's context-tarring helpers on first use, not at module import.

    They are reused so a staged build context honours `.dockerignore` exactly as an SDK-driven build
    would - `APIClient.build` calls the same two - but nothing documents `docker.utils`, so this is a
    soft dependency on internals. At module scope that risk lands in the wrong place: every tool module
    imports this one, so an upstream rename would stop the whole server from starting even for a session
    that never stages anything (verified by deleting `docker.utils.tar` in a live interpreter: a
    module-level `from docker.utils import tar` then raises ImportError and takes startup with it).
    Importing here confines it to the one call that needs them, as an actionable error.

    Signatures verified against docker==7.1.0: `tar(path, exclude=None, dockerfile=None, fileobj=None,
    gzip=False)` returns a rewound file object, and `exclude_paths(root, patterns, dockerfile=None)`
    *mutates* `patterns`, hence the fresh copy at each call site. A silently changed *meaning* is not
    detectable here - only absence is.

    returns: tuple - (tar, exclude_paths) from docker.utils
    raises: CapabilityError - the installed docker-py does not provide them
    """
    try:
        from docker.utils import exclude_paths, tar
    except ImportError as exc:
        raise CapabilityError(
            f"Cannot stage a build context onto the remote host: the installed docker-py does not provide the "
            f"context-tarring helpers this needs ({exc}). `docker.utils.tar` / `docker.utils.exclude_paths` "
            f"have been stable for years but are not part of docker-py's published API, so a release can drop "
            f"them. Until this is updated, either install the docker CLI on this host (the local-CLI path "
            f"needs no staging) or pin docker-py to a release that still provides them."
        ) from exc
    return tar, exclude_paths


def _read_dockerignore(context_dir: Path) -> list[str]:
    """Read `.dockerignore` into the pattern list docker-py's tarring helpers expect.

    Mirrors `APIClient.build`'s own reading of the file (blank lines and `#` comments dropped, each
    line stripped) so a staged context excludes exactly what an SDK-driven build would.

    args: context_dir - the build context root
    returns: list[str] - patterns, empty when there is no .dockerignore
    """
    dockerignore = context_dir / ".dockerignore"
    if not dockerignore.is_file():
        return []
    lines = dockerignore.read_text(encoding="utf-8", errors="replace").splitlines()
    return [stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#")]


class RemoteStagingSession:
    """One SSH connection plus a private remote temp directory, for a command that reads local files.

    Built by `remote_staging_session`, which owns the teardown - don't construct one directly. Each
    `stage_*` call lands in its own numbered subdirectory, so two staged items with the same basename
    cannot collide and a staged tree never contains the archive it came from (which would otherwise
    end up inside a build context). Every staged path returned is absolute on the remote host and
    suitable as a `cwd` or an argument to `exec`.
    """

    def __init__(
        self,
        *,
        docker_host: str,
        ssh_client: paramiko.SSHClient,
        sftp: paramiko.SFTPClient,
        dialect_kind: RemoteDialectKind,
        root: str,
    ) -> None:
        self.docker_host = docker_host
        self.root = root
        self._ssh_client = ssh_client
        self._sftp = sftp
        self._dialect_kind = dialect_kind
        self._dialect = get_dialect(dialect_kind)
        self._slots = 0

    def _new_slot_path(self, kind: str) -> str:
        """Reserve the next numbered path under the session root, without creating anything there.

        args: kind - short label for the slot, for legibility while debugging on the remote host
        returns: str - an absolute remote path, guaranteed unused within this session
        """
        self._slots += 1
        return self._dialect.join_path(self.root, f"{kind}{self._slots}")

    def _new_slot(self, kind: str) -> tuple[str, str]:
        """Create the next numbered subdirectory under the session root.

        args: kind - short label for the slot, for legibility while debugging on the remote host
        returns: tuple[str, str] - (the new directory, a sibling path to use for its upload archive)
        """
        directory = self._new_slot_path(kind)
        self._sftp.mkdir(directory, mode=0o700)
        return directory, f"{directory}.tar"

    def join(self, *parts: str) -> str:
        """Join remote path components with the remote separator.

        Lets a caller build an absolute path under something a `stage_*` call returned, instead of
        depending on the command's working directory - which is what keeps `buildx_build`'s `--file`
        resolving the way local buildx resolves it.

        args: parts - remote path components, the first absolute
        returns: str - the joined remote path
        """
        return self._dialect.join_path(*parts)

    def _control(self, argv: list[str], *, timeout: float, what: str) -> RemoteExecResult:
        """Run one of the session's own bookkeeping commands, raising if it fails.

        Unlike `exec` - which runs the *caller's* docker command and reports failure in the result -
        these are our own steps, and a caller cannot do anything useful with a half-staged directory.

        Args:
            argv - the command to run on the remote host
            timeout - seconds allowed
            what - infinitive phrase for the error message, e.g. "unpack the staged archive"
        returns: RemoteExecResult - the successful result
        raises: RemoteFailureError - the command exited non-zero
        """
        result = self.exec(argv, timeout=timeout, max_output_bytes=_STAGING_OUTPUT_CAP_BYTES)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise RemoteFailureError(
                f"Remote-exec could not {what} on {self.docker_host}: `{shlex.join(argv)}` exited "
                f"{result.returncode}: {detail or '<no output>'}"
            )
        return result

    def _upload_and_extract(self, archive: IO[bytes], *, destination: str, archive_path: str) -> None:
        """Upload a tar over SFTP and unpack it into an already-created remote directory.

        The archive is written *beside* the destination rather than inside it, so nothing the caller
        later reads (a Compose file glob, a build context) can see it. It is removed once unpacked;
        the session teardown would collect it anyway, but two copies of a large tree in the remote
        temp dir for the rest of the session is worth avoiding.

        Args:
            archive - a rewound tar
            destination - remote directory to unpack into
            archive_path - remote path to upload the tar to
        raises: RemoteFailureError - the upload or the extraction failed
        """
        self._sftp.putfo(archive, archive_path, confirm=True)
        self._control(
            self._dialect.extract_tar_argv(archive_path, destination),
            timeout=_STAGING_EXTRACT_TIMEOUT_SECONDS,
            what="unpack the staged archive",
        )
        self._control(
            self._dialect.remove_tree_argv(archive_path),
            timeout=_STAGING_CONTROL_TIMEOUT_SECONDS,
            what="remove the staged archive",
        )

    def stage_tree(self, local_dir: Path | str) -> str:
        """Copy a whole local directory to the remote host and return its remote path.

        For tools that resolve relative paths against a working directory - Compose's `project_dir`,
        `stack deploy`'s `-c` files, `buildx bake`'s files. The copy is unfiltered: it cannot know
        which files the command will read (Compose's `build:`, `env_file:` and `include:` all name
        arbitrary relative paths), so `_enforce_stage_limits` is what keeps an oversized directory
        from being pushed silently. Use `stage_build_context` instead when the payload *is* a build
        context, since that has `.dockerignore` to narrow it.

        args: local_dir - the directory to copy; `~` is expanded
        returns: str - the remote directory holding the copied contents
        raises:
            ToolInputError - `local_dir` is not a directory, or exceeds the staging limits
            RemoteFailureError - the upload or remote extraction failed
        """
        source = Path(local_dir).expanduser()
        if not source.is_dir():
            raise ToolInputError(f"Cannot stage {str(source)!r} onto the remote host: it is not a directory.")
        _enforce_stage_limits(source, _walk_relative(source), what="directory")
        destination, archive_path = self._new_slot("tree")
        with _tar_local_tree(source) as archive:
            self._upload_and_extract(archive, destination=destination, archive_path=archive_path)
        return destination

    def stage_file(self, local_file: Path | str) -> str:
        """Copy one local file to the remote host and return its remote path.

        For a lone path argument that is not a whole tree - a buildkitd config, an imagetools
        descriptor, a Dockerfile living outside its build context. Uploaded directly over SFTP; no tar
        or remote extraction is involved.

        args: local_file - the file to copy; `~` is expanded
        returns: str - the remote path of the copied file, keeping its basename
        raises:
            ToolInputError - `local_file` is not a file, or is larger than `_MAX_STAGE_BYTES`
            RemoteFailureError - the upload failed
        """
        source = Path(local_file).expanduser()
        if not source.is_file():
            raise ToolInputError(f"Cannot stage {str(source)!r} onto the remote host: it is not a file.")
        size = source.stat().st_size
        if size > _MAX_STAGE_BYTES:
            raise ToolInputError(
                f"Refusing to stage the file {str(source)!r} onto the remote host: it is "
                f"{size // (1024 * 1024)} MiB, past the staging limit of "
                f"{_MAX_STAGE_BYTES // (1024 * 1024)} MiB."
            )
        destination, _ = self._new_slot("file")
        remote_path = self._dialect.join_path(destination, source.name)
        self._sftp.put(str(source), remote_path, confirm=True)
        return remote_path

    def stage_build_context(self, context_dir: Path | str, *, dockerfile: str | None = None) -> str:
        """Copy a build context to the remote host, honouring `.dockerignore`, and return its path.

        Uses docker-py's own tarring helpers, so what lands remotely is what an SDK-driven build would
        have sent: excluded paths are never read, and the limit check runs over the *included* set, so
        a context that `.dockerignore`s a large dataset is not refused for its size.

        `dockerfile` names a Dockerfile *inside* the context, relative to it, and is passed through so
        the exclusion pass keeps it even when the patterns would have dropped it (`*.dockerfile`, say)
        - the same negation docker-py applies. A Dockerfile *outside* the context is not a context file
        at all: stage it with `stage_file` and point `-f` at the result.

        Args:
            context_dir - the build context root; `~` is expanded
            dockerfile - path to the Dockerfile relative to the context, or None for the default
        returns: str - the remote directory holding the unpacked context
        raises:
            ToolInputError - `context_dir` is not a directory, or the included set exceeds the limits
            RemoteFailureError - the upload or remote extraction failed
        """
        source = Path(context_dir).expanduser()
        if not source.is_dir():
            raise ToolInputError(
                f"Cannot stage the build context {str(source)!r} onto the remote host: not a directory."
            )
        context_tar, exclude_paths = _load_context_tar_helpers()
        patterns = _read_dockerignore(source)
        # Both helpers mutate the pattern list they are given (each appends a `!<dockerfile>`
        # negation), so each gets its own copy.
        included = sorted(exclude_paths(str(source), list(patterns), dockerfile=dockerfile))
        _enforce_stage_limits(source, included, what="build context")
        destination, archive_path = self._new_slot("context")
        # docker-py types the return as its temp-file wrapper *or* a caller-supplied fileobj; with no
        # fileobj passed it is always the former, and `create_archive` rewinds it before returning.
        archive = cast(
            IO[bytes],
            context_tar(
                source,
                exclude=list(patterns),
                # The tuple form docker-py builds internally: (path relative to the context, contents).
                # Contents None means "the file is already in the context" - it only needs protecting
                # from the exclusion patterns, not injecting as an extra tar member.
                dockerfile=(dockerfile, None) if dockerfile else None,
                gzip=False,
            ),
        )
        with contextlib.closing(archive):
            self._upload_and_extract(archive, destination=destination, archive_path=archive_path)
        return destination

    def reserve_path(self) -> str:
        """Reserve a fresh, not-yet-existing path under the session root, for a remote command to create.

        Unlike `stage_file`/`stage_tree`, nothing is uploaded and nothing is created here - the path is
        merely guaranteed unused. Handing this to a command that writes to a path (`docker compose cp
        SERVICE:PATH <this>`) gives it the same "does not exist yet" starting state a fresh local
        destination would have, so it produces the same file-or-directory result `docker cp`'s own
        semantics would from that state - `fetch_path` then brings whatever it produced back down.

        returns: str - an absolute remote path, guaranteed not to already exist in this session
        """
        return self._new_slot_path("fetch")

    def _remote_is_dir(self, remote_path: str) -> bool:
        """True if `remote_path` is a directory on the remote host; False if it's anything else."""
        result = self.exec(
            ["test", "-d", remote_path],
            timeout=_STAGING_CONTROL_TIMEOUT_SECONDS,
            max_output_bytes=_STAGING_OUTPUT_CAP_BYTES,
        )
        return result.returncode == 0

    def _fetch_file(self, remote_path: str, local_dest: Path) -> None:
        """Fetch a single remote file straight to `local_dest`, via `stream_to_file`'s safe write."""
        size = self._sftp.stat(remote_path).st_size
        if size is not None and size > _MAX_STAGE_BYTES:
            raise ToolInputError(
                f"Refusing to fetch {remote_path!r} from the remote host: it is {size // (1024 * 1024)} MiB, "
                f"past the staging limit of {_MAX_STAGE_BYTES // (1024 * 1024)} MiB."
            )
        with self._sftp.open(remote_path, "rb") as handle:
            handle.prefetch()
            chunks = iter(lambda: handle.read(_FETCH_CHUNK_BYTES), b"")
            stream_to_file(chunks, str(local_dest), overwrite=False)

    def _fetch_directory(self, remote_path: str, local_dest: Path) -> None:
        """Fetch a remote directory to `local_dest`: pack it remotely, download the tar, extract locally.

        Mirrors `_upload_and_extract` in reverse. The byte cap is checked against the packed archive
        before it is downloaded (cheap: the remote host already made it); the entry-count cap can only
        be checked once the archive is local, since nothing short of downloading or a second remote
        round trip would tell us how many members it holds. `tarfile`'s "data" extraction filter - the
        ordinary safe default (PEP 706) - is what actually stops a member from escaping `local_dest`'s
        parent via `..` or an absolute path; the caps above stop it from being oversized, not malicious.

        The tar's sole top-level member is `remote_path`'s basename, so it lands at
        `local_dest.parent / <that basename>` before the final rename to `local_dest` - refused
        upfront, before any remote work, if that intermediate path already exists: `extractall` would
        otherwise merge into an existing directory there rather than fail, before this function ever
        gets to the rename that would have caught the collision.
        """
        extracted = local_dest.parent / posixpath.basename(remote_path.rstrip("/"))
        if extracted.exists():
            raise ToolInputError(
                f"Cannot fetch {remote_path!r} to {str(local_dest)!r}: the temporary extraction path "
                f"{str(extracted)!r} already exists on this host, and extracting into it could merge with "
                f"unrelated content there. Remove it, or choose a different destination."
            )
        archive_path = f"{self._new_slot_path('fetchout')}.tar"
        self._control(
            self._dialect.create_tar_argv(remote_path, archive_path),
            timeout=_STAGING_EXTRACT_TIMEOUT_SECONDS,
            what="pack the result for download",
        )
        try:
            size = self._sftp.stat(archive_path).st_size
            if size is not None and size > _MAX_STAGE_BYTES:
                raise ToolInputError(
                    f"Refusing to fetch {remote_path!r} from the remote host: the result is "
                    f"{size // (1024 * 1024)} MiB, past the staging limit of {_MAX_STAGE_BYTES // (1024 * 1024)} MiB."
                )
            archive = tempfile.TemporaryFile()
            try:
                self._sftp.getfo(archive_path, archive)
                archive.seek(0)
                with tarfile.open(fileobj=archive, mode="r") as bundle:
                    members = bundle.getmembers()
                    if len(members) > _MAX_STAGE_FILES:
                        raise ToolInputError(
                            f"Refusing to fetch {remote_path!r} from the remote host: the result holds "
                            f"{len(members)} entries, past the staging limit of {_MAX_STAGE_FILES}."
                        )
                    assert_host_writable(str(local_dest))
                    bundle.extractall(path=local_dest.parent, filter="data")
            finally:
                archive.close()
        finally:
            self._control(
                self._dialect.remove_tree_argv(archive_path),
                timeout=_STAGING_CONTROL_TIMEOUT_SECONDS,
                what="remove the temporary download archive",
            )
        if extracted != local_dest:
            extracted.rename(local_dest)

    def fetch_path(self, remote_path: str, local_dest: Path | str) -> None:
        """Bring a path a remote command just produced (via `reserve_path`) back to a local destination.

        The inverse of `stage_file`/`stage_tree`: probes whether `remote_path` is a file or a
        directory (only the command that produced it knows), then fetches accordingly - a file
        directly over SFTP, a directory by packing it remotely and extracting the download locally.
        `local_dest` must not already exist: the whole point of `reserve_path` is that the remote
        command started from a clean slate, so this does too rather than merging into or silently
        overwriting something already there.

        Args:
            remote_path - absolute remote path a command wrote to (typically a `reserve_path` result)
            local_dest - local path to create; refused if it already exists
        raises:
            ToolInputError - `local_dest` already exists
            ToolInputError - `local_dest`'s parent is not a directory, or the fetched payload exceeds the
                         staging limits
            RemoteFailureError - the remote path is missing, or packing/removing it remotely failed
        """
        local_dest = Path(local_dest).expanduser()
        if local_dest.exists():
            raise ToolInputError(
                f"Refusing to fetch {remote_path!r} to {str(local_dest)!r}: the destination already exists "
                f"on this host. The remote-exec fallback only creates a new path, matching the state the "
                f"remote command started from - remove the existing path first, or choose a different one."
            )
        if not local_dest.parent.is_dir():
            raise ToolInputError(
                f"Cannot fetch {remote_path!r} to {str(local_dest)!r}: {str(local_dest.parent)!r} is not a "
                f"directory on this host."
            )
        if self._remote_is_dir(remote_path):
            self._fetch_directory(remote_path, local_dest)
        else:
            self._fetch_file(remote_path, local_dest)

    def exec(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_output_bytes: int,
        cwd: str | None = None,
    ) -> RemoteExecResult:
        """Run a command on the session's host, reusing its connection.

        Same semantics as `run_remote_exec` (watchdog timeout, concurrent drain, `TimeoutExpired` on
        expiry) without a second handshake, and `cwd` here is a *remote* path - typically one a
        `stage_*` call just returned.

        Args:
            argv - the remote command, including the binary
            timeout - seconds the remote watchdog allows the command
            max_output_bytes - per-stream cap on retained output
            cwd - remote directory to run in
        returns: RemoteExecResult - exit status plus captured (possibly truncated) output
        raises:
            RuntimeError - the transport is gone
            subprocess.TimeoutExpired - the command exceeded `timeout`
        """
        return exec_remote(
            self._ssh_client,
            argv,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            cwd=cwd,
            dialect=self._dialect_kind,
        )


def _make_stage_root(ssh_client: paramiko.SSHClient, dialect_kind: RemoteDialectKind, docker_host: str) -> str:
    """Create the session's private temp directory on the remote host and return its path.

    Args:
        ssh_client - an already-connected client for the host
        dialect_kind - the host's detected dialect
        docker_host - the host's URL, for the error message
    returns: str - the absolute remote path of the new directory
    raises: RemoteFailureError - the remote could not create a temp directory, or named it unusably
    """
    argv = get_dialect(dialect_kind).temp_dir_argv()
    result = exec_remote(
        ssh_client,
        argv,
        max_output_bytes=_STAGING_OUTPUT_CAP_BYTES,
        timeout=_STAGING_CONTROL_TIMEOUT_SECONDS,
        dialect=dialect_kind,
    )
    root = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not root or "\n" in root:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise RemoteFailureError(
            f"Could not create a staging directory on {docker_host}: `{shlex.join(argv)}` exited "
            f"{result.returncode} and reported {detail or '<no output>'}. Staging needs a writable temp "
            f"directory (${{TMPDIR:-/tmp}}) on the target host."
        )
    return root


def _verify_shared_filesystem(sftp: paramiko.SFTPClient, root: str, docker_host: str) -> None:
    """Confirm the SFTP subsystem sees the directory the exec channel just created.

    One SSH connection does not guarantee one filesystem. A Windows sshd whose `DefaultShell` is
    `wsl.exe` runs exec commands inside the WSL distro - so `uname -s` says Linux and the watchdog
    works - while its SFTP subsystem is still the Windows-side one, leaving the two channels
    disagreeing about what `/tmp/...` means. Staging would then fail somewhere in the middle with a
    confusing error. One `stat` of the directory we just made catches that, and equally catches a
    chrooted or jailed SFTP subsystem.

    Scoped to staging on purpose: exec-only calls touch no SFTP and keep working against such a host,
    so refusing it outright would give up capability for nothing.

    Args:
        sftp - the session's SFTP client
        root - the directory created over the exec channel
        docker_host - the host's URL, for the error message
    raises: CapabilityError - SFTP cannot see `root`
    """
    try:
        sftp.stat(root)
    except OSError as exc:
        raise CapabilityError(
            f"Remote-exec cannot stage files to {docker_host}: the directory {root!r} created over the SSH "
            f"exec channel is not visible to that host's SFTP subsystem ({exc}), so the two are not looking "
            f"at the same filesystem. The usual cause is a Windows sshd whose shell is `wsl.exe` - exec lands "
            f"in the WSL distro while SFTP stays on the Windows side; a chrooted or jailed SFTP subsystem "
            f"does the same. Tools that only run a command remotely still work against this host; only "
            f"staging local files does not. Run sshd inside the WSL distro itself, or install the docker CLI "
            f"on this host so its files never need copying."
        ) from exc


# Teardown must not raise: it runs in a `finally`, where an exception would replace whatever the body
# was already reporting. These are the failures a bounded remote `rm` can produce.
_TEARDOWN_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    EOFError,
    paramiko.SSHException,
    subprocess.TimeoutExpired,
    RuntimeError,
    # get_dialect() raises CapabilityError now, and _remove_stage_root() reaches it from a finally.
    # Not reachable today - the dialect is validated at session open - but the tuple has to keep
    # backing the promise above rather than the set of types that happened to be raised when it was
    # written. Only that one type: unlike the translation table, where naming a family base is right
    # because everything deliberate should reach the caller, a swallow-and-continue list wants the
    # narrowest thing that works. `DockerMcpError` here would quietly eat a `ToolInputError` raised
    # by mistake during teardown, which is a bug worth seeing.
    CapabilityError,
)


def _remove_stage_root(
    ssh_client: paramiko.SSHClient, dialect_kind: RemoteDialectKind, root: str, docker_host: str
) -> None:
    """Delete the session's temp directory, reporting a failure without raising.

    Logged at warning because a surviving directory is a real (if small) problem - remote disk held
    until someone clears /tmp, possibly holding a staged secret file - and the operator can only act
    on it if we say so. The one case this cannot cover is a dropped transport: there is no channel left
    to run `rm` on, which is why the directory carries the project name and 0700 mode.

    Args:
        ssh_client - the session's client, still connected
        dialect_kind - the host's detected dialect
        root - the directory to remove
        docker_host - the host's URL, for the log line
    """
    try:
        result = exec_remote(
            ssh_client,
            get_dialect(dialect_kind).remove_tree_argv(root),
            max_output_bytes=_STAGING_OUTPUT_CAP_BYTES,
            timeout=_STAGING_CONTROL_TIMEOUT_SECONDS,
            dialect=dialect_kind,
        )
        if result.returncode != 0:
            logger.warning(
                "remote-exec: could not remove the staging directory %s on %s (exit %d): %s",
                root,
                docker_host,
                result.returncode,
                result.stderr.decode("utf-8", errors="replace").strip(),
            )
    except _TEARDOWN_ERRORS:
        logger.warning(
            "remote-exec: could not remove the staging directory %s on %s; it may need clearing by hand",
            root,
            docker_host,
            exc_info=True,
        )


@contextlib.contextmanager
def remote_staging_session(docker_host: str, *, timeout: float | None = None) -> Iterator[RemoteStagingSession]:
    """Open a staging session against an ssh:// host: one connection, one temp dir, guaranteed teardown.

    Use it for a command that reads local files (Compose files, a bake file, a build context); use
    `run_remote_exec` when every argument is a reference and there is nothing to copy. Staging and
    running are one logical operation, so they share a connection: the files exist for exactly as long
    as the command that needs them.

    Cleanup falls out of ordinary context-manager semantics rather than being special-cased per exit
    path, so success, an exception and a timeout all remove the directory. A hard transport drop is the
    exception - nothing is left to run `rm` on - which is inherent rather than handled.

    Args:
        docker_host - the host's resolved DOCKER_HOST value, starting with 'ssh://'
        timeout - seconds bounding the SSH handshake and the dialect probe; the session's own
                  bookkeeping commands use their own bounds, and each `exec` takes its own timeout
    returns: Iterator[RemoteStagingSession] - the session, valid inside the `with` block only
    raises:
        RemoteFailureError - the connection could not be opened, or the remote could not create a
                       writable temp directory
        CapabilityError - a non-POSIX remote, or an SFTP subsystem that cannot see the exec
                       channel's filesystem
    """
    ssh_client = connect_ssh_client(docker_host, timeout=timeout)
    sftp: paramiko.SFTPClient | None = None
    root: str | None = None
    try:
        dialect_kind = detect_remote_dialect(ssh_client, docker_host, timeout=timeout)
        # Refuse a non-POSIX remote here, before anything is created or uploaded, rather than letting
        # the first bookkeeping command fail with a shell error.
        get_dialect(dialect_kind)
        root = _make_stage_root(ssh_client, dialect_kind, docker_host)
        sftp = ssh_client.open_sftp()
        _verify_shared_filesystem(sftp, root, docker_host)
        yield RemoteStagingSession(
            docker_host=docker_host,
            ssh_client=ssh_client,
            sftp=sftp,
            dialect_kind=dialect_kind,
            root=root,
        )
    finally:
        if root is not None:
            _remove_stage_root(ssh_client, dialect_kind, root, docker_host)
        if sftp is not None:
            with contextlib.suppress(Exception):  # teardown must never mask the body's outcome
                sftp.close()
        ssh_client.close()

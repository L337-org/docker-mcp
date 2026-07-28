# SSH plumbing shared by every CLI-backed tool (Compose, Stack, Buildx, Context, Scout), built on
# paramiko — the same pure-Python transport docker-py already uses for the SDK-backed tools, so both
# tool families authenticate identically over SSH with no system `ssh` client involved.
#
# Two distinct mechanisms live here, both on top of `connect_ssh_client`:
#
# 1. `ssh_proxy_for_docker_host` — a per-call localhost TCP proxy letting a *local* `docker` CLI
#    drive a remote daemon. Mechanism (see docker-py's docker/transport/sshconn.py): both the docker
#    CLI and docker-py run `docker system dial-stdio` over an SSH session channel, which bridges the
#    remote /var/run/docker.sock to stdin/stdout, one channel per API connection. docker-py opens
#    those channels on its own paramiko transport; here we accept plain TCP connections from the
#    `docker` CLI on 127.0.0.1 and bridge each to its own `dial-stdio` channel over one shared
#    paramiko connection, full-duplex, until either side closes.
#
# 2. `run_remote_exec` — runs the `docker` CLI *on the remote host itself*, for the fallback where
#    there is no local `docker` binary (or plugin) to point at a daemon in the first place. Used
#    only when the local CLI is genuinely unavailable; when it is present, mechanism 1 is unchanged.

import contextlib
import enum
import logging
import math
import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

import paramiko

from docker_mcp._hosts import is_ssh_url

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


def parse_ssh_url(url: str) -> SshTarget:
    """
    Parse a DOCKER_HOST=ssh://... URL into paramiko connection parameters.

    Applies the same ~/.ssh/config lookups (Hostname, Port, User, IdentityFile, ProxyCommand)
    that docker-py's `SSHHTTPAdapter._create_paramiko_client` performs, so this proxy resolves the
    same target docker-py (and the system `ssh` client) would for the same URL.

    The scheme is checked rather than assumed. Both callers (the dial-stdio proxy and the remote-exec
    fallback) only ever pass an ssh:// host, but a wrong scheme would otherwise be parsed as if it
    were one — `tcp://10.0.0.5:2375` yields a plausible target and gets attempted as SSH on port 2375,
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
    (which runs beforehand, to set up the local proxy) unbounded. The bound is itself capped at
    `_CONNECT_TIMEOUT_CAP_SECONDS` so a large operation timeout (e.g. an 1800s build) still fails an
    unreachable host fast rather than hanging for the whole operation budget.

    A connection failure (auth, unknown host key, unreachable host) is re-raised as a `RuntimeError`
    with actionable guidance rather than a bare paramiko/socket exception.

    args:
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
    if target.proxycommand:
        connect_kwargs["sock"] = paramiko.ProxyCommand(target.proxycommand)
    if timeout is not None:
        bounded = min(timeout, _CONNECT_TIMEOUT_CAP_SECONDS)
        connect_kwargs["timeout"] = bounded
        connect_kwargs["banner_timeout"] = bounded
        connect_kwargs["auth_timeout"] = bounded
    try:
        client.connect(**connect_kwargs)
    except (paramiko.SSHException, OSError) as exc:
        client.close()
        raise RuntimeError(
            f"Could not establish the SSH connection to {docker_host!r} for the docker CLI: {exc}. "
            f"Check that your key is loaded (run `ssh-add`, and forward SSH_AUTH_SOCK), that the host "
            f"key is in ~/.ssh/known_hosts (paramiko rejects unknown hosts — connect once with `ssh` "
            f"after verifying its fingerprint), and that the host is reachable."
        ) from exc
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


# --- remote exec: run the `docker` CLI on the far side of the SSH connection ----------------------
#
# The fallback for a machine with no local `docker` binary (or a missing plugin) but SSH access to a
# real Docker host: instead of driving a local CLI at a remote daemon (the proxy above), run the CLI
# on the remote host, which — being a Docker host — plausibly already has it.

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
    """
    Reject arguments the remote path cannot honour, before anything connects or runs.

    A non-positive timeout raises `subprocess.TimeoutExpired` rather than `ValueError`, to match the
    local path exactly: `subprocess.run` raises that same exception immediately for `0`, `-1` and
    `-0.5` (verified), and the command never completes. Remotely the watchdog's one-second floor would
    otherwise quietly grant a whole second of runtime, so a mutating command — `compose down`, say —
    would actually execute where the local backend refused it. Silently running more than asked is a
    worse failure than a clear error.

    args:
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
    """
    Whole seconds the remote watchdog sleeps before killing the command.

    Shared by the wrapper that emits the `sleep` and the attribution check that reasons about when it
    can have fired, so the two cannot drift apart. `sleep` takes whole seconds portably, hence the
    round up; the floor of 1 keeps a sub-second timeout from degenerating into `sleep 0`.

    args: timeout - the caller's timeout in seconds
    returns: int - the watchdog's sleep duration, at least 1
    """
    return max(1, math.ceil(timeout))


def _is_remote_timeout(returncode: int, elapsed: float, timeout: float) -> bool:
    """
    Whether a finished remote command should be reported as having timed out.

    Requires both the wrapper's sentinel status *and* corroborating elapsed time, so a command that
    legitimately exits with the sentinel code well inside its budget — a container propagating 124
    through `docker run`, say — is reported as the ordinary failure it is rather than as a timeout.

    The comparison is against the watchdog's *effective* sleep, not the caller's raw timeout. Those
    differ whenever the timeout is not a whole number of seconds, and the difference is not benign: at
    `timeout=30.2` the watchdog cannot fire before 31s, so testing against 30.2 would misattribute a
    sentinel exit at 30.5s, and at `timeout=0.1` the raw threshold goes negative and would misattribute
    *every* sentinel exit. Comparing against the sleep the watchdog actually performs closes both.

    args:
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

# Idle poll interval while draining a remote command's output. Deliberately a poll rather than
# select(): paramiko Channels are select-able only via fileno(), which both couples this loop to a
# real channel object and allocates an OS pipe per call — a plain readiness poll keeps the loop
# trivially fake-able in tests, and at these call rates the wakeups cost nothing measurable.
_EXEC_POLL_SECONDS = 0.01

# How many extra readiness polls to make after the exit status appears, before accepting that the
# command's output is finished. Guards against a final chunk surfacing from paramiko's transport
# thread just after both streams last read as quiet, which would otherwise be dropped silently.
# Costs at most `_EXIT_SETTLE_POLLS * _EXEC_POLL_SECONDS` once per call, only at the very end.
_EXIT_SETTLE_POLLS = 5


class RemoteDialectKind(enum.Enum):
    """
    Which command-wrapping dialect a remote host needs.

    Only POSIX is implemented. WINDOWS exists so detection can *name* what it found and refuse
    precisely, rather than mis-running a POSIX script against cmd/PowerShell — and so adding Windows
    later is one new dialect implementation rather than a redesign.
    """

    POSIX = "posix"
    WINDOWS = "windows"


# `uname -s` values we accept as POSIX. Matched exactly (lowercased) rather than by "did uname exit
# 0", because exit status alone has a real false positive: a Windows host whose sshd shell is
# cmd/PowerShell but which has Git Bash or Cygwin on PATH answers `uname -s` successfully with
# MINGW64_NT-… , which would classify as POSIX and drop us into a half-working MSYS environment with
# translated paths. Note WSL reports plain "Linux" and so is (correctly) accepted: sshd running
# inside a WSL distro is a genuine Linux target, not a Windows one.
_POSIX_UNAME_VALUES = frozenset({"linux", "darwin", "freebsd", "openbsd", "netbsd", "dragonfly", "sunos", "aix"})


class RemoteDialect(Protocol):
    """Wraps an argv into a single remote shell command string that self-enforces a timeout."""

    def wrap_with_timeout(self, argv: Sequence[str], *, timeout: float, cwd: str | None = None) -> str: ...


class PosixDialect:
    """
    Command wrapper for a POSIX remote shell, needing only `sh`, `sleep`, `kill` and `mktemp`.

    Deliberately not GNU coreutils `timeout`, which is absent on macOS/BSD; this runs anywhere with
    a POSIX shell. Termination is the *remote* side's own responsibility because closing an SSH
    channel does not portably kill what it started.
    """

    def wrap_with_timeout(self, argv: Sequence[str], *, timeout: float, cwd: str | None = None) -> str:
        """
        Build the remote `sh -c` command that runs `argv` under a self-killing watchdog.

        Two things here are load-bearing and easy to get wrong:

        `cd` is emitted as its **own statement**, never joined to the command with `&&`. In POSIX
        shell `&` binds looser than `&&`, so `cd X && cmd &` makes the whole AND-list one async job
        and `$!` becomes the *subshell's* pid — killing that leaves the real `docker` process alive
        and orphaned to init on every single timeout. Keeping `cd` separate means `$!` is the
        command itself.

        The watchdog reports `_REMOTE_TIMEOUT_EXIT_CODE` via a marker file, written only when the
        command was still alive at the deadline, so a timeout is distinguishable from any other
        non-zero exit. Testing whether the *watchdog* is still alive with `kill -0` would instead be
        wrong: a watchdog that has fired but not yet been reaped is a zombie whose pid still answers,
        so real timeouts would be missed. (`kill -0` on the *command* below is a different question —
        asked before signalling, about a live child — and is sound.)

        The marker is written **before** the kill, which is what makes it race-free. The kill is what
        releases the main shell from `wait $pid`, and the main shell then kills the watchdog — so
        writing the marker afterwards leaves a window where the watchdog is killed before the `printf`
        lands, and a genuine timeout is misreported as an ordinary SIGTERM death (observed:
        intermittent 143 instead of the sentinel). Ordering it first guarantees the main shell is
        still blocked when the file is written.

        Both `wait`s run inside `{ ... } 2>/dev/null` groups to swallow the shell's own asynchronous
        job-reap notices ("Terminated: 15 ( sleep 30; ...)"), which otherwise land in the *command's*
        captured stderr and corrupt it — on every fast command, since killing the still-sleeping
        watchdog is the normal path. The redirect only hides the shell's notice, not the command's own
        output: the command inherited fd 2 when it started, so its writes are unaffected. A brace
        group is required rather than a subshell so `ec=$?` assigns in the current shell.

        The watchdog's own stdio is sent to /dev/null so it never holds the command's streams. Killing
        the watchdog subshell does not reliably kill the `sleep` inside it (same reason `cd X && cmd &`
        was wrong above), and an orphaned `sleep` still holding those descriptors keeps the stream open
        for the remainder of the timeout window — which a consumer waiting for EOF experiences as a
        fast command hanging for its full timeout. The watchdog has no legitimate use for them anyway.

        Known and accepted: the kill is SIGTERM to the direct child only (no process-group signal, no
        SIGKILL escalation) — the same semantics `subprocess.run(timeout=...)` already has locally,
        so this is parity rather than a new gap. A command that exits on its own at the exact instant
        the watchdog fires may be attributed either way; the window is microseconds wide.

        args:
            argv - the remote command as an argv list; joined with shell quoting, never concatenated
            timeout - seconds before the remote watchdog kills the command (rounded up, floor 1s)
            cwd - remote directory to run in; a failure to enter it exits 127 without running argv
        returns: str - a complete `sh -c '...'` command string for `Channel.exec_command`
        """
        seconds = _watchdog_sleep_seconds(timeout)
        # An explicit template, because the bare `mktemp` form is not portable: macOS accepts it, but
        # FreeBSD/OpenBSD/NetBSD require a template argument and would fail here — before running argv
        # at all — on hosts this dialect claims to support.
        lines = [
            # Fail fast if the marker cannot be created (absent `mktemp`, unwritable or read-only
            # TMPDIR). Continuing leaves `m` empty, and the marker write then fails silently while the
            # kill still happens — so a genuine timeout comes back as a plain SIGTERM exit and is
            # reported as an ordinary failure (verified: rc=143 instead of the sentinel). Exit 125
            # follows GNU `timeout`'s convention for "the wrapper itself could not run".
            'm=$(mktemp "${TMPDIR:-/tmp}/docker-mcp-server.XXXXXXXX") || {'
            ' echo "docker-mcp-server: cannot create a temp file on the remote host'
            ' (is ${TMPDIR:-/tmp} writable?)" >&2; exit 125; }',
            # Remove the marker on any exit rather than only the happy path: the early `cd` failure
            # below returns without reaching the end of the script, and an outer timeout or dropped
            # channel can kill this shell outright — both of which otherwise strand the file in the
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
                # A single `sleep <timeout>` cannot be cleaned up portably — killing the subshell that
                # owns it leaves the `sleep` orphaned (verified on Linux/dash: one stray per call, alive
                # for the rest of the timeout window), and putting each background job in its own process
                # group with `set -m` so `kill -- -$wpid` reaches the child hangs outright on a shell with
                # no controlling terminal, which is exactly what an SSH exec channel provides.
                f'(i=0; while [ "$i" -lt {seconds} ]; do sleep 1;'
                " kill -0 $pid 2>/dev/null || exit 0; i=$((i+1)); done;"
                ' printf t >"$m"; kill $pid 2>/dev/null) >/dev/null 2>&1 &',
                "{ wait $pid; ec=$?; } 2>/dev/null",
                f'[ -s "$m" ] && ec={_REMOTE_TIMEOUT_EXIT_CODE}',
                "exit $ec",
            ]
        )
        return f"sh -c {shlex.quote(chr(10).join(lines))}"


_DIALECTS: dict[RemoteDialectKind, RemoteDialect] = {RemoteDialectKind.POSIX: PosixDialect()}


def get_dialect(kind: RemoteDialectKind) -> RemoteDialect:
    """
    Return the wrapper implementation for a dialect, or refuse if it isn't implemented yet.

    args: kind - the dialect a host was detected as
    returns: RemoteDialect - the implementation to wrap commands with
    raises: RuntimeError - for a detected-but-unimplemented dialect (today: WINDOWS)
    """
    dialect = _DIALECTS.get(kind)
    if dialect is None:
        # Deliberately not phrased as "this is a Windows host": `detect_remote_dialect` also routes a
        # failed probe and an unrecognized `uname -s` here, so a restricted shell or an uncommon Unix
        # kernel reaches this message too, and calling those Windows would be wrong.
        raise RuntimeError(
            "Remote-exec fallback: no supported POSIX shell was detected on this host, so the docker "
            "CLI cannot be run on it. Any host presenting a POSIX shell is supported — including "
            f"{', '.join(sorted(_POSIX_UNAME_VALUES))} — as is sshd running inside a WSL distro. Common "
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
    """
    Detect which command dialect a remote host needs, by probing `uname -s`.

    This is a *behavioural* probe — "is there a POSIX shell here that will run my script?" — not an
    OS fingerprint, which is why sshd inside a WSL distro is correctly accepted (it answers "Linux"
    and has real `sh`/`sleep`/`kill`). Only an allow-listed kernel name counts as POSIX; matching on
    "did `uname` exit 0" would wrongly accept a Windows host with Git Bash or Cygwin on PATH, which
    answers successfully with `MINGW64_NT-…`.

    Everything else — an MSYS/Cygwin-flavoured value, an unrecognised kernel, a restricted shell, a
    failed probe — is reported as WINDOWS, which here means only "no supported POSIX shell answered"
    rather than a claim about the OS. Since `get_dialect`'s refusal therefore cannot name a cause, both
    routes to WINDOWS log a warning before returning, so the refusal is diagnosable on first
    occurrence: the observed exit status and output when the probe ran, or the underlying error when it
    could not be completed at all. A successful POSIX detection logs nothing — there is nothing to
    explain, and this runs on every call whose host is not already cached.

    Cached per host with a short TTL (mirroring `_cli.has_plugin`), so a long-lived server neither
    re-probes on every call nor needs a restart after a remote change.

    args:
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
            # the `recv` below blocks first — before the exit-status deadline further down can help — so
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
            # indefinitely"), so a wedged remote shell would hang detection — and with it
            # run_remote_exec — regardless of the caller's timeout. A probe that never answers is
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
                # this line is the only place recording *what the host actually reported* — without it
                # the refusal is not diagnosable on first occurrence.
                logger.warning(
                    "remote-exec: host %s is not a supported POSIX remote — `uname -s` exited %d and "
                    "reported %r; remote-exec will be refused for this host",
                    cache_key,
                    status,
                    output,
                )
        finally:
            channel.close()
    except OSError, EOFError, paramiko.SSHException, RuntimeError:
        # Probe failure is itself the signal ("no POSIX shell answered"), never a hard error here —
        # get_dialect() is what turns a non-POSIX result into an actionable refusal.
        # Warning, not debug, for the same reason as the branch above: `get_dialect` can only refuse
        # generically, so this is the only record of *why*. This path is the least self-evident of the
        # lot — an unreachable host, a dead transport, a shell that never answered — so leaving it at
        # debug would mean the refusal could not be explained without reproducing it.
        logger.warning(
            "remote-exec: host %s is not a supported POSIX remote — the `uname -s` probe could not be "
            "completed; remote-exec will be refused for this host",
            cache_key,
            exc_info=True,
        )

    with _dialect_cache_lock:
        _dialect_cache[cache_key] = (time.monotonic(), kind)
    return kind


@dataclass(frozen=True)
class RemoteExecResult:
    """
    Outcome of one remote command: raw captured bytes plus whether the cap truncated them.

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
    """
    Pump a channel's stdout and stderr until the command ends, capping what we keep.

    Both streams must be drained *concurrently*: paramiko stops advertising window space for a
    stream nobody reads, so draining only stdout lets a chatty stderr fill the window and block the
    remote command until our own deadline — the classic pipe deadlock. `subprocess.run` avoids this
    locally via `communicate()`; this is the equivalent. For the same reason, once the cap is hit we
    keep reading and discard rather than stopping, since an unread stream would hang the remote
    instead of merely truncating its output.

    Completion is decided by `exit_status_ready()`, deliberately not by EOF on the streams. A command
    that spawns its own children leaves those children holding the inherited stdout/stderr after the
    watchdog SIGTERMs their parent, so waiting for EOF would block for the rest of the timeout window
    even though the command itself has already exited (measured: `subprocess.run(capture_output=True)`
    blocks exactly this way locally, since it *does* wait for EOF). Keying on the exit status makes
    this path return as soon as the command is genuinely done.

    args:
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
            # — the greedy drain cannot help, having already given up — and breaking here loses it
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
    """
    Run one command on an already-connected host, under the dialect's own timeout watchdog.

    Prefer `run_remote_exec` unless you already hold a connection you intend to reuse (staging a
    file and then running against it, say) — this takes a client rather than opening one.

    A timeout raises `subprocess.TimeoutExpired`, the same exception `run_docker`'s local path
    raises, so callers see one contract regardless of which backend ran. That covers both the
    remote watchdog's own kill (reported as `_REMOTE_TIMEOUT_EXIT_CODE`) and the local grace
    deadline, which only fires if the watchdog never ran at all.

    args:
        ssh_client - an already-connected client for the target host
        argv - the remote command as an argv list, including the binary (e.g. ["docker", "ps"])
        max_output_bytes - per-stream cap on retained output; the rest is drained and dropped
        timeout - seconds the remote watchdog allows the command before killing it
        cwd - remote directory to run in; entering it is part of the wrapped command
        dialect - the host's detected dialect; a non-POSIX one is refused by `get_dialect`
    returns: RemoteExecResult - exit status plus captured (possibly truncated) stdout/stderr bytes
    raises:
        RuntimeError - the transport is gone, or the dialect isn't implemented
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
    """
    Connect to an ssh:// host, run one command on it, and close the connection.

    A fresh connection per call, which suits commands with no local inputs to stage (`scout_*`, and
    the reference-only buildx/stack subcommands): there is nothing to keep a session open for, and
    per-call teardown matches how `ssh_proxy_for_docker_host` already behaves.

    args:
        docker_host - the host's resolved DOCKER_HOST value, starting with 'ssh://'
        argv - the remote command as an argv list, including the binary
        max_output_bytes - per-stream cap on retained output
        timeout - seconds the remote watchdog allows the command; also bounds the SSH handshake
        cwd - remote directory to run in
    returns: RemoteExecResult - exit status plus captured (possibly truncated) stdout/stderr bytes
    raises:
        RuntimeError - connection failure (with guidance), or a non-POSIX remote
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

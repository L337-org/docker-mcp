"""Tests for the SSH remote-exec primitives in `docker_mcp.tools._ssh_proxy`.

Split out from `test_ssh_proxy.py`, which covers the dial-stdio proxy (the other, older mechanism
in that module). Follows the same conventions: paramiko is never really contacted — `SSHClient` is
patched wholesale, or a fake channel is injected — and the generated shell script is additionally
executed through the *real* `/bin/sh`, since its correctness is entirely about shell semantics that
a mock cannot verify.
"""

import contextlib
import glob
import os
import shlex
import signal
import subprocess
import sys
import time
import unittest.mock
from typing import cast

import paramiko
import pytest

from docker_mcp.tools._ssh_proxy import (
    PosixDialect,
    _is_remote_timeout,
    RemoteDialectKind,
    _clear_dialect_cache,
    _REMOTE_TERM_GRACE_SECONDS,
    _REMOTE_TIMEOUT_EXIT_CODE,
    detect_remote_dialect,
    exec_remote,
    get_dialect,
    parse_ssh_url,
    run_remote_exec,
)

_CAP = 1_048_576  # per-stream output cap used by tests that don't care about truncation


@pytest.fixture(autouse=True)
def _clear_caches():
    """Dialect detection is TTL-cached per host; isolate every test from its neighbours."""
    _clear_dialect_cache()
    yield
    _clear_dialect_cache()


class FakeChannel:
    """
    Minimal `paramiko.Channel` stand-in for the exec path.

    Serves stdout/stderr in scripted chunks so a test can interleave them, and only reports the exit
    status once both are drained (mirroring a real channel, where output precedes completion).
    """

    def __init__(self, *, stdout=(), stderr=(), exit_status=0, exit_ready_immediately=False):
        self.stdout_chunks = list(stdout)
        self.stderr_chunks = list(stderr)
        self._exit_status = exit_status
        self._exit_ready_immediately = exit_ready_immediately
        self.executed: str | None = None
        self.closed = False
        self.timeout: float | None = None

    def settimeout(self, value):
        self.timeout = value

    def exec_command(self, command):
        self.executed = command

    def recv_ready(self):
        return bool(self.stdout_chunks)

    def recv_stderr_ready(self):
        return bool(self.stderr_chunks)

    def recv(self, _n):
        return self.stdout_chunks.pop(0)

    def recv_stderr(self, _n):
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self):
        if self._exit_ready_immediately:
            return True
        return not self.stdout_chunks and not self.stderr_chunks

    def recv_exit_status(self):
        return self._exit_status

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, channel):
        self.channel = channel

    def open_session(self):
        return self.channel


class FakeSshClient:
    def __init__(self, channel, *, transport=True):
        self._transport = FakeTransport(channel) if transport else None
        self.closed = False

    def get_transport(self):
        return self._transport

    def close(self):
        self.closed = True


def fake_client(channel, *, transport: bool = True) -> paramiko.SSHClient:
    """A FakeSshClient typed as the real thing, so these tests type-check without littering casts."""
    return cast(paramiko.SSHClient, FakeSshClient(channel, transport=transport))


# --- PosixDialect.wrap_with_timeout: script construction -----------------------------------------


def _script_body(argv, *, timeout, cwd=None) -> str:
    """
    The inner shell script, recovered from the `sh -c '<script>'` wrapper.

    Assertions must run against this rather than the wrapper string: the whole script is one quoted
    argument, so every single quote inside it appears as `'"'"'` in the outer form and a naive
    substring check either fails or (worse) passes for the wrong reason.
    """
    parts = shlex.split(PosixDialect().wrap_with_timeout(argv, timeout=timeout, cwd=cwd))
    assert parts[:2] == ["sh", "-c"], parts
    return parts[2]


def test_cwd_is_its_own_statement_never_joined_with_and():
    """
    Regression guard for the orphaned-remote-process bug.

    `cd X && cmd &` makes the whole AND-list one async job, so `$!` is the subshell's pid and killing
    it leaves the real command alive and reparented to init. Nothing fails visibly until a call times
    out, so the shape is pinned here rather than left to a functional test to notice.
    """
    for cwd in ("/srv/app", "/srv/my app"):
        body = _script_body(["docker", "ps"], timeout=30, cwd=cwd)
        cd_line = f"cd {shlex.quote(cwd)} || exit 127"
        assert cd_line in body.splitlines()
        # The backgrounded job is the command itself, on its own line — not tacked onto the cd.
        assert "docker ps & pid=$!" in body.splitlines()
        assert "&&" not in cd_line


def test_no_cd_statement_when_cwd_is_none():
    assert "cd " not in _script_body(["docker", "ps"], timeout=5)


def test_timeout_is_rounded_up_with_a_one_second_floor():
    """The deadline is the watchdog's loop bound (it counts one-second sleeps), rounded up because
    `sleep` takes whole seconds portably, with a floor of 1 so a sub-second timeout still waits."""
    assert 'while [ "$i" -lt 31 ]' in _script_body(["true"], timeout=30.2)
    assert 'while [ "$i" -lt 1 ]' in _script_body(["true"], timeout=0.1)


def test_argv_is_shell_quoted_not_concatenated():
    body = _script_body(["docker", "run", "a b; rm -rf /", "$HOME"], timeout=5)
    assert "'a b; rm -rf /'" in body
    assert "'$HOME'" in body


def test_reap_notice_is_suppressed_around_the_wait():
    """The shell's async "Terminated" job notice would otherwise land in the command's captured
    stderr when the watchdog kills it. A brace group, not a subshell, so `ec=$?` survives."""
    body = _script_body(["true"], timeout=5)
    assert "{ wait $pid; ec=$?; } 2>/dev/null" in body


def test_watchdog_stdio_is_detached_from_the_commands_streams():
    """A watchdog still holding the command's stdout/stderr keeps the stream open after the command
    has gone, which a consumer waiting for EOF sees as a hang."""
    body = _script_body(["true"], timeout=5)
    assert ">/dev/null 2>&1 &" in body


def test_watchdog_self_terminates_rather_than_being_killed():
    """
    Regression guard for a stray `sleep` on the remote host, one per call.

    A single `sleep <timeout>` cannot be cleaned up portably: killing the subshell that owns it leaves
    the `sleep` orphaned (verified on Linux/dash — one stray alive for the rest of the timeout window,
    which is 30 minutes for a build), and `set -m` plus `kill -- -$wpid` to reach the child hangs on a
    shell with no controlling terminal, which is what an SSH exec channel provides. So the watchdog
    counts in one-second sleeps and exits as soon as the command is gone, and nothing kills it.
    """
    body = _script_body(["true"], timeout=5)
    assert "kill $wpid" not in body  # nothing kills the watchdog any more
    assert "wpid" not in body  # so its pid is never even recorded
    watchdog = next(line for line in body.splitlines() if "printf t" in line)
    assert 'while [ "$i" -lt 5 ]' in watchdog  # counts to the timeout
    assert "sleep 1;" in watchdog  # in short sleeps, so a stray cannot outlive the call by long
    assert "kill -0 $pid 2>/dev/null || exit 0" in watchdog  # gives up once the command is gone


def test_watchdog_escalates_from_sigterm_to_sigkill():
    """
    SIGTERM first so the docker CLI can clean up, then SIGKILL so the timeout is a real guarantee.

    The local path is not a precedent for stopping at SIGTERM, as this once claimed: CPython's
    `subprocess.run(timeout=...)` calls `Popen.kill()`, which is SIGKILL on POSIX. Without escalation
    a command that traps or ignores SIGTERM outlives its own timeout while the caller is released.
    """
    watchdog = next(line for line in _script_body(["true"], timeout=5).splitlines() if "printf t" in line)
    assert watchdog.index("kill $pid") < watchdog.index("kill -9 $pid"), watchdog
    assert f"sleep {_REMOTE_TERM_GRACE_SECONDS}; kill -9 $pid" in watchdog


def test_timeout_is_reported_via_a_marker_not_the_signal_status():
    """A marker written only when the command was alive at the deadline, so a timeout is
    distinguishable from any other non-zero exit (143 would be indistinguishable from any SIGTERM
    death)."""
    body = _script_body(["true"], timeout=5)
    assert f'[ -s "$m" ] && ec={_REMOTE_TIMEOUT_EXIT_CODE}' in body


def test_marker_is_written_before_the_kill_not_after():
    """
    Race regression guard.

    The kill is what releases the main shell from `wait $pid`, and the main shell then kills the
    watchdog — so writing the marker *after* the kill leaves a window where the watchdog dies before
    the `printf` lands, and a genuine timeout is misreported as a plain SIGTERM death (observed as an
    intermittent 143). Writing it first means the main shell is provably still blocked.
    """
    body = _script_body(["true"], timeout=5)
    watchdog = next(line for line in body.splitlines() if "printf t" in line)
    assert watchdog.index('printf t >"$m"') < watchdog.index("kill $pid"), watchdog
    # Guarded on the command still being alive, so a command that already exited is not marked.
    assert "kill -0 $pid" in watchdog


def test_mktemp_failure_aborts_before_running_the_command():
    """
    A silent `mktemp` failure would misreport timeouts, not merely lose the marker.

    With `m` empty the marker write fails while the kill still happens, so a genuine timeout returns a
    plain SIGTERM status and is reported as an ordinary failure — verified as rc=143 instead of the
    sentinel. Exit 125 follows GNU `timeout`'s convention for "the wrapper itself could not run".
    """
    body = _script_body(["true"], timeout=5)
    mktemp_line = next(line for line in body.splitlines() if "mktemp" in line)
    assert "|| {" in mktemp_line
    assert "exit 125" in mktemp_line
    assert "cannot create a temp file" in mktemp_line  # says why, on stderr


def test_mktemp_is_given_an_explicit_template():
    """The bare `mktemp` form is accepted by macOS but rejected by FreeBSD/OpenBSD/NetBSD, which
    require a template — there it would fail before running argv at all, on hosts this dialect
    claims to support."""
    body = _script_body(["true"], timeout=5)
    assert 'mktemp "${TMPDIR:-/tmp}/docker-mcp-server.XXXXXXXX"' in body


def test_marker_is_removed_on_any_exit_not_just_the_happy_path():
    """The `cd` failure path returns without reaching the end of the script, and a dropped channel
    has sshd signal the shell — both stranded the marker in the remote temp dir before the trap."""
    cwd = "/srv/app"
    lines = _script_body(["true"], timeout=5, cwd=cwd).splitlines()
    trap = "trap 'rm -f \"$m\"' EXIT HUP INT TERM"
    assert trap in lines
    # The trap must be armed before anything that can exit early, or it cannot cover it.
    assert lines.index(trap) < lines.index(f"cd {shlex.quote(cwd)} || exit 127")


# --- argument validation -------------------------------------------------------------------------


@pytest.mark.parametrize("timeout", [0, -1, -0.5])
def test_non_positive_timeout_matches_the_local_backends_behaviour(timeout):
    """
    `subprocess.run` raises TimeoutExpired immediately for these values and never lets the command
    complete. Without this check the watchdog's one-second floor would quietly grant a second of
    runtime instead, so a mutating command would actually execute where the local backend refused it.
    """
    channel = FakeChannel(stdout=[b"must not run"])
    with pytest.raises(subprocess.TimeoutExpired):
        exec_remote(fake_client(channel), ["docker", "compose", "down"], max_output_bytes=_CAP, timeout=timeout)
    assert channel.executed is None, "the command must not reach the remote host"


def test_empty_argv_is_rejected():
    """The wrapper interpolates the joined argv, so an empty one emits a bare `& pid=$!` and the remote
    shell dies with a syntax error — a caller bug must not surface as a broken wrapper script."""
    channel = FakeChannel(stdout=[b"x"])
    with pytest.raises(ValueError, match="at least the binary"):
        exec_remote(fake_client(channel), [], max_output_bytes=_CAP, timeout=5)
    assert channel.executed is None


def test_negative_output_cap_is_rejected():
    channel = FakeChannel(stdout=[b"x"])
    with pytest.raises(ValueError, match="must not be negative"):
        exec_remote(fake_client(channel), ["docker", "ps"], max_output_bytes=-1, timeout=5)
    assert channel.executed is None


def test_run_remote_exec_validates_before_opening_a_connection(monkeypatch):
    """Rejecting the caller's own arguments must not cost an SSH handshake first."""
    connected = []
    monkeypatch.setattr(
        "docker_mcp.tools._ssh_proxy.connect_ssh_client",
        lambda *a, **k: connected.append(True),  # pyright: ignore[reportUnknownLambdaType]
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_remote_exec("ssh://h", ["docker", "ps"], max_output_bytes=_CAP, timeout=0)
    assert not connected, "connected before validating its arguments"


# --- timeout attribution -------------------------------------------------------------------------


@contextlib.contextmanager
def _clock_advanced_past_the_watchdog():
    """
    Make an instant fake channel look like it ran long enough for the watchdog to have fired.

    Attribution deliberately requires corroborating elapsed time, so a fake that returns immediately
    is (correctly) not a timeout. Driving the clock is how a test exercises the raising path without
    a degenerate timeout value that would sidestep the very check under test.
    """
    calls = iter([0.0])  # first reading is the start; every later reading is far in the future

    def fake_monotonic():
        return next(calls, 10_000.0)

    with unittest.mock.patch("docker_mcp.tools._ssh_proxy.time.monotonic", fake_monotonic):
        yield


def test_timeout_attribution_uses_the_watchdogs_sleep_not_the_raw_timeout():
    """
    The two differ whenever the timeout is not a whole number of seconds, and the gap is not benign.

    At `timeout=30.2` the watchdog cannot fire before 31s, so comparing against 30.2 would misattribute
    a sentinel exit at 30.5s; at `timeout=0.1` the raw threshold goes negative and would misattribute
    *every* sentinel exit, including an instant one.
    """
    # Sub-second timeout: the watchdog still sleeps its 1s floor, so nothing quicker is a timeout.
    assert _is_remote_timeout(_REMOTE_TIMEOUT_EXIT_CODE, elapsed=0.02, timeout=0.1) is False
    assert _is_remote_timeout(_REMOTE_TIMEOUT_EXIT_CODE, elapsed=0.5, timeout=0.1) is False
    assert _is_remote_timeout(_REMOTE_TIMEOUT_EXIT_CODE, elapsed=1.0, timeout=0.1) is True
    # Fractional timeout: the deadline is the rounded-up 31s, not 30.2s.
    assert _is_remote_timeout(_REMOTE_TIMEOUT_EXIT_CODE, elapsed=30.5, timeout=30.2) is False
    assert _is_remote_timeout(_REMOTE_TIMEOUT_EXIT_CODE, elapsed=31.0, timeout=30.2) is True


def test_timeout_attribution_requires_the_full_budget_to_have_elapsed():
    """
    The sentinel exit code alone must not decide it.

    `docker run`/`compose run` propagate the *container's* status, so a container may legitimately
    exit with the sentinel code — no exit code is collision-proof. Corroborating elapsed time keeps
    such a command an ordinary failure instead of a spurious TimeoutExpired.
    """
    # Genuine timeout: ran its whole budget.
    assert _is_remote_timeout(_REMOTE_TIMEOUT_EXIT_CODE, elapsed=30.0, timeout=30.0) is True
    # Same status, but finished early — a real exit status, not a timeout.
    assert _is_remote_timeout(_REMOTE_TIMEOUT_EXIT_CODE, elapsed=0.4, timeout=30.0) is False
    # Any other status is never a timeout, however long it ran.
    assert _is_remote_timeout(0, elapsed=99.0, timeout=30.0) is False
    assert _is_remote_timeout(143, elapsed=99.0, timeout=30.0) is False


def test_timeout_attribution_tolerates_sub_second_measurement_noise():
    """A 1s timeout elapses at just under 1s often enough that an exact comparison would flap."""
    assert _is_remote_timeout(_REMOTE_TIMEOUT_EXIT_CODE, elapsed=0.99, timeout=1.0) is True


def test_exec_remote_reports_an_early_sentinel_exit_as_an_ordinary_failure():
    """End to end for the collision case: the status is returned, not raised as a timeout."""
    channel = FakeChannel(stdout=[b"container said no"], exit_status=_REMOTE_TIMEOUT_EXIT_CODE)
    result = exec_remote(fake_client(channel), ["docker", "run", "img"], max_output_bytes=_CAP, timeout=600)
    assert result.returncode == _REMOTE_TIMEOUT_EXIT_CODE
    assert result.stdout == b"container said no"


# --- PosixDialect: behaviour through a real shell -------------------------------------------------
#
# The script's correctness is shell semantics, which no mock can check. These run it for real.

pytestmark_posix = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX /bin/sh")


# Deliberately odd sleep durations, so a `pgrep` for one cannot plausibly match an unrelated process
# on a developer machine or a concurrently running test. Matches are additionally diffed against a
# pre-existing snapshot rather than trusted outright.
_WATCHDOG_MARKER_SECONDS = 4517
_GRANDCHILD_MARKER_SECONDS = 4519
# How long the watchdog may legitimately take to notice the command has gone. It ticks once a second,
# so it is normally alive when the call returns; this bounds "promptly exits" without asserting an
# impossible zero.
_WATCHDOG_EXIT_GRACE_SECONDS = 10.0


def _processes_naming(seconds: int) -> list[str]:
    """
    Pids whose command line names this timeout, via pgrep. Empty when pgrep finds nothing (exit 1).

    Matches two shapes on purpose, because the watchdog's footprint changed and both are worth
    catching. `-lt <seconds>` finds the watchdog subshell, whose argv is the whole wrapper script —
    that is what a lingering watchdog looks like today. `sleep <seconds>` finds the single long sleep
    the watchdog used to be, which is the regression this most needs to catch: it leaked one stray per
    call on Linux for the remainder of the timeout window.

    Both were confirmed against `/proc` on dash. Probing only `sleep <seconds>` — as this helper did
    when the watchdog was one long sleep — silently matches nothing under the current implementation,
    making its caller vacuous. The watchdog's own `sleep 1` is deliberately not matched: it is
    indistinguishable from any other second-long sleep on the machine.

    Skips the calling test when `pgrep` is absent — it is not in a minimal container image, and an
    unavailable probe should not look like a failing assertion.
    """
    probe = ["pgrep", "-f", f"sleep {seconds}|-lt {seconds}"]
    try:
        completed = subprocess.run(probe, capture_output=True, text=True, check=False)  # noqa: S603
    except (FileNotFoundError, PermissionError) as exc:
        pytest.skip(f"pgrep unavailable, cannot inspect stray processes: {exc}")
    return completed.stdout.split()


def _run_script(argv, *, timeout, cwd=None, capture_timeout=30):
    script = PosixDialect().wrap_with_timeout(argv, timeout=timeout, cwd=cwd)
    return subprocess.run(  # noqa: S603 — fixed argv, no shell; the script itself is under test
        ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=capture_timeout, check=False
    )


@pytestmark_posix
def test_real_shell_preserves_exit_status_and_streams():
    result = _run_script(["sh", "-c", "echo out; echo err >&2; exit 7"], timeout=30)
    assert result.returncode == 7
    assert result.stdout.strip() == "out"
    # Exactly the command's own stderr — no shell job-reap notice mixed in.
    assert result.stderr.strip() == "err"


@pytestmark_posix
def test_real_shell_reports_timeout_exit_code_and_kills_the_command():
    # `exec` so the killed process is the leaf. Without it the shell would fork `sleep` as a child
    # that survives the SIGTERM to its parent and keeps holding this capture's pipes — the documented
    # direct-child-only limitation (identical on the local subprocess path), not something under test
    # here; `test_real_shell_grandchildren_are_a_known_limitation` covers that explicitly.
    result = _run_script(["sh", "-c", "echo pre >&2; exec sleep 60"], timeout=1, capture_timeout=15)
    assert result.returncode == _REMOTE_TIMEOUT_EXIT_CODE
    assert "pre" in result.stderr  # the command's own output up to the kill is kept
    assert "Terminated" not in result.stderr


@pytestmark_posix
def test_real_shell_returns_promptly_when_the_command_beats_a_long_timeout():
    """The watchdog's stdio is detached, so a fast command with a long timeout must not keep the
    caller's captured streams open waiting for an orphaned `sleep` to finish."""
    result = _run_script(["true"], timeout=120, capture_timeout=10)
    assert result.returncode == 0


@pytestmark_posix
def test_real_shell_grandchildren_are_a_known_limitation():
    """
    Pins the accepted limitation rather than pretending it away: the watchdog SIGTERMs only the
    direct child, so a process the command itself forked survives and keeps the streams open.

    `subprocess.run(capture_output=True)` waits for EOF and so blocks here — which is exactly why
    `_drain_exec_channel` keys completion on the exit status instead, making the real remote path
    return promptly where this local-style capture cannot. The local `run_docker` path has the
    identical limitation, so it is parity, not a regression.
    """
    # A duration nothing else plausibly uses, so the cleanup below cannot match another process. It
    # is reaped by the pids this test's own probe found — never by a `pkill -f` pattern, which would
    # signal unrelated processes on a developer's machine.
    with pytest.raises(subprocess.TimeoutExpired):
        _run_script(["sh", "-c", f"sleep {_GRANDCHILD_MARKER_SECONDS} & wait"], timeout=1, capture_timeout=4)
    for pid in _processes_naming(_GRANDCHILD_MARKER_SECONDS):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(int(pid), signal.SIGKILL)


@pytestmark_posix
def test_real_shell_honours_cwd_and_refuses_an_unreachable_one():
    assert _run_script(["pwd"], timeout=30, cwd="/tmp").stdout.strip().endswith("/tmp")
    refused = _run_script(["echo", "must-not-run"], timeout=30, cwd="/no-such-dir-zzz")
    assert refused.returncode == 127
    assert "must-not-run" not in refused.stdout


@pytestmark_posix
def test_real_shell_kills_a_command_that_ignores_sigterm():
    """
    The case the old "same as subprocess.run" claim got wrong.

    A command trapping SIGTERM survives the watchdog's first signal. Locally that cannot happen —
    `subprocess.run` uses SIGKILL — so without escalation the remote path had the weaker guarantee:
    the caller would be released by the local deadline while the remote command kept running.
    """
    # `trap '' TERM` ignores SIGTERM outright; only SIGKILL can end this.
    result = _run_script(
        ["sh", "-c", "trap '' TERM; while :; do sleep 1; done"],
        timeout=1,
        capture_timeout=30,
    )
    # Killed by SIGKILL (128+9) rather than exiting on its own, and still attributed as a timeout via
    # the marker the watchdog wrote before signalling.
    assert result.returncode in (_REMOTE_TIMEOUT_EXIT_CODE, 137), result.returncode


@pytestmark_posix
def test_real_shell_mktemp_failure_exits_125_without_running_argv():
    """The command must not run at all when the wrapper cannot set up its marker, since without one a
    genuine timeout is indistinguishable from an ordinary SIGTERM exit."""
    canary = "/tmp/docker-mcp-mktemp-canary"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(canary)
    script = PosixDialect().wrap_with_timeout(["touch", canary], timeout=5)
    # Shadow mktemp so it fails, exactly as an absent binary or unwritable TMPDIR would. The function
    # definition is POSIX, and shadowing an external command this way is honoured by dash, busybox ash
    # and macOS sh alike (verified) — so this runs under /bin/sh like every other real-shell test here,
    # rather than depending on bash, which a minimal image such as Alpine does not ship at all.
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell; the script itself is under test
        ["/bin/sh", "-c", "mktemp() { return 1; }\n" + shlex.split(script)[2]],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 125
    assert "cannot create a temp file" in result.stderr
    assert not os.path.exists(canary), "argv ran despite the wrapper being unable to set itself up"


@pytestmark_posix
def test_real_shell_leaves_no_marker_file_behind():
    """Covers the happy path and the early-`cd`-failure path, which used to strand its marker."""
    pattern = os.path.join(os.environ.get("TMPDIR", "/tmp"), "docker-mcp-server.*")
    before = set(glob.glob(pattern))
    _run_script(["true"], timeout=5)
    _run_script(["echo", "x"], timeout=5, cwd="/no-such-dir-zzz")
    _run_script(["sh", "-c", "exec sleep 30"], timeout=1, capture_timeout=15)  # timeout path
    assert set(glob.glob(pattern)) - before == set()


@pytestmark_posix
def test_real_shell_watchdog_does_not_outlive_the_call_for_long():
    """
    A watchdog armed for 4517s must not still be around long after a command that took no time.

    Asserting zero *immediately* would be wrong, not merely flaky: the watchdog is legitimately still
    mid-`sleep 1` when the call returns (confirmed against `/proc` on dash), and only notices the
    command has gone on its next tick. The guarantee worth testing is that it exits promptly rather
    than living for the whole timeout window — which is exactly what leaked one stray per call before.

    Note this bites on Linux, not on macOS: the leak it guards is dash's, and bash cleans up the same
    construct. Reverting the fix locally leaves this test green, so a passing run here is not evidence
    the guard works — CI's Linux job is what exercises it. That asymmetry is precisely how the leak
    reached CI in the first place.
    """
    # Diff against pre-existing matches so an unrelated process cannot fail this; the duration is also
    # deliberately implausible, making a collision unlikely to begin with.
    before = set(_processes_naming(_WATCHDOG_MARKER_SECONDS))
    result = _run_script(["true"], timeout=_WATCHDOG_MARKER_SECONDS, capture_timeout=15)
    assert result.returncode == 0

    deadline = time.monotonic() + _WATCHDOG_EXIT_GRACE_SECONDS
    leaked = set(_processes_naming(_WATCHDOG_MARKER_SECONDS)) - before
    while leaked and time.monotonic() < deadline:
        time.sleep(0.25)
        leaked = set(_processes_naming(_WATCHDOG_MARKER_SECONDS)) - before
    for pid in leaked:  # don't leave our own strays behind if the assertion is about to fail
        with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
            os.kill(int(pid), signal.SIGKILL)
    assert not leaked, (
        f"watchdog still alive {_WATCHDOG_EXIT_GRACE_SECONDS}s after the command finished: {sorted(leaked)}"
    )


@pytestmark_posix
def test_real_shell_quoting_blocks_metacharacter_injection():
    result = _run_script(["sh", "-c", 'printf "%s" "$1"', "x", 'a"b;touch /tmp/pwned-$$'], timeout=30)
    assert result.stdout == 'a"b;touch /tmp/pwned-$$'


# --- URL scheme validation -----------------------------------------------------------------------


@pytest.mark.parametrize("url", ["tcp://10.0.0.5:2375", "unix:///var/run/docker.sock", "npipe:////./pipe/x", ""])
def test_non_ssh_urls_are_rejected_before_any_connection_attempt(url):
    """
    A wrong scheme was parsed as if it were ssh://: `tcp://10.0.0.5:2375` yielded a plausible target
    and would be attempted as SSH on port 2375, failing with advice about keys and known_hosts for
    what is really a caller bug. Checked in `parse_ssh_url`, so both the dial-stdio proxy and the
    remote-exec fallback are covered at the one place that already validates the URL.
    """
    with pytest.raises(ValueError, match="Expected an ssh:// URL"):
        parse_ssh_url(url)


def test_ssh_urls_still_parse():
    target = parse_ssh_url("ssh://bob@example.com:2222")
    assert (target.hostname, target.port, target.username) == ("example.com", 2222, "bob")


# --- get_dialect ---------------------------------------------------------------------------------


def test_get_dialect_returns_the_posix_implementation():
    assert isinstance(get_dialect(RemoteDialectKind.POSIX), PosixDialect)


def test_get_dialect_refuses_a_non_posix_host_without_claiming_it_is_windows():
    """
    Detection routes a failed probe, an unrecognised kernel and a restricted shell to WINDOWS too, so
    the refusal must not assert the host *is* Windows — it names the causes instead, and points at the
    log line that records what the host actually reported.
    """
    with pytest.raises(RuntimeError, match="no supported POSIX shell") as excinfo:
        get_dialect(RemoteDialectKind.WINDOWS)
    message = str(excinfo.value)
    assert "WSL" in message  # the supported alternative a Windows user needs to be told about
    assert "restricted shell" in message  # the non-Windows causes are acknowledged
    assert not message.startswith("Remote-exec fallback: this host needs")  # the old, wrong framing


# --- detect_remote_dialect -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("uname", "expected"),
    [
        # WSL reports plain "Linux" — sshd inside a WSL distro is a genuine Linux target and must be
        # accepted, not refused as "Windows".
        ("Linux\n", RemoteDialectKind.POSIX),
        ("Darwin\n", RemoteDialectKind.POSIX),
        ("FreeBSD\n", RemoteDialectKind.POSIX),
        ("SunOS\n", RemoteDialectKind.POSIX),
        # The false positive an exit-status-only probe would let through: a Windows host with Git Bash
        # or Cygwin on PATH answers `uname -s` successfully.
        ("MINGW64_NT-10.0-19045\n", RemoteDialectKind.WINDOWS),
        ("CYGWIN_NT-10.0\n", RemoteDialectKind.WINDOWS),
        ("MSYS_NT-10.0-19045\n", RemoteDialectKind.WINDOWS),
        ("Windows_NT\n", RemoteDialectKind.WINDOWS),
        ("SomethingExotic\n", RemoteDialectKind.WINDOWS),
        ("\n", RemoteDialectKind.WINDOWS),
    ],
)
def test_detect_remote_dialect_classifies_uname_output(uname, expected):
    channel = FakeChannel(stdout=[uname.encode()], exit_ready_immediately=True)
    client = fake_client(channel)
    assert detect_remote_dialect(client, "ssh://host") is expected


def test_detect_remote_dialect_treats_a_failed_probe_as_non_posix():
    """A non-zero `uname` (cmd/PowerShell: command not found) means no POSIX shell answered."""
    channel = FakeChannel(stdout=[b""], exit_status=127, exit_ready_immediately=True)
    assert detect_remote_dialect(fake_client(channel), "ssh://host") is RemoteDialectKind.WINDOWS


def test_detect_remote_dialect_treats_a_dead_transport_as_non_posix():
    assert detect_remote_dialect(fake_client(None, transport=False), "ssh://h") is RemoteDialectKind.WINDOWS


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [(None, 30.0), (5, 5.0), (900, 30.0)],  # None falls back to the cap; a large timeout is capped
)
def test_detect_remote_dialect_always_bounds_the_probe_channel(timeout, expected):
    """
    The channel must be bounded even when the caller passes no timeout.

    `channel.recv` runs before the exit-status deadline can help, so leaving the channel unbounded
    means a remote that wedges without writing anything hangs detection outright — the deadline below
    it never gets a chance to fire.
    """
    channel = FakeChannel(stdout=[b"Linux\n"], exit_ready_immediately=True)
    detect_remote_dialect(fake_client(channel), "ssh://h", timeout=timeout)
    assert channel.timeout == expected


def test_detect_remote_dialect_does_not_hang_on_a_wedged_remote():
    """
    `Channel.settimeout` bounds reads and writes only. `recv_exit_status()` waits on an Event with no
    timeout — paramiko's own docstring warns it "will hang indefinitely" — so a shell that never
    reports a status would hang detection, and with it `run_remote_exec`, whatever timeout the caller
    passed. Expiry is treated as "no POSIX shell answered", which is what it is.
    """

    class NeverFinishes(FakeChannel):
        def __init__(self):
            super().__init__(stdout=[b"Linux\n"])

        def exit_status_ready(self):
            return False  # the remote never reports a status

        def recv_exit_status(self):  # pragma: no cover — reaching this means the guard failed
            raise AssertionError("recv_exit_status() would have blocked indefinitely")

    started = time.monotonic()
    kind = detect_remote_dialect(fake_client(NeverFinishes()), "ssh://wedged", timeout=1)
    elapsed = time.monotonic() - started
    assert kind is RemoteDialectKind.WINDOWS
    assert elapsed < 10, f"detection took {elapsed:.1f}s — the deadline did not bound it"


def test_refusal_message_matches_the_allow_list():
    """The message used to name only Linux/macOS/BSD/WSL while the allow-list also accepts SunOS and
    AIX, so a supported-but-unusual host hitting the failure path was told its OS was unsupported."""
    with pytest.raises(RuntimeError) as excinfo:
        get_dialect(RemoteDialectKind.WINDOWS)
    message = str(excinfo.value).lower()
    for kernel in ("sunos", "aix", "linux", "darwin"):
        assert kernel in message, f"{kernel} is accepted by detection but absent from the refusal"


def test_detect_remote_dialect_warns_on_every_refusal_path(caplog):
    """
    `get_dialect`'s refusal cannot name a cause, so this log is the only record of why a host was
    rejected. Both paths must warn, or the refusal is undiagnosable without reproducing it — the
    failed-probe path is the least self-evident of the two and was previously only at debug.
    """
    with caplog.at_level("WARNING"):
        detect_remote_dialect(
            fake_client(FakeChannel(stdout=[b"MINGW64_NT-10.0\n"], exit_ready_immediately=True)), "ssh://a"
        )
        detect_remote_dialect(fake_client(None, transport=False), "ssh://b")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2, [r.getMessage() for r in warnings]
    # And nothing is logged for a host that *is* supported — this runs on every uncached call, so a
    # successful detection must stay silent.
    caplog.clear()
    with caplog.at_level("WARNING"):
        detect_remote_dialect(fake_client(FakeChannel(stdout=[b"Linux\n"], exit_ready_immediately=True)), "ssh://ok")
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    answered, failed = warnings
    assert "mingw64_nt-10.0" in answered.getMessage().lower()  # what the host reported
    assert "ssh://a" in answered.getMessage()
    assert "could not be" in failed.getMessage()  # the probe never completed
    assert "ssh://b" in failed.getMessage()


def test_detect_remote_dialect_caches_per_host():
    channel = FakeChannel(stdout=[b"Linux\n"], exit_ready_immediately=True)
    client = fake_client(channel)
    assert detect_remote_dialect(client, "ssh://host") is RemoteDialectKind.POSIX
    # The fake serves its stdout once; a second probe would see empty output and classify WINDOWS.
    # Getting POSIX back proves the cached value was used instead of re-probing.
    assert detect_remote_dialect(client, "ssh://host") is RemoteDialectKind.POSIX


def test_detect_remote_dialect_does_not_share_results_between_hosts():
    posix = fake_client(FakeChannel(stdout=[b"Linux\n"], exit_ready_immediately=True))
    windows = fake_client(FakeChannel(stdout=[b"MINGW64_NT-10.0\n"], exit_ready_immediately=True))
    assert detect_remote_dialect(posix, "ssh://a") is RemoteDialectKind.POSIX
    assert detect_remote_dialect(windows, "ssh://b") is RemoteDialectKind.WINDOWS


# --- exec_remote ---------------------------------------------------------------------------------


def test_exec_remote_returns_status_and_both_streams():
    channel = FakeChannel(stdout=[b"out"], stderr=[b"err"], exit_status=3)
    result = exec_remote(fake_client(channel), ["docker", "ps"], max_output_bytes=_CAP, timeout=5)
    assert (result.returncode, result.stdout, result.stderr, result.truncated) == (3, b"out", b"err", False)
    assert channel.closed


def test_exec_remote_drains_stderr_as_well_as_stdout():
    """Both streams are pumped: paramiko stops advertising window space for an unread stream, so a
    chatty stderr would otherwise block the remote command until our deadline."""
    channel = FakeChannel(stdout=[b"a", b"b"], stderr=[b"x", b"y", b"z"])
    result = exec_remote(fake_client(channel), ["docker", "ps"], max_output_bytes=_CAP, timeout=5)
    assert result.stdout == b"ab"
    assert result.stderr == b"xyz"


class LateChunkChannel(FakeChannel):
    """
    A channel whose final chunk only becomes readable after `quiet_polls` empty readiness checks.

    Models the real ordering hazard: paramiko surfaces data from its transport thread, so a chunk can
    land in the window between both streams last reading as quiet and the exit status being observed.
    The exit status reads ready throughout, as it does once the server has sent it.
    """

    def __init__(self, *, quiet_polls: int, chunk: bytes = b"LATE"):
        super().__init__(stdout=[chunk], exit_ready_immediately=True)
        self._quiet_polls = quiet_polls
        self._polls = 0

    def recv_ready(self):
        self._polls += 1
        return self._polls > self._quiet_polls and bool(self.stdout_chunks)


@pytest.mark.parametrize("quiet_polls", [1, 3, 5])
def test_exec_remote_captures_output_that_surfaces_after_the_exit_status(quiet_polls):
    """
    Breaking on the first quiet poll drops such a chunk *silently*, which is worse than truncating
    loudly because the caller cannot tell its output is incomplete — an agent would then act on a
    partial `docker ps` or build log. Reproduced before fixing: the chunk was never read.
    """
    channel = LateChunkChannel(quiet_polls=quiet_polls)
    result = exec_remote(fake_client(channel), ["docker", "ps"], max_output_bytes=_CAP, timeout=5)
    assert result.stdout == b"LATE"
    assert not channel.stdout_chunks


def test_exec_remote_caps_each_stream_and_flags_truncation():
    channel = FakeChannel(stdout=[b"0123456789"], stderr=[b"abcdefghij"])
    result = exec_remote(fake_client(channel), ["docker", "ps"], max_output_bytes=4, timeout=5)
    assert result.stdout == b"0123"
    assert result.stderr == b"abcd"
    assert result.truncated is True


def test_exec_remote_keeps_draining_after_the_cap_is_reached():
    """Past the cap we must keep reading and discard — an unread stream hangs the remote command
    rather than merely truncating its output."""
    channel = FakeChannel(stdout=[b"aaaa", b"bbbb", b"cccc"])
    result = exec_remote(fake_client(channel), ["docker", "ps"], max_output_bytes=2, timeout=5)
    assert result.stdout == b"aa"
    assert result.truncated is True
    assert not channel.stdout_chunks, "later chunks were left unread, which would block the remote"


def test_exec_remote_raises_timeout_expired_on_the_watchdog_exit_code():
    """The remote watchdog's 124 becomes the same exception the local subprocess path raises, so
    callers see one contract regardless of which backend ran."""
    channel = FakeChannel(stdout=[b"partial"], exit_status=_REMOTE_TIMEOUT_EXIT_CODE)
    # Advance the clock so the instant fake looks like it consumed its budget: attribution needs the
    # watchdog to have plausibly fired, which is what separates a timeout from a plain sentinel exit.
    with _clock_advanced_past_the_watchdog(), pytest.raises(subprocess.TimeoutExpired) as excinfo:
        exec_remote(fake_client(channel), ["docker", "build", "."], max_output_bytes=_CAP, timeout=7)
    assert excinfo.value.timeout == 7
    assert excinfo.value.cmd == ["docker", "build", "."]
    assert excinfo.value.output == b"partial"  # output captured before the kill is preserved


def test_exec_remote_refuses_a_non_posix_dialect_before_running_anything():
    channel = FakeChannel()
    with pytest.raises(RuntimeError, match="no supported POSIX shell"):
        exec_remote(
            fake_client(channel),
            ["docker", "ps"],
            max_output_bytes=_CAP,
            timeout=5,
            dialect=RemoteDialectKind.WINDOWS,
        )
    assert channel.executed is None


def test_exec_remote_raises_when_the_transport_is_gone():
    with pytest.raises(RuntimeError, match="transport is not connected"):
        exec_remote(fake_client(None, transport=False), ["docker", "ps"], max_output_bytes=_CAP, timeout=5)


def test_exec_remote_closes_the_channel_even_when_the_command_raises(monkeypatch):
    channel = FakeChannel(stdout=[b"x"], exit_status=_REMOTE_TIMEOUT_EXIT_CODE)
    with _clock_advanced_past_the_watchdog(), pytest.raises(subprocess.TimeoutExpired):
        exec_remote(fake_client(channel), ["docker", "ps"], max_output_bytes=_CAP, timeout=7)
    assert channel.closed


# --- run_remote_exec -----------------------------------------------------------------------------


def test_run_remote_exec_connects_detects_runs_and_closes(monkeypatch):
    channel = FakeChannel(stdout=[b"Linux\n"], exit_ready_immediately=True)
    fake = FakeSshClient(channel)
    client = cast(paramiko.SSHClient, fake)
    monkeypatch.setattr("docker_mcp.tools._ssh_proxy.connect_ssh_client", lambda *a, **k: client)

    # After the uname probe the same fake channel serves the command; reload its scripted output.
    def fake_exec_remote(ssh_client, argv, **kwargs):
        assert ssh_client is client
        return "sentinel"

    monkeypatch.setattr("docker_mcp.tools._ssh_proxy.exec_remote", fake_exec_remote)
    result = run_remote_exec("ssh://bob@example.com", ["docker", "ps"], max_output_bytes=_CAP, timeout=5)
    assert result == "sentinel"
    assert fake.closed


def test_run_remote_exec_closes_the_client_even_on_failure(monkeypatch):
    fake = FakeSshClient(FakeChannel(stdout=[b"Linux\n"], exit_ready_immediately=True))
    client = cast(paramiko.SSHClient, fake)
    monkeypatch.setattr("docker_mcp.tools._ssh_proxy.connect_ssh_client", lambda *a, **k: client)
    monkeypatch.setattr(
        "docker_mcp.tools._ssh_proxy.exec_remote",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        run_remote_exec("ssh://h", ["docker", "ps"], max_output_bytes=_CAP, timeout=5)
    assert fake.closed


def test_run_remote_exec_refuses_a_non_posix_host(monkeypatch):
    """End to end: a cmd/PowerShell host is refused with guidance, and the client still gets closed."""
    fake = FakeSshClient(FakeChannel(stdout=[b""], exit_status=127, exit_ready_immediately=True))
    client = cast(paramiko.SSHClient, fake)
    monkeypatch.setattr("docker_mcp.tools._ssh_proxy.connect_ssh_client", lambda *a, **k: client)
    with pytest.raises(RuntimeError, match="no supported POSIX shell"):
        run_remote_exec("ssh://win-host", ["docker", "ps"], max_output_bytes=_CAP, timeout=5)
    assert fake.closed

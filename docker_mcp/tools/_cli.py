# Cross-platform helper for shelling out to the `docker` CLI and its plugins.
#
# Everything that wraps a CLI rather than a docker-py method funnels through
# `run_docker()` so the platform-specific concerns (binary discovery, Windows
# console suppression, env scrubbing, byte-level output caps) live in one place.

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from docker_mcp.exceptions import CapabilityError, RemoteFailureError, ToolInputError
from docker_mcp._hosts import is_multi as _is_multi, is_ssh_url, resolve as _resolve_host
from docker_mcp.tools._ssh_proxy import (
    RemoteExecResult,
    RemoteStagingSession,
    remote_staging_session,
    run_remote_exec,
    ssh_proxy_for_docker_host,
)

DEFAULT_TIMEOUT_SECONDS = 60.0

# Per-call cap on captured stdout/stderr bytes. CLI output is intended for human
# consumption so a few MiB is plenty; the cap keeps a runaway subcommand from
# OOM'ing the MCP server. Mirrors the buffer-cap rationale in SECURITY.md.
MAX_CLI_OUTPUT_BYTES = 4_194_304  # 4 MiB

# Env vars we always forward to child docker invocations. Anything not in this
# allow-list is dropped so the subprocess gets a minimal, predictable environment.
# SSH_* keys are kept for the best-effort fallback case where the CLI dials an ssh:// daemon
# through a *context* rather than DOCKER_HOST directly (run_docker only rewrites DOCKER_HOST
# itself to the local proxy below) - that path still shells out to the system ssh client.
_BASE_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_API_VERSION",
    "DOCKER_BUILDKIT",
    "DOCKER_CLI_HINTS",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SSH_ASKPASS",
    "XDG_RUNTIME_DIR",
)

# Windows-only env vars Docker Desktop and credential helpers need to locate
# the user's config, temp dirs, and system DLLs.
_WINDOWS_EXTRA_ENV_KEYS = (
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
)


@dataclass(frozen=True)
class CliResult:
    """Captured outcome of a single `docker` subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    truncated: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _resolve(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise CapabilityError(
            f"Required executable {binary!r} was not found on PATH. "
            f"Install it (e.g. Docker Desktop on macOS/Windows, the docker package on Linux) "
            f"or extend PATH for the user running the MCP server."
        )
    return path


def _safe_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _BASE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if sys.platform == "win32":  # pyright: ignore[reportUnreachable]
        for key in _WINDOWS_EXTRA_ENV_KEYS:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
    return env


def _decode(blob: bytes | None) -> tuple[str, bool]:
    if not blob:
        return "", False
    truncated = False
    if len(blob) > MAX_CLI_OUTPUT_BYTES:
        blob = blob[:MAX_CLI_OUTPUT_BYTES]
        truncated = True
    return blob.decode("utf-8", errors="replace"), truncated


def _apply_host_env(env: dict[str, str], host: str | None) -> None:
    """Point the child `docker` CLI at the selected host by overriding DOCKER_HOST + per-host TLS in `env`.

    Inert for the legacy single host (DOCKER_MCP_SERVER_HOSTS unset), which keeps inheriting the ambient
    DOCKER_HOST / DOCKER_CONTEXT exactly as before. For an explicitly-configured host we pin DOCKER_HOST
    to its resolved URL (so the CLI and the docker-py SDK provably target the same daemon for a label),
    drop DOCKER_CONTEXT, and apply the per-host cert dir - else fall through to the global
    DOCKER_CERT_PATH/DOCKER_TLS_VERIFY, else plaintext. The ssh:// proxy rewrite below keys off the
    resulting DOCKER_HOST, so an ssh:// host is handled there.
    """
    resolved = _resolve_host(host)
    if not _is_multi() and not (os.environ.get("DOCKER_MCP_SERVER_HOSTS") or "").strip():
        return  # legacy single host: inherit the ambient docker env (unchanged behavior)
    # Explicit host: pin to this host's endpoint and never inherit the ambient DOCKER_HOST / DOCKER_CONTEXT
    # (DOCKER_HOST is ignored when DOCKER_MCP_SERVER_HOSTS is set). A host that resolved to the platform
    # default (url=None) drops them so the CLI finds its own default socket/npipe.
    env.pop("DOCKER_CONTEXT", None)
    if resolved.url is None:
        env.pop("DOCKER_HOST", None)
    else:
        env["DOCKER_HOST"] = resolved.url
    if resolved.cert_dir:
        env["DOCKER_CERT_PATH"] = resolved.cert_dir
        env["DOCKER_TLS_VERIFY"] = "1"
    elif not (os.environ.get("DOCKER_TLS_VERIFY") or "").strip():
        env.pop("DOCKER_CERT_PATH", None)
        env.pop("DOCKER_TLS_VERIFY", None)


def run_docker(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    stdin: bytes | None = None,
    extra_env: dict[str, str] | None = None,
    host: str | None = None,
) -> CliResult:
    """Run `docker <args...>` with safe, cross-platform defaults.

    - Binary resolved via `shutil.which` (handles `docker` vs `docker.exe`).
    - `shell=False` always; argv is a list so PowerShell/cmd/zsh quoting cannot bite us.
    - Output captured as bytes and decoded UTF-8 with `errors="replace"`
      (Windows default cp1252 would mangle non-ASCII otherwise).
    - Output truncated at MAX_CLI_OUTPUT_BYTES; `truncated=True` is surfaced in the result.
    - On Windows, `CREATE_NO_WINDOW` suppresses console pop-ups when the MCP server is run from a GUI host.
    - Environment is restricted to the allow-list in `_BASE_ENV_KEYS` (+ Windows extras),
      with optional `extra_env` overlay for subcommand-specific knobs.
    - `host` selects which configured host to target: for an explicitly-configured host its resolved
      DOCKER_HOST + per-host TLS are injected (`_apply_host_env`); the legacy single host inherits the
      ambient docker env unchanged.
    - When DOCKER_HOST is `ssh://...`, the child's DOCKER_HOST is transparently rewritten to a
      per-call local TCP proxy (`_ssh_proxy.py`) that authenticates via paramiko, so the CLI uses
      the same SSH credentials as the docker-py-backed tools instead of the system `ssh` binary.
      Any forwarded DOCKER_TLS_VERIFY/DOCKER_CERT_PATH are dropped in that case, since a native
      ssh:// DOCKER_HOST ignores TLS and the rewritten tcp:// one must too. The paramiko connect
      itself (which runs before the subprocess, to stand up that proxy) is bounded by this same
      `timeout`, so a slow/unreachable ssh:// host can't hang past the caller's own deadline.
    """
    binary = _resolve("docker")
    cmd = [binary, *args]
    env = _safe_env()
    _apply_host_env(env, host)
    if extra_env:
        env.update(extra_env)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    with contextlib.ExitStack() as stack:
        if is_ssh_url(env.get("DOCKER_HOST")):
            # Bound the paramiko connect/banner/auth phases to this call's own timeout - they run
            # before subprocess.run(timeout=timeout) below, so without this an unreachable or
            # filtered ssh:// host could hang here indefinitely regardless of the caller's timeout.
            proxy = stack.enter_context(ssh_proxy_for_docker_host(env["DOCKER_HOST"], timeout=timeout))
            env["DOCKER_HOST"] = f"tcp://127.0.0.1:{proxy.port}"
            # A native ssh:// DOCKER_HOST ignores TLS entirely; the rewritten tcp:// one would
            # otherwise pick up any forwarded DOCKER_TLS_VERIFY/DOCKER_CERT_PATH and attempt a TLS
            # handshake against this plaintext loopback proxy, breaking every CLI call.
            env.pop("DOCKER_TLS_VERIFY", None)
            env.pop("DOCKER_CERT_PATH", None)
        proc = subprocess.run(  # noqa: S603 - shell=False, argv is a list, binary is resolved via shutil.which
            cmd,
            shell=False,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            input=stdin,
            env=env,
            creationflags=creationflags,
            check=False,
        )
    stdout, truncated_out = _decode(proc.stdout)
    stderr, truncated_err = _decode(proc.stderr)
    return CliResult(
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated_out or truncated_err,
    )


# Errors that mean "we couldn't even probe the plugin" - never let them propagate
# out of `has_plugin`. Assigning the tuple to a module-level constant also dodges
# the PEP 758 parenthesis-free `except` form that older parsers (and PR review bots)
# flag as a syntax error.
# CapabilityError is here because `_resolve()` above raises it when the binary is missing: this
# probe asks *whether* a plugin is there and must answer False, not propagate.
_PLUGIN_PROBE_ERRORS: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    subprocess.TimeoutExpired,
    CapabilityError,
)

# Plugin availability is cached with a short TTL rather than forever (the old `functools.cache`):
# a plugin installed (or removed) while the server is running becomes visible within the TTL
# instead of requiring a restart. The probe shells out, so the TTL also avoids re-probing on
# every call. `_plugin_cache` maps plugin name -> (monotonic timestamp, available).
_PLUGIN_CACHE_TTL_SECONDS = 60.0
_plugin_cache: dict[str, tuple[float, bool]] = {}
_plugin_cache_lock = threading.Lock()


def _clear_plugin_cache() -> None:
    """Drop all cached plugin-availability results (used by tests; also handy after install/remove)."""
    with _plugin_cache_lock:
        _plugin_cache.clear()


def has_plugin(name: str) -> bool:
    """Return True if `docker <name> version` exits 0. Cached per process with a short TTL."""
    now = time.monotonic()
    with _plugin_cache_lock:
        entry = _plugin_cache.get(name)
        if entry is not None and now - entry[0] < _PLUGIN_CACHE_TTL_SECONDS:
            return entry[1]
    try:
        result = run_docker([name, "version"], timeout=10)
        available = result.returncode == 0
    except _PLUGIN_PROBE_ERRORS:
        available = False
    with _plugin_cache_lock:
        _plugin_cache[name] = (time.monotonic(), available)
    return available


def require_plugin(name: str) -> None:
    """Raise CapabilityError with an actionable message if the named CLI plugin is unavailable.

    Every caller reaches this only after `should_remote_exec` has already returned False for the
    target host, which means that host is not reached over ssh:// (an ssh:// host with no local
    plugin runs the call there instead of raising here). So alongside installing the plugin, pointing
    this host at an ssh:// endpoint that already has it is always a live alternative - named in the
    message for the three plugins that share this helper (compose, buildx, scout), all of which
    support that fallback.
    """
    if not has_plugin(name):
        raise CapabilityError(
            f"Docker CLI plugin {name!r} is not installed or not available on PATH. Install it "
            f"(Docker Desktop ships it by default; on a plain Docker Engine install, use your "
            f"distribution's docker-{name}-plugin package, or follow the upstream docs) - or point "
            f"this host at an ssh:// endpoint that already has it, via DOCKER_MCP_SERVER_HOSTS: the "
            f"call then runs there automatically instead."
        )


# --- remote-exec fallback -------------------------------------------------------------------------
#
# CLI-backed tools need a local `docker` binary; the docker-py-backed ones need nothing but the
# daemon. So on a machine with SSH access to a real Docker host but no local Docker install, every
# CLI-backed tool fails at `_resolve("docker")` above before any host logic runs. When the target
# host is reached over ssh://, the command can instead run *on that host* - which, being a Docker
# host, plausibly has the CLI and its plugins already.
#
# This is a pure fallback. With a usable local CLI nothing below is reached and behavior is
# unchanged, including the dial-stdio proxy in `run_docker`: only the "we have no local option at
# all" case changes, from an error into a remote call.


def should_remote_exec(host: str | None, *, plugin: str | None = None) -> bool:
    """Whether a CLI call against `host` has to run on the remote host instead of locally.

    True only when the target is an ssh:// host *and* nothing local can serve the call, so a machine
    with a working CLI keeps using it - the credentials, filesystem, and buildx state a call sees
    change only when there is no alternative. For a non-ssh host this returns False and the caller's
    existing `_resolve`/`require_plugin` errors stand, which is the honest outcome: we have no way to
    reach a unix://, tcp:// or npipe:// daemon's host to run anything on it.

    A CLI-backed tool module calls this in exactly one place - its shared `_run_*` wrapper - rather
    than probing per tool, so the decision, and the conditions under which behavior changes at all,
    live here.

    Args:
        host: configured host label, or None for the default host
        plugin: the CLI plugin the call needs ("compose"/"buildx"/"scout"), or None for a core-CLI
                 subcommand such as `docker stack ...` (probes only the `docker` binary itself)

    Returns:
        bool: True if the caller should route through `remote_exec_cli` instead of `run_docker`
    """
    if not _resolve_host(host).is_ssh:
        return False
    if shutil.which("docker") is None:
        return True  # no local CLI at all, so no local call is possible whatever the subcommand is
    return plugin is not None and not has_plugin(plugin)


def remote_exec_cli(
    host: str | None,
    args: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    stdin: bytes | None = None,
    extra_env: dict[str, str] | None = None,
) -> CliResult:
    """Run `docker <args...>` on the target ssh:// host, in `run_docker`'s own result shape.

    A drop-in for `run_docker` on the calls `should_remote_exec` selects - same `CliResult`, same
    `truncated` flag, same `subprocess.TimeoutExpired` on a timeout - so each tool keeps its existing
    error convention (raw dict vs `raise_on_cli_failure`) with no remote-specific branch. Call it only
    behind `should_remote_exec`; it raises rather than falling back for a non-ssh host.

    Nothing is forwarded from the local environment and no DOCKER_HOST rewriting happens (the command
    runs on the daemon's own host, against its own socket), so registry credentials, `~/.docker`
    config and the filesystem are the *remote* user's. `stdin`/`extra_env` are rejected rather than
    dropped, so a tool that starts needing either fails loudly here instead of silently diverging
    from its local path.

    Args:
        host: configured host label, or None for the default host; must resolve to an ssh:// URL
        args: the docker argv *without* the binary, exactly as passed to `run_docker`
        timeout: seconds the remote watchdog allows the command; also bounds the SSH handshake. Total
                  wall clock can exceed it by the connect time plus a short kill grace.
        stdin: must be None/empty: the remote channel carries no input
        extra_env: must be None/empty: the child's environment is the remote login shell's

    Returns:
        CliResult: exit status, decoded stdout/stderr, and whether the byte cap truncated them

    Raises:
        ValueError: `stdin` or `extra_env` was supplied (an internal guard: no caller needs either, so its text stays in
            the log rather than reaching the model)
        CapabilityError: `host` is not an ssh:// host, or the remote is not POSIX
        RemoteFailureError: the SSH connection could not be opened
        subprocess.TimeoutExpired: the command exceeded `timeout`
    """
    _reject_unforwardable(stdin, extra_env)
    url = _ssh_url_for(host, args)
    return _as_cli_result(
        run_remote_exec(url, ["docker", *args], max_output_bytes=MAX_CLI_OUTPUT_BYTES, timeout=timeout)
    )


def remote_stage_and_exec(
    host: str | None,
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    path_values: Sequence[str] = (),
    stage_cwd: bool = True,
    stdin: bytes | None = None,
    extra_env: dict[str, str] | None = None,
) -> CliResult:
    """Copy a working directory to the target ssh:// host, run `docker <args...>` in it, and clean up.

    The counterpart to `remote_exec_cli` for a command that reads local files - Compose files, a bake
    file, a stack's `-c` files. Same `CliResult` contract, so a tool's error convention needs no remote
    branch, and the staged copy lives exactly as long as the command.

    **`cwd=None` means the server's own working directory, not "stage nothing".** That matches the
    local path, where `cwd=None` reaches `subprocess.run` as the server's cwd. Resolving it to nothing
    would leave the command running in the SSH login home directory, so `compose_up(files=[...])` would
    quietly act on whatever project happened to live there - worse than any error.

    Tokens in `args` that name local paths (declared by `path_values`) are reconciled with the staged
    copy: a relative one already resolves against it and is left alone; an absolute one pointing inside
    it is rewritten relative, so it resolves remotely instead of naming a local path that does not
    exist there; one pointing outside it is staged separately and rewritten to the staged path. A path
    outside the tree that itself references relative paths (an override Compose file with its own
    `build:` context, say) will not find them - the remote CLI reports that, since nothing can follow
    those references without parsing the file.

    Args:
        host: configured host label, or None for the default host; must resolve to an ssh:// URL
        args: the docker argv *without* the binary, exactly as passed to `run_docker`
        cwd: local directory to stage and run in; None means the server's own working directory
        timeout: seconds allowed for the command itself, and the bound on the SSH handshake. Staging
                  has its own bounds, so total wall clock exceeds this by the upload time.
        path_values: values in `args` that name local paths, so they can be reconciled as above.
                      Matched against `args` by whole token.
        stage_cwd: True (the default) for a command that reads files from a working directory. False
                    for one whose only local inputs are the paths it names (`buildx create --config`,
                    `buildx imagetools create --file`): nothing is staged as a working directory, the
                    remote command gets no cwd, and every `path_values` entry that exists locally is
                    staged individually. `cwd` is then used only to resolve relative ones, matching
                    where the local subprocess would have resolved them.
        stdin: must be None/empty: the remote channel carries no input
        extra_env: must be None/empty: the child's environment is the remote login shell's

    Returns:
        CliResult: exit status, decoded stdout/stderr, and whether the byte cap truncated them

    Raises:
        ToolInputError: `cwd` is not a directory (when `stage_cwd`), or the payload exceeds the staging limits
        ValueError: `stdin`/`extra_env` was supplied (an internal guard; see `remote_exec_cli`)
        CapabilityError: `host` is not an ssh:// host, this server's own working directory is gone, the remote is not
            POSIX, or its SFTP subsystem sees a different filesystem
        RemoteFailureError: the SSH connection could not be opened, or staging failed remotely
        subprocess.TimeoutExpired: the command exceeded `timeout`
    """
    _reject_unforwardable(stdin, extra_env)
    url = _ssh_url_for(host, args)
    if cwd is not None:
        # Verbatim, deliberately: `subprocess.run(cwd=...)` does not expand `~` (verified - it raises
        # FileNotFoundError for '~/proj'), and the compose/stack docstrings promise paths are used as
        # given with no shell expansion. Expanding here would make the same call succeed remotely and
        # fail locally, which is the one divergence this whole backend exists to avoid.
        local_cwd = Path(cwd)
    elif not stage_cwd and all(not value or Path(value).is_absolute() for value in path_values):
        # Nothing is being copied from a working directory and every path named is already absolute, so
        # this server's own directory plays no part - don't consult it, since a tool in this mode
        # (`buildx_create --config /etc/...`) may not even expose a `cwd` for the caller to supply. The
        # value is never read: `_reconcile_path_tokens` only resolves *relative* values against it.
        local_cwd = Path("/")
    else:
        try:
            local_cwd = Path.cwd()
        except OSError as exc:
            # `Path.cwd()` raises a bare "No such file or directory" if the server's own working
            # directory has been deleted underneath it, which says nothing about what to do. The local
            # backend tolerates that (a process keeps its deleted cwd), so name the difference - and the
            # remedy differs by mode, since a tool in the no-staging mode may have no `cwd` parameter.
            remedy = (
                "that is what would be copied over. Pass one explicitly."
                if stage_cwd
                else "that is what this call's relative paths resolve against. Pass absolute paths instead."
            )
            raise CapabilityError(
                f"Cannot run `docker {args[0] if args else ''}` on the remote host: this server's own working "
                f"directory is unavailable ({exc}), and with no explicit working directory {remedy}"
            ) from exc
    if stage_cwd and not local_cwd.is_dir():
        # Two different mistakes reach here - a missing path and a file passed where a directory belongs -
        # and saying which one it is saves the caller a round trip. Only checked when the directory is
        # actually being copied: with `stage_cwd=False` it is just the base for relative path values, and
        # a missing one simply leaves them unresolved for the remote CLI to report.
        detail = "it exists but is not a directory" if local_cwd.exists() else "nothing exists at that path"
        raise ToolInputError(
            f"Cannot run `docker {args[0] if args else ''}` on the remote host: {str(local_cwd)!r} is not a usable "
            f"working directory on this host ({detail}), and it is what would be copied over."
        )
    if not stage_cwd and not any(_local_target(value, base=local_cwd) for value in path_values):
        # Nothing will be copied, so do not open SFTP for this call - and therefore do not apply the
        # staging-only filesystem guard. A host whose exec channel works while its SFTP subsystem does
        # not (a Windows sshd shelling into `wsl.exe`) has to keep serving calls that stage nothing;
        # scoping that guard to staging is pointless if merely *maybe* staging trips it. Reached when a
        # declared path names something only the remote has, e.g. `buildx_create --config /etc/...`.
        return remote_exec_cli(host, args, timeout=timeout)
    with remote_staging_session(url, timeout=timeout) as session:
        staged_tree = session.stage_tree(local_cwd) if stage_cwd else None
        staged_args = _reconcile_path_tokens(session, args, path_values, base=local_cwd, staged_tree=staged_tree)
        result = session.exec(
            ["docker", *staged_args],
            cwd=staged_tree,
            timeout=timeout,
            max_output_bytes=MAX_CLI_OUTPUT_BYTES,
        )
    return _as_cli_result(result)


@contextlib.contextmanager
def remote_cli_session(host: str | None, *, timeout: float) -> Iterator[RemoteStagingSession]:
    """Open a staging session for a tool whose inputs need bespoke handling, and run it yourself.

    `remote_stage_and_exec` covers the common shape: a working directory plus whole-token path
    arguments. `buildx_build` fits neither half of that - its context needs `.dockerignore`-aware
    tarring, and its `--build-context` / `--secret` values are composite `key=value` tokens whose path
    lives inside them - so it drives a session directly and calls `run_in_session` when the rewriting
    is done. Reach for this only when the generic backend genuinely cannot express the staging.

    Args:
        host: configured host label, or None for the default host; must resolve to an ssh:// URL
        timeout: bound on the SSH handshake and dialect probe

    Returns:
        Iterator[RemoteStagingSession]: the session, valid inside the `with` block only

    Raises:
        CapabilityError: not an ssh:// host, a non-POSIX remote, or an unusable SFTP subsystem
        RemoteFailureError: the connection could not be opened, or staging setup failed remotely
    """
    with remote_staging_session(_ssh_url_for(host, []), timeout=timeout) as session:
        yield session


def run_in_session(
    session: RemoteStagingSession, args: list[str], *, timeout: float, cwd: str | None = None
) -> CliResult:
    """Run `docker <args...>` in an open staging session, in `run_docker`'s result shape.

    Args:
        session: a session from `remote_cli_session`
        args: the docker argv *without* the binary
        timeout: seconds the remote watchdog allows the command
        cwd: remote directory to run in, typically one a `stage_*` call returned

    Returns:
        CliResult: exit status, decoded stdout/stderr, and whether the byte cap truncated them

    Raises:
        subprocess.TimeoutExpired: the command exceeded `timeout`
    """
    return _as_cli_result(
        session.exec(["docker", *args], cwd=cwd, timeout=timeout, max_output_bytes=MAX_CLI_OUTPUT_BYTES)
    )


def _reject_unforwardable(stdin: bytes | None, extra_env: dict[str, str] | None) -> None:
    """Refuse the two `run_docker` inputs the remote backends cannot honour.

    Rejected rather than dropped: no in-scope tool passes either today, and one that starts to should
    fail loudly here instead of silently diverging from its local path.

    Args:
        stdin: must be None/empty
        extra_env: must be None/empty

    Raises:
        ValueError: either was supplied
    """
    if stdin:
        raise ValueError("remote-exec cannot send stdin to a remote docker command (no consumer needs it today).")
    if extra_env:
        raise ValueError(
            f"remote-exec cannot forward extra_env {sorted(extra_env)} to a remote docker command: the "
            f"environment is the remote login shell's, not this server's."
        )


def _ssh_url_for(host: str | None, args: list[str]) -> str:
    """The ssh:// URL for a host that a remote backend was asked to use.

    Args:
        host: configured host label, or None for the default host
        args: the docker argv, for the error message only

    Returns:
        str: the host's resolved ssh:// URL

    Raises:
        CapabilityError: the host is not reached over ssh://
    """
    resolved = _resolve_host(host)
    url = resolved.url
    if url is None or not resolved.is_ssh:
        # A programming error, not a user-facing condition: every call site gates on
        # should_remote_exec, which is False for a host we cannot reach over SSH.
        raise CapabilityError(
            f"remote-exec was requested for host {resolved.label!r} ({url or 'platform default'}), which is "
            f"not reached over ssh:// - there is no remote shell to run `docker {args[0] if args else ''}` on."
        )
    return url


def _as_cli_result(result: RemoteExecResult) -> CliResult:
    """Convert a remote result into `run_docker`'s shape, decoding at the same boundary the local path does.

    The retention cap already applied remotely, so `_decode` re-checks a bound the bytes cannot exceed;
    `truncated` is carried through from the drain, which is the only place that saw what was dropped.

    Args:
        result: the raw remote outcome

    Returns:
        CliResult: the decoded equivalent
    """
    stdout, truncated_out = _decode(result.stdout)
    stderr, truncated_err = _decode(result.stderr)
    return CliResult(
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=result.truncated or truncated_out or truncated_err,
    )


def flag_values(args: Sequence[str], flag: str) -> list[str]:
    """The values following each occurrence of `flag` in an already-built argv.

    Used to recover the local paths a compose/stack/bake argv names (`-f`, `-c`) for
    `remote_stage_and_exec`'s `path_values`. Reading them back out of the argv, rather than threading
    the original list from every tool, keeps one producer and one consumer in the same place: twenty
    call sites each passing the same list is twenty chances for one to be forgotten, and the omission
    would only show up for an absolute path against a remote host.

    Args:
        args: the argv to scan
        flag: the exact flag whose values to collect, e.g. "-f"

    Returns:
        list[str]: one value per occurrence, in order
    """
    return [value for name, value in zip(args, args[1:], strict=False) if name == flag]


def _local_target(value: str, *, base: Path) -> Path | None:
    """The local path a declared value names, if it exists on this machine.

    Shared by the decision to stage at all and by the rewriting that follows, so the two cannot
    disagree about what counts as a local input. No `~` expansion, matching the docker CLI, which
    receives argv tokens verbatim.

    Args:
        value: a value from `path_values`
        base: the directory a relative value resolves against

    Returns:
        Path | None: the absolute local path, or None when the value names nothing here
    """
    if not value:
        return None
    candidate = Path(value)
    absolute = candidate if candidate.is_absolute() else base / candidate
    try:
        return absolute if absolute.exists() else None
    except OSError:  # an unresolvable path is not something we can stage; let the remote say so
        return None


def _reconcile_path_tokens(
    session: RemoteStagingSession,
    args: list[str],
    path_values: Sequence[str],
    *,
    base: Path,
    staged_tree: str | None,
) -> list[str]:
    """Rewrite path-naming tokens in `args` so they resolve on the remote host.

    With a staged tree, three cases in order: a relative path inside it needs nothing (the remote cwd
    *is* that tree); an absolute path inside it is rewritten relative, because the local absolute path
    means nothing remotely even though the file was copied; anything else is staged on its own and
    rewritten to where it landed. With `staged_tree=None` there is no tree to be inside, so every value
    that exists locally is staged individually.

    A value naming nothing that exists locally is left alone in every one of those cases, including the
    in-tree one. There is nothing to reconcile it with, and rewriting it would make the remote CLI
    complain about `missing.yml` where the local backend would have named the absolute path the caller
    actually passed - a worse error for no gain.

    Replacement is by whole token, so a value coinciding with an unrelated argument (a service named
    exactly like an out-of-tree file path) would be rewritten too - accepted, being both unlikely and
    visible in the resulting command.

    Args:
        session: the staging session to copy extra paths through
        args: the docker argv to rewrite
        path_values: the values in `args` that name local paths
        base: the local directory relative values resolve against
        staged_tree: the remote path `base` was staged to, or None when it was not staged

    Returns:
        list[str]: `args` with path tokens reconciled
    """
    replacements: dict[str, str] = {}
    resolved_base = base.resolve()
    for value in path_values:
        if value in replacements:
            continue
        absolute = _local_target(value, base=base)
        if absolute is None:
            continue  # nothing to reconcile; both backends then report the path the caller passed
        try:
            inside = absolute.resolve().is_relative_to(resolved_base)
        except OSError:
            continue
        if staged_tree is None:
            # No working directory was staged, so "inside the tree" does not exist as a case: whatever
            # the value names has to be copied on its own to be readable remotely.
            replacements[value] = session.stage_tree(absolute) if absolute.is_dir() else session.stage_file(absolute)
        elif inside:
            relative = absolute.resolve().relative_to(resolved_base).as_posix()
            if relative != value:
                replacements[value] = relative
        elif absolute.is_dir():
            replacements[value] = session.stage_tree(absolute)
        elif absolute.is_file():
            replacements[value] = session.stage_file(absolute)
    return [replacements.get(token, token) for token in args]


def safe_positional(value: str, what: str = "value") -> str:
    """Validate a string that will be appended as a *positional* docker CLI argument.

    `shell=False` (enforced by `run_docker`) blocks shell-metacharacter injection, but it does NOT
    block *flag* injection: the docker CLI parses any argument starting with '-' as an option, even
    when we intend it as a positional value. For example a service list of ["--follow"] handed to
    `docker compose logs` would silently become a flag rather than a (nonexistent) service name,
    and an image of "--output=/etc/x" handed to a scout/buildx subcommand could smuggle a flag that
    writes to the server host's filesystem.

    A legitimate image reference, service, context, or builder name never starts with '-', so we
    reject those outright with an actionable error. Returns `value` unchanged when it is safe, so
    call sites can wrap inline: `args.append(safe_positional(image, "image"))`.
    """
    if value.startswith("-"):
        raise ToolInputError(
            f"Refusing to pass {what}={value!r} as a positional docker argument: it starts with '-', "
            f"which the docker CLI parses as a flag rather than a value. This is blocked to prevent "
            f"flag injection; a real {what} cannot start with '-'."
        )
    return value


def safe_spec_value(value: str, what: str = "value") -> str:
    """Validate a string interpolated into a comma-separated `key=value` docker CLI spec.

    The sibling of `safe_positional` for a different injection shape. Flags like
    `docker context create --docker` take one argument holding several keys separated by commas, so
    a comma inside an interpolated value does not corrupt the argument, it adds a *key* the caller
    never asked for. The concrete case: a `docker_host` of
    `"tcp://host:2376,skip-tls-verify=true"` turns TLS verification off while the tool's own
    `skip_tls_verify` parameter still reads False, defeating the point of having that parameter be
    explicit and visible.

    Only ',' is rejected. An '=' inside a value is harmless, because the spec is split on commas
    first and then on the first '=' of each part, so `host=tcp://a=b` parses as one key with the
    value `tcp://a=b`. Rejecting '=' as well would refuse legitimate filesystem paths containing
    one, so it is deliberately allowed - do not "harden" this to include it.
    """
    if "," in value:
        raise ToolInputError(
            f"Refusing to pass {what}={value!r} into a docker spec argument: it contains ',', which "
            f"separates keys in the spec, so the value would inject additional settings (such as "
            f"skip-tls-verify) that were not requested. Remove the comma."
        )
    return value


def filter_args(filters: dict | None) -> list[str]:
    """Translate an SDK-shaped filters dict into repeated `--filter key=value` CLI arguments.

    Lets CLI-backed tools accept the same `filters` shape as the docker-py-backed tools (one
    `filters` contract across the surface). A list value emits one `--filter` per element -
    docker-py's own convention for repeated filters (`{"label": ["a=1", "b=2"]}`) - and a bool
    lowercases to the CLI's `true`/`false`.
    """
    args: list[str] = []
    for key, value in (filters or {}).items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            rendered = str(item).lower() if isinstance(item, bool) else str(item)
            args.extend(["--filter", f"{key}={rendered}"])
    return args


def raise_on_cli_failure(result: CliResult, command: str) -> None:
    """Raise RemoteFailureError if a docker subprocess exited non-zero.

    Args:
        result: the CliResult from run_docker.
        command: the docker subcommand for the message, e.g. "buildx ls" or "context inspect".
    """
    if result.returncode != 0:
        raise RemoteFailureError(
            f"`docker {command}` failed with exit code {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )


def parse_ndjson(text: str, *, truncated: bool = False, what: str = "docker output") -> list[dict]:
    """Parse one JSON object per non-blank line (NDJSON), as emitted by `docker ... --format '{{json .}}'`.

    Args:
        text: the NDJSON body to parse.
        truncated: True if the underlying stdout was capped by run_docker's byte limit. When set,
                   the final non-blank line is assumed to be a partial record and is dropped before
                   parsing rather than crashing on a half-record.
        what: short label used in error messages, e.g. "buildx ls output".
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if truncated and lines:
        lines = lines[:-1]
    items: list[dict] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Could not parse {what} as JSON (line {line_number}, truncated={truncated}): {exc}. "
                f"Snippet: {line[:200]!r}"
            ) from exc
    return items


def parse_json_or_ndjson(
    text: str, *, truncated: bool = False, what: str = "docker output"
) -> list[dict] | dict | None:
    """Parse output that may be a single JSON document OR NDJSON.

    Compose v2.21+ emits NDJSON (one object per line); older versions emit a single JSON array or
    object. Returns the parsed structure on success, or None if the body is empty.

    Args:
        text: the body to parse.
        truncated: True if the underlying stdout was capped by run_docker's byte limit. When set,
                   the NDJSON branch drops the final (likely partial) line rather than crashing on a
                   half-record; see `parse_ndjson`.
        what: short label used in error messages, e.g. "compose ps output".
    """
    stripped = text.strip()
    if not stripped:
        return None
    # Try a single-JSON-document parse first (covers `compose config --format json` and older `ps`).
    # A truncated single document can't parse cleanly, so this falls through to the NDJSON branch,
    # which handles truncation and raises a descriptive error on a genuinely unparseable body.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    return parse_ndjson(stripped, truncated=truncated, what=what)

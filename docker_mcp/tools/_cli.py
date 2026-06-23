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
from dataclasses import asdict, dataclass
from pathlib import Path

from docker_mcp._hosts import is_multi as _is_multi, resolve as _resolve_host
from docker_mcp.tools._ssh_proxy import ssh_proxy_for_docker_host

DEFAULT_TIMEOUT_SECONDS = 60.0

# Per-call cap on captured stdout/stderr bytes. CLI output is intended for human
# consumption so a few MiB is plenty; the cap keeps a runaway subcommand from
# OOM'ing the MCP server. Mirrors the buffer-cap rationale in SECURITY.md.
MAX_CLI_OUTPUT_BYTES = 4_194_304  # 4 MiB

# Env vars we always forward to child docker invocations. Anything not in this
# allow-list is dropped so the subprocess gets a minimal, predictable environment.
# SSH_* keys are kept for the best-effort fallback case where the CLI dials an ssh:// daemon
# through a *context* rather than DOCKER_HOST directly (run_docker only rewrites DOCKER_HOST
# itself to the local proxy below) — that path still shells out to the system ssh client.
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
        raise FileNotFoundError(
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
    """
    Point the child `docker` CLI at the selected host by overriding DOCKER_HOST + per-host TLS in `env`.

    Inert for the legacy single host (DOCKER_MCP_SERVER_HOSTS unset), which keeps inheriting the ambient
    DOCKER_HOST / DOCKER_CONTEXT exactly as before. For an explicitly-configured host we pin DOCKER_HOST
    to its resolved URL (so the CLI and the docker-py SDK provably target the same daemon for a label),
    drop DOCKER_CONTEXT, and apply the per-host cert dir — else fall through to the global
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
    """
    Run `docker <args...>` with safe, cross-platform defaults.

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
        if env.get("DOCKER_HOST", "").startswith("ssh://"):
            # Bound the paramiko connect/banner/auth phases to this call's own timeout — they run
            # before subprocess.run(timeout=timeout) below, so without this an unreachable or
            # filtered ssh:// host could hang here indefinitely regardless of the caller's timeout.
            proxy = stack.enter_context(ssh_proxy_for_docker_host(env["DOCKER_HOST"], timeout=timeout))
            env["DOCKER_HOST"] = f"tcp://127.0.0.1:{proxy.port}"
            # A native ssh:// DOCKER_HOST ignores TLS entirely; the rewritten tcp:// one would
            # otherwise pick up any forwarded DOCKER_TLS_VERIFY/DOCKER_CERT_PATH and attempt a TLS
            # handshake against this plaintext loopback proxy, breaking every CLI call.
            env.pop("DOCKER_TLS_VERIFY", None)
            env.pop("DOCKER_CERT_PATH", None)
        proc = subprocess.run(  # noqa: S603 — shell=False, argv is a list, binary is resolved via shutil.which
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


# Errors that mean "we couldn't even probe the plugin" — never let them propagate
# out of `has_plugin`. Assigning the tuple to a module-level constant also dodges
# the PEP 758 parenthesis-free `except` form that older parsers (and PR review bots)
# flag as a syntax error.
_PLUGIN_PROBE_ERRORS: tuple[type[BaseException], ...] = (FileNotFoundError, subprocess.TimeoutExpired)

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
    """Raise RuntimeError with an actionable message if the named CLI plugin is unavailable."""
    if not has_plugin(name):
        raise RuntimeError(
            f"Docker CLI plugin {name!r} is not installed or not available on PATH. "
            f"On Docker Desktop it ships by default; on a plain Docker Engine install, "
            f"install it via your distribution's docker-{name}-plugin package "
            f"(or follow the upstream docs)."
        )


def safe_positional(value: str, what: str = "value") -> str:
    """
    Validate a string that will be appended as a *positional* docker CLI argument.

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
        raise ValueError(
            f"Refusing to pass {what}={value!r} as a positional docker argument: it starts with '-', "
            f"which the docker CLI parses as a flag rather than a value. This is blocked to prevent "
            f"flag injection; a real {what} cannot start with '-'."
        )
    return value


def raise_on_cli_failure(result: CliResult, command: str) -> None:
    """
    Raise RuntimeError if a docker subprocess exited non-zero.

    args:
        result: the CliResult from run_docker.
        command: the docker subcommand for the message, e.g. "buildx ls" or "context inspect".
    """
    if result.returncode != 0:
        raise RuntimeError(
            f"`docker {command}` failed with exit code {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )


def parse_ndjson(text: str, *, truncated: bool = False, what: str = "docker output") -> list[dict]:
    """
    Parse one JSON object per non-blank line (NDJSON), as emitted by `docker ... --format '{{json .}}'`.

    args:
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
    """
    Parse output that may be a single JSON document OR NDJSON.

    Compose v2.21+ emits NDJSON (one object per line); older versions emit a single JSON array or
    object. Returns the parsed structure on success, or None if the body is empty.

    args:
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

# Cross-platform helper for shelling out to the `docker` CLI and its plugins.
#
# Everything that wraps a CLI rather than a docker-py method funnels through
# `run_docker()` so the platform-specific concerns (binary discovery, Windows
# console suppression, env scrubbing, byte-level output caps) live in one place.

import functools
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 60.0

# Per-call cap on captured stdout/stderr bytes. CLI output is intended for human
# consumption so a few MiB is plenty; the cap keeps a runaway subcommand from
# OOM'ing the MCP server. Mirrors the buffer-cap rationale in SECURITY.md.
MAX_CLI_OUTPUT_BYTES = 4_194_304  # 4 MiB

# Env vars we always forward to child docker invocations. Anything not in this
# allow-list is dropped so the subprocess gets a minimal, predictable environment.
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


def run_docker(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    stdin: bytes | None = None,
    extra_env: dict[str, str] | None = None,
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
    """
    binary = _resolve("docker")
    cmd = [binary, *args]
    env = _safe_env()
    if extra_env:
        env.update(extra_env)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
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


@functools.cache
def has_plugin(name: str) -> bool:
    """Return True if `docker <name> version` exits 0. Cached per process."""
    try:
        result = run_docker([name, "version"], timeout=10)
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def require_plugin(name: str) -> None:
    """Raise RuntimeError with an actionable message if the named CLI plugin is unavailable."""
    if not has_plugin(name):
        raise RuntimeError(
            f"Docker CLI plugin {name!r} is not installed or not available on PATH. "
            f"On Docker Desktop it ships by default; on a plain Docker Engine install, "
            f"install it via your distribution's docker-{name}-plugin package "
            f"(or follow the upstream docs)."
        )

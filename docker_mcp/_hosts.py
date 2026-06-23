# Host registry: parse DOCKER_MCP_SERVER_HOSTS into a pinned map of label -> resolved daemon URL.
#
# We resolve the `auto`/`local`/`default` concepts to concrete daemon URLs ourselves (reading Docker
# CLI context files and probing well-known sockets) and pin them at load() time, so the docker-py SDK
# and the docker-CLI shell-out target the *same* daemon for a given label — every action is
# attributable to one host (auditing), and a mid-session `docker context use` cannot silently move a
# label (restart to re-resolve).
#
# Lives at the package root (not under tools/) so docker_mcp.server can import it for the host guard
# without importing docker_mcp.tools (a circular import at tool-registration time), mirroring _env.py.
# Pure registry/resolution logic: only env + Docker config/meta file reads, no docker-py/CLI calls.

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from docker_mcp._env import read_env, scrub_unresolved_env

# Internal label for the single synthesized host built when DOCKER_MCP_SERVER_HOSTS is unset or a bare
# single endpoint. It only exists in single-host mode, where labels are never surfaced (no `host`
# param, no enum), so it is never offered as a selectable target. A multi-host list never synthesizes
# it; a user MAY label one of their hosts "default" there, which is an ordinary label — conceptually
# separate from this internal fallback, and resolvable like any other (a deliberate design choice).
_DEFAULT_LABEL = "default"

_VALID_LABEL = re.compile(r"[A-Za-z0-9_.-]+")
_URL_SCHEMES = ("unix://", "tcp://", "ssh://", "npipe://")
_TRAILING_MARKER = re.compile(r"\(([^)]*)\)\s*$")


class HostConfigError(Exception):
    """A malformed DOCKER_MCP_SERVER_HOSTS value. load() turns this into a stderr line + exit(1)."""


@dataclass(frozen=True)
class Host:
    """
    One configured daemon.

    `url` is the resolved concrete daemon URL, or None to let docker-py's from_env() apply its own
    platform default (e.g. the Windows named pipe). `cert_dir` is a tcp+TLS cert directory (`ca.pem`
    required — the daemon is always verified; `cert.pem`/`key.pem` optional for mutual TLS), or None.
    """

    label: str
    url: str | None
    read_only: bool = False
    cert_dir: str | None = None


# --- auto / local resolution (relocated from client.py; reads Docker config files, not the daemon) ---


def _docker_config_dir() -> Path:
    """The Docker CLI config directory ($DOCKER_CONFIG, else ~/.docker) — where contexts live."""
    return Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker")


def _active_context_name() -> str | None:
    """Name of the active Docker CLI context: $DOCKER_CONTEXT, else config.json's currentContext."""
    name = (os.environ.get("DOCKER_CONTEXT") or "").strip()
    if name:
        return name
    try:
        cfg = json.loads((_docker_config_dir() / "config.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return (cfg.get("currentContext") or "").strip() or None


def _context_host(name: str) -> str | None:
    """The docker endpoint Host for a named CLI context, read from its meta.json, or None."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    meta_path = _docker_config_dir() / "contexts" / "meta" / digest / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    host = (((meta.get("Endpoints") or {}).get("docker") or {}).get("Host") or "").strip()
    return host or None


def resolve_local() -> str | None:
    """
    The platform-local daemon socket — first existing well-known location, Docker Desktop / rootless
    first. Context-bypassing. Returns None to let from_env() apply its platform default (e.g. the
    Windows named pipe, which has nothing to probe on disk).
    """
    if sys.platform == "win32":  # pyright: ignore[reportUnreachable]
        return None
    home = Path.home()
    candidates = [
        home / ".docker" / "run" / "docker.sock",  # Docker Desktop (mac/linux), 4.13+
        home / ".docker" / "desktop" / "docker.sock",  # older Docker Desktop layout
    ]
    xdg = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if xdg:
        candidates.append(Path(xdg) / "docker.sock")  # rootless Linux
    candidates.append(Path("/var/run/docker.sock"))  # classic / native Linux engine
    for sock in candidates:
        try:
            if sock.is_socket():
                return f"unix://{sock}"
        except OSError:
            continue
    return None


def resolve_auto() -> str | None:
    """
    The daemon a context-aware `docker` with no DOCKER_HOST would use: the active CLI context's
    endpoint (DOCKER_CONTEXT / config.json currentContext -> its meta.json Host), else the local-socket
    probe. docker-py's from_env() is context-blind, so we resolve the context ourselves. Returns None
    to let from_env() apply its own platform default.
    """
    name = _active_context_name()
    if name and name != "default":
        host = _context_host(name)
        if host:
            return host
    return resolve_local()


# --- parsing -------------------------------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    raise HostConfigError(message)


def _parse_markers(text: str, context: str) -> tuple[str, bool, str | None]:
    """Strip trailing (ro)/(tls=<dir>) markers (any order, case-insensitive) off an endpoint string,
    returning (endpoint, read_only, cert_dir)."""
    read_only = False
    cert_dir: str | None = None
    while (match := _TRAILING_MARKER.search(text)) is not None:
        body = match.group(1).strip()
        low = body.lower()
        if low == "ro":
            read_only = True
        elif low.startswith("tls="):
            cert_dir = body[len("tls=") :].strip()  # preserve the path's case
            if not cert_dir:
                _fail(f"{context!r}: (tls=) needs a directory, e.g. (tls=/etc/docker/prod)")
        else:
            _fail(f"{context!r}: unknown marker '({body})'; only (ro) and (tls=<dir>) are allowed")
        text = text[: match.start()].rstrip()
    return text.strip(), read_only, cert_dir


def _readable(path: Path) -> bool:
    """True if `path` exists and can be opened for reading."""
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _validate_cert_dir(label: str, cert_dir: str) -> None:
    """
    Fail-fast on a malformed tcp+TLS cert dir (a misparsed TLS config must never silently leave a
    daemon connection unencrypted or misconfigured).

    `ca.pem` is always required — the daemon is always verified against it (this is encryption + server
    authentication, never opportunistic encryption). The client cert is optional but paired: provide
    `cert.pem` AND `key.pem` for mutual TLS (a daemon that requires client auth), or neither to verify
    the daemon only (e.g. a self-signed daemon you pin via `ca.pem`).
    """
    directory = Path(cert_dir)
    if not _readable(directory / "ca.pem"):
        _fail(f"host {label!r}: TLS dir {cert_dir!r} is missing or cannot read ca.pem (required to verify the daemon)")
    if _readable(directory / "cert.pem") != _readable(directory / "key.pem"):
        _fail(
            f"host {label!r}: TLS dir {cert_dir!r} has exactly one of cert.pem/key.pem — provide both "
            f"(mutual TLS) or neither (verify the daemon only)"
        )


def _make_host(label: str, raw_endpoint: str, context: str) -> Host:
    """Build a Host from one endpoint spec: parse markers, validate, and resolve to a concrete URL."""
    endpoint, read_only, cert_dir = _parse_markers(raw_endpoint.strip(), context)
    low = endpoint.lower()
    if cert_dir is not None:
        if not low.startswith("tcp://"):
            _fail(f"host {label!r}: (tls=) is only valid on a tcp:// endpoint, not {endpoint or '(empty)'!r}")
        cert_dir = str(Path(cert_dir).expanduser())
        _validate_cert_dir(label, cert_dir)
    if low in ("", "auto"):
        url = resolve_auto()
    elif low == "local":
        url = resolve_local()
    elif endpoint.startswith(_URL_SCHEMES):
        url = endpoint
    else:
        _fail(
            f"host {label!r}: unrecognized endpoint {endpoint!r} "
            f"(use 'auto', 'local', or a unix:// / tcp:// / ssh:// / npipe:// URL)"
        )
    return Host(label=label, url=url, read_only=read_only, cert_dir=cert_dir)


def _legacy_host() -> Host:
    """The single synthesized host when DOCKER_MCP_SERVER_HOSTS is unset: DOCKER_HOST if set, else auto."""
    docker_host = (os.environ.get("DOCKER_HOST") or "").strip()
    url = docker_host or resolve_auto()
    return Host(label=_DEFAULT_LABEL, url=url)


def parse_registry(raw: str | None) -> dict[str, Host]:
    """
    Parse DOCKER_MCP_SERVER_HOSTS into an ordered {label: Host} (first entry = default).

    Unset/empty -> a single synthesized host from DOCKER_HOST/auto. A value with no '=' is the bare
    single-host shorthand (the whole value is one endpoint). Otherwise it is a comma-separated
    `label=endpoint` list. Raises HostConfigError on any malformed value; the caller fail-fasts.
    """
    text = (raw or "").strip()
    if not text:
        return {_DEFAULT_LABEL: _legacy_host()}
    if "=" not in text:
        return {_DEFAULT_LABEL: _make_host(_DEFAULT_LABEL, text, context=text)}
    registry: dict[str, Host] = {}
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            _fail(f"entry {entry!r} is missing '=' (expected label=endpoint)")
        label, _, endpoint = entry.partition("=")
        label = label.strip()
        if not label:
            _fail(f"entry {entry!r} has an empty label")
        if not _VALID_LABEL.fullmatch(label):
            _fail(f"label {label!r} is invalid (allowed characters: letters, digits, '_', '.', '-')")
        if label in registry:
            _fail(f"duplicate host label {label!r}")
        registry[label] = _make_host(label, endpoint, context=entry)
    if not registry:
        _fail("DOCKER_MCP_SERVER_HOSTS is set but contains no host entries")
    return registry


# --- pinned module state + accessors -------------------------------------------------------------

_registry: dict[str, Host] = {}
_docker_host_notice_shown = False


def _notify_docker_host_ignored() -> None:
    """One-time stderr notice that DOCKER_HOST is ignored because DOCKER_MCP_SERVER_HOSTS is set."""
    global _docker_host_notice_shown
    if _docker_host_notice_shown:
        return
    _docker_host_notice_shown = True
    print(
        "docker-mcp-server: DOCKER_MCP_SERVER_HOSTS is set; ignoring DOCKER_HOST.",
        file=sys.stderr,
        flush=True,
    )


def load() -> None:
    """
    Parse DOCKER_MCP_SERVER_HOSTS and pin the registry. Call once at startup, before the tool modules
    import (the @tool() decorator and resources read the registry at registration time).

    Scrubs unresolved `${...}` placeholders first so an mcpb blank field resolves to the default host
    rather than fail-fast. A malformed value prints one stderr line and exits non-zero — a misparsed
    (ro)/(tls=) must never silently leave a host writable or unencrypted.
    """
    global _registry
    scrub_unresolved_env()
    raw = read_env("DOCKER_MCP_SERVER_HOSTS")
    # Warn only when HOSTS actually takes effect — a whitespace-only value parses as unset (DOCKER_HOST
    # is then honored, not ignored), so it must not trigger the notice.
    if (raw or "").strip() and (os.environ.get("DOCKER_HOST") or "").strip():
        _notify_docker_host_ignored()
    try:
        _registry = parse_registry(raw)
    except HostConfigError as exc:
        print(f"docker-mcp-server: invalid DOCKER_MCP_SERVER_HOSTS — {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


def resolve(host: str | None) -> Host:
    """The Host for a label, or the default (first entry) when host is None. Raises KeyError naming the
    configured labels for an unknown label."""
    if host is None:
        return default()
    try:
        return _registry[host]
    except KeyError:
        raise KeyError(f"unknown host {host!r}; configured hosts: {labels()}") from None


def default() -> Host:
    """The default host (first registry entry) used when a host is omitted."""
    return next(iter(_registry.values()))


def labels() -> list[str]:
    """Configured host labels in declared order (first = default)."""
    return list(_registry)


def is_read_only(host: str | None = None) -> bool:
    """Whether the named (or default) host is flagged read-only."""
    return resolve(host).read_only


def is_multi() -> bool:
    """True when 2+ hosts are configured (gates the per-call host param, its enum, and multi-host prompts)."""
    return len(_registry) >= 2


def registry() -> dict[str, Host]:
    """A copy of the pinned registry, for list_hosts / the docker-mcp://hosts resource."""
    return dict(_registry)

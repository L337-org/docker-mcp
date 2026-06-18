# internal helper: environment-variable lookup with backward-compatible aliases
#
# The server's tunables are namespaced DOCKER_MCP_SERVER_* (matching the published package and
# image name, docker-mcp-server). The older DOCKER_MCP_* spellings are still honored as deprecated
# aliases so existing MCP-client configs and `docker run -e ...` invocations keep working unchanged.
# The first time a deprecated alias is read we print a one-line notice to stderr (never stdout —
# that is the stdio MCP transport) naming the canonical replacement.
#
# Lives at the package root (not under tools/) so docker_mcp.server can use it without importing
# docker_mcp.tools, which would be a circular import at tool-registration time.

import os
import sys

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Deprecated aliases already warned about, so a repeatedly-read var nags at most once per process.
_warned_aliases: set[str] = set()


def read_env(canonical: str, *aliases: str, default: str | None = None) -> str | None:
    """
    Value of env var `canonical`, falling back to each deprecated `alias` in order, else `default`.

    The canonical (DOCKER_MCP_SERVER_*) name is checked first. Reading a value via one of the older
    `aliases` emits a one-time stderr deprecation notice naming `canonical`.
    """
    value = os.environ.get(canonical)
    if value is not None:
        return value
    for alias in aliases:
        value = os.environ.get(alias)
        if value is not None:
            _warn_deprecated(alias, canonical)
            return value
    return default


def env_flag(canonical: str, *aliases: str) -> bool:
    """True when `canonical` (or a deprecated `alias`) is set to a truthy value (1/true/yes/on)."""
    return (read_env(canonical, *aliases) or "").strip().lower() in _TRUTHY


def _warn_deprecated(alias: str, canonical: str) -> None:
    """Print a one-time stderr notice the first time a deprecated alias is read this process."""
    if alias in _warned_aliases:
        return
    _warned_aliases.add(alias)
    print(
        f"docker-mcp-server: environment variable {alias} is deprecated; use {canonical} instead.",
        file=sys.stderr,
    )

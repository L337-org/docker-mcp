"""Redaction settings: the global switch, the names setting, and which hosts are redacted.

Read once at startup by `load()`, after `_hosts.load()`, and pinned for the life of the process like
the other switches.  Pure configuration: no daemon calls, and nothing here redacts anything.
"""

import fnmatch
import re
import sys
from dataclasses import dataclass
from typing import Any, NoReturn

from docker_mcp import _hosts
from docker_mcp._env import env_flag, read_env
from docker_mcp.exceptions import RedactionConfigError

REDACT_VAR = "DOCKER_MCP_SERVER_REDACT"
NAMES_VAR = "DOCKER_MCP_SERVER_REDACT_NAMES"

# Variable names whose values are harvested as credentials, as case-insensitive globs.  Kept to the
# words a credential is usually named with: every pattern here costs false replacements wherever a
# harmless value under a matching name appears (`*KEY*` harvests `SORT_KEY`), which is why the names
# setting can exclude one with `!`.  `PASS` is matched only as a whole trailing or inner word, because
# `*PASS*` would harvest `PASSTHROUGH` and `BYPASS_*`.  Adding a pattern here is a one-line change plus
# its case in tests/test_redaction_settings.py.
BUILTIN_NAME_PATTERNS: tuple[str, ...] = (
    "*PASSWORD*",
    "*PASSWD*",
    "*PASSPHRASE*",
    "*PASS",
    "*_PASS_*",
    "*SECRET*",
    "*TOKEN*",
    "*KEY*",
    "*CERT*",
    "*CREDENTIAL*",
)

# Whether this build redacts output at all.  While it does not, configuring redaction warns that it
# does nothing, and REDACTION.md carries a status note saying so.
OUTPUT_REDACTED = True

# A names entry: a variable name, optionally a glob, optionally preceded by `!`.  Deliberately narrow,
# because anything else is more likely a value pasted by mistake than a name.
_NAME_ENTRY = re.compile(r"[A-Za-z0-9_.*?\[\]-]+")


@dataclass(frozen=True)
class NameRules:
    """Which variable names are harvested: the built-in patterns, plus additions, minus exclusions.

    Attributes:
        added: globs from the names setting that extend the built-in patterns.
        excluded: globs from the names setting (written with a leading `!`) that are never harvested,
            whichever pattern they would otherwise match.
    """

    added: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    def matches(self, name: str) -> bool:
        """Whether a variable of this name is harvested.

        An exclusion wins over both the built-in patterns and an addition, so a name both added and
        excluded is not harvested.

        Args:
            name: a variable name, in any case

        Returns:
            bool: True when its value is to be harvested
        """
        upper = name.upper()
        if any(fnmatch.fnmatchcase(upper, pattern.upper()) for pattern in self.excluded):
            return False
        return any(fnmatch.fnmatchcase(upper, pattern.upper()) for pattern in (*BUILTIN_NAME_PATTERNS, *self.added))

    def report(self) -> dict[str, list[str]]:
        """The resolved name list, as shown wherever the effective settings are.

        Returns:
            dict: ``builtin``, ``added`` and ``excluded`` glob lists
        """
        return {"builtin": list(BUILTIN_NAME_PATTERNS), "added": list(self.added), "excluded": list(self.excluded)}


def _fail(message: str) -> NoReturn:
    raise RedactionConfigError(message)


def parse_names(raw: str | None) -> NameRules:
    """Parse the comma-separated names setting into NameRules.

    Entries are names or case-insensitive globs; a leading `!` excludes.  Empty entries are skipped.
    A malformed entry raises RedactionConfigError, whose message gives the entry's position rather
    than its text: the commonest malformed entry is `NAME=value`, and repeating that would print a
    credential to the log.

    Args:
        raw: the DOCKER_MCP_SERVER_REDACT_NAMES value, or None when unset

    Returns:
        NameRules: the additions and exclusions
    """
    added: list[str] = []
    excluded: list[str] = []
    for position, entry in enumerate((raw or "").split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        exclude = entry.startswith("!")
        name = entry[1:].strip() if exclude else entry
        if not name:
            _fail(f"entry {position} is a bare '!'; write '!NAME' to exclude a name")
        if "=" in name:
            _fail(
                f"entry {position} contains '='; this setting takes variable names only, "
                "never values (the entry is not repeated here in case it holds one)"
            )
        if not _NAME_ENTRY.fullmatch(name):
            _fail(
                f"entry {position} is not a variable name or glob; allowed are letters, "
                "digits, '_', '.', '-' and the glob characters '*', '?', '[' and ']' (the entry is not "
                "repeated here in case it holds a value)"
            )
        (excluded if exclude else added).append(name)
    return NameRules(added=tuple(added), excluded=tuple(excluded))


# --- pinned module state + accessors -------------------------------------------------------------

_global = False
_names = NameRules()
_names_set = False


def load() -> None:
    """Read the redaction settings and pin them.

    Call once at startup, after `_hosts.load()` and before the tool modules import.  A malformed value
    prints one stderr line and exits non-zero: a misread names setting must never silently leave a
    credential unharvested.

    Raises:
        SystemExit: a setting is malformed, so the server must not start.
    """
    global _global, _names, _names_set
    _global = env_flag(REDACT_VAR)
    raw = read_env(NAMES_VAR)
    try:
        _names = parse_names(raw)
    except RedactionConfigError as exc:
        print(f"docker-mcp-server: invalid {NAMES_VAR} - {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    _names_set = bool(_names.added or _names.excluded)


def enabled_globally() -> bool:
    """Whether DOCKER_MCP_SERVER_REDACT redacts every host.

    Returns:
        bool: True when the global switch is on
    """
    return _global


def names() -> NameRules:
    """The pinned name rules.

    Returns:
        NameRules: the additions and exclusions from DOCKER_MCP_SERVER_REDACT_NAMES
    """
    return _names


def redacts(host: _hosts.Host) -> bool:
    """Whether output from this host is redacted: the global switch or the host's own option.

    The one place the two are combined; `Host.redact` alone misses the global switch.

    Args:
        host: a configured host

    Returns:
        bool: True when output from that host is redacted
    """
    return _global or host.redact


def host_redacted(host: str | None = None) -> bool:
    """Whether calls to the named (or default) host are redacted.

    Args:
        host: the host label, or None for the default

    Returns:
        bool: True when output from that host is redacted
    """
    return redacts(_hosts.resolve(host))


def redacted_hosts() -> list[str]:
    """The labels of every redacted host, in declared order.

    Returns:
        list: the redacted host labels
    """
    return [label for label, host in _hosts.registry().items() if redacts(host)]


def any_redacted() -> bool:
    """Whether redaction is on for at least one host.

    Returns:
        bool: True when any host is redacted
    """
    return bool(redacted_hosts())


def report() -> dict[str, Any]:
    """The effective redaction settings, for the `switches` that tool_list and the catalog report.

    Returns:
        dict: the global switch, the resolved name list, and the redacted host labels
    """
    return {REDACT_VAR: _global, NAMES_VAR: _names.report(), "redacted_hosts": redacted_hosts()}


def startup_warnings(*, readonly: bool) -> list[str]:
    """What the redaction settings leave weaker than they look, worded for the operator who set them.

    Judged per redacted host: exec and the tools that write daemon data to files are writes, so a
    host is covered if it is `ro` or READONLY is on, whether it is redacted by its own option or by
    the global switch.

    Args:
        readonly: whether DOCKER_MCP_SERVER_READONLY is on, which the recommended setup pairs with redaction

    Returns:
        list: one sentence per problem; empty when there is none
    """
    warnings: list[str] = []
    if not readonly:
        for label, host in _hosts.registry().items():
            if redacts(host) and not host.read_only:
                warnings.append(
                    f"host {label!r} is redacted without ro or DOCKER_MCP_SERVER_READONLY: exec and the tools "
                    "that write daemon data to files stay available on it, and either can show an agent a "
                    "credential redaction cannot catch. Mark it (ro,redact) or set DOCKER_MCP_SERVER_READONLY=1. "
                    "See REDACTION.md."
                )
    if _names_set and not any_redacted():
        warnings.append(
            f"{NAMES_VAR} is set but redaction is not on for any host, so it does nothing. Set {REDACT_VAR}=1 "
            "or mark a host redact in DOCKER_MCP_SERVER_HOSTS."
        )
    # Configuring redaction must never read as protection while nothing redacts output.  REDACTION.md
    # carries a matching status note, and a test holds the two together through this flag.
    if not OUTPUT_REDACTED and any_redacted():
        warnings.append(
            "redaction is configured, but this build does not redact anything yet: output from every "
            "host is returned unredacted."
        )
    return warnings

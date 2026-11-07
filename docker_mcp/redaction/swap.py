"""Swapping markers in a write's arguments back for the values they stand for, or refusing the write.

Redaction rewrites output only, but an agent can copy what it read into a write: `Env` from
`container_inspect` into `container_create`.  Before a tool that is not read-only runs, every marker in
its arguments, nested values included, is checked.  A marker this process issued for the host the call
targets is replaced with exactly the text it replaced; any other marker refuses the whole call before
anything runs.  As a guardrail against copying a credential far beyond the host, markers are refused
outright in names and identifiers, labels, build arguments and bake overrides, where a credential is
never legitimate.

The argument table below is the extension point: `tests/test_redaction_swap.py` fails until every
argument of every write tool that can hold text is in it, so a new one cannot take a marker without
someone deciding what happens to it.  Substituted arguments are never logged.
"""

import enum
import re
from typing import Any

from docker_mcp.exceptions import MarkerRefusedError
from docker_mcp.redaction.markers import MARKER_PATTERN
from docker_mcp.redaction.redactor import Redactor, Refusal

_MARKER_BYTES = re.compile(MARKER_PATTERN.pattern.encode("ascii"))


class Kind(enum.Enum):
    """What happens to a marker in an argument.

    Attributes:
        SWAP: swapped back for its value.
        NAME: refused; a name, identifier, image reference or hostname, copied into every listing and
            event, and into registries when pushed.
        LABEL: refused; labels, annotations and commit metadata, kept in image metadata and published
            by `image_push`.
        BUILD_ARG: refused; recorded in the image's history, which anyone who can pull it can read.
        BAKE_OVERRIDE: refused; passed on the command line, where other users of the machine can read it.
        ARCHIVE: archive bytes, never swapped back, since replacing text would change a member's size;
            `container_archive_put` searches them for markers instead (see `disk.py`).
    """

    SWAP = "swap"
    NAME = "name"
    LABEL = "label"
    BUILD_ARG = "build_arg"
    BAKE_OVERRIDE = "bake_override"
    ARCHIVE = "archive"


# Every argument of a write tool that can hold text, by name: a name means the same thing in every tool
# that takes it.  `_TOOL_ARGUMENTS` overrides it for one tool where it does not.
ARGUMENTS: dict[str, Kind] = {
    # Names and identifiers, image references and tags, hostnames.
    **dict.fromkeys(
        (
            "aliases",
            "builder",
            "container",
            "docker_host",
            "from_image",
            "hostname",
            "id_or_name",
            "image",
            "links",
            "local_name",
            "name",
            "names",
            "network",
            "node_name",
            "project_name",
            "registry",
            "remote",
            "repository",
            "service",
            "services",
            "sources",
            "tag",
            "tags",
            "target",
            "targets",
        ),
        Kind.NAME,
    ),
    **dict.fromkeys(("annotations", "author", "labels", "message"), Kind.LABEL),
    **dict.fromkeys(("build_args", "buildargs"), Kind.BUILD_ARG),
    "set_overrides": Kind.BAKE_OVERRIDE,
    # Everything else is swapped back: environments, commands, `vars`, credentials passed as arguments
    # (`password`, `join_token`, `key`, `auth_config`), and paths, which only ever reach a file name.
    **dict.fromkeys(
        (
            "advertise_addr",
            "attest",
            "auth_config",
            "build_contexts",
            "cache_from",
            "cache_to",
            "changes",
            "cmd",
            "command",
            "compose_files",
            "conf",
            "config",
            "container_limits",
            "context",
            "cwd",
            "data",
            "data_path_addr",
            "default_addr_pool",
            "description",
            "descriptor_files",
            "dest",
            "dest_path",
            "dockercfg_path",
            "dockerfile",
            "driver",
            "driver_opt",
            "driver_opts",
            "entrypoint",
            "env",
            "environment",
            "extra_hosts",
            "extra_kwargs",
            "file",
            "files",
            "filters",
            "from_file",
            "from_url",
            "ipam",
            "ipv4_address",
            "ipv6_address",
            "isolation",
            "join_token",
            "key",
            "link_local_ips",
            "listen_addr",
            "log_driver",
            "mac_address",
            "max_used_space",
            "mem_limit",
            "min_free_space",
            "network_mode",
            "no_cache_filter",
            "options",
            "output",
            "password",
            "path",
            "platform",
            "platforms",
            "ports",
            "profiles",
            "project_dir",
            "provenance",
            "pull",
            "remote_addrs",
            "reserved_space",
            "resolve_image",
            "restart_policy",
            "sbom",
            "scope",
            "secret",
            "signal",
            "source",
            "ssh",
            "templating",
            "tls_ca",
            "tls_cert",
            "tls_key",
            "updates",
            "user",
            "username",
            "vars",
            "volumes",
            "workdir",
            "working_dir",
        ),
        Kind.SWAP,
    ),
}

_TOOL_ARGUMENTS: dict[tuple[str, str], Kind] = {
    ("container_archive_put", "data"): Kind.ARCHIVE,
    ("image_import", "data"): Kind.ARCHIVE,
    ("image_load", "data"): Kind.ARCHIVE,
}

# Arguments holding docker-py keyword arguments or an Engine spec, whose keys name what they set: a
# marker under one of these keys is refused as the matching top-level argument would be.
_NESTED_ARGUMENTS = frozenset({"conf", "extra_kwargs", "updates"})
_NESTED_KEYS: dict[str, Kind] = {
    **dict.fromkeys(("domainname", "Domainname", "hostname", "Hostname", "image", "Image"), Kind.NAME),
    **dict.fromkeys(("name", "Name"), Kind.NAME),
    **dict.fromkeys(("container_labels", "labels", "Labels"), Kind.LABEL),
}

_GUARDRAIL_REASONS = {
    Kind.NAME: (
        "a name or identifier, where a credential is never legitimate: names are copied into every listing "
        "and event, and into registries when pushed"
    ),
    Kind.LABEL: (
        "a label, annotation or commit field, where a credential is never legitimate: they are kept in image "
        "metadata and published by image_push"
    ),
    Kind.BUILD_ARG: (
        "a build argument, which is recorded in the image's history, where anyone who can pull the image "
        "can read it. Pass the value in buildx_build's `vars` and use it as a build secret "
        "(`secret`: id=NAME,env=VAR) instead"
    ),
    Kind.BAKE_OVERRIDE: (
        "a bake override, which is passed on the command line, where other users of the machine can read "
        "it. Pass the value in `vars` and reference it as a bake variable instead"
    ),
}


def kind_of(tool: str, argument: str) -> Kind | None:
    """What happens to a marker in one argument of one tool.

    Args:
        tool: the tool's name
        argument: the argument's name

    Returns:
        Kind | None: its kind, or None for an argument the table does not classify
    """
    return _TOOL_ARGUMENTS.get((tool, argument), ARGUMENTS.get(argument))


def has_marker(value: Any) -> bool:
    """Whether a value holds anything shaped like a marker, at any depth.

    Args:
        value: an argument's value

    Returns:
        bool: True when a marker-shaped string appears in it, dict keys included
    """
    if isinstance(value, str):
        return MARKER_PATTERN.search(value) is not None
    if isinstance(value, bytes | bytearray):
        return _MARKER_BYTES.search(value) is not None
    if isinstance(value, dict):
        return any(has_marker(key) or has_marker(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(has_marker(item) for item in value)
    return False


class _Swapper:
    """Walks one call's arguments, swapping markers back or raising on the first that cannot be."""

    def __init__(self, redactor: Redactor, tool: str, label: str | None, *, swaps: bool) -> None:
        """Set up the walk of one call.

        Args:
            redactor: the redactor that issued the markers
            tool: the tool's name, for messages
            label: the host the call targets, or None for a tool that reaches no daemon
            swaps: whether a marker may be swapped back on that host
        """
        self._redactor = redactor
        self._tool = tool
        self._label = label
        self._swaps = swaps

    def argument(self, name: str, value: Any) -> Any:
        if not has_marker(value):
            return value
        kind = kind_of(self._tool, name)
        if kind is Kind.ARCHIVE:
            return value
        # An argument the table does not know is swapped back, as the design's default; the test that
        # holds the table to the registered tools keeps this from being reached.
        return self._walk(value, name, kind or Kind.SWAP, nested=name in _NESTED_ARGUMENTS)

    def _walk(self, value: Any, where: str, kind: Kind, *, nested: bool = False) -> Any:
        if isinstance(value, str):
            return MARKER_PATTERN.sub(lambda match: self._resolve(match.group(1), where, kind), value)
        if isinstance(value, bytes | bytearray):
            return _MARKER_BYTES.sub(
                lambda match: self._resolve(match.group(1).decode("ascii"), where, kind).encode("utf-8"), value
            )
        if isinstance(value, dict):
            walked = {}
            for key, item in value.items():
                inner = _NESTED_KEYS.get(key, kind) if nested and isinstance(key, str) else kind
                at = f"{where}[{key!r}]"
                walked[self._walk(key, at, inner)] = self._walk(item, at, inner)
            return walked
        if isinstance(value, list | tuple):
            return type(value)(self._walk(item, f"{where}[{index}]", kind) for index, item in enumerate(value))
        return value

    def _refuse(self, where: str, reason: str) -> MarkerRefusedError:
        target = f"on host {self._label!r}" if self._label is not None else "by a tool that reaches no daemon"
        return MarkerRefusedError(
            f"{self._tool}: refused before anything ran {target}: the [REDACTED:...] marker in `{where}` "
            f"{reason}. See REDACTION.md."
        )

    def _resolve(self, token: str, where: str, kind: Kind) -> str:
        if kind is not Kind.SWAP:
            raise self._refuse(where, f"is not swapped back there: `{where}` is {_GUARDRAIL_REASONS[kind]}")
        if self._label is None:
            raise self._refuse(
                where, "cannot be used: a marker is swapped back only in a write to the host it came from"
            )
        if not self._swaps:
            raise self._refuse(
                where,
                "cannot be used: markers are swapped back only on a redacted host that accepts writes, and only "
                "for the host they came from. Read the value from this host itself",
            )
        resolved = self._redactor.resolve(token, self._label)
        if isinstance(resolved, str):
            return resolved
        raise self._refuse(where, _REFUSALS[resolved].format(label=self._label))


_REFUSALS = {
    Refusal.MALFORMED: (
        "was not issued by this server process: it was altered, or issued before the server last started, "
        "so the value it stood for is not known. Read the value again from host {label!r} and use the new marker"
    ),
    Refusal.NOT_THIS_PROCESS: (
        "was not issued by this server process: it was altered, or issued before the server last started, "
        "so the value it stood for is not known. Read the value again from host {label!r} and use the new marker"
    ),
    Refusal.OTHER_HOST: (
        "was issued for another host, and a marker is swapped back only on the host it came from. Read the "
        "value again from host {label!r} and use that marker"
    ),
    Refusal.UNSTORED: (
        "stands for a value that was redacted but not kept for use in writes, because the server's store of "
        "values found by its rules was full. Reading it again will not help: stop and ask the user what to do"
    ),
}


def swap_markers(  # noqa: DOC502 - raised by `_Swapper._resolve`
    redactor: Redactor, tool: str, arguments: dict[str, Any], label: str | None, *, swaps: bool
) -> dict[str, Any]:
    """A write's arguments with every marker swapped back, or a refusal naming the first that cannot be.

    Args:
        redactor: the redactor that issued the markers
        tool: the tool's name
        arguments: the call's arguments, by name; not modified
        label: the host the call targets, or None for a tool that reaches no daemon
        swaps: True when the host is redacted and accepts writes, the only place a marker is swapped back;
            elsewhere every marker refuses the call

    Returns:
        dict: the arguments, the same object when nothing was swapped

    Raises:
        MarkerRefusedError: a marker could not be swapped back, or sat where markers are refused; the
            message names the argument and the reason, never a value
    """
    swapper = _Swapper(redactor, tool, label, swaps=swaps)
    swapped = {name: swapper.argument(name, value) for name, value in arguments.items() if name != "host"}
    if all(swapped[name] is arguments[name] for name in swapped):
        return arguments
    return {**arguments, **swapped}

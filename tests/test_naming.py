# Guards the 2.0 tool-naming convention so it can't drift as tools are added.
#
# The rule (see AGENTS.md "Conventions"): every tool is named
# `<management-command>_<verb-or-noun>`, anchored to the docker CLI's management-command
# structure, with long-form verb vocabulary (list/remove/inspect — never ls/rm/get).

import inspect
import re

import docker_mcp.tools
from docker_mcp.server import _seen_tool_names

# The approved namespace prefixes: one per tool domain, plus `host_` for the host-registry
# surface (host_list). A new domain must add its prefix here AND follow the naming rule.
_APPROVED_PREFIXES = (
    "container",
    "image",
    "network",
    "volume",
    "config",
    "secret",
    "service",
    "node",
    "swarm",
    "plugin",
    "compose",
    "stack",
    "context",
    "buildx",
    "scout",
    "registry",
    "hub",
    "system",
    "host",
    "docs",
    "tool",
)

_NAME_PATTERN = re.compile(rf"^({'|'.join(_APPROVED_PREFIXES)})_[a-z0-9_]+$")

# Short-form verbs the convention bans in favor of their long forms. `_ls`/`_rm` must never
# reappear ( → `_list` / `_remove`); `get_`-style prefixes died with the 1.x names ( → `_inspect`).
_BANNED_FRAGMENTS = ("_ls", "_rm", "_rmi")


def test_every_tool_name_uses_an_approved_namespace_prefix():
    unmatched = sorted(name for name in _seen_tool_names if not _NAME_PATTERN.match(name))
    assert unmatched == [], f"tool names outside the naming convention: {unmatched}"


def test_no_tool_name_uses_a_banned_short_form_verb():
    offenders = sorted(
        name
        for name in _seen_tool_names
        if any(name.endswith(fragment) or f"{fragment}_" in name for fragment in _BANNED_FRAGMENTS)
    )
    assert offenders == [], f"tool names using banned short-form verbs: {offenders}"


def test_no_tool_name_starts_with_a_bare_verb():
    # The 1.x style (`list_containers`, `get_image`, `create_volume`) put the verb first; the 2.0
    # convention leads with the namespace. Catch regressions to verb-first names.
    banned_leading_verbs = ("list_", "get_", "create_", "remove_", "update_", "inspect_")
    offenders = sorted(name for name in _seen_tool_names if name.startswith(banned_leading_verbs))
    assert offenders == [], f"verb-first tool names (1.x style): {offenders}"


# ---------- parameter conventions ----------

# Identifier params use exactly these spellings (see AGENTS.md "Conventions"):
#   id_or_name — daemon objects addressable by either (containers, images, networks, services, ...)
#   name/names — name-only resources (volumes, contexts, plugins, stacks, builders)
#   repository — remote repo refs (pull/push, hub_*, registry_*)
# These 1.x spellings must never come back; `v` and bare `timeout` are banned outright.
_BANNED_PARAMS = frozenset(
    {
        "container_id",
        "image_id",
        "network_id",
        "volume_id",
        "config_id",
        "secret_id",
        "service_id",
        "node_id",
        "plugin_id",
        "stack_name",
        "stack_names",
        "id_or_names",
        "v",
        "timeout",  # always timeout_seconds (or stop_timeout_seconds for Docker's stop grace)
        "filter",  # always plural
        "node_spec",  # node_update takes `updates`, as container_update and swarm_update do
        "remote_name",  # plugin tools take `remote`
    }
)


def _tool_functions():
    """Yield (tool_name, function) for every registered tool, via the star-imported tools namespace."""
    for name in sorted(_seen_tool_names):
        func = getattr(docker_mcp.tools, name, None)
        if func is not None:
            yield name, func


# An `Args:` entry must say what the schema cannot (architecture/tool-descriptions.md rule 3): the schema
# already gives each parameter's name, type, default and enum. This is a heuristic for an entry that only
# echoes its name - short, with at most two words beyond the name and common filler - so a hit is a
# prompt to delete the line or give it meaning, and a deliberate exception is listed with its reason.
_ECHO_FILLER = frozenset(
    "the a an to of for in on as or and by be is it its this with use set name id container image network "
    "volume service node optional default none value string str whether if true false label target".split()
)
_ECHO_EXCEPTIONS = {
    ("buildx_create", "platforms"): "what the builder advertises, not what it builds",
    ("buildx_remove", "keep_state"): "names the BuildKit state volume it keeps",
    ("compose_config", "services_only"): "maps to `--services`",
    ("compose_port", "protocol"): "a plain string: gives the accepted values",
    ("container_archive_get", "path"): "inside the container, not on the server host",
    ("container_archive_get_to_file", "path"): "inside the container, not on the server host",
    ("container_commit", "tag"): "the daemon's default, which the schema cannot show",
    ("image_history", "id_or_name"): "accepts a tag or digest",
    ("image_inspect", "id_or_name"): "accepts a tag or digest",
    ("image_registry_data", "repository"): "a full reference, not a repository name",
    ("image_tag", "id_or_name"): "the source, not the new reference",
    ("image_tag", "tag"): "the daemon's default, which the schema cannot show",
    ("network_connect", "id_or_name"): "which of two ids is the network",
    ("network_connect", "container"): "which of two ids is the container",
    ("network_connect", "links"): "deprecated",
    ("network_disconnect", "container"): "which of two ids is the container",
    ("registry_image_config", "reference"): "a tag or a digest",
    ("registry_manifest", "reference"): "a tag or a digest",
    ("system_login", "password"): "a token is accepted",
    ("volume_create", "driver"): "the daemon's default, which the schema cannot show",
}


def _echoes_its_name(param: str, desc: str) -> bool:
    """Whether an `Args:` description says little beyond its parameter's name.

    Args:
        param: the parameter name
        desc: its one-line description

    Returns:
        bool: True for a short description with at most two words beyond the name and common filler
    """
    words = set(re.findall(r"[a-z]+", desc.lower())) - _ECHO_FILLER - set(param.split("_"))
    return len(desc) < 60 and len(words) <= 2


def test_no_args_entry_only_echoes_its_parameter_name():
    canonical = tuple(_CANONICAL_PARAM_PREFIXES.values())
    echoes = [
        f"{name}.{param}: {desc}"
        for name, func in _tool_functions()
        for param, desc in _documented_params(func)
        if _echoes_its_name(param, desc) and not desc.startswith(canonical) and (name, param) not in _ECHO_EXCEPTIONS
    ]
    assert not echoes, (
        "these Args entries repeat the parameter name the schema already shows - delete each, or say what "
        "the schema cannot (format, accepted values, units, interactions):\n  " + "\n  ".join(echoes)
    )


def test_every_echo_exception_still_names_an_entry_the_check_would_flag():
    # An exception for a line since removed, or reworded so it no longer echoes, would quietly let a later
    # regression of that line through. Each must still be documented and still be flagged.
    documented = {(name, param): desc for name, func in _tool_functions() for param, desc in _documented_params(func)}
    stale = sorted(
        key for key in _ECHO_EXCEPTIONS if key not in documented or not _echoes_its_name(key[1], documented[key])
    )
    assert not stale, f"these exceptions no longer match an echoing entry; remove them: {stale}"


def test_the_echo_check_flags_an_echo_and_passes_a_line_with_meaning():
    assert _echoes_its_name("tty", "Allocate a pseudo-TTY")
    assert _echoes_its_name("hostname", "Hostname for the container")
    assert not _echoes_its_name("link", "Remove the legacy link named `id_or_name`, not the container")


def test_no_tool_uses_a_banned_parameter_spelling():
    offenders = [
        f"{name}({param})"
        for name, func in _tool_functions()
        for param in inspect.signature(func).parameters
        if param in _BANNED_PARAMS
    ]
    assert offenders == [], f"banned parameter spellings: {offenders}"


# Durations are `timeout_seconds`, or `stop_timeout_seconds` for Docker's stop grace. The one exception is
# container_run's `wait_seconds`, which also switches the wait on and would read as a limit on the container's
# run time if called `timeout_seconds` (AGENTS.md, naming convention).
_DURATION_NAMES = frozenset({"timeout_seconds", "stop_timeout_seconds"})
_DURATION_EXCEPTIONS = frozenset({("container_run", "wait_seconds")})


def test_every_duration_parameter_is_timeout_seconds_unless_listed():
    offenders = [
        f"{name}({param})"
        for name, func in _tool_functions()
        for param in inspect.signature(func).parameters
        if param.endswith("_seconds") and param not in _DURATION_NAMES and (name, param) not in _DURATION_EXCEPTIONS
    ]
    assert offenders == [], f"durations must be timeout_seconds, or be listed with a reason: {offenders}"


def test_every_duration_exception_is_still_a_parameter():
    params = {(name, param) for name, func in _tool_functions() for param in inspect.signature(func).parameters}
    assert _DURATION_EXCEPTIONS <= params, f"stale duration exceptions: {sorted(_DURATION_EXCEPTIONS - params)}"


# Canonical docstring lines for params shared across many tools: same param, same words.
# Keyed by param name -> the exact `args:` line text the description must START with (a tool may
# append a tool-specific clause after the canonical prefix). (tool, param) pairs in _PARAM_EXCEPTIONS
# opt out where the same param name deliberately carries a different meaning.
_CANONICAL_PARAM_PREFIXES = {
    "project_dir": "Dir with the compose file (default: server cwd",
    "files": "Explicit compose file paths (repeatable, `-f`",
    "labels": "Labels to set on the",
}
_PARAM_EXCEPTIONS = {
    ("buildx_bake", "files"),  # bake files are HCL/compose *bake* definitions, not compose files
}

# Google style: an entry under `Args:` is "    param: desc" or "    param (type): desc". The
# type form is accepted here deliberately, even though AGENTS.md forbids it in a tool docstring:
# this pattern is how the tests below *find* an entry, and a pattern that rejected the type would
# make a typed `host (str): ...` invisible to
# test_host_param_is_never_documented_in_docstrings, which would then pass while `host` was
# documented. The convention is enforced separately, by
# test_no_tool_repeats_a_parameter_type_in_its_docstring.
_ARG_LINE = re.compile(r"^\s+(?P<param>\w+)(?:\s*\([^)]*\))?: (?P<desc>.+)$")
_TYPED_ARG_LINE = re.compile(r"^\s+\*{0,2}\w+\s*\([^)]*\):")
# Any other section header ends the args block. `Returns:` and `Raises:` entries have the same
# shape as a parameter - "dict: ..." and "ToolInputError: ..." both match _ARG_LINE - so a scan
# that does not stop here reports a return type as a documented parameter.
_SECTION = re.compile(r"^\s*(Args|Returns|Raises|Yields|Note|Notes|Example|Examples):\s*$")


def _documented_params(func):
    """Yield the (param, description) pairs from a tool docstring's `Args:` section.

    Args:
        func: the tool function whose docstring to read.

    Yields:
        tuple: one (parameter name, description) per documented parameter.
    """
    in_args = False
    for line in (func.__doc__ or "").splitlines():
        header = _SECTION.match(line)
        if header:
            in_args = header.group(1) == "Args"
            continue
        if not in_args:
            continue
        match = _ARG_LINE.match(line)
        if match:
            yield match.group("param"), match.group("desc")


def test_shared_params_carry_canonical_descriptions():
    offenders = []
    for name, func in _tool_functions():
        for param, desc in _documented_params(func):
            canonical = _CANONICAL_PARAM_PREFIXES.get(param)
            if canonical is None or (name, param) in _PARAM_EXCEPTIONS:
                continue
            if not desc.startswith(canonical):
                offenders.append(f"{name}({param}): {desc!r} does not start with {canonical!r}")
    assert offenders == [], "shared-param description drift:\n" + "\n".join(offenders)


def test_no_tool_repeats_a_parameter_type_in_its_docstring():
    # The annotation already reaches the client in `inputSchema` alongside the description, so a
    # type in an `Args:` entry is duplication paid for on every session that loads the surface.
    # pydoclint cannot check this: its DOC101/DOC103 are suppressed per tool precisely because
    # the entries carry no types, so it would report a typed entry as documented and be happy.
    offenders = []
    for name, func in _tool_functions():
        in_args = False
        for line in (func.__doc__ or "").splitlines():
            header = _SECTION.match(line)
            if header:
                in_args = header.group(1) == "Args"
                continue
            if in_args and _TYPED_ARG_LINE.match(line):
                offenders.append(f"{name}: {line.strip()}")

    assert offenders == [], (
        "these tool `Args:` entries repeat a type the inputSchema already carries:\n  " + "\n  ".join(offenders)
    )


def test_host_param_is_never_documented_in_docstrings():
    # `host` is stripped from single-host schemas and gets an injected canonical description in
    # multi-host mode (_apply_host_schema) — a docstring line would duplicate or contradict that.
    offenders = [
        name for name, func in _tool_functions() if any(param == "host" for param, _ in _documented_params(func))
    ]
    assert offenders == [], f"tools documenting `host` in their docstring: {offenders}"

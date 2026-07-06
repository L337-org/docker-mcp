# Guards the 2.0 tool-naming convention so it can't drift as tools are added.
#
# The rule (see CLAUDE.md "Naming convention"): every tool is named
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

# Identifier params use exactly these spellings (see CLAUDE.md "Naming convention"):
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
        "node_spec",  # node_update takes `spec`
        "remote_name",  # plugin tools take `remote`
    }
)


def _tool_functions():
    """Yield (tool_name, function) for every registered tool, via the star-imported tools namespace."""
    for name in sorted(_seen_tool_names):
        func = getattr(docker_mcp.tools, name, None)
        if func is not None:
            yield name, func


def test_no_tool_uses_a_banned_parameter_spelling():
    offenders = [
        f"{name}({param})"
        for name, func in _tool_functions()
        for param in inspect.signature(func).parameters
        if param in _BANNED_PARAMS
    ]
    assert offenders == [], f"banned parameter spellings: {offenders}"


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

# Matches both the block form ("    param - desc") and the one-line form ("args: param - desc").
_ARG_LINE = re.compile(r"^\s*(?:args:\s*)?(?P<param>\w+) - (?P<desc>.+)$")


def _documented_params(func):
    """Parse `param - description` lines out of a tool docstring's args block."""
    for line in (func.__doc__ or "").splitlines():
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


def test_host_param_is_never_documented_in_docstrings():
    # `host` is stripped from single-host schemas and gets an injected canonical description in
    # multi-host mode (_apply_host_schema) — a docstring line would duplicate or contradict that.
    offenders = [
        name for name, func in _tool_functions() if any(param == "host" for param, _ in _documented_params(func))
    ]
    assert offenders == [], f"tools documenting `host` in their docstring: {offenders}"

# Guards the 2.0 tool-naming convention so it can't drift as tools are added.
#
# The rule (see CLAUDE.md "Naming convention"): every tool is named
# `<management-command>_<verb-or-noun>`, anchored to the docker CLI's management-command
# structure, with long-form verb vocabulary (list/remove/inspect — never ls/rm/get).

import re

import docker_mcp.tools  # noqa: F401 — imports every tool module so the registry is populated
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

# MCP server singleton plus the central tool-registration helper.
#
# Tool modules import `tool` from here (never `mcp` directly) and decorate with `@tool()`.
# That indirection lets one place own (a) the read-only / destructive classification of every
# tool, (b) the two env switches that decide what gets registered, and (c) the ToolAnnotations
# attached to each registered tool. `mcp` is still exported for `@mcp.prompt` / `@mcp.resource`.

import functools
import inspect
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, NoReturn, cast

import docker.errors
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations

import docker_mcp._hosts as _hosts
from docker_mcp._env import env_flag, read_env
from docker_mcp.exceptions import DockerMcpError, HostGuardError, RemoteFailureError, ToolInputError

mcp = MCPServer("docker-mcp-server")


class ToolCategory(Enum):
    """How a tool affects state - drives both ToolAnnotations and the read-only env switches."""

    READ_ONLY = "read_only"  # no state change: queries, log/data reads, scans
    MUTATING = "mutating"  # changes state but does not destroy data
    DESTRUCTIVE = "destructive"  # removes/destroys data, kills a process, or prunes


# Central classification of every @tool. Auditable in one place and consumed by both the
# read-only env switches (what to register) and the ToolAnnotations we attach. Adding a tool
# without an entry here makes `tests/test_server.py::test_every_registered_tool_is_classified`
# fail, so the taxonomy can't silently drift.
TOOL_CATEGORIES: dict[str, ToolCategory] = {
    # system
    "system_ping": ToolCategory.READ_ONLY,
    "system_version": ToolCategory.READ_ONLY,
    "system_info": ToolCategory.READ_ONLY,
    "system_df": ToolCategory.READ_ONLY,
    "system_events": ToolCategory.READ_ONLY,
    "host_list": ToolCategory.READ_ONLY,
    "system_login": ToolCategory.MUTATING,
    "system_logout": ToolCategory.MUTATING,
    "system_close": ToolCategory.MUTATING,
    "system_reconnect": ToolCategory.MUTATING,
    # containers
    "container_run": ToolCategory.MUTATING,
    "container_create": ToolCategory.MUTATING,
    "container_inspect": ToolCategory.READ_ONLY,
    "container_list": ToolCategory.READ_ONLY,
    "container_prune": ToolCategory.DESTRUCTIVE,
    "container_start": ToolCategory.MUTATING,
    "container_stop": ToolCategory.MUTATING,
    "container_restart": ToolCategory.MUTATING,
    "container_kill": ToolCategory.DESTRUCTIVE,
    "container_pause": ToolCategory.MUTATING,
    "container_unpause": ToolCategory.MUTATING,
    "container_remove": ToolCategory.DESTRUCTIVE,
    "container_logs": ToolCategory.READ_ONLY,
    "container_stats": ToolCategory.READ_ONLY,
    "container_top": ToolCategory.READ_ONLY,
    "container_exec": ToolCategory.MUTATING,
    "container_commit": ToolCategory.MUTATING,
    "container_diff": ToolCategory.READ_ONLY,
    "container_rename": ToolCategory.MUTATING,
    "container_update": ToolCategory.MUTATING,
    "container_wait": ToolCategory.READ_ONLY,
    "container_export": ToolCategory.MUTATING,  # can write a file on the server host (dest_path)
    "container_archive_get": ToolCategory.READ_ONLY,
    "container_archive_get_to_file": ToolCategory.MUTATING,  # writes a file on the server host
    "container_archive_put": ToolCategory.MUTATING,
    # images
    "image_build": ToolCategory.MUTATING,
    "image_inspect": ToolCategory.READ_ONLY,
    "image_registry_data": ToolCategory.READ_ONLY,
    "image_list": ToolCategory.READ_ONLY,
    "image_pull": ToolCategory.MUTATING,
    "image_push": ToolCategory.MUTATING,
    "image_remove": ToolCategory.DESTRUCTIVE,
    "image_search": ToolCategory.READ_ONLY,
    "image_prune": ToolCategory.DESTRUCTIVE,
    "image_prune_builds": ToolCategory.DESTRUCTIVE,
    "image_load": ToolCategory.MUTATING,
    "image_import": ToolCategory.MUTATING,
    "image_save": ToolCategory.MUTATING,  # can write a file on the server host (dest_path)
    "image_tag": ToolCategory.MUTATING,
    "image_history": ToolCategory.READ_ONLY,
    # networks
    "network_create": ToolCategory.MUTATING,
    "network_inspect": ToolCategory.READ_ONLY,
    "network_list": ToolCategory.READ_ONLY,
    "network_prune": ToolCategory.DESTRUCTIVE,
    "network_remove": ToolCategory.DESTRUCTIVE,
    "network_connect": ToolCategory.MUTATING,
    "network_disconnect": ToolCategory.MUTATING,
    # volumes
    "volume_create": ToolCategory.MUTATING,
    "volume_inspect": ToolCategory.READ_ONLY,
    "volume_list": ToolCategory.READ_ONLY,
    "volume_prune": ToolCategory.DESTRUCTIVE,
    "volume_remove": ToolCategory.DESTRUCTIVE,
    # configs
    "config_create": ToolCategory.MUTATING,
    "config_inspect": ToolCategory.READ_ONLY,
    "config_list": ToolCategory.READ_ONLY,
    "config_remove": ToolCategory.DESTRUCTIVE,
    # secrets
    "secret_create": ToolCategory.MUTATING,
    "secret_inspect": ToolCategory.READ_ONLY,
    "secret_list": ToolCategory.READ_ONLY,
    "secret_remove": ToolCategory.DESTRUCTIVE,
    # nodes
    "node_inspect": ToolCategory.READ_ONLY,
    "node_list": ToolCategory.READ_ONLY,
    "node_update": ToolCategory.MUTATING,
    "node_remove": ToolCategory.DESTRUCTIVE,
    "node_wait": ToolCategory.READ_ONLY,
    # services
    "service_create": ToolCategory.MUTATING,
    "service_inspect": ToolCategory.READ_ONLY,
    "service_list": ToolCategory.READ_ONLY,
    "service_update": ToolCategory.MUTATING,
    "service_remove": ToolCategory.DESTRUCTIVE,
    "service_ps": ToolCategory.READ_ONLY,
    "service_logs": ToolCategory.READ_ONLY,
    "service_scale": ToolCategory.MUTATING,
    "service_rollback": ToolCategory.MUTATING,
    "service_wait": ToolCategory.READ_ONLY,
    # swarm
    "swarm_init": ToolCategory.MUTATING,
    "swarm_join": ToolCategory.MUTATING,
    "swarm_leave": ToolCategory.DESTRUCTIVE,
    "swarm_update": ToolCategory.MUTATING,
    "swarm_inspect": ToolCategory.READ_ONLY,
    "swarm_unlock": ToolCategory.MUTATING,
    "swarm_unlock_key": ToolCategory.READ_ONLY,
    "swarm_join_tokens": ToolCategory.READ_ONLY,
    "swarm_task_list": ToolCategory.READ_ONLY,
    "swarm_task_inspect": ToolCategory.READ_ONLY,
    # plugins
    "plugin_create": ToolCategory.MUTATING,
    "plugin_inspect": ToolCategory.READ_ONLY,
    "plugin_install": ToolCategory.MUTATING,
    "plugin_privileges": ToolCategory.READ_ONLY,
    "plugin_push": ToolCategory.MUTATING,
    "plugin_list": ToolCategory.READ_ONLY,
    "plugin_configure": ToolCategory.MUTATING,
    "plugin_disable": ToolCategory.MUTATING,
    "plugin_enable": ToolCategory.MUTATING,
    "plugin_remove": ToolCategory.DESTRUCTIVE,
    "plugin_upgrade": ToolCategory.MUTATING,
    # compose
    "compose_up": ToolCategory.MUTATING,
    "compose_down": ToolCategory.DESTRUCTIVE,
    "compose_ps": ToolCategory.READ_ONLY,
    "compose_logs": ToolCategory.READ_ONLY,
    "compose_config": ToolCategory.READ_ONLY,
    "compose_build": ToolCategory.MUTATING,
    "compose_pull": ToolCategory.MUTATING,
    "compose_restart": ToolCategory.MUTATING,
    "compose_stop": ToolCategory.MUTATING,
    "compose_start": ToolCategory.MUTATING,
    "compose_run": ToolCategory.MUTATING,
    "compose_exec": ToolCategory.MUTATING,
    "compose_list": ToolCategory.READ_ONLY,
    "compose_images": ToolCategory.READ_ONLY,
    "compose_port": ToolCategory.READ_ONLY,
    "compose_wait": ToolCategory.READ_ONLY,
    "compose_top": ToolCategory.READ_ONLY,
    "compose_cp": ToolCategory.MUTATING,
    "compose_kill": ToolCategory.DESTRUCTIVE,
    "compose_pause": ToolCategory.MUTATING,
    "compose_unpause": ToolCategory.MUTATING,
    # stack (Compose-on-Swarm, CLI)
    "stack_deploy": ToolCategory.MUTATING,
    "stack_list": ToolCategory.READ_ONLY,
    "stack_ps": ToolCategory.READ_ONLY,
    "stack_services": ToolCategory.READ_ONLY,
    "stack_remove": ToolCategory.DESTRUCTIVE,
    # context
    "context_list": ToolCategory.READ_ONLY,
    "context_inspect": ToolCategory.READ_ONLY,
    "context_create": ToolCategory.MUTATING,
    "context_use": ToolCategory.MUTATING,
    "context_remove": ToolCategory.DESTRUCTIVE,
    # buildx
    "buildx_build": ToolCategory.MUTATING,
    "buildx_bake": ToolCategory.MUTATING,
    "buildx_imagetools_inspect": ToolCategory.READ_ONLY,
    "buildx_imagetools_create": ToolCategory.MUTATING,
    "buildx_list": ToolCategory.READ_ONLY,
    "buildx_history_list": ToolCategory.READ_ONLY,
    "buildx_history_inspect": ToolCategory.READ_ONLY,
    "buildx_inspect": ToolCategory.READ_ONLY,
    "buildx_du": ToolCategory.READ_ONLY,
    "buildx_prune": ToolCategory.DESTRUCTIVE,
    "buildx_create": ToolCategory.MUTATING,
    "buildx_use": ToolCategory.MUTATING,
    "buildx_remove": ToolCategory.DESTRUCTIVE,
    # scout
    "scout_cves": ToolCategory.READ_ONLY,
    "scout_quickview": ToolCategory.READ_ONLY,
    "scout_recommendations": ToolCategory.READ_ONLY,
    "scout_compare": ToolCategory.READ_ONLY,
    "scout_sbom": ToolCategory.READ_ONLY,
    # registry (HTTPS, no daemon)
    "registry_tags": ToolCategory.READ_ONLY,
    "registry_tag_wait": ToolCategory.READ_ONLY,
    "registry_manifest": ToolCategory.READ_ONLY,
    "registry_image_config": ToolCategory.READ_ONLY,
    "hub_tags": ToolCategory.READ_ONLY,
    "hub_repo_info": ToolCategory.READ_ONLY,
    "hub_rate_limit": ToolCategory.READ_ONLY,
    # docs and catalog (no domain - always registered, see _NO_DOMAIN_TOOLS)
    "docs_lookup": ToolCategory.READ_ONLY,
    "tool_list": ToolCategory.READ_ONLY,
}

# Destructive tools whose effect is idempotent - re-running has no additional effect (the targets
# are already gone). Surfaced via ToolAnnotations.idempotent_hint so clients can treat retries as safe.
_IDEMPOTENT_TOOLS = frozenset(
    {"container_prune", "image_prune", "image_prune_builds", "network_prune", "volume_prune", "buildx_prune"}
)

# The optional per-call parameter that selects which configured host a daemon-targeting tool acts on.
_HOST_PARAM = "host"

# Tools that take a `host` param but are client-connection control, not daemon writes: they're exempt
# from the "host required for writes" rule and the (ro)-host refusal (you must be able to close/reconnect
# a read-only host's client, and login/logout touch an in-process cache). The unknown-host check still
# applies to them. They are MUTATING in TOOL_CATEGORIES but never mutate daemon state.
_CONNECTION_CONTROL = frozenset({"system_close", "system_reconnect", "system_login", "system_logout"})


# Read-only env switches, evaluated once at import (registration time):
#   DOCKER_MCP_SERVER_READONLY       - register only READ_ONLY tools (a true read-only server).
#   DOCKER_MCP_SERVER_NO_DESTRUCTIVE - register everything except DESTRUCTIVE tools (a "no data loss" mode).
# READONLY is the stricter of the two and wins when both are set.
READONLY = env_flag("DOCKER_MCP_SERVER_READONLY")
NO_DESTRUCTIVE = env_flag("DOCKER_MCP_SERVER_NO_DESTRUCTIVE")


def _parse_domains(value: str | None) -> frozenset[str]:
    """Parse the comma-separated DOCKER_MCP_SERVER_DISABLE list into a normalized set of domain names."""
    return frozenset(part.strip().lower() for part in (value or "").split(",") if part.strip())


# Domain switch, orthogonal to the category switches above:
#   DOCKER_MCP_SERVER_DISABLE=swarm,plugins - skip every tool whose domain is listed, regardless of category.
# A tool's domain is its defining module under docker_mcp.tools (e.g. containers, compose, scout), so a
# user who never touches swarm can drop the whole swarm/services/nodes/configs/secrets surface from the
# tool list the client has to reason about. This filters *registration*, not classification - disabled
# tools still appear in the tool-catalog resource so the choice is auditable.
DISABLED_DOMAINS = _parse_domains(read_env("DOCKER_MCP_SERVER_DISABLE"))


@dataclass(frozen=True)
class ToolRecord:
    """What the `@tool()` decorator saw for one tool: its taxonomy and whether it actually registered."""

    name: str
    domain: str | None
    category: ToolCategory
    registered: bool
    # Captured at registration so a catalog query needs no reach-in to MCPServer's tool manager:
    # `summary` is the docstring's first line (the one-liner a briefing shows instead of a full
    # definition), `params` the declared parameter names (so "which tools take a host?" is answerable).
    summary: str = ""
    params: tuple[str, ...] = ()


# Every tool the `@tool()` decorator has processed this run, whether or not it was registered (the
# restrictive modes and domain switch skip registration). `_seen_tool_names` keeps the drift test's
# simple set comparison; `_tool_registry` carries the richer per-tool record the catalog resource renders.
_seen_tool_names: set[str] = set()
_tool_registry: dict[str, ToolRecord] = {}


@dataclass(frozen=True)
class PromptRecord:
    """What the `@prompt()` decorator saw for one prompt: its (optional) domain, whether it's gated to
    multi-host mode, and whether it actually registered."""

    name: str
    domain: str | None
    registered: bool
    multi_host: bool = False


# Prompts processed by `@prompt()` this run (registered or skipped by DOCKER_MCP_SERVER_DISABLE), plus the
# doc-resource section -> domain map that resources.py registers at import. Both let tool_catalog()
# report the prompts and doc sections a domain switch hides, so the non-tool surface is auditable too.
_prompt_registry: dict[str, PromptRecord] = {}
_resource_domains: dict[str, str] = {}


def register_resource_domains(section_to_domain: dict[str, str]) -> None:
    """Record which doc-resource sections belong to which domain (called by resources.py at import)."""
    _resource_domains.update(section_to_domain)


def is_domain_disabled(domain: str | None) -> bool:
    """True if a (non-None) domain is currently dropped by DOCKER_MCP_SERVER_DISABLE. Reads the live set, so
    it reflects test monkeypatching of DISABLED_DOMAINS (unlike the import-time tool/prompt gating)."""
    return domain is not None and domain in DISABLED_DOMAINS


# Tools with no domain at all - never gated by DOCKER_MCP_SERVER_DISABLE, since their value doesn't
# correspond to a specific Docker feature area being enabled/disabled. Mirrors `@prompt(domain=None)`'s
# identical "cross-cutting, always available" semantics for prompts (see `docs_lookup` in resources.py).
_NO_DOMAIN_TOOLS: frozenset[str] = frozenset({"docs_lookup", "tool_list"})


def _domain_for(func: Callable) -> str | None:
    """Derive a tool's domain from its defining module: docker_mcp.tools.containers -> 'containers'.

    Returns None for `_NO_DOMAIN_TOOLS` members, which then never get gated by
    DOCKER_MCP_SERVER_DISABLE (see `_domain_enabled`'s call sites).
    """
    if func.__name__ in _NO_DOMAIN_TOOLS:
        return None
    return (func.__module__ or "").rsplit(".", 1)[-1]


def _should_register(category: ToolCategory, *, readonly: bool, no_destructive: bool) -> bool:
    """Decide whether a tool of `category` is registered under the given category-switch state."""
    if readonly:
        return category is ToolCategory.READ_ONLY
    if no_destructive:
        return category is not ToolCategory.DESTRUCTIVE
    return True


def _domain_enabled(domain: str, disabled: frozenset[str]) -> bool:
    """Decide whether a tool's domain survives the DOCKER_MCP_SERVER_DISABLE switch."""
    return domain not in disabled


def _summary_for(func: Callable[..., Any]) -> str:
    """
    First line of a tool's docstring -- the one-liner a catalog row carries instead of the full text.

    The house docstring format puts a standalone summary sentence first, so the first non-empty line
    is the summary by construction. Returns "" for an undocumented tool rather than raising, since a
    missing summary should degrade the catalog row, not prevent registration.
    """
    for line in (func.__doc__ or "").strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def query_catalog(
    domain: str | None = None,
    category: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """
    Registered tools matching the given filters, as compact rows.

    Only registered tools are listed. A tool dropped by a read-only switch or a disabled domain is
    absent rather than present-and-flagged: advertising a capability the server will refuse leaks its
    existence and invites a bypass attempt. What each domain is *hiding* is still visible in
    aggregate through `hidden_by_configuration`, so the configuration stays auditable without naming
    the tools it removed.

    args:
        domain - Exact domain name to restrict to, or None for every domain
        category - Exact category value ("read_only"/"mutating"/"destructive"), or None for all
        keyword - Case-insensitive substring matched against name, summary and parameter names
    returns: dict - {"matched", "tools", "domains", "no_domain", "hidden_by_configuration", "switches",
                    "filters"}. `domains` and `hidden_by_configuration` are keyed by real domain
                    names only; `no_domain` counts the always-registered domain-less tools, whose
                    rows carry `domain: None` and which no `domain=` value selects.
    """
    wanted = keyword.lower() if keyword else None
    rows = []
    for record in sorted(_tool_registry.values(), key=lambda r: (r.domain or "", r.name)):
        if not record.registered:
            continue
        if domain is not None and record.domain != domain:
            continue
        if category is not None and record.category.value != category:
            continue
        if wanted is not None and not (
            wanted in record.name.lower()
            or wanted in record.summary.lower()
            or any(wanted in param.lower() for param in record.params)
        ):
            continue
        rows.append(
            {
                "name": record.name,
                "domain": record.domain,
                "category": record.category.value,
                "summary": record.summary,
            }
        )
    # Both maps are keyed by real domain names only, so every key is a value `domain=` accepts.
    # The `_NO_DOMAIN_TOOLS` are counted separately rather than under an empty-string key: "" is not
    # a domain, `domain=""` would match nothing (their rows carry `domain: None`), and offering it as
    # if it were selectable is the kind of near-miss that costs a caller a wasted call to discover.
    counts: dict[str, int] = {}
    no_domain = 0
    for record in _tool_registry.values():
        if not record.registered:
            continue
        if record.domain is None:
            no_domain += 1
        else:
            counts[record.domain] = counts.get(record.domain, 0) + 1
    hidden: dict[str, int] = {}
    for record in _tool_registry.values():
        # A domain-less tool cannot be hidden *by domain*; only a category switch could drop one, and
        # `switches` already reports that, so it would be misleading to attribute it to a domain here.
        if not record.registered and record.domain is not None:
            hidden[record.domain] = hidden.get(record.domain, 0) + 1
    return {
        "matched": len(rows),
        "tools": rows,
        # Always present, so a query matching nothing still shows what does exist to search instead.
        "domains": dict(sorted(counts.items())),
        "no_domain": no_domain,
        "hidden_by_configuration": dict(sorted(hidden.items())),
        "switches": {
            "DOCKER_MCP_SERVER_READONLY": READONLY,
            "DOCKER_MCP_SERVER_NO_DESTRUCTIVE": NO_DESTRUCTIVE,
            "DOCKER_MCP_SERVER_DISABLE": sorted(DISABLED_DOMAINS),
        },
        "filters": {"domain": domain, "category": category, "keyword": keyword},
    }


def tool_catalog() -> dict[str, Any]:
    """
    Snapshot of the tool surface: which tools exist, their domain/category, and what the active env
    switches registered. Drives the `docker-mcp://tool-catalog` resource so a client can see the blast
    radius of each tool - and which whole domains a server has disabled - before calling anything.
    """
    # `r.domain or ""` only affects sort order - the stored/reported domain stays None for the
    # handful of `_NO_DOMAIN_TOOLS` (e.g. docs_lookup), sorting before every named domain.
    records = sorted(_tool_registry.values(), key=lambda r: (r.domain or "", r.name))
    domains = sorted({r.domain for r in records}, key=lambda d: d or "")
    domain_summary = [
        {
            "domain": d,
            "total": sum(1 for r in records if r.domain == d),
            "registered": sum(1 for r in records if r.domain == d and r.registered),
        }
        for d in domains
    ]
    return {
        "switches": {
            "DOCKER_MCP_SERVER_READONLY": READONLY,
            "DOCKER_MCP_SERVER_NO_DESTRUCTIVE": NO_DESTRUCTIVE,
            "DOCKER_MCP_SERVER_DISABLE": sorted(DISABLED_DOMAINS),
        },
        # Disabled domains that match no known tool - usually a typo in DOCKER_MCP_SERVER_DISABLE.
        "unknown_disabled_domains": sorted(DISABLED_DOMAINS - set(domains)),
        "domains": domain_summary,
        "tools": [
            {"name": r.name, "domain": r.domain, "category": r.category.value, "registered": r.registered}
            for r in records
        ],
        # The non-tool surface DOCKER_MCP_SERVER_DISABLE also affects: prompts tied to a disabled domain are
        # skipped, and doc-resource sections for a disabled domain are hidden from docker-docs://contents.
        "prompts": [
            {"name": r.name, "domain": r.domain, "registered": r.registered, "multi_host": r.multi_host}
            for r in sorted(_prompt_registry.values(), key=lambda r: r.name)
        ],
        "disabled_doc_sections": sorted(s for s, d in _resource_domains.items() if d in DISABLED_DOMAINS),
    }


# One-line router blurb per tool domain (the module leaf), keyed in display order. The server's
# `instructions` string - pre-loaded into a client's context alongside the server name and tool names,
# *before* any per-tool schema - is built from these. For a lazy-loading client (e.g. Claude Code) that
# fetches tool schemas on demand, `instructions` is the main surface we control that's always in context,
# so it acts as a router: it maps user vocabulary onto the domain keyword a tool search will hit. It does
# not enumerate tools (that's the live `docker-mcp://tool-catalog` resource) - it's a map, not a manual.
# A domain's line is emitted only when that domain has a *registered* tool, so DOCKER_MCP_SERVER_DISABLE
# and the read-only switches never leave the router advertising a domain the client can't actually call.
_DOMAIN_BLURBS: dict[str, str] = {
    "containers": "run/create/start/stop/restart/kill/remove, logs, stats, top, exec, diff, commit, archive/cp, "
    "wait, rename",
    "images": "pull/push/build/tag/remove/save/load/history, search, prune (dangling images, build cache)",
    "networks": "create/connect/disconnect/inspect/remove",
    "volumes": "create/list/inspect/remove",
    "compose": "Docker Compose v2 (up/down/ps/logs/build/run/exec/...); CLI-backed",
    "stack": "Compose-on-Swarm (deploy/ls/ps/rm/services); CLI-backed",
    "swarm": "swarm init/join/leave/unlock, join-tokens, cluster-wide task list/inspect; manager node only",
    "services": "Swarm services (create/scale/update/rollback/logs/tasks); manager node only",
    "nodes": "Swarm nodes (list/inspect/update/remove); manager node only",
    "secrets": "Swarm secrets; manager node only",
    "configs": "Swarm configs; manager node only",
    "buildx": "multi-arch builds, imagetools (supersedes `docker manifest`), build history; CLI-backed",
    "scout": "CVE scan, SBOM, base-image recommendations; CLI-backed",
    "context": "docker CLI contexts; CLI-backed",
    "registry": "OCI registries + Docker Hub over HTTPS; no daemon needed",
    "plugins": "plugin lifecycle (create/install/push/enable/disable/configure/upgrade/remove)",
    "system": "ping, version, info, df (disk usage), events, login, logout, host_list (configured daemons)",
}

# CLI- and swarm-tied caveats are only worth emitting when the relevant domains actually registered.
_CLI_DOMAINS = ("compose", "stack", "buildx", "scout", "context")
# The CLI-backed domains that fall back to running on an ssh:// host when no local CLI/plugin is
# installed. `context` is absent deliberately and permanently: its tools manage *this* host's CLI
# context registry, which a remote host knows nothing about.
_REMOTE_EXEC_DOMAINS = ("compose", "stack", "buildx", "scout")
_SWARM_DOMAINS = ("swarm", "services", "nodes", "secrets", "configs")


def build_instructions(registered_domains: set[str] | None = None) -> str:
    """
    Render the server `instructions` router from the domains that actually registered tools.

    Pass `registered_domains` to render for an arbitrary set (tests); by default it reads the live
    `_tool_registry`, so the switches (DOCKER_MCP_SERVER_DISABLE / _READONLY / _NO_DESTRUCTIVE) are
    reflected - a domain with no registered tool contributes no line, so the router never points the
    client at tools that aren't there.
    """
    present = (
        registered_domains
        if registered_domains is not None
        else {r.domain for r in _tool_registry.values() if r.registered}
    )

    lines = [
        "docker-mcp-server - manage Docker through the docker-py SDK and the docker CLI.",
        "",
        "Tools load on demand: search by a domain keyword below to pull a tool's full schema before calling it.",
        "",
        "Domains (and the words that find them):",
    ]
    lines += [f"- {domain} - {blurb}" for domain, blurb in _DOMAIN_BLURBS.items() if domain in present]

    caveats = []
    if present & {"containers", "images"}:
        caveats.append(
            "To persist output to the host disk, pass `dest_path` to `container_export`/`image_save`, "
            "or use `container_archive_get_to_file` (prefer these over in-band bytes for anything large)."
        )
    if present & {"containers", "networks", "volumes", "services"}:
        caveats.append("`list_*(managed_only=True)` returns only resources this server created (provenance-labeled).")
    cli_present = [d for d in _CLI_DOMAINS if d in present]
    if cli_present:
        # Only worth the tokens when a domain that actually has the fallback is registered.
        fallback_present = [d for d in _REMOTE_EXEC_DOMAINS if d in present]
        if not fallback_present:
            caveats.append(
                f"CLI-backed domains ({', '.join(cli_present)}) shell out to the docker CLI/plugins; those "
                "calls raise if the CLI or a required plugin isn't installed."
            )
        else:
            # One statement of what happens when the local CLI can't serve a call, rather than a blanket
            # "raises" followed by a fallback that contradicts it. Every domain list here is an object
            # rather than a subject: the lists are whatever registered, so a subject would need its verb
            # to agree with a length that varies.
            no_fallback = [d for d in cli_present if d not in fallback_present]
            caveat = (
                f"CLI-backed domains ({', '.join(cli_present)}) shell out to the docker CLI/plugins. With "
                "the CLI or a required plugin missing locally, the call runs on the target host instead "
                "when that host is reached over `ssh://` - its CLI, its registry credentials, and local "
                "files (a compose project dir, a build context) copied over, so keep them small; a usable "
                f"local CLI always wins. Applies to {', '.join(fallback_present)}"
            )
            caveat += f"; no fallback for {', '.join(no_fallback)}, which raises instead." if no_fallback else "."
            caveats.append(caveat)
    if present & set(_SWARM_DOMAINS):
        caveats.append("Swarm-family tools require a swarm manager node.")
    if _hosts.is_multi():
        caveats.append(
            f"Multiple hosts are configured ({_hosts.labels()}): read-only tools take `host=<label>` "
            "(omit → the default, the first listed); mutating/destructive tools require an explicit "
            "`host`; a host marked `(ro)` rejects writes, and one marked `(nd)` rejects destructive "
            "calls only. See the `docker-mcp://hosts` resource."
        )
    if caveats:
        lines += ["", "Picking the right tool:"]
        lines += [f"- {c}" for c in caveats]

    lines += [
        "",
        "The registered surface changes with env switches; read the `docker-mcp://tool-catalog` resource for "
        "the live tool/domain/category list and which switches are active. Docs are under "
        "`docker-docs://contents`, or call `docs_lookup` if your client can't read resources. For "
        "multi-step jobs (deploy, troubleshoot, prune, audit, migrate, "
        "multi-arch build, volume backup/restore) prefer the matching MCP prompt.",
    ]
    return "\n".join(lines)


def finalize_instructions() -> None:
    """
    Set the server's `instructions` from the actually-registered surface - called once after every tool
    module has imported (docker_mcp/__init__.py), so the switch-dependent registration is already known.

    MCPServer.instructions is a read-only property backed by the low-level server's `instructions`, which
    is read at run() time (create_initialization_options), so writing it through here after registration
    propagates to the MCP initialize handshake. Reaching into `_lowlevel_server` is guarded the same way as
    the schema-title strip below: an MCPServer refactor degrades to "instructions stay unset" rather than
    raising.
    """
    try:
        mcp._lowlevel_server.instructions = build_instructions()
    except AttributeError:
        pass


# Acronyms that a naive title-case of a snake_case tool name gets wrong (e.g. "scout_cves" ->
# "Scout Cves"). Keyed by the title-cased word so `_title_for` can substitute in place.
_TITLE_ACRONYMS: dict[str, str] = {"Cves": "CVEs", "Sbom": "SBOM"}


def _title_for(name: str) -> str:
    """Human-readable display title for a tool, mechanically derived from its snake_case name
    (e.g. "container_list" -> "Container List") so every tool has one without hand-authoring ~150
    of them. Distinct from the schema `title` `_slim_schema` strips - this is the ToolAnnotations
    field some directories (e.g. the Claude Connectors Directory) require independent of prose."""
    words = name.replace("_", " ").title().split(" ")
    return " ".join(_TITLE_ACRONYMS.get(word, word) for word in words)


def _annotations_for(name: str, category: ToolCategory) -> ToolAnnotations:
    """Build the ToolAnnotations a client uses to auto-allow reads and gate destructive calls."""
    return ToolAnnotations(
        title=_title_for(name),
        read_only_hint=category is ToolCategory.READ_ONLY,
        destructive_hint=category is ToolCategory.DESTRUCTIVE,
        idempotent_hint=True if name in _IDEMPOTENT_TOOLS else None,
    )


# JSON Schema keywords whose value is a {name: subschema-or-other} map - their keys are caller-supplied
# names (a property literally named "title", a $def called "title"), NOT schema keywords, so we must
# recurse into the values without ever treating those keys as a title annotation to drop. Covers the
# full set across draft-07 / 2019-09 / 2020-12 so a future pydantic emitting any of them stays safe.
_SCHEMA_NAME_MAPS = frozenset(
    {
        "properties",
        "$defs",
        "definitions",
        "patternProperties",
        "dependentSchemas",
        "dependencies",
        "dependentRequired",
    }
)


def _slim_schema(node: Any) -> None:
    """
    Recursively slim a JSON Schema in place, dropping annotations the client already has (or that
    only restate a default). All three transforms are display-only - call-time validation runs off
    the tool's separate `fn_metadata`, so none changes behavior - and were measured to be
    information-free, together ~18% of the advertised schema tokens:

    - **`title`** (~10%): pydantic stamps one on every property/`$def` (the title-cased field name,
      e.g. `cache_from` -> "Cache From") plus a top-level `<tool>Arguments` title - it duplicates the
      property name.
    - **nullable `anyOf`** (~7%): an `X | None` param emits `anyOf: [<X>, {"type": "null"}]`; the null
      branch is redundant with the field's optionality (absence from `required` + its `default`), so
      drop it - hoisting the sole remaining branch, or keeping a multi-branch `anyOf` minus the null.
      Gated on a sibling `default` so a (hypothetical) required nullable with no default is never
      collapsed to look non-nullable.
    - **`additionalProperties: true`** (~1%): the JSON Schema default - an explicit `true` says nothing
      an omitted key wouldn't. A *schema-valued* `additionalProperties` (e.g. `dict[str, str]`) is
      meaningful and kept.

    `tests/test_server.py` asserts none of the three survive on any registered tool.
    """
    if isinstance(node, dict):
        node.pop("title", None)
        any_of = node.get("anyOf")
        if isinstance(any_of, list) and {"type": "null"} in any_of and "default" in node:
            non_null = [sub for sub in any_of if sub != {"type": "null"}]
            if len(non_null) == 1:
                # Sole remaining branch: hoist its keys onto this node (setdefault never clobbers an
                # existing sibling like `default`), then drop the now-empty anyOf.
                node.pop("anyOf")
                for key, value in non_null[0].items():
                    node.setdefault(key, value)
            else:
                node["anyOf"] = non_null
        # After the anyOf hoist, so a hoisted `additionalProperties: true` (the null branch of a
        # `dict[str, Any] | None` param lives in anyOf[0]) is also caught.
        if node.get("additionalProperties") is True:
            node.pop("additionalProperties")
        for key, value in node.items():
            if key in _SCHEMA_NAME_MAPS and isinstance(value, dict):
                for subschema in value.values():
                    _slim_schema(subschema)
            else:
                _slim_schema(value)
    elif isinstance(node, list):
        for item in node:
            _slim_schema(item)


def _has_host_param(func: Callable) -> bool:
    """A tool is daemon-targeting iff its signature declares the `host` param (registry/hub/context
    tools and host_list don't, so they're untouched by the host machinery)."""
    return _HOST_PARAM in inspect.signature(func).parameters


def _is_host_write(name: str, category: ToolCategory) -> bool:
    """A host-targeting *write*: a MUTATING/DESTRUCTIVE tool that is not connection-control. These
    require an explicit host (multi-host) and refuse an (ro) host; everything else may default."""
    return category in (ToolCategory.MUTATING, ToolCategory.DESTRUCTIVE) and name not in _CONNECTION_CONTROL


def _is_host_destructive(name: str, category: ToolCategory) -> bool:
    """A host-targeting *destructive* call: a DESTRUCTIVE tool that is not connection-control. This
    is what the per-host (nd) marker blocks, while still allowing READ_ONLY/MUTATING calls."""
    return category is ToolCategory.DESTRUCTIVE and name not in _CONNECTION_CONTROL


def _host_param_description(name: str, category: ToolCategory) -> str:
    """The advertised `host` description in multi-host mode - the enum carries the valid labels."""
    if _is_host_write(name, category):
        return "Target host label (required when multiple hosts are configured)."
    return f"Target host label; omit to use the default ({_hosts.default().label!r})."


def _raise_read_only(name: str, label: str, category: ToolCategory) -> NoReturn:
    """Refuse a write to a host carrying the per-host (ro) marker (distinct from the
    DOCKER_MCP_SERVER_READONLY switch, which drops write tools from the surface entirely)."""
    raise HostGuardError(
        f"{name}: host {label!r} is read-only (configured with the (ro) marker); refusing this "
        f"{category.value} operation. For a fully read-only server use DOCKER_MCP_SERVER_READONLY."
    )


def _raise_non_destructive(name: str, label: str, category: ToolCategory) -> NoReturn:
    """Refuse a DESTRUCTIVE call to a host carrying the per-host (nd) marker (distinct from the
    DOCKER_MCP_SERVER_NO_DESTRUCTIVE switch, which drops destructive tools from the surface entirely)."""
    raise HostGuardError(
        f"{name}: host {label!r} is non-destructive (configured with the (nd) marker); refusing this "
        f"{category.value} operation. For a fully non-destructive server use DOCKER_MCP_SERVER_NO_DESTRUCTIVE."
    )


def _enforce_host_guard(name: str, category: ToolCategory, host: str | None) -> None:
    """
    Central call-time guard for a daemon-targeting tool. Wired whenever there is something to enforce:
    multiple hosts (host selection + per-host (ro)/(nd) refusal) or a single host flagged (ro) or (nd).
    Raises when a write omits `host` in multi-host mode, when `host` is not a configured label, when a
    write targets an (ro) host, or when a destructive call targets an (nd) host. A host carrying both
    markers is refused by the (ro) check first - (ro) is strictly stronger, so (nd) never fires for it.
    Read-only and connection-control tools may omit `host` (None -> default / all).
    """
    known = _hosts.labels()
    write = _is_host_write(name, category)
    destructive = _is_host_destructive(name, category)
    if host is None:
        # Multi-host: a write must name its target. Single-host: the schema carries no host param to
        # pass, but an (ro)/(nd) default host must still refuse writes/destructive calls.
        if write and _hosts.is_multi():
            raise HostGuardError(
                f"{name}: 'host' is required when multiple hosts are configured; choose one of {known}."
            )
        if write and _hosts.is_read_only():
            _raise_read_only(name, _hosts.default().label, category)
        if destructive and _hosts.is_non_destructive():
            _raise_non_destructive(name, _hosts.default().label, category)
        return
    if host not in known:
        raise HostGuardError(f"{name}: unknown host {host!r}; configured hosts: {known}.")
    if write and _hosts.is_read_only(host):
        _raise_read_only(name, host, category)
    if destructive and _hosts.is_non_destructive(host):
        _raise_non_destructive(name, host, category)


def _apply_host_schema(parameters: Any, name: str, category: ToolCategory) -> None:
    """
    Display-only surgery on a daemon-targeting tool's advertised `host` property (run after _slim_schema;
    call-time validation runs off the separate fn_metadata, so this never changes behavior).

    Single-host mode: drop `host` entirely so the schema is byte-for-byte today's (footprint-neutral).
    Multi-host mode: constrain `host` to an `enum` of the configured labels with a generated description,
    and for writes mark it required (advisory - the guard is the teeth) by adding it to `required` and
    dropping its default.
    """
    if not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or _HOST_PARAM not in properties:
        return
    if not _hosts.is_multi():
        del properties[_HOST_PARAM]
        required = parameters.get("required")
        if isinstance(required, list) and _HOST_PARAM in required:
            required.remove(_HOST_PARAM)
            if not required:
                parameters.pop("required", None)
        return
    host_schema = properties[_HOST_PARAM]
    if not isinstance(host_schema, dict):
        return
    host_schema["enum"] = _hosts.labels()
    host_schema["description"] = _host_param_description(name, category)
    if _is_host_write(name, category):
        host_schema.pop("default", None)
        required = parameters.setdefault("required", [])
        if isinstance(required, list) and _HOST_PARAM not in required:
            required.append(_HOST_PARAM)


def _host_guard_needed() -> bool:
    """Whether daemon-targeting tools need the call-time host guard wrapped on. Two cases: multiple hosts
    (host selection + per-host (ro)/(nd) refusal), or a single host flagged (ro) or (nd) (refuse
    writes/destructive calls even though the schema carries no host param). A single unrestricted host
    needs no guard - today's footprint-neutral path."""
    return _hosts.is_multi() or _hosts.is_read_only() or _hosts.is_non_destructive()


# The `[F: Callable[..., Any]]` type parameter on this and on `tool` below is what lets a decorator
# say "returns exactly what it was given". The bare `Callable` these used to return carries no
# parameter list, which silently disabled argument checking for every call to every decorated tool,
# in the tests as well as at internal call sites.
def _wrap_with_host_guard[F: Callable[..., Any]](func: F, name: str, category: ToolCategory) -> F:
    """Wrap a daemon-targeting tool so the host guard runs before it (when `_host_guard_needed()` -
    multi-host, or a single host flagged (ro) or (nd)). Preserves the signature so MCPServer builds
    the same schema/fn_metadata, and matches the func's sync/async-ness."""
    signature = inspect.signature(func)

    def _host_of(args: tuple, kwargs: dict) -> str | None:
        try:
            bound = signature.bind_partial(*args, **kwargs)
        except TypeError:
            return kwargs.get(_HOST_PARAM)
        return bound.arguments.get(_HOST_PARAM)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            _enforce_host_guard(name, category, _host_of(args, kwargs))
            return await func(*args, **kwargs)

        async_wrapper.__signature__ = signature  # pyright: ignore[reportAttributeAccessIssue]
        # cast because functools.wraps is opaque to the type checker: it produces a _Wrapped[...],
        # not the wrapped function's own type. The claim being made is true at run time -- the
        # signature assigned just above is the original's, which is exactly what callers and
        # MCPServer's schema builder see.
        return cast(F, async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _enforce_host_guard(name, category, _host_of(args, kwargs))
        return func(*args, **kwargs)

    wrapper.__signature__ = signature  # pyright: ignore[reportAttributeAccessIssue]
    return cast(F, wrapper)  # see the note on the async branch


# Set on every wrapper `_translate_failures` builds. `tests/test_server.py` walks the built server
# and fails any registered tool or resource whose callable lacks it, so a registration that bypasses
# the registrars is caught wherever it is written, including in a module that does not exist yet.
TRANSLATES_FAILURES = "docker_mcp_translates_failures"


# Failures raised by a library rather than by this code, and the project type whose meaning each
# one carries. Converting them here rather than at the ~106 call sites that touch the SDK is the
# whole point: a client only ever sees a message when the exception is a `ToolError`/`ResourceError`,
# and before this table the daemon's own "No such container: x", a registry's 401, and a CLI timeout
# all reached the model as `Error executing tool <name>` with the text withheld and a traceback
# logged at ERROR - the same failure the project-exception translation was written to prevent,
# left standing for the exceptions this server does not raise itself.
#
# Order matters and is load-bearing: `NotFound` subclasses `APIError` subclasses `DockerException`,
# so the narrower entry has to come first or every missing container reads as a daemon failure. That
# is the only place a narrow entry earns its keep - everywhere else the base is listed deliberately,
# because a leaf is only ever the one you happened to think of. `HTTPStatusError` was listed here
# once and every httpx timeout and connection failure went out generic as a result.
#
# So: when adding to this table, name the base of the family unless a subclass genuinely maps to a
# different project type, and put that subclass first when it does.
#
# `docker.errors.APIError` is also an `OSError` (via `requests`), so do not add `OSError` here
# expecting it to mean "a local file or socket problem": it would capture every daemon error too.
_LIBRARY_FAILURES: tuple[tuple[type[BaseException], type[DockerMcpError]], ...] = (
    # The caller named something the daemon does not have: an argument they can correct.
    (docker.errors.NotFound, ToolInputError),
    # Anything else the daemon or the SDK reports. Its own text is the useful part, so it travels.
    (docker.errors.DockerException, RemoteFailureError),
    # A URL that cannot be formed at all, which here means a caller-supplied registry or reference
    # that is not usable in one - a fixable argument, and not an `HTTPError` at all (it derives
    # straight from `Exception`), so it needs its own entry rather than riding the family below.
    (httpx.InvalidURL, ToolInputError),
    # Anything httpx reports: a 4xx/5xx from `raise_for_status()`, and equally a connection refused,
    # a DNS failure or a timeout, which never reach that call at all. The base rather than
    # `HTTPStatusError`, because both mean the same thing to a caller and naming the narrower one
    # left every transport failure arriving generic.
    (httpx.HTTPError, RemoteFailureError),
    # A CLI call that outran its bound. `run_docker` checks `returncode` itself rather than passing
    # `check=True`, so `TimeoutExpired` is the only member reached today - the base is named anyway,
    # for the same reason as above: the leaf is the one you happen to think of while reading the
    # code that raises it.
    (subprocess.SubprocessError, RemoteFailureError),
)

_LIBRARY_FAILURE_TYPES: tuple[type[BaseException], ...] = tuple(exc for exc, _ in _LIBRARY_FAILURES)


def _as_project_failure(exc: BaseException) -> DockerMcpError:
    """The project exception a library failure corresponds to, carrying the library's own message.

    Only called for the types in `_LIBRARY_FAILURE_TYPES`, which is derived from the table above, so
    the fallback cannot be reached by any edit to the table: adding an entry adds it to both. It
    exists for the caller that invokes this directly with something outside the table, and names the
    class in its message rather than silently classifying it as something it is not.
    """
    for library_type, project_type in _LIBRARY_FAILURES:
        if isinstance(exc, library_type):
            return project_type(str(exc))
    return DockerMcpError(f"{type(exc).__name__}: {exc}")


def _translate_failures[F: Callable[..., Any]](func: F, error_cls: type[ToolError] | type[ResourceError]) -> F:
    """Re-raise a `DockerMcpError` as the SDK error whose message the client is allowed to see.

    The SDK classifies a failure by type: `ToolError`/`ResourceError` keep their message and log at
    INFO, and everything else is a crash - the client gets `Error executing tool <name>` or `Error
    reading resource <uri>`, the original text is withheld, and a traceback is logged at ERROR. So a
    refusal raised as a bare `RuntimeError` reaches the model saying nothing it can act on.

    Two families are translated and nothing else. `DockerMcpError` - what this code raises
    deliberately. And the named library failures in `_LIBRARY_FAILURES`, because a daemon rejection,
    a registry status or a CLI timeout is equally deliberate from the caller's side and its text is
    equally useful; leaving them out meant the majority of real failures still arrived generic.
    Never `Exception`: a bug dressed as a deliberate refusal loses its traceback and puts internal
    text on the wire, which is what the SDK's rule exists to prevent.

    Preserves the signature (the SDK builds the input schema from it) and the sync/async-ness (the
    SDK decides whether to await by asking `is_async_callable`), the same way `_wrap_with_host_guard`
    does and for the same reasons.
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except DockerMcpError as exc:
                raise error_cls(str(exc)) from exc
            except _LIBRARY_FAILURE_TYPES as exc:
                raise error_cls(str(_as_project_failure(exc))) from exc

        async_wrapper.__signature__ = inspect.signature(func)  # pyright: ignore[reportAttributeAccessIssue]
        # After functools.wraps, which copies the wrapped function's __dict__ and would drop this.
        setattr(async_wrapper, TRANSLATES_FAILURES, True)
        # cast for the same reason as _wrap_with_host_guard: functools.wraps is opaque to the type
        # checker, and the signature assigned above is the original's.
        return cast(F, async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except DockerMcpError as exc:
            raise error_cls(str(exc)) from exc
        except _LIBRARY_FAILURE_TYPES as exc:
            raise error_cls(str(_as_project_failure(exc))) from exc

    wrapper.__signature__ = inspect.signature(func)  # pyright: ignore[reportAttributeAccessIssue]
    setattr(wrapper, TRANSLATES_FAILURES, True)
    return cast(F, wrapper)  # see the note on the async branch


def tool[F: Callable[..., Any]](**kwargs: Any) -> Callable[[F], F]:
    """
    Register an @mcp.tool with central classification - the drop-in `@tool()` every tool module uses.

    The tool's category comes from TOOL_CATEGORIES (defaulting to MUTATING, the safe assumption, for
    anything unclassified) and its domain from the defining module. We skip registration when a
    read-only env switch forbids the category or DOCKER_MCP_SERVER_DISABLE drops the domain, and otherwise
    attach the matching ToolAnnotations.
    """

    def decorator(func: F) -> F:
        name = func.__name__
        domain = _domain_for(func)
        category = TOOL_CATEGORIES.get(name, ToolCategory.MUTATING)
        registered = _should_register(category, readonly=READONLY, no_destructive=NO_DESTRUCTIVE) and (
            domain is None or _domain_enabled(domain, DISABLED_DOMAINS)
        )
        _seen_tool_names.add(name)
        _tool_registry[name] = ToolRecord(
            name=name,
            domain=domain,
            category=category,
            registered=registered,
            summary=_summary_for(func),
            params=tuple(inspect.signature(func).parameters),
        )
        if not registered:
            return func
        # Daemon-targeting tools (those declaring a `host` param) get a call-time host guard when there's
        # something to enforce - multiple hosts, or a single host flagged (ro); wrap before registering so
        # MCPServer builds the schema from the wrapper, whose signature mirrors the original. A single
        # writable host (and host-agnostic tools) register func unchanged.
        target = func
        if _has_host_param(func) and _host_guard_needed():
            target = _wrap_with_host_guard(func, name, category)
        # Register the translated wrapper, but leave `target` - the untranslated one - as what this
        # decorator returns. The translation is a wire concern: a client must be told why a call was
        # refused, while an internal caller (another tool, or a test) wants the project type it can
        # branch on. `mcp.tool()` returns its argument unchanged, so registering one callable and
        # returning the other is the whole of the difference. Translating outermost carries a
        # host-guard refusal across as well as one raised by the tool body.
        mcp.tool(annotations=_annotations_for(name, category), **kwargs)(_translate_failures(target, ToolError))
        # Slim the advertised input schema (drop information-free titles, nullable-anyOf null branches,
        # and redundant `additionalProperties: true`), then apply the host-param surgery (enum + required
        # in multi-host, or strip it in single-host). Both reach into MCPServer internals
        # (`_tool_manager.get_tool(...).parameters`); guard it so a future MCPServer refactor degrades to
        # "schema not slimmed" (a test catches that) rather than crashing the server at import time.
        try:
            registered_tool = mcp._tool_manager.get_tool(kwargs.get("name") or name)
        except AttributeError, KeyError:
            registered_tool = None
        parameters = registered_tool.parameters if registered_tool is not None else None
        if isinstance(parameters, dict):
            _slim_schema(parameters)
            _apply_host_schema(parameters, name, category)
        return target

    return decorator


def resource[F: Callable[..., Any]](uri: str, **kwargs: Any) -> Callable[[F], F]:
    """
    Register an `@mcp.resource` whose anticipated failures keep their message - the `@resource()`
    every resource registration uses, decorator or call form.

    The SDK applies the same type rule to a read as to a tool call: a `ResourceError` keeps its
    message and logs at INFO, and anything else becomes `Error reading resource <uri>` with the text
    withheld and a traceback logged at ERROR. Without the translation an unusable path, a disabled
    domain and a bug in this server are indistinguishable to a client, which is a poor answer for a
    URI it attached as context.

    Like `@tool()`, the translated wrapper is registered while the plain function is returned, so an
    internal caller still gets the project type. Applies to a template as well as a static resource:
    the SDK routes both through `read_resource`, and a template's own creation failure is classified
    the same way.

    args:
        uri - the resource URI or URI template, passed straight to `mcp.resource`
        kwargs - passed to `mcp.resource` (name, title, description, mime_type, ...)
    returns: Callable - a decorator registering the function as a resource
    """

    def decorator(func: F) -> F:
        mcp.resource(uri, **kwargs)(_translate_failures(func, ResourceError))
        return func

    return decorator


def prompt(description: str, *, domain: str | None = None, multi_host: bool = False) -> Callable[[Callable], Callable]:
    """
    Register an `@mcp.prompt`, honoring DOCKER_MCP_SERVER_DISABLE - the `@prompt()` every prompt module uses.

    A prompt tied to a feature area (`domain`) is skipped when that domain is disabled, so a server that
    drops e.g. `scout` doesn't keep prompts that steer the agent toward tools that are no longer
    registered. `domain=None` is for general / cross-domain prompts (doc lookup, prune, disk usage) that
    always register. A `multi_host=True` prompt registers only when 2+ hosts are configured (via
    DOCKER_MCP_SERVER_HOSTS), so a multi-host workflow prompt stays hidden in the common single-host case
    - the prompt-side parallel of the per-tool host param. Gating happens at import like `@tool()`, and
    the choice is recorded for tool_catalog().
    """

    def decorator(func: Callable) -> Callable:
        registered = (domain is None or _domain_enabled(domain, DISABLED_DOMAINS)) and (
            not multi_host or _hosts.is_multi()
        )
        _prompt_registry[func.__name__] = PromptRecord(
            name=func.__name__, domain=domain, registered=registered, multi_host=multi_host
        )
        if not registered:
            return func
        return mcp.prompt(description=description)(func)

    return decorator

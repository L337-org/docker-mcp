# MCP server singleton plus the central tool-registration helper.
#
# Tool modules import `tool` from here (never `mcp` directly) and decorate with `@tool()`.
# That indirection lets one place own (a) the read-only / destructive classification of every
# tool, (b) the two env switches that decide what gets registered, and (c) the ToolAnnotations
# attached to each registered tool. `mcp` is still exported for `@mcp.prompt` / `@mcp.resource`.

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, NoReturn

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import docker_mcp._hosts as _hosts
from docker_mcp._env import env_flag, read_env

mcp = FastMCP("docker-mcp-server")


class ToolCategory(Enum):
    """How a tool affects state — drives both ToolAnnotations and the read-only env switches."""

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
    "image_load": ToolCategory.MUTATING,
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
    # swarm
    "swarm_init": ToolCategory.MUTATING,
    "swarm_join": ToolCategory.MUTATING,
    "swarm_leave": ToolCategory.DESTRUCTIVE,
    "swarm_update": ToolCategory.MUTATING,
    "swarm_inspect": ToolCategory.READ_ONLY,
    "swarm_unlock": ToolCategory.MUTATING,
    "swarm_unlock_key": ToolCategory.READ_ONLY,
    "swarm_join_tokens": ToolCategory.READ_ONLY,
    # plugins
    "plugin_inspect": ToolCategory.READ_ONLY,
    "plugin_install": ToolCategory.MUTATING,
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
    "registry_manifest": ToolCategory.READ_ONLY,
    "registry_image_config": ToolCategory.READ_ONLY,
    "hub_tags": ToolCategory.READ_ONLY,
    "hub_repo_info": ToolCategory.READ_ONLY,
    "hub_rate_limit": ToolCategory.READ_ONLY,
}

# Destructive tools whose effect is idempotent — re-running has no additional effect (the targets
# are already gone). Surfaced via ToolAnnotations.idempotentHint so clients can treat retries as safe.
_IDEMPOTENT_TOOLS = frozenset({"container_prune", "image_prune", "network_prune", "volume_prune", "buildx_prune"})

# The optional per-call parameter that selects which configured host a daemon-targeting tool acts on.
_HOST_PARAM = "host"

# Tools that take a `host` param but are client-connection control, not daemon writes: they're exempt
# from the "host required for writes" rule and the (ro)-host refusal (you must be able to close/reconnect
# a read-only host's client, and login/logout touch an in-process cache). The unknown-host check still
# applies to them. They are MUTATING in TOOL_CATEGORIES but never mutate daemon state.
_CONNECTION_CONTROL = frozenset({"system_close", "system_reconnect", "system_login", "system_logout"})


# Read-only env switches, evaluated once at import (registration time):
#   DOCKER_MCP_SERVER_READONLY       — register only READ_ONLY tools (a true read-only server).
#   DOCKER_MCP_SERVER_NO_DESTRUCTIVE — register everything except DESTRUCTIVE tools (a "no data loss" mode).
# READONLY is the stricter of the two and wins when both are set.
READONLY = env_flag("DOCKER_MCP_SERVER_READONLY")
NO_DESTRUCTIVE = env_flag("DOCKER_MCP_SERVER_NO_DESTRUCTIVE")


def _parse_domains(value: str | None) -> frozenset[str]:
    """Parse the comma-separated DOCKER_MCP_SERVER_DISABLE list into a normalized set of domain names."""
    return frozenset(part.strip().lower() for part in (value or "").split(",") if part.strip())


# Domain switch, orthogonal to the category switches above:
#   DOCKER_MCP_SERVER_DISABLE=swarm,plugins — skip every tool whose domain is listed, regardless of category.
# A tool's domain is its defining module under docker_mcp.tools (e.g. containers, compose, scout), so a
# user who never touches swarm can drop the whole swarm/services/nodes/configs/secrets surface from the
# tool list the client has to reason about. This filters *registration*, not classification — disabled
# tools still appear in the tool-catalog resource so the choice is auditable.
DISABLED_DOMAINS = _parse_domains(read_env("DOCKER_MCP_SERVER_DISABLE"))


@dataclass(frozen=True)
class ToolRecord:
    """What the `@tool()` decorator saw for one tool: its taxonomy and whether it actually registered."""

    name: str
    domain: str
    category: ToolCategory
    registered: bool


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


def _domain_for(func: Callable) -> str:
    """Derive a tool's domain from its defining module: docker_mcp.tools.containers -> 'containers'."""
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


def tool_catalog() -> dict[str, Any]:
    """
    Snapshot of the tool surface: which tools exist, their domain/category, and what the active env
    switches registered. Drives the `docker-mcp://tool-catalog` resource so a client can see the blast
    radius of each tool — and which whole domains a server has disabled — before calling anything.
    """
    records = sorted(_tool_registry.values(), key=lambda r: (r.domain, r.name))
    domains = sorted({r.domain for r in records})
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
        # Disabled domains that match no known tool — usually a typo in DOCKER_MCP_SERVER_DISABLE.
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
# `instructions` string — pre-loaded into a client's context alongside the server name and tool names,
# *before* any per-tool schema — is built from these. For a lazy-loading client (e.g. Claude Code) that
# fetches tool schemas on demand, `instructions` is the main surface we control that's always in context,
# so it acts as a router: it maps user vocabulary onto the domain keyword a tool search will hit. It does
# not enumerate tools (that's the live `docker-mcp://tool-catalog` resource) — it's a map, not a manual.
# A domain's line is emitted only when that domain has a *registered* tool, so DOCKER_MCP_SERVER_DISABLE
# and the read-only switches never leave the router advertising a domain the client can't actually call.
_DOMAIN_BLURBS: dict[str, str] = {
    "containers": "run/create/start/stop/restart/kill/remove, logs, stats, top, exec, diff, commit, archive/cp, "
    "wait, rename",
    "images": "pull/push/build/tag/remove/save/load/history, search",
    "networks": "create/connect/disconnect/inspect/remove",
    "volumes": "create/list/inspect/remove",
    "compose": "Docker Compose v2 (up/down/ps/logs/build/run/exec/...); CLI-backed",
    "stack": "Compose-on-Swarm (deploy/ls/ps/rm/services); CLI-backed",
    "swarm": "swarm init/join/leave/unlock, join-tokens; manager node only",
    "services": "Swarm services (create/scale/update/rollback/logs/tasks); manager node only",
    "nodes": "Swarm nodes (list/inspect/update/remove); manager node only",
    "secrets": "Swarm secrets; manager node only",
    "configs": "Swarm configs; manager node only",
    "buildx": "multi-arch builds, imagetools (supersedes `docker manifest`), build history; CLI-backed",
    "scout": "CVE scan, SBOM, base-image recommendations; CLI-backed",
    "context": "docker CLI contexts; CLI-backed",
    "registry": "OCI registries + Docker Hub over HTTPS; no daemon needed",
    "plugins": "plugin lifecycle (install/enable/disable/configure/upgrade/remove)",
    "system": "ping, version, info, df (disk usage), events, login, logout, host_list (configured daemons)",
}

# CLI- and swarm-tied caveats are only worth emitting when the relevant domains actually registered.
_CLI_DOMAINS = ("compose", "stack", "buildx", "scout", "context")
_SWARM_DOMAINS = ("swarm", "services", "nodes", "secrets", "configs")


def build_instructions(registered_domains: set[str] | None = None) -> str:
    """
    Render the server `instructions` router from the domains that actually registered tools.

    Pass `registered_domains` to render for an arbitrary set (tests); by default it reads the live
    `_tool_registry`, so the switches (DOCKER_MCP_SERVER_DISABLE / _READONLY / _NO_DESTRUCTIVE) are
    reflected — a domain with no registered tool contributes no line, so the router never points the
    client at tools that aren't there.
    """
    present = (
        registered_domains
        if registered_domains is not None
        else {r.domain for r in _tool_registry.values() if r.registered}
    )

    lines = [
        "docker-mcp-server — manage Docker through the docker-py SDK and the docker CLI.",
        "",
        "Tools load on demand: search by a domain keyword below to pull a tool's full schema before calling it.",
        "",
        "Domains (and the words that find them):",
    ]
    lines += [f"- {domain} — {blurb}" for domain, blurb in _DOMAIN_BLURBS.items() if domain in present]

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
        caveats.append(
            f"CLI-backed domains ({', '.join(cli_present)}) shell out to the docker CLI/plugins; those "
            "calls raise if the CLI or a required plugin isn't installed."
        )
    if present & set(_SWARM_DOMAINS):
        caveats.append("Swarm-family tools require a swarm manager node.")
    if _hosts.is_multi():
        caveats.append(
            f"Multiple hosts are configured ({_hosts.labels()}): read-only tools take `host=<label>` "
            "(omit → the default, the first listed); mutating/destructive tools require an explicit "
            "`host`; a host marked `(ro)` rejects writes. See the `docker-mcp://hosts` resource."
        )
    if caveats:
        lines += ["", "Picking the right tool:"]
        lines += [f"- {c}" for c in caveats]

    lines += [
        "",
        "The registered surface changes with env switches; read the `docker-mcp://tool-catalog` resource for "
        "the live tool/domain/category list and which switches are active. Docs are under "
        "`docker-docs://contents`. For multi-step jobs (deploy, troubleshoot, prune, audit, migrate, "
        "multi-arch build, volume backup/restore) prefer the matching MCP prompt.",
    ]
    return "\n".join(lines)


def finalize_instructions() -> None:
    """
    Set the server's `instructions` from the actually-registered surface — called once after every tool
    module has imported (docker_mcp/__init__.py), so the switch-dependent registration is already known.

    FastMCP.instructions is a read-only property backed by the low-level server's `instructions`, which is
    read at run() time (create_initialization_options), so writing it through here after registration
    propagates to the MCP initialize handshake. Reaching into `_mcp_server` is guarded the same way as the
    schema-title strip below: a FastMCP refactor degrades to "instructions stay unset" rather than raising.
    """
    try:
        mcp._mcp_server.instructions = build_instructions()
    except AttributeError:
        pass


def _annotations_for(name: str, category: ToolCategory) -> ToolAnnotations:
    """Build the ToolAnnotations a client uses to auto-allow reads and gate destructive calls."""
    return ToolAnnotations(
        readOnlyHint=category is ToolCategory.READ_ONLY,
        destructiveHint=category is ToolCategory.DESTRUCTIVE,
        idempotentHint=True if name in _IDEMPOTENT_TOOLS else None,
    )


# JSON Schema keywords whose value is a {name: subschema-or-other} map — their keys are caller-supplied
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
    only restate a default). All three transforms are display-only — call-time validation runs off
    the tool's separate `fn_metadata`, so none changes behavior — and were measured to be
    information-free, together ~18% of the advertised schema tokens:

    - **`title`** (~10%): pydantic stamps one on every property/`$def` (the title-cased field name,
      e.g. `cache_from` -> "Cache From") plus a top-level `<tool>Arguments` title — it duplicates the
      property name.
    - **nullable `anyOf`** (~7%): an `X | None` param emits `anyOf: [<X>, {"type": "null"}]`; the null
      branch is redundant with the field's optionality (absence from `required` + its `default`), so
      drop it — hoisting the sole remaining branch, or keeping a multi-branch `anyOf` minus the null.
      Gated on a sibling `default` so a (hypothetical) required nullable with no default is never
      collapsed to look non-nullable.
    - **`additionalProperties: true`** (~1%): the JSON Schema default — an explicit `true` says nothing
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


def _host_param_description(name: str, category: ToolCategory) -> str:
    """The advertised `host` description in multi-host mode — the enum carries the valid labels."""
    if _is_host_write(name, category):
        return "Target host label (required when multiple hosts are configured)."
    return f"Target host label; omit to use the default ({_hosts.default().label!r})."


def _raise_read_only(name: str, label: str, category: ToolCategory) -> NoReturn:
    """Refuse a write to a host carrying the per-host (ro) marker (distinct from the
    DOCKER_MCP_SERVER_READONLY switch, which drops write tools from the surface entirely)."""
    raise RuntimeError(
        f"{name}: host {label!r} is read-only (configured with the (ro) marker); refusing this "
        f"{category.value} operation. For a fully read-only server use DOCKER_MCP_SERVER_READONLY."
    )


def _enforce_host_guard(name: str, category: ToolCategory, host: str | None) -> None:
    """
    Central call-time guard for a daemon-targeting tool. Wired whenever there is something to enforce:
    multiple hosts (host selection + per-host (ro) refusal) or a single host flagged (ro). Raises when a
    write omits `host` in multi-host mode, when `host` is not a configured label, or when a write targets
    an (ro) host. Read-only and connection-control tools may omit `host` (None -> default / all).
    """
    known = _hosts.labels()
    write = _is_host_write(name, category)
    if host is None:
        # Multi-host: a write must name its target. Single-host: the schema carries no host param to
        # pass, but an (ro) default host must still refuse writes.
        if write and _hosts.is_multi():
            raise RuntimeError(f"{name}: 'host' is required when multiple hosts are configured; choose one of {known}.")
        if write and _hosts.is_read_only():
            _raise_read_only(name, _hosts.default().label, category)
        return
    if host not in known:
        raise RuntimeError(f"{name}: unknown host {host!r}; configured hosts: {known}.")
    if write and _hosts.is_read_only(host):
        _raise_read_only(name, host, category)


def _apply_host_schema(parameters: Any, name: str, category: ToolCategory) -> None:
    """
    Display-only surgery on a daemon-targeting tool's advertised `host` property (run after _slim_schema;
    call-time validation runs off the separate fn_metadata, so this never changes behavior).

    Single-host mode: drop `host` entirely so the schema is byte-for-byte today's (footprint-neutral).
    Multi-host mode: constrain `host` to an `enum` of the configured labels with a generated description,
    and for writes mark it required (advisory — the guard is the teeth) by adding it to `required` and
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
    (host selection + per-host (ro) refusal), or a single host flagged (ro) (refuse writes even though the
    schema carries no host param). A single writable host needs no guard — today's footprint-neutral path."""
    return _hosts.is_multi() or _hosts.is_read_only()


def _wrap_with_host_guard(func: Callable, name: str, category: ToolCategory) -> Callable:
    """Wrap a daemon-targeting tool so the host guard runs before it (when `_host_guard_needed()` —
    multi-host, or a single host flagged (ro)). Preserves the signature so FastMCP builds the same
    schema/fn_metadata, and matches the func's sync/async-ness."""
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
        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _enforce_host_guard(name, category, _host_of(args, kwargs))
        return func(*args, **kwargs)

    wrapper.__signature__ = signature  # pyright: ignore[reportAttributeAccessIssue]
    return wrapper


def tool(**kwargs: Any) -> Callable[[Callable], Callable]:
    """
    Register an @mcp.tool with central classification — the drop-in `@tool()` every tool module uses.

    The tool's category comes from TOOL_CATEGORIES (defaulting to MUTATING, the safe assumption, for
    anything unclassified) and its domain from the defining module. We skip registration when a
    read-only env switch forbids the category or DOCKER_MCP_SERVER_DISABLE drops the domain, and otherwise
    attach the matching ToolAnnotations.
    """

    def decorator(func: Callable) -> Callable:
        name = func.__name__
        domain = _domain_for(func)
        category = TOOL_CATEGORIES.get(name, ToolCategory.MUTATING)
        registered = _should_register(category, readonly=READONLY, no_destructive=NO_DESTRUCTIVE) and _domain_enabled(
            domain, DISABLED_DOMAINS
        )
        _seen_tool_names.add(name)
        _tool_registry[name] = ToolRecord(name=name, domain=domain, category=category, registered=registered)
        if not registered:
            return func
        # Daemon-targeting tools (those declaring a `host` param) get a call-time host guard when there's
        # something to enforce — multiple hosts, or a single host flagged (ro); wrap before registering so
        # FastMCP builds the schema from the wrapper, whose signature mirrors the original. A single
        # writable host (and host-agnostic tools) register func unchanged.
        target = func
        if _has_host_param(func) and _host_guard_needed():
            target = _wrap_with_host_guard(func, name, category)
        decorated = mcp.tool(annotations=_annotations_for(name, category), **kwargs)(target)
        # Slim the advertised input schema (drop information-free titles, nullable-anyOf null branches,
        # and redundant `additionalProperties: true`), then apply the host-param surgery (enum + required
        # in multi-host, or strip it in single-host). Both reach into FastMCP internals
        # (`_tool_manager.get_tool(...).parameters`); guard it so a future FastMCP refactor degrades to
        # "schema not slimmed" (a test catches that) rather than crashing the server at import time.
        try:
            registered_tool = mcp._tool_manager.get_tool(kwargs.get("name") or name)
        except AttributeError, KeyError:
            registered_tool = None
        parameters = registered_tool.parameters if registered_tool is not None else None
        if isinstance(parameters, dict):
            _slim_schema(parameters)
            _apply_host_schema(parameters, name, category)
        return decorated

    return decorator


def prompt(description: str, *, domain: str | None = None, multi_host: bool = False) -> Callable[[Callable], Callable]:
    """
    Register an `@mcp.prompt`, honoring DOCKER_MCP_SERVER_DISABLE — the `@prompt()` every prompt module uses.

    A prompt tied to a feature area (`domain`) is skipped when that domain is disabled, so a server that
    drops e.g. `scout` doesn't keep prompts that steer the agent toward tools that are no longer
    registered. `domain=None` is for general / cross-domain prompts (doc lookup, prune, disk usage) that
    always register. A `multi_host=True` prompt registers only when 2+ hosts are configured (via
    DOCKER_MCP_SERVER_HOSTS), so a multi-host workflow prompt stays hidden in the common single-host case
    — the prompt-side parallel of the per-tool host param. Gating happens at import like `@tool()`, and
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

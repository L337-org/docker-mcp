# MCP server singleton plus the central tool-registration helper.
#
# Tool modules import `tool` from here (never `mcp` directly) and decorate with `@tool()`.
# That indirection lets one place own (a) the read-only / destructive classification of every
# tool, (b) the two env switches that decide what gets registered, and (c) the ToolAnnotations
# attached to each registered tool. `mcp` is still exported for `@mcp.prompt` / `@mcp.resource`.

import os
from collections.abc import Callable
from enum import Enum
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("docker-mcp")


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
    # client / system
    "ping": ToolCategory.READ_ONLY,
    "version": ToolCategory.READ_ONLY,
    "info": ToolCategory.READ_ONLY,
    "df": ToolCategory.READ_ONLY,
    "events": ToolCategory.READ_ONLY,
    "login": ToolCategory.MUTATING,
    "close": ToolCategory.MUTATING,
    "reconnect": ToolCategory.MUTATING,
    # containers
    "run_container": ToolCategory.MUTATING,
    "create_container": ToolCategory.MUTATING,
    "get_container": ToolCategory.READ_ONLY,
    "list_containers": ToolCategory.READ_ONLY,
    "prune_containers": ToolCategory.DESTRUCTIVE,
    "start_container": ToolCategory.MUTATING,
    "stop_container": ToolCategory.MUTATING,
    "restart_container": ToolCategory.MUTATING,
    "kill_container": ToolCategory.DESTRUCTIVE,
    "pause_container": ToolCategory.MUTATING,
    "unpause_container": ToolCategory.MUTATING,
    "remove_container": ToolCategory.DESTRUCTIVE,
    "container_logs": ToolCategory.READ_ONLY,
    "follow_container_logs": ToolCategory.READ_ONLY,
    "container_stats": ToolCategory.READ_ONLY,
    "container_top": ToolCategory.READ_ONLY,
    "exec_in_container": ToolCategory.MUTATING,
    "commit_container": ToolCategory.MUTATING,
    "container_diff": ToolCategory.READ_ONLY,
    "rename_container": ToolCategory.MUTATING,
    "resize_container": ToolCategory.MUTATING,
    "update_container": ToolCategory.MUTATING,
    "wait_container": ToolCategory.READ_ONLY,
    "export_container": ToolCategory.READ_ONLY,
    "get_container_archive": ToolCategory.READ_ONLY,
    "put_container_archive": ToolCategory.MUTATING,
    # images
    "build_image": ToolCategory.MUTATING,
    "get_image": ToolCategory.READ_ONLY,
    "get_registry_data": ToolCategory.READ_ONLY,
    "list_images": ToolCategory.READ_ONLY,
    "pull_image": ToolCategory.MUTATING,
    "push_image": ToolCategory.MUTATING,
    "remove_image": ToolCategory.DESTRUCTIVE,
    "search_images": ToolCategory.READ_ONLY,
    "prune_images": ToolCategory.DESTRUCTIVE,
    "load_image": ToolCategory.MUTATING,
    "save_image": ToolCategory.READ_ONLY,
    "tag_image": ToolCategory.MUTATING,
    "image_history": ToolCategory.READ_ONLY,
    # networks
    "create_network": ToolCategory.MUTATING,
    "get_network": ToolCategory.READ_ONLY,
    "list_networks": ToolCategory.READ_ONLY,
    "prune_networks": ToolCategory.DESTRUCTIVE,
    "remove_network": ToolCategory.DESTRUCTIVE,
    "connect_network": ToolCategory.MUTATING,
    "disconnect_network": ToolCategory.MUTATING,
    # volumes
    "create_volume": ToolCategory.MUTATING,
    "get_volume": ToolCategory.READ_ONLY,
    "list_volumes": ToolCategory.READ_ONLY,
    "prune_volumes": ToolCategory.DESTRUCTIVE,
    "remove_volume": ToolCategory.DESTRUCTIVE,
    # configs
    "create_config": ToolCategory.MUTATING,
    "get_config": ToolCategory.READ_ONLY,
    "list_configs": ToolCategory.READ_ONLY,
    "remove_config": ToolCategory.DESTRUCTIVE,
    # secrets
    "create_secret": ToolCategory.MUTATING,
    "get_secret": ToolCategory.READ_ONLY,
    "list_secrets": ToolCategory.READ_ONLY,
    "remove_secret": ToolCategory.DESTRUCTIVE,
    # nodes
    "get_node": ToolCategory.READ_ONLY,
    "list_nodes": ToolCategory.READ_ONLY,
    "update_node": ToolCategory.MUTATING,
    # services
    "create_service": ToolCategory.MUTATING,
    "get_service": ToolCategory.READ_ONLY,
    "list_services": ToolCategory.READ_ONLY,
    "update_service": ToolCategory.MUTATING,
    "remove_service": ToolCategory.DESTRUCTIVE,
    "service_tasks": ToolCategory.READ_ONLY,
    "service_logs": ToolCategory.READ_ONLY,
    "scale_service": ToolCategory.MUTATING,
    "force_update_service": ToolCategory.MUTATING,
    # swarm
    "init_swarm": ToolCategory.MUTATING,
    "join_swarm": ToolCategory.MUTATING,
    "leave_swarm": ToolCategory.DESTRUCTIVE,
    "update_swarm": ToolCategory.MUTATING,
    "reload_swarm": ToolCategory.READ_ONLY,
    "unlock_swarm": ToolCategory.MUTATING,
    "get_swarm_unlock_key": ToolCategory.READ_ONLY,
    # plugins
    "get_plugin": ToolCategory.READ_ONLY,
    "install_plugin": ToolCategory.MUTATING,
    "list_plugins": ToolCategory.READ_ONLY,
    "configure_plugin": ToolCategory.MUTATING,
    "disable_plugin": ToolCategory.MUTATING,
    "enable_plugin": ToolCategory.MUTATING,
    "push_plugin": ToolCategory.MUTATING,
    "remove_plugin": ToolCategory.DESTRUCTIVE,
    "upgrade_plugin": ToolCategory.MUTATING,
    # compose
    "compose_up": ToolCategory.MUTATING,
    "compose_down": ToolCategory.DESTRUCTIVE,
    "compose_ps": ToolCategory.READ_ONLY,
    "compose_logs": ToolCategory.READ_ONLY,
    "compose_config": ToolCategory.READ_ONLY,
    "compose_build": ToolCategory.MUTATING,
    "compose_pull": ToolCategory.MUTATING,
    "compose_restart": ToolCategory.MUTATING,
    "compose_run": ToolCategory.MUTATING,
    "compose_exec": ToolCategory.MUTATING,
    "compose_ls": ToolCategory.READ_ONLY,
    # context
    "context_ls": ToolCategory.READ_ONLY,
    "context_inspect": ToolCategory.READ_ONLY,
    "context_create": ToolCategory.MUTATING,
    "context_use": ToolCategory.MUTATING,
    "context_rm": ToolCategory.DESTRUCTIVE,
    # buildx
    "buildx_build": ToolCategory.MUTATING,
    "buildx_bake": ToolCategory.MUTATING,
    "buildx_imagetools_inspect": ToolCategory.READ_ONLY,
    "buildx_imagetools_create": ToolCategory.MUTATING,
    "buildx_ls": ToolCategory.READ_ONLY,
    "buildx_inspect": ToolCategory.READ_ONLY,
    "buildx_du": ToolCategory.READ_ONLY,
    "buildx_prune": ToolCategory.DESTRUCTIVE,
    "buildx_create": ToolCategory.MUTATING,
    "buildx_use": ToolCategory.MUTATING,
    "buildx_rm": ToolCategory.DESTRUCTIVE,
    # scout
    "scout_cves": ToolCategory.READ_ONLY,
    "scout_quickview": ToolCategory.READ_ONLY,
    "scout_recommendations": ToolCategory.READ_ONLY,
    "scout_compare": ToolCategory.READ_ONLY,
    "scout_sbom": ToolCategory.READ_ONLY,
    # registry (HTTPS, no daemon)
    "registry_list_tags": ToolCategory.READ_ONLY,
    "registry_inspect_manifest": ToolCategory.READ_ONLY,
    "hub_list_tags": ToolCategory.READ_ONLY,
    "hub_repo_info": ToolCategory.READ_ONLY,
}

# Destructive tools whose effect is idempotent — re-running has no additional effect (the targets
# are already gone). Surfaced via ToolAnnotations.idempotentHint so clients can treat retries as safe.
_IDEMPOTENT_TOOLS = frozenset({"prune_containers", "prune_images", "prune_networks", "prune_volumes", "buildx_prune"})


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# Read-only env switches, evaluated once at import (registration time):
#   DOCKER_MCP_READONLY       — register only READ_ONLY tools (a true read-only server).
#   DOCKER_MCP_NO_DESTRUCTIVE — register everything except DESTRUCTIVE tools (a "no data loss" mode).
# READONLY is the stricter of the two and wins when both are set.
READONLY = _is_truthy(os.environ.get("DOCKER_MCP_READONLY"))
NO_DESTRUCTIVE = _is_truthy(os.environ.get("DOCKER_MCP_NO_DESTRUCTIVE"))

# Every tool name a `@tool()` decorator has processed this run, whether or not it was registered
# (the restrictive modes skip registration). Lets the drift test compare against TOOL_CATEGORIES.
_seen_tool_names: set[str] = set()


def _should_register(category: ToolCategory, *, readonly: bool, no_destructive: bool) -> bool:
    """Decide whether a tool of `category` is registered under the given switch state."""
    if readonly:
        return category is ToolCategory.READ_ONLY
    if no_destructive:
        return category is not ToolCategory.DESTRUCTIVE
    return True


def _annotations_for(name: str, category: ToolCategory) -> ToolAnnotations:
    """Build the ToolAnnotations a client uses to auto-allow reads and gate destructive calls."""
    return ToolAnnotations(
        readOnlyHint=category is ToolCategory.READ_ONLY,
        destructiveHint=category is ToolCategory.DESTRUCTIVE,
        idempotentHint=True if name in _IDEMPOTENT_TOOLS else None,
    )


def tool(**kwargs: Any) -> Callable[[Callable], Callable]:
    """
    Register an @mcp.tool with central classification — the drop-in `@tool()` every tool module uses.

    The tool's category comes from TOOL_CATEGORIES (defaulting to MUTATING, the safe assumption, for
    anything unclassified). Based on it we skip registration entirely when a read-only env switch
    forbids the category, and otherwise attach the matching ToolAnnotations.
    """

    def decorator(func: Callable) -> Callable:
        name = func.__name__
        category = TOOL_CATEGORIES.get(name, ToolCategory.MUTATING)
        _seen_tool_names.add(name)
        if not _should_register(category, readonly=READONLY, no_destructive=NO_DESTRUCTIVE):
            return func
        return mcp.tool(annotations=_annotations_for(name, category), **kwargs)(func)

    return decorator

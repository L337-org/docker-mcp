# library of mcp resources for viewing docker SDK and CLI-feature documentation

import json

import httpx

from docker_mcp.server import is_domain_disabled, mcp, register_resource_domains, tool_catalog
from docker_mcp.tools._utils import package_version
from docker_mcp.tools.client import _get_client
from docker_mcp.tools.containers import _read_log_tail, _read_stats_summary

DOCKER_DOCS_BASE_URL = "https://docker-py.readthedocs.io/en/stable"

# Bounded wait for a docs fetch — a stalled readthedocs connection must not hang the resource read.
_DOCS_TIMEOUT = 30.0
_USER_AGENT = f"docker-mcp-server/{package_version()}"

# Sections served from the docker-py SDK documentation. Each maps to
# DOCKER_DOCS_BASE_URL/<section>.html for backwards compatibility.
SDK_SECTIONS: tuple[str, ...] = (
    "index",
    "client",
    "containers",
    "images",
    "networks",
    "volumes",
    "configs",
    "secrets",
    "nodes",
    "services",
    "swarm",
    "plugins",
)

# Sections served from external documentation sources (not docker-py). These cover the
# functionality that this MCP server exposes via the docker CLI or by talking to a
# registry directly, which the SDK does not document.
EXTERNAL_SECTIONS: dict[str, str] = {
    "compose": "https://docs.docker.com/compose/intro/compose-application-model/",
    "compose-cli": "https://docs.docker.com/reference/cli/docker/compose/",
    "compose-file": "https://docs.docker.com/reference/compose-file/",
    "context": "https://docs.docker.com/engine/manage-resources/contexts/",
    "context-cli": "https://docs.docker.com/reference/cli/docker/context/",
    "stack": "https://docs.docker.com/engine/swarm/stack-deploy/",
    "stack-cli": "https://docs.docker.com/reference/cli/docker/stack/",
    "registry-api": "https://distribution.github.io/distribution/spec/api/",
    "oci-distribution-spec": "https://github.com/opencontainers/distribution-spec/blob/main/spec.md",
    "hub-api": "https://docs.docker.com/reference/api/hub/latest/",
    "buildx": "https://docs.docker.com/build/builders/",
    "buildx-cli": "https://docs.docker.com/reference/cli/docker/buildx/",
    "buildx-bake": "https://docs.docker.com/build/bake/reference/",
    "scout": "https://docs.docker.com/scout/",
    "scout-cli": "https://docs.docker.com/reference/cli/docker/scout/",
    "dockerfile": "https://docs.docker.com/reference/dockerfile/",
    "build-best-practices": "https://docs.docker.com/build/building/best-practices/",
    "engine-security": "https://docs.docker.com/engine/security/",
    "engine-api": "https://docs.docker.com/reference/api/engine/",
}


# Maps each doc section to the tool domain it documents, so DOCKER_MCP_DISABLE also hides the docs for
# a disabled feature area (e.g. disabling `scout` hides the `scout` / `scout-cli` sections). Sections
# with no entry here — general references like `index`, `client`, `dockerfile`, `engine-security` — are
# always available. Registered with the server so tool_catalog() can report the hidden sections.
_SECTION_DOMAINS: dict[str, str] = {
    "containers": "containers",
    "images": "images",
    "networks": "networks",
    "volumes": "volumes",
    "configs": "configs",
    "secrets": "secrets",
    "nodes": "nodes",
    "services": "services",
    "swarm": "swarm",
    "plugins": "plugins",
    "compose": "compose",
    "compose-cli": "compose",
    "compose-file": "compose",
    "context": "context",
    "context-cli": "context",
    "stack": "stack",
    "stack-cli": "stack",
    "registry-api": "registry",
    "oci-distribution-spec": "registry",
    "hub-api": "registry",
    "buildx": "buildx",
    "buildx-cli": "buildx",
    "buildx-bake": "buildx",
    "scout": "scout",
    "scout-cli": "scout",
}
register_resource_domains(_SECTION_DOMAINS)


def _section_enabled(section: str) -> bool:
    """A doc section is available unless the domain it documents is dropped by DOCKER_MCP_DISABLE."""
    return not is_domain_disabled(_SECTION_DOMAINS.get(section))


def _section_url(section: str) -> str:
    if section in SDK_SECTIONS:
        return f"{DOCKER_DOCS_BASE_URL}/{section}.html"
    if section in EXTERNAL_SECTIONS:
        return EXTERNAL_SECTIONS[section]
    raise ValueError(f"Unknown documentation section '{section}'. Read docker-docs://contents to list valid sections.")


@mcp.resource("docker-docs://contents", mime_type="application/json")
def list_docs_sections() -> str:
    """
    List the available documentation sections.

    The response keeps the original `base_url` and `sections` (a list of section names)
    fields for backward compatibility with clients that parsed the pre-extension shape.
    Sections served from external URLs (compose, context, registry specs) appear in
    `sections` alongside the SDK ones; their absolute URLs live in `section_urls`.

    returns: str - JSON describing each section's source URL and how to read it
    """
    all_sections: list[str] = [*SDK_SECTIONS, *EXTERNAL_SECTIONS.keys()]
    # Hide sections whose domain is disabled via DOCKER_MCP_DISABLE, mirroring how disabled tools and
    # prompts drop out — the agent isn't pointed at docs for a feature area this server doesn't expose.
    section_names = [s for s in all_sections if _section_enabled(s)]
    disabled_sections = [s for s in all_sections if not _section_enabled(s)]
    section_urls: dict[str, str] = {
        section: f"{DOCKER_DOCS_BASE_URL}/{section}.html" for section in SDK_SECTIONS if _section_enabled(section)
    }
    section_urls.update({s: url for s, url in EXTERNAL_SECTIONS.items() if _section_enabled(s)})
    return json.dumps(
        {
            "base_url": DOCKER_DOCS_BASE_URL,
            "sdk_base_url": DOCKER_DOCS_BASE_URL,
            "sections": section_names,
            "section_urls": section_urls,
            "disabled_sections": disabled_sections,
            "usage": (
                "Read docker-docs://<section> to fetch the documentation for that section. "
                "Sections served from `base_url` cover the Docker SDK for Python; the "
                "remaining sections (see `section_urls`) cover docker CLI features (compose, "
                "context) and registry HTTP APIs (OCI distribution spec, Docker Hub) that "
                "this server exposes outside the SDK. `disabled_sections` lists sections hidden "
                "because their domain is dropped via DOCKER_MCP_DISABLE."
            ),
        },
        indent=2,
    )


@mcp.resource("docker-mcp://tool-catalog", mime_type="application/json")
def get_tool_catalog() -> str:
    """
    List every tool this server knows about with its domain, mutation category, and whether the
    active env switches actually registered it.

    Read this to see the blast radius of a tool before calling it (READ_ONLY / MUTATING /
    DESTRUCTIVE) and to confirm which whole domains the operator disabled via DOCKER_MCP_DISABLE
    (or the read-only switches) — a tool absent from the live tool list but present here as
    `registered: false` was filtered out by configuration, not missing by mistake.

    returns: str - JSON with `switches`, per-domain counts, and a per-tool list
    """
    return json.dumps(tool_catalog(), indent=2)


# Container observability resources. These mirror the container_logs / container_stats tools but as
# read-only @mcp.resource endpoints a client can attach as context. Gated on the `containers` domain.
_CONTAINERS_DOMAIN = "containers"


def _require_containers_domain() -> None:
    """Refuse a container resource read when the `containers` domain is disabled via DOCKER_MCP_DISABLE."""
    if is_domain_disabled(_CONTAINERS_DOMAIN):
        raise ValueError(
            "Container observability resources are unavailable because the 'containers' domain is "
            "disabled via DOCKER_MCP_DISABLE."
        )


@mcp.resource("docker://containers", mime_type="application/json")
def list_container_resources() -> str:
    """
    Index every container with the resource URIs for reading its logs and live stats.

    Lists all containers (running and stopped). Each entry carries a `logs` URI (readable in any
    state — useful for diagnosing why a container exited) and, for running containers only, a `stats`
    URI (a stopped container has no live cgroup to sample). Exited containers include their
    `exit_code` as a triage signal.

    returns: str - JSON object {"containers": [{id, name, image, status, exit_code?, logs, stats?}, ...]}
    """
    _require_containers_domain()
    entries = []
    for container in _get_client().containers.list(all=True):
        state = container.attrs.get("State", {}) or {}
        status = state.get("Status")
        ref = container.name or container.short_id
        entry: dict = {
            "id": container.short_id,
            "name": container.name,
            "image": (container.attrs.get("Config", {}) or {}).get("Image"),
            "status": status,
            "logs": f"docker-logs://{ref}",
            "stats": f"docker-stats://{ref}" if status == "running" else None,
        }
        if status == "exited":
            entry["exit_code"] = state.get("ExitCode")
        entries.append(entry)
    return json.dumps({"containers": entries}, indent=2)


@mcp.resource("docker-logs://{id_or_name}", mime_type="text/plain")
def get_container_logs_resource(id_or_name: str) -> str:
    """
    Read a bounded tail of a container's combined stdout/stderr logs.

    Works on running and stopped containers, so it can surface why a container exited. The read is
    capped to a recent tail so it can't flood the agent's context.

    args: id_or_name - The container id or name (from the docker://containers index)
    returns: str - The decoded recent log tail
    """
    _require_containers_domain()
    return _read_log_tail(id_or_name)


@mcp.resource("docker-stats://{id_or_name}", mime_type="application/json")
def get_container_stats_resource(id_or_name: str) -> str:
    """
    Read a computed resource-usage summary for a running container.

    Returns a small summary (CPU %, memory used/limit/%, network and block I/O) derived from a single
    stats snapshot. Raises if the container isn't running, since stats require a live cgroup.

    args: id_or_name - The container id or name (from the docker://containers index)
    returns: str - JSON {container, cpu_percent, mem_used_mb, mem_limit_mb, mem_percent,
                   net_rx_mb, net_tx_mb, blk_read_mb, blk_write_mb}
    """
    _require_containers_domain()
    return json.dumps(_read_stats_summary(id_or_name), indent=2)


@mcp.resource("docker-docs://{section}", mime_type="text/html")
def get_docs_section(section: str) -> str:
    """
    Fetch the documentation page for a section.

    args: section - Section name from `docker-docs://contents`
    returns: str - The HTML (or rendered Markdown) content of the documentation page
    """
    if not _section_enabled(section):
        raise ValueError(
            f"Documentation section '{section}' is unavailable because its domain is disabled via "
            f"DOCKER_MCP_DISABLE. Read docker-docs://contents for the sections this server exposes."
        )
    url = _section_url(section)
    resp = httpx.get(url, timeout=_DOCS_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")

# library of mcp resources for viewing docker SDK and CLI-feature documentation

import json

import httpx

import docker_mcp._hosts as _hosts
from docker_mcp.server import is_domain_disabled, mcp, register_resource_domains, tool_catalog
from docker_mcp.tools._utils import package_version
from docker_mcp.tools.system import _get_client, host_list
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


# Maps each doc section to the tool domain it documents, so DOCKER_MCP_SERVER_DISABLE also hides the docs for
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
    """A doc section is available unless the domain it documents is dropped by DOCKER_MCP_SERVER_DISABLE."""
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
    # Hide sections whose domain is disabled via DOCKER_MCP_SERVER_DISABLE, mirroring how disabled tools and
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
                "because their domain is dropped via DOCKER_MCP_SERVER_DISABLE."
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
    DESTRUCTIVE) and to confirm which whole domains the operator disabled via DOCKER_MCP_SERVER_DISABLE
    (or the read-only switches) — a tool absent from the live tool list but present here as
    `registered: false` was filtered out by configuration, not missing by mistake.

    returns: str - JSON with `switches`, per-domain counts, and a per-tool list
    """
    return json.dumps(tool_catalog(), indent=2)


@mcp.resource("docker-mcp://hosts", mime_type="application/json")
def get_hosts_resource() -> str:
    """
    The Docker hosts configured via DOCKER_MCP_SERVER_HOSTS — the same data as the `host_list` tool:
    each host's name, resolved daemon URL, read_only / tls flags, and which one is the default used when
    a tool's `host` argument is omitted. The resolved default is observable here but is not itself a
    selectable label.

    returns: str - JSON list, one object per configured host
    """
    return json.dumps(host_list(), indent=2)


# Container observability resources. These mirror the container_logs / container_stats tools but as
# read-only @mcp.resource endpoints a client can attach as context. Gated on the `containers` domain.
_CONTAINERS_DOMAIN = "containers"


def _require_containers_domain() -> None:
    """Refuse a container resource read when the `containers` domain is disabled via DOCKER_MCP_SERVER_DISABLE."""
    if is_domain_disabled(_CONTAINERS_DOMAIN):
        raise ValueError(
            "Container observability resources are unavailable because the 'containers' domain is "
            "disabled via DOCKER_MCP_SERVER_DISABLE."
        )


def _child_uri(scheme: str, ref: str, host: str | None) -> str:
    """The child logs/stats URI matching the index's host context: host-qualified when an index is
    host-scoped, else empty-authority (multi-host default) or bare (single-host)."""
    if host is not None:
        return f"{scheme}://{host}/{ref}"
    return f"{scheme}:///{ref}" if _hosts.is_multi() else f"{scheme}://{ref}"


def _render_index(host: str | None) -> str:
    _require_containers_domain()
    entries = []
    for container in _get_client(host).containers.list(all=True):
        state = container.attrs.get("State", {}) or {}
        status = state.get("Status")
        ref = container.name or container.short_id
        entry: dict = {
            "id": container.short_id,
            "name": container.name,
            "image": (container.attrs.get("Config", {}) or {}).get("Image"),
            "status": status,
            "logs": _child_uri("docker-logs", ref, host),
            "stats": _child_uri("docker-stats", ref, host) if status == "running" else None,
        }
        if status == "exited":
            entry["exit_code"] = state.get("ExitCode")
        entries.append(entry)
    return json.dumps({"containers": entries}, indent=2)


def list_container_resources() -> str:
    """
    Index every container with the resource URIs for reading its logs and live stats.

    Lists all containers (running and stopped). Each entry carries a `logs` URI (readable in any
    state — useful for diagnosing why a container exited) and, for running containers only, a `stats`
    URI (a stopped container has no live cgroup to sample). Exited containers include their
    `exit_code` as a triage signal.

    returns: str - JSON object {"containers": [{id, name, image, status, exit_code?, logs, stats?}, ...]}
    """
    return _render_index(None)


def list_host_container_resources(host: str) -> str:
    """
    Index every container on a named host (the host-qualified container index).

    Same shape as the default container index, but the child logs/stats URIs stay on `host` so
    following them reads the same daemon.

    args: host - Configured host label (from the docker-mcp://hosts resource)
    returns: str - JSON object {"containers": [...]}
    """
    return _render_index(host)


def get_container_logs_resource(id_or_name: str) -> str:
    """
    Read a bounded tail of a container's combined stdout/stderr logs.

    Works on running and stopped containers, so it can surface why a container exited. The read is
    capped to a recent tail so it can't flood the agent's context.

    args: id_or_name - The container id or name (from the container index)
    returns: str - The decoded recent log tail
    """
    _require_containers_domain()
    return _read_log_tail(id_or_name)


def get_host_container_logs_resource(host: str, id_or_name: str) -> str:
    """
    Read a bounded log tail for a container on a named host (host-qualified docker-logs variant).

    args:
        host - Configured host label (from the docker-mcp://hosts resource)
        id_or_name - The container id or name (from that host's index)
    returns: str - The decoded recent log tail
    """
    _require_containers_domain()
    return _read_log_tail(id_or_name, host=host)


def get_container_stats_resource(id_or_name: str) -> str:
    """
    Read a computed resource-usage summary for a running container.

    Returns a small summary (CPU %, memory used/limit/%, network and block I/O) derived from a single
    stats snapshot. Raises if the container isn't running, since stats require a live cgroup.

    args: id_or_name - The container id or name (from the container index)
    returns: str - JSON {container, cpu_percent, mem_used_mb, mem_limit_mb, mem_percent,
                   net_rx_mb, net_tx_mb, blk_read_mb, blk_write_mb}
    """
    _require_containers_domain()
    return json.dumps(_read_stats_summary(id_or_name), indent=2)


def get_host_container_stats_resource(host: str, id_or_name: str) -> str:
    """
    Resource-usage summary for a running container on a named host (host-qualified docker-stats variant).

    args:
        host - Configured host label (from the docker-mcp://hosts resource)
        id_or_name - The container id or name (from that host's index)
    returns: str - JSON usage summary (same shape as docker-stats://{id_or_name})
    """
    _require_containers_domain()
    return json.dumps(_read_stats_summary(id_or_name, host=host), indent=2)


# Single-host keeps today's bare URIs (back-compat); multi-host uses empty-authority (`docker:///…`) for
# the default host plus host-qualified (`docker://{host}/…`) variants, disambiguated by path-segment
# count. The default index emits child URIs matching its own scheme (see `_child_uri`).
if _hosts.is_multi():
    mcp.resource("docker:///containers", mime_type="application/json")(list_container_resources)
    mcp.resource("docker://{host}/containers", mime_type="application/json")(list_host_container_resources)
    mcp.resource("docker-logs:///{id_or_name}", mime_type="text/plain")(get_container_logs_resource)
    mcp.resource("docker-logs://{host}/{id_or_name}", mime_type="text/plain")(get_host_container_logs_resource)
    mcp.resource("docker-stats:///{id_or_name}", mime_type="application/json")(get_container_stats_resource)
    mcp.resource("docker-stats://{host}/{id_or_name}", mime_type="application/json")(get_host_container_stats_resource)
else:
    mcp.resource("docker://containers", mime_type="application/json")(list_container_resources)
    mcp.resource("docker-logs://{id_or_name}", mime_type="text/plain")(get_container_logs_resource)
    mcp.resource("docker-stats://{id_or_name}", mime_type="application/json")(get_container_stats_resource)


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
            f"DOCKER_MCP_SERVER_DISABLE. Read docker-docs://contents for the sections this server exposes."
        )
    url = _section_url(section)
    resp = httpx.get(url, timeout=_DOCS_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")

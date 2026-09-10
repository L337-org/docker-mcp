"""The advertised resources: read-only documents a client fetches by URI rather than calling."""

# library of mcp resources for viewing docker SDK and CLI-feature documentation

import json
from typing import Literal

import httpx

import docker_mcp._hosts as _hosts
from docker_mcp.exceptions import CapabilityError, ToolInputError, ToolRefusalError
from docker_mcp.server import is_domain_disabled, query_catalog, register_resource_domains, resource, tool, tool_catalog
from docker_mcp.tools._utils import package_version
from docker_mcp.tools.system import host_list
from docker_mcp.tools.containers import _read_log_tail, _read_stats_summary
from docker_mcp.tools.services import _read_service_log_tail, _read_service_task_summary

DOCKER_DOCS_BASE_URL = "https://docker-py.readthedocs.io/en/stable"

# Bounded wait for a docs fetch - a stalled readthedocs connection must not hang the resource read.
_DOCS_TIMEOUT = 30.0
_USER_AGENT = f"docker-mcp-server/{package_version()}"

# Cap on the (decoded) bytes we'll buffer from a single docs fetch. These are fixed, known-good
# doc URLs (not agent-pointed like registry.py's targets), but a compromised/redesigned upstream
# page could still serve something huge, and docs_lookup makes this path directly tool-callable -
# bound it the same way registry.py bounds registry responses (mirrors `_read_capped_response`).
_MAX_DOCS_RESPONSE_BYTES = 16 * 1024 * 1024  # 16 MiB


def _read_capped_docs_response(url: str) -> bytes:
    """Stream a docs page bounded by `_MAX_DOCS_RESPONSE_BYTES`, raising if it's exceeded.

    Args:
        url: the documentation page to fetch

    Returns:
        bytes: the page body, undecoded

    Raises:
        ToolRefusalError: the page exceeds the response cap.
    """
    with httpx.stream(
        "GET", url, timeout=_DOCS_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > _MAX_DOCS_RESPONSE_BYTES:
                raise ToolRefusalError(
                    f"Docs response from {url} exceeded the {_MAX_DOCS_RESPONSE_BYTES}-byte limit; "
                    f"refusing to buffer a response this large."
                )
            chunks.append(chunk)
        return b"".join(chunks)


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
# with no entry here - general references like `index`, `client`, `dockerfile`, `engine-security` - are
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
    """A doc section is available unless the domain it documents is dropped by DOCKER_MCP_SERVER_DISABLE.

    Args:
        section: the documentation section to test

    Returns:
        bool: True unless the domain it documents is disabled
    """
    return not is_domain_disabled(_SECTION_DOMAINS.get(section))


def _section_url(section: str) -> str:
    if section in SDK_SECTIONS:
        return f"{DOCKER_DOCS_BASE_URL}/{section}.html"
    if section in EXTERNAL_SECTIONS:
        return EXTERNAL_SECTIONS[section]
    raise ToolInputError(
        f"Unknown documentation section '{section}'. Read docker-docs://contents to list valid sections."
    )


@resource("docker-docs://contents", mime_type="application/json")
def list_docs_sections() -> str:
    """List the available documentation sections.

    The response keeps the original `base_url` and `sections` (a list of section names)
    fields for backward compatibility with clients that parsed the pre-extension shape.
    Sections served from external URLs (compose, context, registry specs) appear in
    `sections` alongside the SDK ones; their absolute URLs live in `section_urls`.

    Returns:
        str: JSON describing each section's source URL and how to read it
    """
    all_sections: list[str] = [*SDK_SECTIONS, *EXTERNAL_SECTIONS.keys()]
    # Hide sections whose domain is disabled via DOCKER_MCP_SERVER_DISABLE, mirroring how disabled tools and
    # prompts drop out - the agent isn't pointed at docs for a feature area this server doesn't expose.
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


@resource("docker-mcp://tool-catalog", mime_type="application/json")
def get_tool_catalog() -> str:
    """List every tool this server knows about.

    Each entry carries its domain, its mutation category, and whether the active env switches
    actually registered it.

    Read this to see the blast radius of a tool before calling it (READ_ONLY / MUTATING /
    DESTRUCTIVE) and to confirm which whole domains the operator disabled via DOCKER_MCP_SERVER_DISABLE
    (or the read-only switches) - a tool absent from the live tool list but present here as
    `registered: false` was filtered out by configuration, not missing by mistake.

    Returns:
        str: JSON with `switches`, per-domain counts, and a per-tool list
    """
    return json.dumps(tool_catalog(), indent=2)


@resource("docker-mcp://hosts", mime_type="application/json")
def get_hosts_resource() -> str:
    """The Docker hosts configured via DOCKER_MCP_SERVER_HOSTS.

    The same data as the `host_list` tool: each host's name, resolved daemon URL, read_only /
    non_destructive / tls flags, and which one is the default used when a tool's `host` argument is
    omitted. The resolved default is observable here but is not itself a selectable label.

    Returns:
        str: JSON list, one object per configured host
    """
    return json.dumps(host_list(), indent=2)


# Container observability resources. These mirror the container_logs / container_stats tools but as
# read-only @mcp.resource endpoints a client can attach as context. Gated on the `containers` domain.
_CONTAINERS_DOMAIN = "containers"


def _require_containers_domain() -> None:
    """Refuse a container resource read when the `containers` domain is disabled via DOCKER_MCP_SERVER_DISABLE.

    Raises:
        CapabilityError: the ``containers`` domain is disabled.
    """
    if is_domain_disabled(_CONTAINERS_DOMAIN):
        raise CapabilityError(
            "Container observability resources are unavailable because the 'containers' domain is "
            "disabled via DOCKER_MCP_SERVER_DISABLE."
        )


def get_container_logs_resource(id_or_name: str) -> str:
    """Read a bounded tail of a container's combined stdout/stderr logs.

    Works on running and stopped containers, so it can surface why a container exited. The read is
    capped to a recent tail so it can't flood the agent's context.

    Args:
        id_or_name: The container id or name (from the container index)

    Returns:
        str: The decoded recent log tail
    """
    _require_containers_domain()
    return _read_log_tail(id_or_name)


def get_host_container_logs_resource(host: str, id_or_name: str) -> str:
    """Read a bounded log tail for a container on a named host (host-qualified docker-logs variant).

    Args:
        host: Configured host label (from the docker-mcp://hosts resource)
        id_or_name: The container id or name (from that host's index)

    Returns:
        str: The decoded recent log tail
    """
    _require_containers_domain()
    return _read_log_tail(id_or_name, host=host)


def get_container_stats_resource(id_or_name: str) -> str:
    """Read a computed resource-usage summary for a running container.

    Returns a small summary (CPU %, memory used/limit/%, network and block I/O) derived from a single
    stats snapshot. Raises if the container isn't running, since stats require a live cgroup.

    Args:
        id_or_name: The container id or name (from the container index)

    Returns:
        str: JSON {container, cpu_percent, mem_used_mb, mem_limit_mb, mem_percent,
            net_rx_mb, net_tx_mb, blk_read_mb, blk_write_mb}
    """
    _require_containers_domain()
    return json.dumps(_read_stats_summary(id_or_name), indent=2)


def get_host_container_stats_resource(host: str, id_or_name: str) -> str:
    """Resource-usage summary for a running container on a named host (host-qualified docker-stats variant).

    Args:
        host: Configured host label (from the docker-mcp://hosts resource)
        id_or_name: The container id or name (from that host's index)

    Returns:
        str: JSON usage summary (same shape as docker-stats://{id_or_name})
    """
    _require_containers_domain()
    return json.dumps(_read_stats_summary(id_or_name, host=host), indent=2)


# Single-host keeps today's bare URIs (back-compat); multi-host uses empty-authority
# (`docker-logs:///...`) for the default host plus host-qualified (`docker-logs://{host}/...`)
# variants, disambiguated by path-segment count.
if _hosts.is_multi():
    resource("docker-logs:///{id_or_name}", mime_type="text/plain")(get_container_logs_resource)
    resource("docker-logs://{host}/{id_or_name}", mime_type="text/plain")(get_host_container_logs_resource)
    resource("docker-stats:///{id_or_name}", mime_type="application/json")(get_container_stats_resource)
    resource("docker-stats://{host}/{id_or_name}", mime_type="application/json")(get_host_container_stats_resource)
else:
    resource("docker-logs://{id_or_name}", mime_type="text/plain")(get_container_logs_resource)
    resource("docker-stats://{id_or_name}", mime_type="application/json")(get_container_stats_resource)


# Service observability resources. Same pattern as the container resources above (domain gate,
# index renderer, private read-helpers on services.py), gated on the `services` domain.
_SERVICES_DOMAIN = "services"


def _require_services_domain() -> None:
    """Refuse a service resource read when the `services` domain is disabled via DOCKER_MCP_SERVER_DISABLE.

    Raises:
        CapabilityError: the ``services`` domain is disabled.
    """
    if is_domain_disabled(_SERVICES_DOMAIN):
        raise CapabilityError(
            "Service observability resources are unavailable because the 'services' domain is "
            "disabled via DOCKER_MCP_SERVER_DISABLE."
        )


def get_service_logs_resource(id_or_name: str) -> str:
    """Read a bounded tail of a swarm service's combined stdout/stderr logs.

    Args:
        id_or_name: The service id or name (from the service index)

    Returns:
        str: The decoded recent log tail
    """
    _require_services_domain()
    return _read_service_log_tail(id_or_name)


def get_host_service_logs_resource(host: str, id_or_name: str) -> str:
    """Read a bounded log tail for a swarm service on a named host (host-qualified service-logs variant).

    Args:
        host: Configured host label (from the docker-mcp://hosts resource)
        id_or_name: The service id or name (from that host's index)

    Returns:
        str: The decoded recent log tail
    """
    _require_services_domain()
    return _read_service_log_tail(id_or_name, host=host)


def get_service_tasks_resource(id_or_name: str) -> str:
    """Read a computed task/rollout status summary for a swarm service.

    Returns running vs. desired task counts, any failing tasks (id, node, error), and the current
    rolling-update state if one is in progress - the "is this service OK right now" signal, since a
    service has no cgroup-style stats of its own.

    Args:
        id_or_name: The service id or name (from the service index)

    Returns:
        str: JSON {service, mode, running_tasks, desired_tasks, failed_tasks, update_state}
    """
    _require_services_domain()
    return json.dumps(_read_service_task_summary(id_or_name), indent=2)


def get_host_service_tasks_resource(host: str, id_or_name: str) -> str:
    """Task/rollout status summary for a swarm service on a named host (host-qualified variant).

    Args:
        host: Configured host label (from the docker-mcp://hosts resource)
        id_or_name: The service id or name (from that host's index)

    Returns:
        str: JSON summary (same shape as service-tasks://{id_or_name})
    """
    _require_services_domain()
    return json.dumps(_read_service_task_summary(id_or_name, host=host), indent=2)


if _hosts.is_multi():
    resource("service-logs:///{id_or_name}", mime_type="text/plain")(get_service_logs_resource)
    resource("service-logs://{host}/{id_or_name}", mime_type="text/plain")(get_host_service_logs_resource)
    resource("service-tasks:///{id_or_name}", mime_type="application/json")(get_service_tasks_resource)
    resource("service-tasks://{host}/{id_or_name}", mime_type="application/json")(get_host_service_tasks_resource)
else:
    resource("service-logs://{id_or_name}", mime_type="text/plain")(get_service_logs_resource)
    resource("service-tasks://{id_or_name}", mime_type="application/json")(get_service_tasks_resource)


@resource("docker-docs://{section}", mime_type="text/html")
def get_docs_section(section: str) -> str:  # noqa: DOC501,DOC503
    """Fetch the documentation page for a section.

    Args:
        section: Section name from `docker-docs://contents`

    Returns:
        str: The HTML (or rendered Markdown) content of the documentation page
    """
    if not _section_enabled(section):
        raise CapabilityError(
            f"Documentation section '{section}' is unavailable because its domain is disabled via "
            f"DOCKER_MCP_SERVER_DISABLE. Read docker-docs://contents for the sections this server exposes."
        )
    url = _section_url(section)
    return _read_capped_docs_response(url).decode("utf-8", errors="replace")


@tool()
def docs_lookup(section: str | None = None) -> str:
    """
    Look up Docker SDK/CLI/registry reference documentation by section.

    A tool-callable mirror of the docker-docs:// resources, for clients that can't read MCP
    resources (e.g. Claude Desktop, Cursor). Always registered regardless of
    DOCKER_MCP_SERVER_DISABLE - looking something up costs nothing and isn't tied to any single
    Docker feature area - but an individual section still refuses if the domain it documents is
    disabled, matching the equivalent `docker-docs://{section}` resource exactly.

    Omit `section` to list every available section with its source URL (same as
    `docker-docs://contents`); pass a `section` name to fetch that page's content (same as
    `docker-docs://{section}`). Most useful before constructing an `extra_kwargs`-style passthrough
    dict for a tool like `container_run`/`container_create`/`service_create` (their docstrings only
    list common keys, not every key docker-py accepts), or before writing Compose/Dockerfile/buildx
    bake-file syntax, which no tool generates.

    Args:
        section: Section name (from a no-argument call's index); omit to list all sections instead

    Returns:
        str: JSON section index (no `section`) or that section's raw HTML/Markdown content
    """
    if section is None:
        return list_docs_sections()
    return get_docs_section(section)


@tool()
def tool_list(
    domain: str | None = None,
    category: Literal["read_only", "mutating", "destructive"] | None = None,
    keyword: str | None = None,
) -> dict:
    """
    List this server's registered tools as compact rows, filtered by domain, category or keyword.

    A tool-callable mirror of `docker-mcp://tool-catalog` for clients that can't read MCP resources
    (e.g. Claude Desktop, Cursor), and the only way to ask what no per-tool description search can
    express: which tools are destructive, which accept a `host`, what this server actually
    registered. Use it to brief on an unfamiliar area (`domain="buildx"` returns one line per tool
    rather than ~13 full definitions), to check blast radius (`category="destructive"`), or to
    establish that nothing matches - `matched: 0` is a definitive negative, which a client's fuzzy
    search cannot give. Covers this server's own surface; `docs_lookup` covers external Docker
    reference documentation. Rows are summaries, not definitions - fetch a tool's own definition for
    its parameters. Read-only, never raises on a query matching nothing, and always registered even
    when DOCKER_MCP_SERVER_DISABLE drops every domain. A tool dropped by a switch or a disabled
    domain is absent rather than flagged; `hidden_by_configuration` reports how many each domain
    hides.

    Args:
        domain: Exact domain name (see any result's `domains` key); omit for every domain
        category: Exact category; omit for all three
        keyword: Case-insensitive substring over tool names, summaries and parameter names

    Returns:
        dict: {"matched": int, "tools": [{"name", "domain", "category", "summary"}], "domains": {domain: count},
            "no_domain": int, "hidden_by_configuration": {domain: count}, "switches", "filters"}. Every `domains` key is
            a value `domain` accepts; `no_domain` counts the domain-less tools, whose rows carry `domain: null` and
            which no `domain` value selects.
    """
    return query_catalog(domain=domain, category=category, keyword=keyword)

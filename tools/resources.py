# library of mcp resources for viewing docker SDK and CLI-feature documentation

import json
import urllib.request

from server import mcp

DOCKER_DOCS_BASE_URL = "https://docker-py.readthedocs.io/en/stable"

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
    "registry-api": "https://distribution.github.io/distribution/spec/api/",
    "oci-distribution-spec": "https://github.com/opencontainers/distribution-spec/blob/main/spec.md",
    "hub-api": "https://docs.docker.com/reference/api/hub/latest/",
}


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
    section_names: list[str] = [*SDK_SECTIONS, *EXTERNAL_SECTIONS.keys()]
    section_urls: dict[str, str] = {section: f"{DOCKER_DOCS_BASE_URL}/{section}.html" for section in SDK_SECTIONS}
    section_urls.update(EXTERNAL_SECTIONS)
    return json.dumps(
        {
            "base_url": DOCKER_DOCS_BASE_URL,
            "sdk_base_url": DOCKER_DOCS_BASE_URL,
            "sections": section_names,
            "section_urls": section_urls,
            "usage": (
                "Read docker-docs://<section> to fetch the documentation for that section. "
                "Sections served from `base_url` cover the Docker SDK for Python; the "
                "remaining sections (see `section_urls`) cover docker CLI features (compose, "
                "context) and registry HTTP APIs (OCI distribution spec, Docker Hub) that "
                "this server exposes outside the SDK."
            ),
        },
        indent=2,
    )


@mcp.resource("docker-docs://{section}", mime_type="text/html")
def get_docs_section(section: str) -> str:
    """
    Fetch the documentation page for a section.

    args: section: str - Section name from `docker-docs://contents`
    returns: str - The HTML (or rendered Markdown) content of the documentation page
    """
    url = _section_url(section)
    with urllib.request.urlopen(url) as response:  # noqa: S310 — URL is built from a static allow-list
        return response.read().decode("utf-8", errors="replace")

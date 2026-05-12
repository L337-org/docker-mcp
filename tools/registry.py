# library of mcp tools for OCI registries and Docker Hub.
#
# These tools talk to registry HTTPS APIs directly, not to a Docker daemon, so
# they work without a running daemon and without the docker CLI. Anonymous
# (unauthenticated) access is used unless a username/password is supplied.

import re
from typing import Any

import httpx

from server import mcp

_DEFAULT_TIMEOUT = 30.0
_USER_AGENT = "docker-mcp/0.1"
_HUB_API_BASE = "https://hub.docker.com/v2"
_DEFAULT_REGISTRY = "registry-1.docker.io"
_MAX_TAG_PAGES = 50  # cap on registry/Hub pagination follow-through

# Manifest media types we accept when inspecting a reference. The order matters:
# clients with no preference get a manifest list first when one exists.
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def _parse_image_ref(image: str) -> tuple[str, str]:
    """
    Split an image reference into (registry_host, repository_path).

    Docker Hub conventions:
      "alpine"          -> ("registry-1.docker.io", "library/alpine")
      "user/repo"       -> ("registry-1.docker.io", "user/repo")
      "ghcr.io/u/r"     -> ("ghcr.io", "u/r")
      "localhost:5000/r"-> ("localhost:5000", "r")
    """
    if "/" not in image:
        return (_DEFAULT_REGISTRY, f"library/{image}")
    first, _, rest = image.partition("/")
    if "." in first or ":" in first or first == "localhost":
        return (first, rest)
    return (_DEFAULT_REGISTRY, image)


def _parse_bearer_challenge(header: str) -> dict[str, str]:
    """Parse a `WWW-Authenticate: Bearer ...` header into its key=value pairs."""
    if not header.lower().startswith("bearer "):
        return {}
    out: dict[str, str] = {}
    for match in re.finditer(r'(\w+)="([^"]*)"', header[7:]):
        out[match.group(1)] = match.group(2)
    return out


def _get_bearer_token(
    client: httpx.Client,
    challenge: dict[str, str],
    *,
    username: str | None,
    password: str | None,
) -> str:
    realm = challenge.get("realm")
    if not realm:
        raise RuntimeError("Registry bearer challenge missing 'realm' parameter; cannot authenticate.")
    params: dict[str, str] = {}
    if "service" in challenge:
        params["service"] = challenge["service"]
    if "scope" in challenge:
        params["scope"] = challenge["scope"]
    auth = (username, password) if username and password else None
    resp = client.get(realm, params=params, auth=auth, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    body = resp.json()
    token = body.get("token") or body.get("access_token")
    if not token:
        raise RuntimeError(f"Registry token endpoint at {realm!r} returned no token.")
    return token


def _registry_get(
    registry: str,
    path: str,
    *,
    username: str | None,
    password: str | None,
    accept: str | None = None,
    timeout: float,
) -> httpx.Response:
    """GET https://<registry>/<path>, transparently handling a Bearer 401 challenge."""
    url = f"https://{registry}{path}"
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    if accept:
        headers["Accept"] = accept
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code == 401:
            challenge = _parse_bearer_challenge(resp.headers.get("WWW-Authenticate", ""))
            if not challenge:
                resp.raise_for_status()
            token = _get_bearer_token(client, challenge, username=username, password=password)
            headers["Authorization"] = f"Bearer {token}"
            resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp


def _next_link(link_header: str | None) -> str | None:
    """Return the URL of the rel=next entry in an RFC 5988 Link header, or None."""
    if not link_header:
        return None
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if match:
            return match.group(1)
    return None


@mcp.tool()
def registry_list_tags(
    image: str,
    username: str | None = None,
    password: str | None = None,
    limit: int = 1000,
) -> dict:
    """
    List tags for an image in an OCI v2 registry without pulling.

    Works against Docker Hub, GHCR, ECR public/private, GAR, and any other OCI-compliant
    registry. Anonymous if no credentials are passed.

    Security: many MCP clients log tool arguments verbatim. Avoid passing `password` from
    an agent loop — prefer `docker login` on the host running this MCP server and (for
    private images on Hub) use the `hub_*` tools which honour the same credentials cache
    indirectly via your scoped daemon, or pass credentials only for one-off probes.

    args:
        image: str - Image reference, e.g. "alpine", "library/alpine", "ghcr.io/org/repo"
        username: str - Optional registry username
        password: str - Optional registry password or token
        limit: int - Maximum number of tags to return (default 1000). The OCI pagination
                     loop is also capped at 50 pages to keep the call bounded.
    returns: dict - {"name": <repo>, "registry": <host>, "tags": [..], "truncated": bool}
    """
    registry, repo = _parse_image_ref(image)
    tags: list[str] = []
    truncated = False
    path: str | None = f"/v2/{repo}/tags/list"
    pages_followed = 0
    while path and pages_followed < _MAX_TAG_PAGES:
        resp = _registry_get(
            registry, path, username=username, password=password, accept=None, timeout=_DEFAULT_TIMEOUT
        )
        body = resp.json()
        for tag in body.get("tags", []) or []:
            tags.append(tag)
            if len(tags) >= limit:
                truncated = True
                return {"name": repo, "registry": registry, "tags": tags, "truncated": True}
        next_url = _next_link(resp.headers.get("Link"))
        if next_url is None:
            path = None
        elif next_url.startswith("http://") or next_url.startswith("https://"):
            # Link header may be absolute; strip the scheme+host to keep going through _registry_get.
            from urllib.parse import urlparse

            parsed = urlparse(next_url)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        else:
            path = next_url
        pages_followed += 1
    if pages_followed >= _MAX_TAG_PAGES and path:
        truncated = True
    return {"name": repo, "registry": registry, "tags": tags, "truncated": truncated}


@mcp.tool()
def registry_inspect_manifest(
    image: str,
    reference: str = "latest",
    username: str | None = None,
    password: str | None = None,
) -> dict:
    """
    Fetch the manifest for an image reference without pulling.

    The returned manifest may be either a single-platform image manifest or a multi-platform
    manifest list / OCI image index, depending on what the registry serves for that tag.

    args:
        image: str - Image reference, e.g. "alpine", "ghcr.io/org/repo"
        reference: str - Tag or digest (default "latest")
        username: str - Optional registry username
        password: str - Optional registry password or token
    returns: dict - {"name": <repo>, "registry": <host>, "reference": <ref>,
                     "media_type": <Content-Type>, "digest": <Docker-Content-Digest>,
                     "manifest": <decoded JSON body>}
    """
    registry, repo = _parse_image_ref(image)
    resp = _registry_get(
        registry,
        f"/v2/{repo}/manifests/{reference}",
        username=username,
        password=password,
        accept=_MANIFEST_ACCEPT,
        timeout=_DEFAULT_TIMEOUT,
    )
    return {
        "name": repo,
        "registry": registry,
        "reference": reference,
        "media_type": resp.headers.get("Content-Type", ""),
        "digest": resp.headers.get("Docker-Content-Digest", ""),
        "manifest": resp.json(),
    }


def _hub_normalize(repository: str) -> str:
    """Normalize a Hub repository to "namespace/name" form (official images get "library/")."""
    if "/" not in repository:
        return f"library/{repository}"
    return repository


@mcp.tool()
def hub_list_tags(repository: str, limit: int = 100) -> dict:
    """
    List tags on a Docker Hub repository with Hub-specific metadata.

    Unlike `registry_list_tags`, this hits the Hub UI API (hub.docker.com) which returns
    richer per-tag data — last pushed date, image sizes per platform, digest. Use this
    when you need that metadata; use `registry_list_tags` for parity across non-Hub
    registries.

    args:
        repository: str - Hub repository, e.g. "library/alpine" or "myorg/myimage"
        limit: int - Maximum number of tags to return (default 100). Pagination is
                     also capped at 50 pages to keep the call bounded.
    returns: dict - {"name": <repo>, "tags": [{name, full_size, last_updated, digest, images}, ...],
                     "truncated": bool}
    """
    repo = _hub_normalize(repository)
    url: str | None = f"{_HUB_API_BASE}/repositories/{repo}/tags?page_size=100"
    tags: list[dict[str, Any]] = []
    truncated = False
    pages = 0
    with httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
        while url and pages < _MAX_TAG_PAGES:
            resp = client.get(url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            body = resp.json()
            for entry in body.get("results", []) or []:
                tags.append(
                    {
                        "name": entry.get("name"),
                        "full_size": entry.get("full_size"),
                        "last_updated": entry.get("last_updated"),
                        "digest": entry.get("digest"),
                        "images": entry.get("images"),
                    }
                )
                if len(tags) >= limit:
                    return {"name": repo, "tags": tags, "truncated": True}
            url = body.get("next")
            pages += 1
    if pages >= _MAX_TAG_PAGES and url:
        truncated = True
    return {"name": repo, "tags": tags, "truncated": truncated}


@mcp.tool()
def hub_repo_info(repository: str) -> dict:
    """
    Fetch Docker Hub metadata for a repository.

    args: repository: str - Hub repository, e.g. "library/alpine" or "myorg/myimage"
    returns: dict - The Hub /v2/repositories/<repo>/ response (description, star_count,
                    pull_count, last_updated, is_private, etc.)
    """
    repo = _hub_normalize(repository)
    resp = httpx.get(
        f"{_HUB_API_BASE}/repositories/{repo}/",
        timeout=_DEFAULT_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.json()

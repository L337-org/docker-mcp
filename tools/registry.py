# library of mcp tools for OCI registries and Docker Hub.
#
# These tools talk to registry HTTPS APIs directly, not to a Docker daemon, so
# they work without a running daemon and without the docker CLI. Anonymous
# (unauthenticated) access is used unless a username/password is supplied.

import email.utils
import re
import time
from typing import Any, NoReturn

import httpx

from server import mcp

_DEFAULT_TIMEOUT = 30.0
_USER_AGENT = "docker-mcp/0.1"
_HUB_API_BASE = "https://hub.docker.com/v2"
_DEFAULT_REGISTRY = "registry-1.docker.io"
_MAX_TAG_PAGES = 50  # cap on registry/Hub pagination follow-through

# 429 rate-limit policy: if the registry tells us to wait this many seconds or less,
# we sleep and transparently retry once. Anything longer is surfaced to the caller
# so an agent / human can decide whether to back off rather than blocking inside a tool.
_RATE_LIMIT_RETRY_THRESHOLD_SECONDS = 10.0

# Errors emitted by email.utils.parsedate_to_datetime for non-date input. Bound to a
# module-level tuple so ruff format leaves the `except` form alone — PEP 758 makes the
# parentheses optional on Python 3.14, but we keep them for clarity to review bots.
_RETRY_AFTER_PARSE_ERRORS: tuple[type[BaseException], ...] = (TypeError, ValueError)

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


def _strip_tag_and_digest(image: str) -> str:
    """
    Strip an optional `@sha256:...` digest and `:tag` from an image reference.

    The colon in a registry hostname's port (e.g. `localhost:5000/foo`) is preserved —
    we only strip a trailing `:tag` that appears *after* the last `/` in the reference,
    so `ghcr.io:443/org/repo:v1` correctly becomes `ghcr.io:443/org/repo`.
    """
    if "@" in image:
        image = image.split("@", 1)[0]
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        image = image[:last_colon]
    return image


def _parse_image_ref(image: str) -> tuple[str, str]:
    """
    Split an image reference into (registry_host, repository_path).

    Any `:tag` or `@digest` suffix is stripped — pass tag/digest separately via the
    `reference` parameter of `registry_inspect_manifest`.

    Docker Hub conventions:
      "alpine"             -> ("registry-1.docker.io", "library/alpine")
      "alpine:3.19"        -> ("registry-1.docker.io", "library/alpine")
      "user/repo"          -> ("registry-1.docker.io", "user/repo")
      "ghcr.io/u/r"        -> ("ghcr.io", "u/r")
      "ghcr.io/u/r@sha256:..." -> ("ghcr.io", "u/r")
      "localhost:5000/r"   -> ("localhost:5000", "r")
    """
    image = _strip_tag_and_digest(image)
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


def _parse_retry_after(value: str | None) -> float | None:
    """
    Decode a `Retry-After` header (RFC 7231).

    The value is either an integer number of seconds (``"30"``) or an HTTP-date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``). Returns the delay in seconds, or None
    if the header is missing or unparseable.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except _RETRY_AFTER_PARSE_ERRORS:
        return None
    if parsed is None:
        return None
    delta = parsed.timestamp() - time.time()
    return max(0.0, delta)


# Hosts where Docker Hub's documented anonymous-pull cap applies. Used to tailor the
# 429 error message — every other registry (GHCR, ECR, GAR, Quay, self-hosted, …)
# enforces its own limits with different remedies, so a Hub-specific hint there is
# more misleading than helpful.
_DOCKER_HUB_HOSTS = frozenset({"registry-1.docker.io", "index.docker.io", "hub.docker.com"})


def _raise_rate_limited(resp: httpx.Response, url: str) -> NoReturn:
    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
    suffix = f"; retry after ~{retry_after:.0f}s" if retry_after is not None else ""
    parsed_host = httpx.URL(url).host
    if parsed_host in _DOCKER_HUB_HOSTS:
        guidance = (
            " Docker Hub caps anonymous pulls at ~100 requests / 6h per IP — "
            "authenticate with `docker login` (for SDK-backed tools) or pass "
            "`username`/`password` to `registry_list_tags` to raise the limit."
        )
    else:
        guidance = (
            " Consult the target registry's rate-limit policy; most registries raise the "
            "limit substantially once you authenticate with `username`/`password`."
        )
    raise RuntimeError(f"Registry rate-limited (HTTP 429) for {url}{suffix}.{guidance}")


def _get_with_429_policy(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """
    Single GET that applies the project's 429 retry policy.

    - On HTTP 429 with `Retry-After <= 10s`: sleep + retry once.
    - On HTTP 429 with no Retry-After, or a longer delay, or a second 429: raise.
    - Other status codes are returned as-is for the caller to handle.
    """
    resp = client.get(url, headers=headers, params=params)
    if resp.status_code != 429:
        return resp
    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
    if retry_after is None or retry_after > _RATE_LIMIT_RETRY_THRESHOLD_SECONDS:
        _raise_rate_limited(resp, url)
    time.sleep(retry_after)
    resp = client.get(url, headers=headers, params=params)
    if resp.status_code == 429:
        _raise_rate_limited(resp, url)
    return resp


def _registry_get(
    registry: str,
    path: str,
    *,
    username: str | None,
    password: str | None,
    accept: str | None = None,
    timeout: float,
) -> httpx.Response:
    """GET https://<registry>/<path>, transparently handling a Bearer 401 challenge and 429 rate limits."""
    url = f"https://{registry}{path}"
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    if accept:
        headers["Accept"] = accept
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = _get_with_429_policy(client, url, headers=headers)
        if resp.status_code == 401:
            challenge = _parse_bearer_challenge(resp.headers.get("WWW-Authenticate", ""))
            if not challenge:
                resp.raise_for_status()
            token = _get_bearer_token(client, challenge, username=username, password=password)
            headers["Authorization"] = f"Bearer {token}"
            resp = _get_with_429_policy(client, url, headers=headers)
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

    Note: this tool talks directly to the registry over HTTPS and does NOT read the local
    Docker credential store (`~/.docker/config.json`). For private registries you must pass
    `username` and `password` explicitly. Many MCP clients log tool arguments verbatim, so
    treat any password you pass through this tool as exposed — prefer per-invocation tokens
    with the minimum required scope rather than long-lived passwords.

    args:
        image: str - Image reference, e.g. "alpine", "library/alpine", "ghcr.io/org/repo".
                     Any trailing `:tag` or `@digest` is stripped before listing.
        username: str - Optional registry username (required only for private repos)
        password: str - Optional registry password or token (required only for private repos)
        limit: int - Maximum number of tags to return (default 1000; must be >= 1). The OCI
                     pagination loop is also capped at 50 pages to keep the call bounded.
    returns: dict - {"name": <repo>, "registry": <host>, "tags": [..], "truncated": bool}
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
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
        image: str - Image reference, e.g. "alpine", "ghcr.io/org/repo". Any trailing
                     `:tag` or `@digest` is stripped — pass the tag/digest as `reference`.
        reference: str - Tag or digest (default "latest")
        username: str - Optional registry username (required only for private repos;
                        this tool does NOT read `~/.docker/config.json`)
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

    Public Hub repositories only — this tool sends no authentication and does NOT read
    `~/.docker/config.json` or `docker login` credentials. Private Hub repositories will
    return a 404 / 401 from the Hub API. (For private images, use `registry_list_tags`
    against `registry-1.docker.io` with explicit credentials.)

    args:
        repository: str - Hub repository, e.g. "library/alpine" or "myorg/myimage"
        limit: int - Maximum number of tags to return (default 100; must be >= 1).
                     Pagination is also capped at 50 pages to keep the call bounded.
    returns: dict - {"name": <repo>, "tags": [{name, full_size, last_updated, digest, images}, ...],
                     "truncated": bool}
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    repo = _hub_normalize(repository)
    url: str | None = f"{_HUB_API_BASE}/repositories/{repo}/tags?page_size=100"
    tags: list[dict[str, Any]] = []
    truncated = False
    pages = 0
    with httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
        while url and pages < _MAX_TAG_PAGES:
            resp = _get_with_429_policy(client, url, headers={"User-Agent": _USER_AGENT})
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

    Public Hub repositories only — sends no authentication and does NOT read the local
    Docker credential store. Private repositories will return 404 / 401.

    args: repository: str - Hub repository, e.g. "library/alpine" or "myorg/myimage"
    returns: dict - The Hub /v2/repositories/<repo>/ response (description, star_count,
                    pull_count, last_updated, is_private, etc.)
    """
    repo = _hub_normalize(repository)
    url = f"{_HUB_API_BASE}/repositories/{repo}/"
    with httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
        resp = _get_with_429_policy(client, url, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    return resp.json()

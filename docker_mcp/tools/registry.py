# library of mcp tools for OCI registries and Docker Hub.
#
# These tools talk to registry HTTPS APIs directly, not to a Docker daemon, so
# they work without a running daemon and without the docker CLI. Anonymous
# (unauthenticated) access is used unless a username/password is supplied.

import datetime
import email.utils
import ipaddress
import re
import time
from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx

from docker_mcp.server import tool
from docker_mcp.tools._utils import package_version, read_env

_DEFAULT_TIMEOUT = 30.0
_USER_AGENT = f"docker-mcp-server/{package_version()}"
_HUB_API_BASE = "https://hub.docker.com/v2"
_DEFAULT_REGISTRY = "registry-1.docker.io"
_MAX_TAG_PAGES = 50  # cap on registry/Hub pagination follow-through


def _env_credentials(username: str | None, password: str | None) -> tuple[str | None, str | None]:
    """
    Fall back to DOCKER_MCP_SERVER_REGISTRY_USERNAME / DOCKER_MCP_SERVER_REGISTRY_PASSWORD when no
    explicit credentials are passed.

    Setting credentials in the server's environment keeps them out of tool arguments, which many
    MCP clients log verbatim. The password may be a personal-access token. Explicit arguments win
    over the environment; the env pair is only used when *both* arguments are unset, so a caller
    can't accidentally mix an argument username with an environment password. The DOCKER_MCP_*
    spellings remain honored as deprecated aliases.
    """
    if username is not None or password is not None:
        return username, password
    return (
        read_env("DOCKER_MCP_SERVER_REGISTRY_USERNAME", "DOCKER_MCP_REGISTRY_USERNAME"),
        read_env("DOCKER_MCP_SERVER_REGISTRY_PASSWORD", "DOCKER_MCP_REGISTRY_PASSWORD"),
    )


# 429 rate-limit policy: if the registry tells us to wait this many seconds or less,
# we sleep and transparently retry once. Anything longer is surfaced to the caller
# so an agent / human can decide whether to back off rather than blocking inside a tool.
_RATE_LIMIT_RETRY_THRESHOLD_SECONDS = 10.0

# Transient server-side statuses: the request was well-formed but the registry / auth
# endpoint hiccuped (Docker Hub's auth.docker.io intermittently 502s under load). These
# are worth a brief, bounded retry rather than failing the tool call outright — a single
# upstream blip should not surface as a hard error.
_TRANSIENT_STATUS = frozenset({502, 503, 504})
_TRANSIENT_MAX_RETRIES = 2
_TRANSIENT_BACKOFF_SECONDS = 0.5

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


def _host_of(netloc: str) -> str:
    """Extract the bare host from a "host", "host:port", or "[ipv6]:port" netloc."""
    return urlparse(f"//{netloc}").hostname or netloc


def _is_local_host(host: str) -> bool:
    """
    True if `host` is a loopback / private / link-local address or an obvious local name.

    Used to decide whether sending registry credentials to a token realm is safe. This is a
    best-effort, no-DNS check: it recognizes IP literals and the conventional local-name suffixes
    (localhost, *.local, *.internal) but does not resolve bare hostnames, so an internal host
    addressed by a plain name that doesn't match those suffixes is treated as non-local.
    """
    if not host:
        return False
    h = host.lower().rstrip(".")
    if h == "localhost" or h.endswith((".localhost", ".local", ".internal")):
        return True
    # Parse the *normalized* host so an IP literal with a trailing dot (e.g. "127.0.0.1.") is still
    # recognized rather than slipping through as a non-local name.
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _validate_bearer_realm(realm: str, registry: str) -> None:
    """
    Reject a token realm that would leak credentials in plaintext or to an internal host.

    The realm is attacker-controlled — it comes from the registry's `WWW-Authenticate` header — and
    we are about to send (possibly authenticated) requests to it. We therefore require:
      - an http/https scheme (no file://, gopher://, etc.);
      - https whenever the realm host is not local (plaintext to a public host would leak creds);
      - the realm host to be public, unless the registry we're talking to is itself local — this
        stops a public registry from redirecting credentialed requests at an internal service (SSRF),
        while still allowing a genuinely local dev registry (e.g. localhost:5000) to use a local realm.
    """
    parsed = urlparse(realm)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(
            f"Registry bearer realm {realm!r} has an unsupported scheme {parsed.scheme!r}; "
            f"refusing to send credentials. Only http/https token endpoints are allowed."
        )
    realm_host = parsed.hostname or ""
    if parsed.scheme != "https" and not _is_local_host(realm_host):
        raise RuntimeError(
            f"Registry bearer realm {realm!r} is plaintext http to a non-local host; refusing to "
            f"send credentials over an unencrypted connection. A legitimate public registry uses an "
            f"https token endpoint."
        )
    if _is_local_host(realm_host) and not _is_local_host(_host_of(registry)):
        raise RuntimeError(
            f"Registry {registry!r} pointed its token realm at a private/loopback host ({realm_host!r}); "
            f"refusing to send credentials to an internal address on behalf of a public registry "
            f"(possible SSRF)."
        )


def _get_bearer_token(
    client: httpx.Client,
    challenge: dict[str, str],
    *,
    username: str | None,
    password: str | None,
    registry: str,
) -> str:
    realm = challenge.get("realm")
    if not realm:
        raise RuntimeError("Registry bearer challenge missing 'realm' parameter; cannot authenticate.")
    _validate_bearer_realm(realm, registry)
    params: dict[str, str] = {}
    if "service" in challenge:
        params["service"] = challenge["service"]
    if "scope" in challenge:
        params["scope"] = challenge["scope"]
    auth = (username, password) if username and password else None
    resp = _get_with_retry_policy(client, realm, headers={"User-Agent": _USER_AGENT}, params=params, auth=auth)
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
    # RFC 7231 / 9110 mandates that HTTP-dates are UTC. `parsedate_to_datetime` returns
    # a naive datetime when the source said `-0000` (and only then); naive `.timestamp()`
    # would re-interpret in local time and skew the delay. Force UTC explicitly.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
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


def _get_with_retry_policy(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
) -> httpx.Response:
    """
    GET that applies the project's transient-failure retry policy, retrying in a single loop so a
    5xx blip after a 429 retry (or vice versa) is still absorbed rather than bubbling up.

    - On a transient 5xx (502/503/504): retry up to `_TRANSIENT_MAX_RETRIES` times with a short
      backoff (honoring `Retry-After` when present, capped at the threshold). If it still fails,
      the last response is returned for the caller's `raise_for_status` to surface — a sustained
      outage is not swallowed, only a blip is absorbed.
    - On HTTP 429 with `Retry-After <= 10s`: sleep + retry once.
    - On HTTP 429 with no Retry-After, or a longer delay, or a second 429: raise.
    - Other status codes are returned as-is for the caller to handle.
    """
    transient_attempts = 0
    retried_429 = False
    while True:
        resp = client.get(url, headers=headers, params=params, auth=auth)
        if resp.status_code in _TRANSIENT_STATUS and transient_attempts < _TRANSIENT_MAX_RETRIES:
            transient_attempts += 1
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            delay = (
                min(retry_after, _RATE_LIMIT_RETRY_THRESHOLD_SECONDS)
                if retry_after is not None
                else _TRANSIENT_BACKOFF_SECONDS
            )
            time.sleep(delay)
            continue
        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if retried_429 or retry_after is None or retry_after > _RATE_LIMIT_RETRY_THRESHOLD_SECONDS:
                _raise_rate_limited(resp, url)
            retried_429 = True
            time.sleep(retry_after)
            continue
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
    """GET https://<registry>/<path>, transparently handling a Bearer 401 challenge, 429 rate
    limits, and transient 5xx retries (see `_get_with_retry_policy`)."""
    url = f"https://{registry}{path}"
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    if accept:
        headers["Accept"] = accept
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = _get_with_retry_policy(client, url, headers=headers)
        if resp.status_code == 401:
            challenge = _parse_bearer_challenge(resp.headers.get("WWW-Authenticate", ""))
            if not challenge:
                resp.raise_for_status()
            token = _get_bearer_token(client, challenge, username=username, password=password, registry=registry)
            headers["Authorization"] = f"Bearer {token}"
            resp = _get_with_retry_policy(client, url, headers=headers)
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


@tool()
def registry_list_tags(
    image: str,
    username: str | None = None,
    password: str | None = None,
    limit: int = 1000,
) -> dict:
    """
    List tags for an image in an OCI v2 registry without pulling.

    Works against Docker Hub, GHCR, ECR, GAR, and any OCI-compliant registry; anonymous if no
    credentials are passed. Talks directly to the registry over HTTPS and does NOT read
    `~/.docker/config.json` — for private registries prefer the DOCKER_MCP_SERVER_REGISTRY_USERNAME /
    DOCKER_MCP_SERVER_REGISTRY_PASSWORD env vars (keeps secrets out of tool args, which clients often log).

    args:
        image - Image ref, e.g. "alpine", "ghcr.io/org/repo"; any `:tag`/`@digest` is stripped
        username - Optional registry username (overrides DOCKER_MCP_SERVER_REGISTRY_USERNAME)
        password - Optional registry password/token (overrides DOCKER_MCP_SERVER_REGISTRY_PASSWORD)
        limit - Max tags to return (default 1000, >= 1); pagination capped at 50 pages
    returns: dict - {"name": <repo>, "registry": <host>, "tags": [..], "truncated": bool}
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    username, password = _env_credentials(username, password)
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
            parsed = urlparse(next_url)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        else:
            path = next_url
        pages_followed += 1
    if pages_followed >= _MAX_TAG_PAGES and path:
        truncated = True
    return {"name": repo, "registry": registry, "tags": tags, "truncated": truncated}


@tool()
def registry_inspect_manifest(
    image: str,
    reference: str = "latest",
    username: str | None = None,
    password: str | None = None,
) -> dict:
    """
    Fetch the manifest for an image reference without pulling.

    May return a single-platform image manifest or a multi-platform manifest list / OCI image
    index, depending on what the registry serves for that tag.

    args:
        image - Image ref, e.g. "ghcr.io/org/repo"; `:tag`/`@digest` is stripped — pass via `reference`
        reference - Tag or digest (default "latest")
        username - Optional registry username (overrides DOCKER_MCP_SERVER_REGISTRY_USERNAME; no config.json)
        password - Optional registry password/token (overrides DOCKER_MCP_SERVER_REGISTRY_PASSWORD)
    returns: dict - {"name", "registry", "reference", "media_type", "digest", "manifest": <JSON body>}
    """
    username, password = _env_credentials(username, password)
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


@tool()
def registry_get_config(
    image: str,
    reference: str = "latest",
    platform: str = "linux/amd64",
    username: str | None = None,
    password: str | None = None,
) -> dict:
    """
    Fetch and parse an image's config blob from a registry without pulling.

    Answers "what's inside this image?" — env vars, entrypoint/cmd, workdir, exposed ports, user,
    labels, layer history (what `registry_inspect_manifest` only points at via `config.digest`).
    Resolves in up to three hops: manifest -> (if multi-platform) the `platform` entry's manifest
    -> the config blob.

    args:
        image - Image ref, e.g. "ghcr.io/org/repo"; `:tag`/`@digest` is stripped — pass via `reference`
        reference - Tag or digest (default "latest")
        platform - Platform to select from a multi-platform image, "os/arch[/variant]"
                        (default "linux/amd64"); ignored for single-platform images
        username - Optional registry username (overrides DOCKER_MCP_SERVER_REGISTRY_USERNAME)
        password - Optional registry password/token (overrides DOCKER_MCP_SERVER_REGISTRY_PASSWORD)
    returns: dict - {"name", "registry", "reference", "platform", "config_digest", "config": <parsed>};
                    `platform` is the selected platform (None if single-platform)
    """
    username, password = _env_credentials(username, password)
    registry, repo = _parse_image_ref(image)

    def _fetch_manifest(ref: str) -> dict:
        resp = _registry_get(
            registry,
            f"/v2/{repo}/manifests/{ref}",
            username=username,
            password=password,
            accept=_MANIFEST_ACCEPT,
            timeout=_DEFAULT_TIMEOUT,
        )
        return resp.json()

    manifest = _fetch_manifest(reference)
    selected_platform: str | None = None
    # A manifest list / OCI image index has "manifests"; a single-platform manifest has "config".
    if "manifests" in manifest:
        digest, selected_platform = _select_platform_digest(manifest, platform)
        manifest = _fetch_manifest(digest)

    config = manifest.get("config")
    if not isinstance(config, dict) or "digest" not in config:
        raise RuntimeError(
            f"Manifest for {repo}:{reference} has no config descriptor; cannot fetch the config blob "
            f"(media type {manifest.get('mediaType', 'unknown')!r})."
        )
    config_digest = config["digest"]
    blob = _registry_get(
        registry,
        f"/v2/{repo}/blobs/{config_digest}",
        username=username,
        password=password,
        accept=None,
        timeout=_DEFAULT_TIMEOUT,
    )
    return {
        "name": repo,
        "registry": registry,
        "reference": reference,
        "platform": selected_platform,
        "config_digest": config_digest,
        "config": blob.json(),
    }


def _parse_platform(platform: str) -> tuple[str, str, str | None]:
    """Split "os/arch[/variant]" (e.g. "linux/amd64", "linux/arm/v7") into (os, arch, variant)."""
    parts = platform.split("/")
    if len(parts) == 2:
        return parts[0], parts[1], None
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError(f"platform must be 'os/arch' or 'os/arch/variant', got {platform!r}")


def _select_platform_digest(index: dict, platform: str) -> tuple[str, str]:
    """
    Pick the sub-manifest matching `platform` from a manifest list / OCI image index.

    `platform` is "os/arch[/variant]". An omitted variant matches any variant of that os/arch — so
    "linux/arm64" still selects a "linux/arm64/v8" entry, following Docker's default-variant
    convention rather than forcing callers to know the variant. The returned platform string always
    reflects the entry *actually* selected (including its variant), so the caller is never misled
    about which variant they got; the first matching entry wins when several share an os/arch. Skips
    attestation manifests (no real os/arch). Raises ValueError if nothing matches, listing what the
    index does offer so the caller can retry.

    returns: (digest, actual_platform) of the selected sub-manifest
    """
    want_os, want_arch, want_variant = _parse_platform(platform)
    available: list[str] = []
    for entry in index.get("manifests", []) or []:
        plat = entry.get("platform", {})
        os_, arch = plat.get("os"), plat.get("architecture")
        if os_ in (None, "unknown") or arch in (None, "unknown"):
            continue  # attestation / non-image manifest
        variant = plat.get("variant")
        actual = "/".join(p for p in (os_, arch, variant) if p)
        available.append(actual)
        if os_ == want_os and arch == want_arch and (want_variant is None or variant == want_variant):
            digest = entry.get("digest")
            if digest:
                return digest, actual
    raise ValueError(
        f"No manifest for platform {platform!r} in image index. Available platforms: "
        f"{', '.join(sorted(set(available))) or 'none'}."
    )


def _parse_ratelimit_header(value: str | None) -> tuple[int | None, int | None]:
    """
    Decode a Docker Hub `RateLimit-Limit` / `RateLimit-Remaining` header.

    The format is "<count>;w=<window-seconds>" (e.g. "100;w=21600") or occasionally a bare count.
    Returns (count, window_seconds); either element is None when absent or unparseable.
    """
    if not value:
        return (None, None)
    head = value.split(";", 1)[0].strip()
    try:
        count: int | None = int(head)
    except ValueError:
        count = None
    window_match = re.search(r"w=(\d+)", value)
    window = int(window_match.group(1)) if window_match else None
    return (count, window)


def _hub_normalize(repository: str) -> str:
    """Normalize a Hub repository to "namespace/name" form (official images get "library/")."""
    if "/" not in repository:
        return f"library/{repository}"
    return repository


@tool()
def hub_list_tags(repository: str, limit: int = 100) -> dict:
    """
    List tags on a Docker Hub repository with Hub-specific metadata.

    Hits the Hub UI API (hub.docker.com) for richer per-tag data than `registry_list_tags` —
    last pushed date, per-platform sizes, digest. Public repos only: sends no auth and does NOT
    read `~/.docker/config.json`; private repos return 404/401 (use `registry_list_tags` against
    registry-1.docker.io with credentials).

    args:
        repository - Hub repository, e.g. "library/alpine" or "myorg/myimage"
        limit - Max tags to return (default 100, >= 1); pagination capped at 50 pages
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
            resp = _get_with_retry_policy(client, url, headers={"User-Agent": _USER_AGENT})
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


@tool()
def hub_repo_info(repository: str) -> dict:
    """
    Fetch Docker Hub metadata for a repository.

    Public repos only: sends no auth and does NOT read the local Docker credential store;
    private repos return 404/401.

    args: repository - Hub repository, e.g. "library/alpine" or "myorg/myimage"
    returns: dict - The Hub /v2/repositories/<repo>/ response (description, star_count,
                    pull_count, last_updated, is_private, etc.)
    """
    repo = _hub_normalize(repository)
    url = f"{_HUB_API_BASE}/repositories/{repo}/"
    with httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
        resp = _get_with_retry_policy(client, url, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    return resp.json()


# The image Docker publishes specifically for probing pull-rate limits. A HEAD against its manifest
# returns the RateLimit headers without counting as a pull (only manifest GETs are metered).
_RATELIMIT_REPO = "ratelimitpreview/test"


@tool()
def hub_rate_limit(username: str | None = None, password: str | None = None) -> dict:
    """
    Report the caller's remaining Docker Hub pull-rate-limit budget.

    Sends a HEAD to the `ratelimitpreview/test` manifest (a HEAD isn't metered as a pull, so the
    check costs no budget) and reads the RateLimit-Limit / RateLimit-Remaining headers. Call it
    before a large `compose_pull` / `pull_image` to avoid hitting the cap mid-deploy. Credentials
    raise the limit and switch metering from per-IP to per-account; falls back to
    DOCKER_MCP_SERVER_REGISTRY_USERNAME / DOCKER_MCP_SERVER_REGISTRY_PASSWORD, does NOT read `~/.docker/config.json`.
    Plans with no limit return no headers — reported as `"unlimited": true`.

    args:
        username - Optional Hub username (overrides DOCKER_MCP_SERVER_REGISTRY_USERNAME)
        password - Optional Hub password/token (overrides DOCKER_MCP_SERVER_REGISTRY_PASSWORD)
    returns: dict - {"authenticated", "limit", "remaining", "window_seconds", "unlimited"}
    """
    username, password = _env_credentials(username, password)
    registry = _DEFAULT_REGISTRY
    url = f"https://{registry}/v2/{_RATELIMIT_REPO}/manifests/latest"
    headers: dict[str, str] = {"User-Agent": _USER_AGENT, "Accept": _MANIFEST_ACCEPT}
    with httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
        resp = client.head(url, headers=headers)
        if resp.status_code == 401:
            challenge = _parse_bearer_challenge(resp.headers.get("WWW-Authenticate", ""))
            if not challenge:
                resp.raise_for_status()
            token = _get_bearer_token(client, challenge, username=username, password=password, registry=registry)
            headers["Authorization"] = f"Bearer {token}"
            resp = client.head(url, headers=headers)
        # 429 means the limit is already spent — that's a valid answer (remaining 0), not an error to
        # raise; the RateLimit headers are still present. Only raise for genuinely unexpected statuses.
        if resp.status_code not in (200, 429):
            resp.raise_for_status()
    limit, window = _parse_ratelimit_header(resp.headers.get("RateLimit-Limit"))
    remaining, _ = _parse_ratelimit_header(resp.headers.get("RateLimit-Remaining"))
    return {
        "authenticated": bool(username and password),
        "limit": limit,
        "remaining": remaining,
        "window_seconds": window,
        # No RateLimit headers at all => this account/plan isn't pull-limited.
        "unlimited": limit is None and remaining is None,
    }

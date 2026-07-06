import json
from unittest.mock import patch

import httpx
import pytest

from docker_mcp.tools.registry import (
    _env_credentials,
    _is_local_host,
    _next_link,
    _parse_bearer_challenge,
    _parse_image_ref,
    _parse_platform,
    _parse_ratelimit_header,
    _select_platform_digest,
    _strip_tag_and_digest,
    _validate_bearer_realm,
    hub_tags,
    hub_rate_limit,
    hub_repo_info,
    registry_image_config,
    registry_manifest,
    registry_tag_wait,
    registry_tags,
)


# ---------- _parse_image_ref ----------


def test_parse_image_ref_official_short():
    assert _parse_image_ref("alpine") == ("registry-1.docker.io", "library/alpine")


def test_parse_image_ref_hub_user_repo():
    assert _parse_image_ref("myorg/myimage") == ("registry-1.docker.io", "myorg/myimage")


def test_parse_image_ref_ghcr_three_segments():
    assert _parse_image_ref("ghcr.io/org/repo") == ("ghcr.io", "org/repo")


def test_parse_image_ref_localhost_with_port():
    assert _parse_image_ref("localhost:5000/myrepo") == ("localhost:5000", "myrepo")


def test_parse_image_ref_registry_with_port():
    assert _parse_image_ref("registry.example.com:443/team/svc") == ("registry.example.com:443", "team/svc")


# ---------- _strip_tag_and_digest / _parse_image_ref with tag or digest ----------


def test_strip_tag_and_digest_strips_tag():
    assert _strip_tag_and_digest("alpine:3.19") == "alpine"


def test_strip_tag_and_digest_strips_digest():
    assert _strip_tag_and_digest("alpine@sha256:abcdef") == "alpine"


def test_strip_tag_and_digest_strips_tag_and_digest_together():
    assert _strip_tag_and_digest("alpine:3.19@sha256:abc") == "alpine"


def test_strip_tag_and_digest_preserves_registry_port():
    assert _strip_tag_and_digest("localhost:5000/foo/bar") == "localhost:5000/foo/bar"


def test_strip_tag_and_digest_preserves_registry_port_and_strips_tag():
    assert _strip_tag_and_digest("localhost:5000/foo/bar:v1") == "localhost:5000/foo/bar"


def test_parse_image_ref_strips_tag_from_official_image():
    assert _parse_image_ref("alpine:3.19") == ("registry-1.docker.io", "library/alpine")


def test_parse_image_ref_strips_digest_from_ghcr_ref():
    assert _parse_image_ref("ghcr.io/org/repo@sha256:deadbeef") == ("ghcr.io", "org/repo")


def test_parse_image_ref_strips_tag_from_registry_with_port():
    assert _parse_image_ref("localhost:5000/foo/bar:v1") == ("localhost:5000", "foo/bar")


# ---------- _parse_bearer_challenge ----------


def test_parse_bearer_challenge_full():
    h = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/alpine:pull"'
    parsed = _parse_bearer_challenge(h)
    assert parsed == {
        "realm": "https://auth.docker.io/token",
        "service": "registry.docker.io",
        "scope": "repository:library/alpine:pull",
    }


def test_parse_bearer_challenge_case_insensitive_scheme():
    assert _parse_bearer_challenge('bearer realm="x"') == {"realm": "x"}


def test_parse_bearer_challenge_wrong_scheme_returns_empty():
    assert _parse_bearer_challenge('Basic realm="x"') == {}


def test_parse_bearer_challenge_empty_input():
    assert _parse_bearer_challenge("") == {}


# ---------- _is_local_host ----------


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "foo.local",
        "svc.internal",
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "192.168.0.5",
        "169.254.1.1",
    ],
)
def test_is_local_host_true_for_local_and_private(host):
    assert _is_local_host(host) is True


@pytest.mark.parametrize("host", ["", "ghcr.io", "auth.docker.io", "8.8.8.8", "1.1.1.1", "registry.example.com"])
def test_is_local_host_false_for_public(host):
    assert _is_local_host(host) is False


def test_is_local_host_recognizes_ip_literal_with_trailing_dot():
    # The host is normalized (trailing dot stripped) before being parsed as an IP, so a
    # fully-qualified loopback literal is still classified local.
    assert _is_local_host("127.0.0.1.") is True
    assert _is_local_host("10.0.0.1.") is True


# ---------- _validate_bearer_realm ----------


def test_validate_bearer_realm_allows_https_public():
    _validate_bearer_realm("https://auth.docker.io/token", "registry-1.docker.io")


def test_validate_bearer_realm_allows_local_realm_for_local_registry():
    # A genuine dev registry on localhost may legitimately use a local (even http) token endpoint.
    _validate_bearer_realm("http://localhost:5000/token", "localhost:5000")
    _validate_bearer_realm("https://127.0.0.1/token", "127.0.0.1:5000")


def test_validate_bearer_realm_rejects_plaintext_http_to_public_host():
    with pytest.raises(RuntimeError, match="plaintext http"):
        _validate_bearer_realm("http://auth.evil.com/token", "registry-1.docker.io")


def test_validate_bearer_realm_rejects_non_http_scheme():
    with pytest.raises(RuntimeError, match="unsupported scheme"):
        _validate_bearer_realm("file:///etc/passwd", "registry-1.docker.io")


def test_validate_bearer_realm_rejects_private_realm_for_public_registry():
    # Public registry trying to redirect credentialed requests to an internal host = SSRF.
    with pytest.raises(RuntimeError, match="possible SSRF"):
        _validate_bearer_realm("https://169.254.169.254/token", "registry-1.docker.io")


def test_registry_list_tags_rejects_malicious_http_realm():
    def handler(request: httpx.Request) -> httpx.Response:
        # Public registry hands back a plaintext-http token realm; the tool must refuse to
        # contact it (and never send credentials) rather than following the challenge.
        return httpx.Response(
            401,
            headers={"WWW-Authenticate": 'Bearer realm="http://auth.evil.com/token",service="reg.example.com"'},
        )

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="plaintext http"):
            registry_tags("reg.example.com/foo/bar", username="u", password="p")


# ---------- _next_link ----------


def test_next_link_finds_next_only():
    h = '<https://example.com/page2>; rel="next", <https://example.com/last>; rel="last"'
    assert _next_link(h) == "https://example.com/page2"


def test_next_link_no_next_returns_none():
    assert _next_link('<https://example.com/last>; rel="last"') is None


def test_next_link_none_input():
    assert _next_link(None) is None


# ---------- registry_tags ----------


def _mock_client(transport_handler):
    """Patch httpx.Client to use a MockTransport that delegates to `transport_handler`."""
    transport = httpx.MockTransport(transport_handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    return patch("docker_mcp.tools.registry.httpx.Client", side_effect=factory)


def test_registry_list_tags_single_page_anonymous():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/library/alpine/tags/list"
        assert request.url.host == "registry-1.docker.io"
        return httpx.Response(200, json={"name": "library/alpine", "tags": ["3.18", "3.19", "latest"]})

    with _mock_client(handler):
        result = registry_tags("alpine")

    assert result["name"] == "library/alpine"
    assert result["registry"] == "registry-1.docker.io"
    assert result["tags"] == ["3.18", "3.19", "latest"]
    assert result["truncated"] is False


def test_registry_list_tags_handles_bearer_challenge():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "auth.example.com":
            assert request.url.params["service"] == "reg.example.com"
            assert request.url.params["scope"] == "repository:foo/bar:pull"
            return httpx.Response(200, json={"token": "fake-token"})
        if "Authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",'
                    'service="reg.example.com",'
                    'scope="repository:foo/bar:pull"'
                },
            )
        assert request.headers["Authorization"] == "Bearer fake-token"
        return httpx.Response(200, json={"name": "foo/bar", "tags": ["v1"]})

    with _mock_client(handler):
        result = registry_tags("reg.example.com/foo/bar")

    assert result["tags"] == ["v1"]
    assert calls == [
        "reg.example.com/v2/foo/bar/tags/list",
        "auth.example.com/token",
        "reg.example.com/v2/foo/bar/tags/list",
    ]


def test_registry_list_tags_follows_link_header_pagination():
    pages = iter(
        [
            httpx.Response(
                200,
                json={"tags": ["a", "b"]},
                headers={"Link": '</v2/library/alpine/tags/list?last=b>; rel="next"'},
            ),
            httpx.Response(200, json={"tags": ["c", "d"]}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(pages)

    with _mock_client(handler):
        result = registry_tags("alpine")

    assert result["tags"] == ["a", "b", "c", "d"]
    assert result["truncated"] is False


def test_registry_list_tags_respects_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tags": ["t1", "t2", "t3", "t4", "t5"]})

    with _mock_client(handler):
        result = registry_tags("alpine", limit=3)

    assert result["tags"] == ["t1", "t2", "t3"]
    assert result["truncated"] is True


def test_registry_list_tags_rejects_non_positive_limit():
    with pytest.raises(ValueError, match="limit must be >= 1"):
        registry_tags("alpine", limit=0)
    with pytest.raises(ValueError, match="limit must be >= 1"):
        registry_tags("alpine", limit=-5)


def test_registry_list_tags_raises_on_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _mock_client(handler):
        with pytest.raises(httpx.HTTPStatusError):
            registry_tags("alpine")


# ---------- registry_manifest ----------


def test_registry_inspect_manifest_sets_accept_and_returns_metadata():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["accept"] = request.headers.get("Accept")
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"},
            headers={
                "Content-Type": "application/vnd.oci.image.manifest.v1+json",
                "Docker-Content-Digest": "sha256:deadbeef",
            },
        )

    with _mock_client(handler):
        result = registry_manifest("alpine", reference="3.19")

    assert captured["path"] == "/v2/library/alpine/manifests/3.19"
    assert "application/vnd.oci.image.manifest.v1+json" in (captured["accept"] or "")
    assert "application/vnd.oci.image.index.v1+json" in (captured["accept"] or "")
    assert result["digest"] == "sha256:deadbeef"
    assert result["media_type"] == "application/vnd.oci.image.manifest.v1+json"
    assert result["manifest"]["schemaVersion"] == 2


# ---------- hub_tags ----------


def test_hub_list_tags_normalizes_official_image_and_paginates():
    page1 = {
        "next": "https://hub.docker.com/v2/repositories/library/alpine/tags?page=2&page_size=100",
        "results": [{"name": "3.18", "full_size": 1, "last_updated": "x", "digest": "sha256:1", "images": []}],
    }
    page2 = {
        "next": None,
        "results": [{"name": "3.19", "full_size": 2, "last_updated": "y", "digest": "sha256:2", "images": []}],
    }
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        body = page1 if request.url.params.get("page") != "2" else page2
        return httpx.Response(200, json=body)

    with _mock_client(handler):
        result = hub_tags("alpine")

    assert seen_urls[0].startswith("https://hub.docker.com/v2/repositories/library/alpine/tags")
    assert [t["name"] for t in result["tags"]] == ["3.18", "3.19"]
    assert result["truncated"] is False


def test_hub_list_tags_respects_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"next": None, "results": [{"name": f"v{i}"} for i in range(10)]},
        )

    with _mock_client(handler):
        result = hub_tags("myorg/img", limit=3)

    assert [t["name"] for t in result["tags"]] == ["v0", "v1", "v2"]
    assert result["truncated"] is True


def test_hub_list_tags_rejects_non_positive_limit():
    with pytest.raises(ValueError, match="limit must be >= 1"):
        hub_tags("alpine", limit=0)


# ---------- hub_repo_info ----------


def test_hub_repo_info_returns_body():
    expected = {
        "user": "library",
        "name": "alpine",
        "star_count": 12345,
        "pull_count": 999999999,
        "is_private": False,
    }
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            content=json.dumps(expected).encode(),
            headers={"Content-Type": "application/json"},
        )

    with _mock_client(handler):
        result = hub_repo_info("alpine")
    assert result == expected
    assert seen_urls == ["https://hub.docker.com/v2/repositories/library/alpine/"]


# ---------- 429 rate-limit policy ----------


def test_registry_list_tags_retries_once_on_short_retry_after():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"tags": ["v1", "v2"]})

    with _mock_client(handler):
        result = registry_tags("alpine")

    assert result["tags"] == ["v1", "v2"]
    assert len(calls) == 2


def test_registry_list_tags_raises_when_retry_after_is_long():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited.*retry after ~3600s") as excinfo:
            registry_tags("alpine")
    # Default registry is Docker Hub — message should mention the Hub-specific cap.
    assert "Docker Hub" in str(excinfo.value)


def test_registry_list_tags_message_is_generic_for_non_hub_registry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited") as excinfo:
            registry_tags("ghcr.io/org/repo")
    # GHCR is not Docker Hub — the Hub-specific guidance must not appear; the
    # registry-agnostic hint about authenticating should.
    msg = str(excinfo.value)
    assert "Docker Hub" not in msg
    assert "authenticate" in msg.lower()


def test_registry_list_tags_raises_when_retry_after_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited"):
            registry_tags("alpine")


def test_registry_list_tags_raises_on_second_429_after_retry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited"):
            registry_tags("alpine")


def test_hub_list_tags_applies_429_policy():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"next": None, "results": [{"name": "v1"}]})

    with _mock_client(handler):
        result = hub_tags("alpine")

    assert [t["name"] for t in result["tags"]] == ["v1"]
    assert len(calls) == 2


def test_hub_repo_info_applies_429_policy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited"):
            hub_repo_info("alpine")


# ---------- transient 5xx retry policy ----------


def test_registry_list_tags_retries_on_transient_502():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            # Docker Hub's auth/registry endpoints intermittently 502 under load.
            return httpx.Response(502, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"tags": ["v1", "v2"]})

    with _mock_client(handler):
        result = registry_tags("alpine")

    assert result["tags"] == ["v1", "v2"]
    assert len(calls) == 2


def test_registry_list_tags_gives_up_after_transient_5xx_retries():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, headers={"Retry-After": "0"})

    with _mock_client(handler):
        # A sustained outage is surfaced (not swallowed) via the caller's raise_for_status.
        with pytest.raises(httpx.HTTPStatusError):
            registry_tags("alpine")

    # Initial attempt + _TRANSIENT_MAX_RETRIES (2) = 3 total GETs.
    assert len(calls) == 3


def test_transient_retry_uses_default_backoff_when_no_retry_after():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(504)  # no Retry-After header
        return httpx.Response(200, json={"tags": ["v1"]})

    with _mock_client(handler), patch("docker_mcp.tools.registry.time.sleep") as mock_sleep:
        result = registry_tags("alpine")

    from docker_mcp.tools.registry import _TRANSIENT_BACKOFF_SECONDS

    assert result["tags"] == ["v1"]
    mock_sleep.assert_called_once_with(_TRANSIENT_BACKOFF_SECONDS)


def test_429_retry_then_transient_5xx_is_still_absorbed():
    """A 5xx blip on the GET that follows a 429 retry must be absorbed, not bubbled up."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if len(calls) == 2:
            return httpx.Response(502, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"tags": ["v1"]})

    with _mock_client(handler):
        result = registry_tags("alpine")

    assert result["tags"] == ["v1"]
    assert len(calls) == 3


def test_bearer_token_fetch_retries_on_transient_5xx():
    """The token endpoint (auth.docker.io) is exactly where the CI 502 hit — it must retry too."""
    auth_calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.com":
            auth_calls.append(1)
            if len(auth_calls) == 1:
                return httpx.Response(502, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"token": "fake-token"})
        if "Authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",'
                    'service="reg.example.com",'
                    'scope="repository:foo/bar:pull"'
                },
            )
        assert request.headers["Authorization"] == "Bearer fake-token"
        return httpx.Response(200, json={"name": "foo/bar", "tags": ["v1"]})

    with _mock_client(handler):
        result = registry_tags("reg.example.com/foo/bar")

    assert result["tags"] == ["v1"]
    # The token endpoint was retried after its transient 502, not failed.
    assert len(auth_calls) == 2


def test_parse_retry_after_seconds():
    from docker_mcp.tools.registry import _parse_retry_after

    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after("  5  ") == 5.0


def test_parse_retry_after_http_date_in_future():
    from docker_mcp.tools.registry import _parse_retry_after

    # An HTTP date far in the future should produce a positive value (the absolute number
    # depends on the wall clock, so only assert ordering).
    result = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert result is not None
    assert result > 1000


def test_parse_retry_after_treats_naive_date_as_utc():
    """RFC 7231 says HTTP-dates are UTC. `-0000` parses to a naive datetime; we must
    treat it as UTC rather than letting `.timestamp()` re-interpret in local time."""
    from docker_mcp.tools.registry import _parse_retry_after

    # `-0000` is the only HTTP-date timezone notation that produces a naive datetime
    # out of email.utils.parsedate_to_datetime. The same wall-clock moment expressed
    # as `-0000` and `+0000` must yield the same delay value.
    naive = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 -0000")
    aware = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 +0000")
    assert naive is not None and aware is not None
    # Allow a 1s slack because two calls to time.time() bracket the math.
    assert abs(naive - aware) < 1.0


def test_parse_retry_after_invalid_returns_none():
    from docker_mcp.tools.registry import _parse_retry_after

    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not a date or number") is None


# ---------- _env_credentials ----------


def test_env_credentials_explicit_args_win(monkeypatch):
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_USERNAME", "envuser")
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_PASSWORD", "envpass")
    assert _env_credentials("arguser", "argpass") == ("arguser", "argpass")


def test_env_credentials_partial_args_do_not_mix_with_env(monkeypatch):
    # A username argument with no password must not silently pick up the env password.
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_USERNAME", "envuser")
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_PASSWORD", "envpass")
    assert _env_credentials("arguser", None) == ("arguser", None)
    assert _env_credentials(None, "argpass") == (None, "argpass")


def test_env_credentials_fall_back_to_env_when_both_unset(monkeypatch):
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_USERNAME", "envuser")
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_PASSWORD", "envpass")
    assert _env_credentials(None, None) == ("envuser", "envpass")


def test_env_credentials_default_anonymous(monkeypatch):
    monkeypatch.delenv("DOCKER_MCP_SERVER_REGISTRY_USERNAME", raising=False)
    monkeypatch.delenv("DOCKER_MCP_SERVER_REGISTRY_PASSWORD", raising=False)
    assert _env_credentials(None, None) == (None, None)


def test_registry_list_tags_uses_env_credentials_for_token_auth(monkeypatch):
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_USERNAME", "envuser")
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_PASSWORD", "envpass")
    saw_auth = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.com":
            saw_auth["header"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={"token": "tok"})
        if "Authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",service="reg.example.com"'},
            )
        return httpx.Response(200, json={"name": "foo/bar", "tags": ["v1"]})

    with _mock_client(handler):
        result = registry_tags("reg.example.com/foo/bar")
    assert result["tags"] == ["v1"]
    # The env credentials were sent (basic auth) to the token endpoint without transiting tool args.
    assert saw_auth["header"].startswith("Basic ")


def test_registry_inspect_manifest_uses_env_credentials_for_token_auth(monkeypatch):
    # Same env-fallback flow as registry_tags, but through the manifest endpoint — both tools
    # share _env_credentials + _registry_get, and both must keep credentials out of tool arguments.
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_USERNAME", "envuser")
    monkeypatch.setenv("DOCKER_MCP_SERVER_REGISTRY_PASSWORD", "envpass")
    saw_auth = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.com":
            saw_auth["header"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={"token": "tok"})
        if "Authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",service="reg.example.com"'},
            )
        assert request.url.path == "/v2/foo/bar/manifests/v2"
        return httpx.Response(
            200,
            json={"schemaVersion": 2},
            headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json", "Docker-Content-Digest": "sha256:d"},
        )

    with _mock_client(handler):
        result = registry_manifest("reg.example.com/foo/bar", reference="v2")
    assert result["manifest"] == {"schemaVersion": 2}
    assert result["digest"] == "sha256:d"
    assert saw_auth["header"].startswith("Basic ")


# ---------- platform / ratelimit helpers ----------


def test_parse_platform_two_and_three_parts():
    assert _parse_platform("linux/amd64") == ("linux", "amd64", None)
    assert _parse_platform("linux/arm/v7") == ("linux", "arm", "v7")


def test_parse_platform_invalid_raises():
    with pytest.raises(ValueError, match="os/arch"):
        _parse_platform("linux")


def test_select_platform_digest_matches_and_skips_attestation():
    index = {
        "manifests": [
            {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
        ]
    }
    assert _select_platform_digest(index, "linux/amd64") == ("sha256:amd", "linux/amd64")


def test_select_platform_digest_honors_variant():
    index = {
        "manifests": [
            {"digest": "sha256:v6", "platform": {"os": "linux", "architecture": "arm", "variant": "v6"}},
            {"digest": "sha256:v7", "platform": {"os": "linux", "architecture": "arm", "variant": "v7"}},
        ]
    }
    assert _select_platform_digest(index, "linux/arm/v7") == ("sha256:v7", "linux/arm/v7")


def test_select_platform_digest_omitted_variant_reports_actual_selected_variant():
    # "linux/arm64" (no variant) matches the arm64/v8 entry, and the returned platform reflects the
    # variant actually selected so the caller is never misled about which one they got.
    index = {
        "manifests": [{"digest": "sha256:v8", "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}}]
    }
    assert _select_platform_digest(index, "linux/arm64") == ("sha256:v8", "linux/arm64/v8")


def test_select_platform_digest_no_match_lists_available():
    index = {"manifests": [{"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]}
    with pytest.raises(ValueError, match="linux/amd64"):
        _select_platform_digest(index, "windows/amd64")


def test_parse_ratelimit_header_variants():
    assert _parse_ratelimit_header("100;w=21600") == (100, 21600)
    assert _parse_ratelimit_header("100") == (100, None)
    assert _parse_ratelimit_header(None) == (None, None)
    assert _parse_ratelimit_header("garbage") == (None, None)


# ---------- registry_image_config ----------


def test_registry_get_config_single_platform_fetches_blob():
    config_blob = {"architecture": "amd64", "os": "linux", "config": {"Entrypoint": ["/bin/sh"]}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/library/alpine/manifests/3.19":
            return httpx.Response(
                200,
                json={
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"digest": "sha256:cfg", "mediaType": "application/vnd.oci.image.config.v1+json"},
                    "layers": [],
                },
            )
        if path == "/v2/library/alpine/blobs/sha256:cfg":
            return httpx.Response(200, json=config_blob)
        return httpx.Response(404)

    with _mock_client(handler):
        result = registry_image_config("alpine", reference="3.19")
    assert result["name"] == "library/alpine"
    assert result["registry"] == "registry-1.docker.io"
    assert result["platform"] is None  # single-platform image
    assert result["config_digest"] == "sha256:cfg"
    assert result["config"] == config_blob


def test_registry_get_config_selects_platform_from_index():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/library/alpine/manifests/latest":
            return httpx.Response(
                200,
                json={
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [
                        {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
                        {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                    ],
                },
            )
        if path == "/v2/library/alpine/manifests/sha256:arm":
            return httpx.Response(200, json={"config": {"digest": "sha256:armcfg"}, "layers": []})
        if path == "/v2/library/alpine/blobs/sha256:armcfg":
            return httpx.Response(200, json={"architecture": "arm64", "os": "linux"})
        return httpx.Response(404)

    with _mock_client(handler):
        result = registry_image_config("alpine", platform="linux/arm64")
    assert result["platform"] == "linux/arm64"
    assert result["config_digest"] == "sha256:armcfg"
    assert result["config"]["architecture"] == "arm64"


def test_registry_get_config_reports_actual_variant_not_caller_input():
    # Caller asks for "linux/arm64" (no variant); the index only has arm64/v8. The reported platform
    # must reflect the entry actually selected (".../v8"), not the bare string the caller passed.
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/library/alpine/manifests/latest":
            return httpx.Response(
                200,
                json={
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [
                        {"digest": "sha256:v8", "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
                    ],
                },
            )
        if path == "/v2/library/alpine/manifests/sha256:v8":
            return httpx.Response(200, json={"config": {"digest": "sha256:cfg"}, "layers": []})
        if path == "/v2/library/alpine/blobs/sha256:cfg":
            return httpx.Response(200, json={"architecture": "arm64", "variant": "v8", "os": "linux"})
        return httpx.Response(404)

    with _mock_client(handler):
        result = registry_image_config("alpine", platform="linux/arm64")
    assert result["platform"] == "linux/arm64/v8"


def test_registry_get_config_raises_when_no_config_descriptor():
    def handler(request: httpx.Request) -> httpx.Response:
        # Neither an index ("manifests") nor a normal image manifest ("config").
        return httpx.Response(200, json={"mediaType": "application/weird", "layers": []})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="no config descriptor"):
            registry_image_config("alpine", reference="3.19")


# ---------- hub_rate_limit ----------


def _ratelimit_handler(final: httpx.Response):
    """Build a handler that walks the bearer flow and returns `final` for the authed HEAD."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.docker.io":
            return httpx.Response(200, json={"token": "t"})
        if "Authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'
                },
            )
        return final

    return handler


def test_hub_rate_limit_anonymous_reports_remaining():
    final = httpx.Response(200, headers={"RateLimit-Limit": "100;w=21600", "RateLimit-Remaining": "76;w=21600"})
    with _mock_client(_ratelimit_handler(final)):
        result = hub_rate_limit()
    assert result == {
        "authenticated": False,
        "limit": 100,
        "remaining": 76,
        "window_seconds": 21600,
        "unlimited": False,
    }


def test_hub_rate_limit_reports_zero_on_429_without_raising():
    final = httpx.Response(429, headers={"RateLimit-Limit": "100;w=21600", "RateLimit-Remaining": "0;w=21600"})
    with _mock_client(_ratelimit_handler(final)):
        result = hub_rate_limit()
    assert result["limit"] == 100
    assert result["remaining"] == 0


def test_hub_rate_limit_unlimited_when_no_headers():
    with _mock_client(_ratelimit_handler(httpx.Response(200))):
        result = hub_rate_limit(username="u", password="p")
    assert result["authenticated"] is True
    assert result["unlimited"] is True
    assert result["limit"] is None
    assert result["remaining"] is None


def test_registry_response_size_is_capped(monkeypatch):
    # An oversized (e.g. malicious) registry response is rejected, not buffered unbounded into memory.
    monkeypatch.setattr("docker_mcp.tools.registry._MAX_RESPONSE_BYTES", 20)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tags": ["a", "b", "c", "d", "e", "f"]})  # > 20 bytes

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="refusing to buffer a response this large"):
            registry_tags("alpine")


# ---------- registry_tag_wait ----------


def test_registry_tag_wait_already_present_returns_immediately():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tags": ["1.0", "1.1", "latest"]})

    with _mock_client(handler), patch("docker_mcp.tools.registry.time.sleep") as sleep:
        result = registry_tag_wait("alpine", "latest", timeout_seconds=5)
    assert result["met"] is True
    assert result["timed_out"] is False
    sleep.assert_not_called()


def test_registry_tag_wait_forwards_limit_to_registry_tags():
    with patch("docker_mcp.tools.registry.registry_tags") as mock_tags:
        mock_tags.return_value = {"tags": ["1.0"]}
        registry_tag_wait("alpine", "9.9", limit=5, timeout_seconds=0.0)
    mock_tags.assert_called_once_with("alpine", username=None, password=None, limit=5)


def test_registry_tag_wait_polls_until_tag_appears():
    responses = iter(
        [
            httpx.Response(200, json={"tags": ["1.0"]}),
            httpx.Response(200, json={"tags": ["1.0", "1.1"]}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    with _mock_client(handler), patch("docker_mcp.tools.registry.time.sleep") as sleep:
        result = registry_tag_wait("alpine", "1.1", timeout_seconds=5, poll_interval=0.01)
    assert result["met"] is True
    sleep.assert_called_once()


def test_registry_tag_wait_times_out():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tags": ["1.0"]})

    with _mock_client(handler):
        result = registry_tag_wait("alpine", "9.9", timeout_seconds=0.0)
    assert result["met"] is False
    assert result["timed_out"] is True


def test_registry_tag_wait_rejects_negative_timeout():
    with pytest.raises(ValueError, match="timeout_seconds"):
        registry_tag_wait("alpine", "latest", timeout_seconds=-1)


def test_registry_tag_wait_rejects_nonpositive_poll_interval():
    with pytest.raises(ValueError, match="poll_interval"):
        registry_tag_wait("alpine", "latest", poll_interval=0)

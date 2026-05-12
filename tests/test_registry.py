import json
from unittest.mock import patch

import httpx
import pytest

from tools.registry import (
    _next_link,
    _parse_bearer_challenge,
    _parse_image_ref,
    _strip_tag_and_digest,
    hub_list_tags,
    hub_repo_info,
    registry_inspect_manifest,
    registry_list_tags,
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


# ---------- _next_link ----------


def test_next_link_finds_next_only():
    h = '<https://example.com/page2>; rel="next", <https://example.com/last>; rel="last"'
    assert _next_link(h) == "https://example.com/page2"


def test_next_link_no_next_returns_none():
    assert _next_link('<https://example.com/last>; rel="last"') is None


def test_next_link_none_input():
    assert _next_link(None) is None


# ---------- registry_list_tags ----------


def _mock_client(transport_handler):
    """Patch httpx.Client to use a MockTransport that delegates to `transport_handler`."""
    transport = httpx.MockTransport(transport_handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    return patch("tools.registry.httpx.Client", side_effect=factory)


def test_registry_list_tags_single_page_anonymous():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/library/alpine/tags/list"
        assert request.url.host == "registry-1.docker.io"
        return httpx.Response(200, json={"name": "library/alpine", "tags": ["3.18", "3.19", "latest"]})

    with _mock_client(handler):
        result = registry_list_tags("alpine")

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
        result = registry_list_tags("reg.example.com/foo/bar")

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
        result = registry_list_tags("alpine")

    assert result["tags"] == ["a", "b", "c", "d"]
    assert result["truncated"] is False


def test_registry_list_tags_respects_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tags": ["t1", "t2", "t3", "t4", "t5"]})

    with _mock_client(handler):
        result = registry_list_tags("alpine", limit=3)

    assert result["tags"] == ["t1", "t2", "t3"]
    assert result["truncated"] is True


def test_registry_list_tags_rejects_non_positive_limit():
    with pytest.raises(ValueError, match="limit must be >= 1"):
        registry_list_tags("alpine", limit=0)
    with pytest.raises(ValueError, match="limit must be >= 1"):
        registry_list_tags("alpine", limit=-5)


def test_registry_list_tags_raises_on_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _mock_client(handler):
        with pytest.raises(httpx.HTTPStatusError):
            registry_list_tags("alpine")


# ---------- registry_inspect_manifest ----------


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
        result = registry_inspect_manifest("alpine", reference="3.19")

    assert captured["path"] == "/v2/library/alpine/manifests/3.19"
    assert "application/vnd.oci.image.manifest.v1+json" in (captured["accept"] or "")
    assert "application/vnd.oci.image.index.v1+json" in (captured["accept"] or "")
    assert result["digest"] == "sha256:deadbeef"
    assert result["media_type"] == "application/vnd.oci.image.manifest.v1+json"
    assert result["manifest"]["schemaVersion"] == 2


# ---------- hub_list_tags ----------


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
        result = hub_list_tags("alpine")

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
        result = hub_list_tags("myorg/img", limit=3)

    assert [t["name"] for t in result["tags"]] == ["v0", "v1", "v2"]
    assert result["truncated"] is True


def test_hub_list_tags_rejects_non_positive_limit():
    with pytest.raises(ValueError, match="limit must be >= 1"):
        hub_list_tags("alpine", limit=0)


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
        result = registry_list_tags("alpine")

    assert result["tags"] == ["v1", "v2"]
    assert len(calls) == 2


def test_registry_list_tags_raises_when_retry_after_is_long():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited.*retry after ~3600s") as excinfo:
            registry_list_tags("alpine")
    # Default registry is Docker Hub — message should mention the Hub-specific cap.
    assert "Docker Hub" in str(excinfo.value)


def test_registry_list_tags_message_is_generic_for_non_hub_registry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited") as excinfo:
            registry_list_tags("ghcr.io/org/repo")
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
            registry_list_tags("alpine")


def test_registry_list_tags_raises_on_second_429_after_retry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited"):
            registry_list_tags("alpine")


def test_hub_list_tags_applies_429_policy():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"next": None, "results": [{"name": "v1"}]})

    with _mock_client(handler):
        result = hub_list_tags("alpine")

    assert [t["name"] for t in result["tags"]] == ["v1"]
    assert len(calls) == 2


def test_hub_repo_info_applies_429_policy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with _mock_client(handler):
        with pytest.raises(RuntimeError, match="rate-limited"):
            hub_repo_info("alpine")


def test_parse_retry_after_seconds():
    from tools.registry import _parse_retry_after

    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after("  5  ") == 5.0


def test_parse_retry_after_http_date_in_future():
    from tools.registry import _parse_retry_after

    # An HTTP date far in the future should produce a positive value (the absolute number
    # depends on the wall clock, so only assert ordering).
    result = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert result is not None
    assert result > 1000


def test_parse_retry_after_treats_naive_date_as_utc():
    """RFC 7231 says HTTP-dates are UTC. `-0000` parses to a naive datetime; we must
    treat it as UTC rather than letting `.timestamp()` re-interpret in local time."""
    from tools.registry import _parse_retry_after

    # `-0000` is the only HTTP-date timezone notation that produces a naive datetime
    # out of email.utils.parsedate_to_datetime. The same wall-clock moment expressed
    # as `-0000` and `+0000` must yield the same delay value.
    naive = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 -0000")
    aware = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 +0000")
    assert naive is not None and aware is not None
    # Allow a 1s slack because two calls to time.time() bracket the math.
    assert abs(naive - aware) < 1.0


def test_parse_retry_after_invalid_returns_none():
    from tools.registry import _parse_retry_after

    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not a date or number") is None

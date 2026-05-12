import json
from unittest.mock import patch

import httpx
import pytest

from tools.registry import (
    _next_link,
    _parse_bearer_challenge,
    _parse_image_ref,
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


# ---------- hub_repo_info ----------


def test_hub_repo_info_returns_body():
    expected = {
        "user": "library",
        "name": "alpine",
        "star_count": 12345,
        "pull_count": 999999999,
        "is_private": False,
    }
    with patch("tools.registry.httpx.get") as fake_get:
        fake_get.return_value = httpx.Response(
            200,
            content=json.dumps(expected).encode(),
            request=httpx.Request("GET", "https://hub.docker.com/v2/repositories/library/alpine/"),
            headers={"Content-Type": "application/json"},
        )
        result = hub_repo_info("alpine")
    assert result == expected
    fake_get.assert_called_once()
    called_url = fake_get.call_args.args[0]
    assert called_url == "https://hub.docker.com/v2/repositories/library/alpine/"

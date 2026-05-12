# integration tests for buildx — require a real Docker daemon AND the `docker buildx` plugin.
# run with: uv run pytest -m integration

from pathlib import Path

import pytest

from tools._cli import has_plugin
from tools.buildx import buildx_build, buildx_du, buildx_imagetools_inspect, buildx_ls

# A minimal Dockerfile that produces a tiny image without pulling anything large.
# `scratch` is the empty base image and ships with the buildx plugin's defaults.
_DOCKERFILE = """\
FROM scratch
COPY hello.txt /hello.txt
"""


@pytest.fixture(scope="module", autouse=True)
def _require_buildx_plugin():
    if not has_plugin("buildx"):
        pytest.skip("docker buildx plugin not installed on this host; skipping buildx integration tests")
    yield


@pytest.fixture
def build_context(tmp_path: Path) -> Path:
    (tmp_path / "Dockerfile").write_text(_DOCKERFILE)
    (tmp_path / "hello.txt").write_text("hello\n")
    return tmp_path


def test_buildx_ls_lists_at_least_one_builder():
    builders = buildx_ls()
    assert isinstance(builders, list)
    assert builders, "expected at least one buildx builder to be configured"
    assert all("Name" in b for b in builders)


def test_buildx_du_returns_records():
    records = buildx_du()
    # An empty cache is allowed but the call must succeed and return a list.
    assert isinstance(records, list)


def test_buildx_build_scratch_context_succeeds(build_context: Path):
    result = buildx_build(
        context=str(build_context),
        tags=["docker-mcp-it-buildx-scratch:test"],
        load=True,
        timeout_seconds=300.0,
    )
    assert result["returncode"] == 0, result["stderr"]


def test_buildx_imagetools_inspect_alpine_returns_manifest():
    # `alpine:3` is a multi-arch manifest list on Docker Hub. The call hits the registry
    # over HTTPS via buildx; no local image is required.
    result = buildx_imagetools_inspect("alpine:3", raw=True)
    if result["returncode"] != 0:
        pytest.skip(f"buildx imagetools inspect unreachable (registry/network?): {result['stderr'][:200]}")
    assert result["stdout"].strip().startswith("{")

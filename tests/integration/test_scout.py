# integration tests for scout — require a real Docker daemon AND the `docker scout` plugin.
# Scout is NOT pre-installed on plain Engine hosts (only Docker Desktop), so the whole
# module skips cleanly when the plugin isn't available.
# run with: uv run pytest -m integration

import pytest

from tools._cli import has_plugin
from tools.scout import scout_quickview


@pytest.fixture(scope="module", autouse=True)
def _require_scout_plugin():
    if not has_plugin("scout"):
        pytest.skip("docker scout plugin not installed on this host; skipping scout integration tests")
    yield


def test_scout_quickview_alpine_returns_json_or_skip():
    # Scout requires network access to its CDN. If the CDN is unreachable or the host
    # is offline, skip rather than fail — this test exercises the wiring, not Scout itself.
    result = scout_quickview("alpine:3")
    if result["raw"]["returncode"] != 0:
        pytest.skip(f"scout quickview unreachable (offline or auth required?): {result['raw']['stderr'][:200]}")
    assert result["format"] == "json"
    # `result` should be a parsed dict or the raw text (if Scout returned non-JSON for some reason).
    assert result["result"] is not None

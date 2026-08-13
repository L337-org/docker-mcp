# integration tests for scout — require a real Docker daemon AND the `docker scout` plugin.
# Scout is NOT pre-installed on plain Engine hosts (only Docker Desktop), so the whole
# module skips cleanly when the plugin isn't available.
# run with: uv run pytest -m integration

import pytest

from docker_mcp.tools._cli import has_plugin
from docker_mcp.tools.scout import scout_quickview
from tests.integration.conftest import fail_unless_environmental


@pytest.fixture(scope="module", autouse=True)
def _require_scout_plugin():
    if not has_plugin("scout"):
        pytest.skip("docker scout plugin not installed on this host; skipping scout integration tests")
    yield


def test_scout_quickview_alpine_runs_with_default_arguments():
    # Skips only for a named environmental cause (Scout's CDN unreachable, or no credentials).
    # Any other non-zero exit fails: this test exists to catch exactly the kind of drift that had
    # `quickview` passing a --format flag Scout does not define, which the old "skip on any
    # non-zero exit" form reported as "offline or auth required" on a working machine.
    result = scout_quickview("alpine:3")
    fail_unless_environmental(
        returncode=result["raw"]["returncode"],
        stderr=result["raw"]["stderr"],
        stdout=result["raw"]["stdout"],
        what="scout quickview",
    )
    assert result["result"] is not None
    assert "format" not in result  # quickview has no --format flag; see DM-21

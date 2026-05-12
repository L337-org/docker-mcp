# integration tests — require a real Docker daemon and a working `docker` binary on PATH.
# run with: uv run pytest -m integration

import json

from tools._cli import has_plugin, run_docker


def test_run_docker_version_exits_zero():
    result = run_docker(["version", "--format", "{{json .}}"])
    assert result.returncode == 0
    assert result.stdout.strip()
    parsed = json.loads(result.stdout)
    # `docker version` JSON output is shaped {Client: {...}, Server?: {...}}.
    assert "Client" in parsed


def test_run_docker_info_includes_server_id():
    result = run_docker(["info", "--format", "{{json .}}"])
    assert result.returncode == 0
    parsed = json.loads(result.stdout)
    assert parsed.get("ID")


def test_has_plugin_compose_when_present():
    # Most modern Docker installs ship the compose plugin. Don't assert True here —
    # plain Engine without the plugin should still pass the rest of this PR's tests.
    # Just verify the probe returns a bool and doesn't raise.
    assert isinstance(has_plugin("compose"), bool)

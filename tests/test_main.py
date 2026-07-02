from unittest.mock import patch

import docker_mcp


def test_version_flag_prints_and_exits(capsys, monkeypatch):
    """
    `--version` must print the installed version and return without starting the server —
    the weekly canary's `uvx docker-mcp-server --version` smoke depends on this exiting.
    """
    monkeypatch.setattr("sys.argv", ["docker-mcp-server", "--version"])
    with (
        patch.object(docker_mcp.mcp, "run") as mock_run,
        patch("docker_mcp.tools.client.startup_preflight") as mock_preflight,
    ):
        docker_mcp.main()
    mock_run.assert_not_called()
    mock_preflight.assert_not_called()

    from importlib.metadata import version

    assert capsys.readouterr().out.strip() == version("docker-mcp-server")


def test_main_without_version_flag_runs_server():
    """Without --version, main() still runs preflight then serves on stdio."""
    with (
        patch.object(docker_mcp.mcp, "run") as mock_run,
        patch("docker_mcp.tools.client.startup_preflight") as mock_preflight,
    ):
        docker_mcp.main()
    mock_preflight.assert_called_once()
    mock_run.assert_called_once_with(transport="stdio")

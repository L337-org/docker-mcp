from unittest.mock import patch

import pytest

from docker_mcp.exceptions import ToolInputError
from docker_mcp.tools._cli import CliResult
from docker_mcp.tools.scout import (
    _JSON_FORMATS,
    _maybe_parse_json,
    scout_compare,
    scout_cves,
    scout_quickview,
    scout_recommendations,
    scout_sbom,
)


@pytest.fixture(autouse=True)
def _stub_plugin_check():  # pyright: ignore[reportUnusedFunction]
    with patch("docker_mcp.tools.scout.require_plugin"):
        yield


def _ok(stdout: str = "", stderr: str = "") -> CliResult:
    return CliResult(returncode=0, stdout=stdout, stderr=stderr, truncated=False)


# ---------- _maybe_parse_json ----------


def test_maybe_parse_json_returns_dict_when_format_is_json():
    assert _maybe_parse_json('{"a": 1}', "json") == {"a": 1}


def test_maybe_parse_json_returns_raw_text_for_non_json_format():
    assert _maybe_parse_json("plain text", "text") == "plain text"


def test_maybe_parse_json_returns_none_for_empty_input():
    assert _maybe_parse_json("", "json") is None


def test_maybe_parse_json_returns_raw_when_json_invalid():
    # Bad JSON when format=json — return raw text rather than raise so the agent can debug.
    assert _maybe_parse_json("not-json", "json") == "not-json"


# ---------- scout_cves ----------


def test_scout_cves_defaults_to_a_format_scout_actually_accepts():
    """`docker scout cves` has no plain "json" format, so the old default failed on every call.

    Its accepted values are packages/sarif/spdx/gitlab/markdown/sbom. This asserts the default is
    one of those and is a JSON-emitting one, so the parsed-`result` contract still holds.
    """
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok('{"runs": []}')) as run:
        result = scout_cves("alpine:3.19")
    args = run.call_args.args[0]
    assert args[:2] == ["scout", "cves"]
    passed = args[args.index("--format") + 1]
    assert passed in {"packages", "sarif", "spdx", "gitlab", "markdown", "sbom"}
    assert passed in _JSON_FORMATS
    assert args[-1] == "alpine:3.19"
    assert result["format"] == passed
    assert result["result"] == {"runs": []}
    assert result["raw"]["returncode"] == 0


def test_scout_cves_only_severity_joins_with_commas():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("{}")) as run:
        scout_cves("alpine:3.19", only_severity=["critical", "high"])
    args = run.call_args.args[0]
    assert args[args.index("--only-severity") + 1] == "critical,high"


def test_scout_cves_flags_set_correctly():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("{}")) as run:
        scout_cves("alpine:3.19", only_fixed=True, ignore_base=True, platform="linux/amd64")
    args = run.call_args.args[0]
    assert "--only-fixed" in args
    assert "--ignore-base" in args
    assert args[args.index("--platform") + 1] == "linux/amd64"


def test_scout_cves_sarif_is_parsed_because_sarif_is_json():
    """SARIF is a JSON schema. Keying the parse on the literal string "json" left it unparsed."""
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok('{"$schema":"https://x/sarif"}')) as run:
        result = scout_cves("alpine:3.19", format="sarif")
    args = run.call_args.args[0]
    assert args[args.index("--format") + 1] == "sarif"
    assert result["format"] == "sarif"
    assert result["result"] == {"$schema": "https://x/sarif"}


def test_scout_cves_markdown_is_returned_verbatim():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("## CVEs\n- none")):
        result = scout_cves("alpine:3.19", format="markdown")
    assert result["result"] == "## CVEs\n- none"


# ---------- scout_quickview ----------


def test_scout_quickview_never_passes_a_format_flag():
    """`docker scout quickview` has no --format flag; passing one failed with "unknown flag"."""
    body = "Target  alpine:3.19\n  0C  2H"
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok(body)) as run:
        result = scout_quickview("alpine:3.19")
    assert "--format" not in run.call_args.args[0]
    assert result["result"] == body
    assert "format" not in result


def test_scout_quickview_rejects_a_format_argument():
    """The parameter is gone rather than accepted-and-ignored, so a caller cannot silently get
    output in a shape the tool never produced."""
    with pytest.raises(TypeError):
        scout_quickview("alpine:3.19", format="text")  # type: ignore[call-arg]


# ---------- scout_recommendations ----------


def test_scout_recommendations_passes_only_flags():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("[]")) as run:
        scout_recommendations("alpine:3.19", only_refresh=True, only_update=True, tag="3.*")
    args = run.call_args.args[0]
    assert "--only-refresh" in args
    assert "--only-update" in args
    assert args[args.index("--tag") + 1] == "3.*"


# ---------- scout_compare ----------


def test_scout_compare_to_ref_target():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok('{"delta": []}')) as run:
        scout_compare("org/app:v2", to="org/app:v1")
    args = run.call_args.args[0]
    assert args[:2] == ["scout", "compare"]
    assert args[args.index("--to") + 1] == "org/app:v1"
    assert args[-1] == "org/app:v2"
    # `--to-latest` is a separate flag and must not be set when `--to` is.
    assert "--to-latest" not in args


def test_scout_compare_to_latest_target():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("{}")) as run:
        scout_compare("org/app:v2", to_latest=True)
    args = run.call_args.args[0]
    assert "--to-latest" in args
    assert "--to" not in args


def test_scout_compare_to_env_target():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("{}")) as run:
        scout_compare("org/app:v2", to_env="prod")
    args = run.call_args.args[0]
    assert args[args.index("--to-env") + 1] == "prod"


def test_scout_compare_requires_exactly_one_target():
    with pytest.raises(ToolInputError, match="exactly one of"):
        scout_compare("org/app:v2")
    with pytest.raises(ToolInputError, match="exactly one of"):
        scout_compare("org/app:v2", to="org/app:v1", to_latest=True)


def test_scout_compare_ignore_unchanged_and_severity():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("{}")) as run:
        scout_compare("org/app:v2", to="org/app:v1", ignore_unchanged=True, only_severity=["critical"])
    args = run.call_args.args[0]
    assert "--ignore-unchanged" in args
    assert args[args.index("--only-severity") + 1] == "critical"


# ---------- scout_sbom ----------


def test_scout_sbom_default_spdx_format_parses_json():
    body = '{"spdxVersion": "SPDX-2.3"}'
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok(body)) as run:
        result = scout_sbom("alpine:3.19")
    args = run.call_args.args[0]
    assert args[args.index("--format") + 1] == "spdx"
    assert result["format"] == "spdx"
    assert result["result"] == {"spdxVersion": "SPDX-2.3"}


def test_scout_sbom_cyclonedx_format_parses_json():
    body = '{"bomFormat": "CycloneDX"}'
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok(body)):
        result = scout_sbom("alpine:3.19", format="cyclonedx")
    assert result["result"] == {"bomFormat": "CycloneDX"}


def test_scout_sbom_list_format_returned_as_text():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("alpine 3.19\nlibc 2.39")):
        result = scout_sbom("alpine:3.19", format="list")
    assert "libc 2.39" in result["result"]


def test_scout_sbom_with_platform():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("{}")) as run:
        scout_sbom("alpine:3.19", platform="linux/arm64")
    args = run.call_args.args[0]
    assert args[args.index("--platform") + 1] == "linux/arm64"


# ---------- remote-exec fallback ----------


def test_scout_runs_remotely_when_no_local_plugin_is_available():
    # The whole point of the fallback: no local scout plugin, an ssh:// target, so the subcommand
    # runs there instead of raising. The plugin probe must not gate the remote path.
    with (
        patch("docker_mcp.tools.scout.should_remote_exec", return_value=True) as should,
        patch("docker_mcp.tools.scout.remote_exec_cli", return_value=_ok('{"runs": []}')) as remote,
        patch("docker_mcp.tools.scout.run_docker") as run,
        patch("docker_mcp.tools.scout.require_plugin") as require,
    ):
        result = scout_cves("alpine:3.19", host="prod")
    run.assert_not_called()
    require.assert_not_called()
    should.assert_called_once_with("prod", plugin="scout")
    assert remote.call_args.args == ("prod", ["scout", "cves", "--format", "sarif", "alpine:3.19"])
    assert remote.call_args.kwargs == {"timeout": 300.0}
    assert result["result"] == {"runs": []}  # SARIF-shaped, matching the default; parsed identically either way


def test_scout_uses_the_local_cli_when_it_can():
    with (
        patch("docker_mcp.tools.scout.should_remote_exec", return_value=False),
        patch("docker_mcp.tools.scout.remote_exec_cli") as remote,
        patch("docker_mcp.tools.scout.run_docker", return_value=_ok("{}")) as run,
        patch("docker_mcp.tools.scout.require_plugin") as require,
    ):
        scout_quickview("alpine:3.19", host="prod")
    remote.assert_not_called()
    require.assert_called_once_with("scout")
    assert run.call_args.kwargs["host"] == "prod"


def test_scout_compare_refuses_a_local_path_target_on_the_remote_path(tmp_path):
    # `to` may be a directory or archive; staging isn't supported here, so a path that exists locally
    # would silently resolve against the *remote* filesystem. Refuse and name the reason instead.
    local_dir = tmp_path / "old-image"
    local_dir.mkdir()
    with (
        patch("docker_mcp.tools.scout.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.scout.remote_exec_cli") as remote,
    ):
        with pytest.raises(ToolInputError, match="names a path on the host running this MCP server"):
            scout_compare("org/app:v2", to=str(local_dir), host="prod")
    remote.assert_not_called()


def test_scout_compare_allows_an_image_ref_target_on_the_remote_path():
    # Only an existing local path is refused — an ordinary reference (even one with a '/') goes through.
    with (
        patch("docker_mcp.tools.scout.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.scout.remote_exec_cli", return_value=_ok("{}")) as remote,
    ):
        scout_compare("org/app:v2", to="org/app:v1", host="prod")
    assert remote.call_args.args[1][-1] == "org/app:v2"


def test_scout_compare_allows_a_local_path_target_when_running_locally(tmp_path):
    local_dir = tmp_path / "old-image"
    local_dir.mkdir()
    with (
        patch("docker_mcp.tools.scout.should_remote_exec", return_value=False),
        patch("docker_mcp.tools.scout.run_docker", return_value=_ok("{}")) as run,
    ):
        scout_compare("org/app:v2", to=str(local_dir))
    assert run.call_args.args[0][run.call_args.args[0].index("--to") + 1] == str(local_dir)


# ---------- argument-injection defense ----------


def test_scout_cves_rejects_flag_like_image():
    with pytest.raises(ToolInputError, match="parses as a flag"):
        scout_cves(image="--output=/etc/passwd")


def test_scout_compare_rejects_flag_like_image():
    with pytest.raises(ToolInputError, match="parses as a flag"):
        scout_compare(image="-x", to="alpine:3.19")

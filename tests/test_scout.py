from unittest.mock import patch

import pytest

from docker_mcp.tools._cli import CliResult
from docker_mcp.tools.scout import (
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


def test_scout_cves_minimal_args_and_default_json_format():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok('{"vulnerabilities": []}')) as run:
        result = scout_cves("alpine:3.19")
    args = run.call_args.args[0]
    assert args[:2] == ["scout", "cves"]
    assert args[args.index("--format") + 1] == "json"
    assert args[-1] == "alpine:3.19"
    assert result["format"] == "json"
    assert result["result"] == {"vulnerabilities": []}
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


def test_scout_cves_sarif_format_returned_as_text():
    sarif_text = '{"$schema":"https://example.com/sarif"}'
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok(sarif_text)) as run:
        result = scout_cves("alpine:3.19", format="sarif")
    args = run.call_args.args[0]
    assert args[args.index("--format") + 1] == "sarif"
    assert result["format"] == "sarif"
    assert result["result"] == sarif_text


# ---------- scout_quickview ----------


def test_scout_quickview_parses_json():
    body = '{"critical": 0, "high": 2}'
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok(body)):
        result = scout_quickview("alpine:3.19")
    assert result["result"] == {"critical": 0, "high": 2}


def test_scout_quickview_text_format_unparsed():
    with patch("docker_mcp.tools.scout.run_docker", return_value=_ok("Image: alpine:3.19\nCritical: 0")) as run:
        result = scout_quickview("alpine:3.19", format="text")
    args = run.call_args.args[0]
    assert args[args.index("--format") + 1] == "text"
    assert "Critical: 0" in result["result"]


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
    with pytest.raises(ValueError, match="exactly one of"):
        scout_compare("org/app:v2")
    with pytest.raises(ValueError, match="exactly one of"):
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


# ---------- argument-injection defense ----------


def test_scout_cves_rejects_flag_like_image():
    with pytest.raises(ValueError, match="parses as a flag"):
        scout_cves(image="--output=/etc/passwd")


def test_scout_compare_rejects_flag_like_image():
    with pytest.raises(ValueError, match="parses as a flag"):
        scout_compare(image="-x", to="alpine:3.19")

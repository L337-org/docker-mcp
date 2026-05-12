from unittest.mock import patch

import pytest

from tools._cli import CliResult
from tools.buildx import (
    _parse_json_lines,
    buildx_bake,
    buildx_build,
    buildx_create,
    buildx_du,
    buildx_imagetools_create,
    buildx_imagetools_inspect,
    buildx_inspect,
    buildx_ls,
    buildx_prune,
    buildx_rm,
    buildx_use,
)


@pytest.fixture(autouse=True)
def _stub_plugin_check():  # pyright: ignore[reportUnusedFunction]
    with patch("tools.buildx.require_plugin"):
        yield


def _ok(stdout: str = "", stderr: str = "") -> CliResult:
    return CliResult(returncode=0, stdout=stdout, stderr=stderr, truncated=False)


def _fail(stderr: str, returncode: int = 1) -> CliResult:
    return CliResult(returncode=returncode, stdout="", stderr=stderr, truncated=False)


# ---------- _parse_json_lines ----------


def test_parse_json_lines_handles_ndjson():
    assert _parse_json_lines('{"a": 1}\n{"a": 2}\n') == [{"a": 1}, {"a": 2}]


def test_parse_json_lines_skips_blank_lines():
    assert _parse_json_lines('{"a": 1}\n\n{"a": 2}\n') == [{"a": 1}, {"a": 2}]


def test_parse_json_lines_empty_returns_empty_list():
    assert _parse_json_lines("") == []


# ---------- buildx_build ----------


def test_buildx_build_minimal_context_only():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".")
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "build", "--progress=plain"]
    assert args[-1] == "."  # context is positional and last


def test_buildx_build_passes_tags_and_platforms():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", tags=["org/app:v1", "org/app:latest"], platforms=["linux/amd64", "linux/arm64"])
    args = run.call_args.args[0]
    assert args.count("--tag") == 2
    assert args[args.index("org/app:v1") - 1] == "--tag"
    # buildx --platform takes a comma-joined list as one value (the documented convention).
    assert args.count("--platform") == 1
    assert args[args.index("--platform") + 1] == "linux/amd64,linux/arm64"


def test_buildx_build_single_platform_passes_one_flag():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", platforms=["linux/amd64"])
    args = run.call_args.args[0]
    assert args.count("--platform") == 1
    assert args[args.index("--platform") + 1] == "linux/amd64"


def test_buildx_build_omits_platform_when_not_supplied():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".")
    assert "--platform" not in run.call_args.args[0]


def test_buildx_build_dict_args_emit_repeated_flags():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(
            context=".",
            build_args={"VERSION": "1.0", "DEBUG": "1"},
            build_contexts={"deps": "./vendor"},
            labels={"org.opencontainers.image.source": "https://example.com"},
        )
    args = run.call_args.args[0]
    build_arg_values = [args[i + 1] for i, a in enumerate(args) if a == "--build-arg"]
    assert set(build_arg_values) == {"VERSION=1.0", "DEBUG=1"}
    assert "--build-context" in args
    assert args[args.index("--build-context") + 1] == "deps=./vendor"
    assert args[args.index("--label") + 1] == "org.opencontainers.image.source=https://example.com"


def test_buildx_build_push_and_load_flags_independent():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", push=True)
    assert "--push" in run.call_args.args[0]

    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", load=True)
    assert "--load" in run.call_args.args[0]


def test_buildx_build_cache_and_attestation_flags():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(
            context=".",
            cache_from=["type=registry,ref=org/cache"],
            cache_to=["type=registry,ref=org/cache,mode=max"],
            sbom="true",
            provenance="mode=max",
            attest=["type=foo"],
            no_cache_filter=["build", "test"],
        )
    args = run.call_args.args[0]
    assert "type=registry,ref=org/cache" in args
    assert "type=registry,ref=org/cache,mode=max" in args
    assert args[args.index("--sbom") + 1] == "true"
    assert args[args.index("--provenance") + 1] == "mode=max"
    assert "--attest" in args
    assert args.count("--no-cache-filter") == 2


def test_buildx_build_secret_and_ssh():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", secret=["id=npmrc,src=~/.npmrc"], ssh=["default"])
    args = run.call_args.args[0]
    assert args[args.index("--secret") + 1] == "id=npmrc,src=~/.npmrc"
    assert args[args.index("--ssh") + 1] == "default"


def test_buildx_build_returns_returncode_dict():
    with patch("tools.buildx.run_docker", return_value=_fail("build failed", returncode=2)):
        result = buildx_build(context=".")
    assert result["returncode"] == 2
    assert result["stderr"] == "build failed"


def test_buildx_build_rejects_push_and_load_together():
    with pytest.raises(ValueError, match="`push` and `load` are mutually exclusive"):
        buildx_build(context=".", push=True, load=True)


# ---------- buildx_bake ----------


def test_buildx_bake_minimal_uses_progress_plain():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_bake()
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "bake", "--progress=plain"]


def test_buildx_bake_targets_appended_last():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_bake(targets=["app", "tests"], files=["docker-bake.hcl"], push=True)
    args = run.call_args.args[0]
    # Targets are positional, must come after all flags
    assert args[-2:] == ["app", "tests"]
    assert "--push" in args
    assert args[args.index("-f") + 1] == "docker-bake.hcl"


def test_buildx_bake_set_overrides_repeat():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_bake(set_overrides=["app.platform=linux/amd64", "tests.no-cache=true"])
    args = run.call_args.args[0]
    assert args.count("--set") == 2


# ---------- buildx_imagetools_inspect ----------


def test_buildx_imagetools_inspect_default_args():
    with patch("tools.buildx.run_docker", return_value=_ok("[ ... ]")) as run:
        result = buildx_imagetools_inspect("alpine:3.19")
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "imagetools", "inspect"]
    assert args[-1] == "alpine:3.19"
    assert "--raw" not in args
    assert "--format" not in args
    assert result["returncode"] == 0


def test_buildx_imagetools_inspect_raw_and_format():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_imagetools_inspect("alpine:3.19", raw=True)
    assert "--raw" in run.call_args.args[0]

    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_imagetools_inspect("alpine:3.19", format="{{json .}}")
    args = run.call_args.args[0]
    assert args[args.index("--format") + 1] == "{{json .}}"


def test_buildx_imagetools_inspect_rejects_raw_and_format_together():
    with pytest.raises(ValueError, match="`raw` and `format` are mutually exclusive"):
        buildx_imagetools_inspect("alpine:3.19", raw=True, format="{{json .}}")


# ---------- buildx_imagetools_create ----------


def test_buildx_imagetools_create_target_and_sources():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_imagetools_create(
            target="org/app:v1",
            sources=["org/app:v1-amd64", "org/app:v1-arm64"],
        )
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "imagetools", "create"]
    assert args[args.index("--tag") + 1] == "org/app:v1"
    assert args[-2:] == ["org/app:v1-amd64", "org/app:v1-arm64"]


def test_buildx_imagetools_create_append_dry_run_annotations():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_imagetools_create(
            target="org/app:v1",
            sources=["org/app:v1-amd64"],
            append=True,
            dry_run=True,
            annotations=["manifest:com.example.k=v"],
        )
    args = run.call_args.args[0]
    assert "--append" in args
    assert "--dry-run" in args
    assert args[args.index("--annotation") + 1] == "manifest:com.example.k=v"


def test_buildx_imagetools_create_requires_sources_or_files():
    with pytest.raises(ValueError, match="at least one source ref or file"):
        buildx_imagetools_create(target="org/app:v1", sources=[])


# ---------- buildx_ls / buildx_du / buildx_inspect ----------


def test_buildx_ls_parses_ndjson():
    body = '{"Name":"default","Driver":"docker","Current":true}\n{"Name":"remote","Driver":"docker-container","Current":false}\n'
    with patch("tools.buildx.run_docker", return_value=_ok(body)) as run:
        result = buildx_ls()
    args = run.call_args.args[0]
    assert args == ["buildx", "ls", "--format", "{{json .}}"]
    assert result == [
        {"Name": "default", "Driver": "docker", "Current": True},
        {"Name": "remote", "Driver": "docker-container", "Current": False},
    ]


def test_buildx_ls_raises_on_failure():
    with patch("tools.buildx.run_docker", return_value=_fail("daemon error")):
        with pytest.raises(RuntimeError, match="daemon error"):
            buildx_ls()


def test_buildx_du_parses_ndjson():
    body = '{"ID":"abc","Size":"1MB"}\n{"ID":"def","Size":"2MB"}\n'
    with patch("tools.buildx.run_docker", return_value=_ok(body)) as run:
        result = buildx_du(builder="builder-x")
    args = run.call_args.args[0]
    assert args[:4] == ["buildx", "du", "--format", "{{json .}}"]
    assert args[args.index("--builder") + 1] == "builder-x"
    assert result == [{"ID": "abc", "Size": "1MB"}, {"ID": "def", "Size": "2MB"}]


def test_buildx_inspect_with_bootstrap():
    with patch("tools.buildx.run_docker", return_value=_ok("Name: default")) as run:
        buildx_inspect(bootstrap=True)
    args = run.call_args.args[0]
    assert "--bootstrap" in args


# ---------- buildx_prune ----------


def test_buildx_prune_always_passes_force():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_prune()
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "prune", "--force"]


def test_buildx_prune_filter_and_space_flags():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_prune(
            all=True,
            filter={"until": "24h", "type": "exec.cachemount"},
            reserved_space="10GB",
            max_used_space="20GB",
            min_free_space="5GB",
        )
    args = run.call_args.args[0]
    assert "--all" in args
    assert args.count("--filter") == 2
    assert args[args.index("--reserved-space") + 1] == "10GB"
    assert args[args.index("--max-used-space") + 1] == "20GB"
    assert args[args.index("--min-free-space") + 1] == "5GB"


# ---------- buildx_create / buildx_use / buildx_rm ----------


def test_buildx_create_driver_opts_repeat():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_create(
            name="builder-x",
            driver="docker-container",
            driver_opts={"image": "moby/buildkit:latest", "network": "host"},
            use=True,
            bootstrap=True,
            platforms=["linux/amd64", "linux/arm64"],
        )
    args = run.call_args.args[0]
    assert args[:2] == ["buildx", "create"]
    assert args[args.index("--driver") + 1] == "docker-container"
    assert args.count("--driver-opt") == 2
    assert "--use" in args
    assert "--bootstrap" in args
    # Comma-joined platforms (the documented buildx convention).
    assert args.count("--platform") == 1
    assert args[args.index("--platform") + 1] == "linux/amd64,linux/arm64"
    assert args[args.index("--name") + 1] == "builder-x"


def test_buildx_use_with_default_flags():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_use("builder-x", default=True, global_default=True)
    args = run.call_args.args[0]
    assert "--default" in args
    assert "--global" in args
    assert args[-1] == "builder-x"


def test_buildx_rm_requires_target():
    with pytest.raises(ValueError, match="`name` or `all_inactive=True`"):
        buildx_rm()


def test_buildx_rm_rejects_name_and_all_inactive_together():
    with pytest.raises(ValueError, match="mutually exclusive"):
        buildx_rm(name="builder-x", all_inactive=True)


def test_buildx_rm_all_inactive():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_rm(all_inactive=True, keep_state=True)
    args = run.call_args.args[0]
    assert args[:2] == ["buildx", "rm"]
    assert "--all-inactive" in args
    assert "--keep-state" in args


def test_buildx_rm_named_with_force():
    with patch("tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_rm(name="builder-x", force=True)
    args = run.call_args.args[0]
    assert "--force" in args
    assert args[-1] == "builder-x"

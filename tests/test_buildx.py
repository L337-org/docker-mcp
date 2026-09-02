import contextlib
import pathlib
from typing import Any
from unittest.mock import patch

import pytest

from docker_mcp.exceptions import RemoteFailureError, ToolInputError
from docker_mcp.tools._cli import CliResult
from docker_mcp.tools.buildx import (
    buildx_bake,
    buildx_build,
    buildx_create,
    buildx_du,
    buildx_history_inspect,
    buildx_history_list,
    buildx_imagetools_create,
    buildx_imagetools_inspect,
    buildx_inspect,
    buildx_list,
    buildx_prune,
    buildx_remove,
    buildx_use,
)


@pytest.fixture(autouse=True)
def _stub_plugin_check():  # pyright: ignore[reportUnusedFunction]
    with patch("docker_mcp.tools.buildx.require_plugin"):
        yield


def _ok(stdout: str = "", stderr: str = "") -> CliResult:
    return CliResult(returncode=0, stdout=stdout, stderr=stderr, truncated=False)


def _fail(stderr: str, returncode: int = 1) -> CliResult:
    return CliResult(returncode=returncode, stdout="", stderr=stderr, truncated=False)


# ---------- buildx_build ----------


def test_buildx_build_minimal_context_only():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".")
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "build", "--progress=plain"]
    assert args[-1] == "."  # context is positional and last


def test_buildx_build_passes_tags_and_platforms():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", tags=["org/app:v1", "org/app:latest"], platforms=["linux/amd64", "linux/arm64"])
    args = run.call_args.args[0]
    assert args.count("--tag") == 2
    assert args[args.index("org/app:v1") - 1] == "--tag"
    # buildx --platform takes a comma-joined list as one value (the documented convention).
    assert args.count("--platform") == 1
    assert args[args.index("--platform") + 1] == "linux/amd64,linux/arm64"


def test_buildx_build_single_platform_passes_one_flag():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", platforms=["linux/amd64"])
    args = run.call_args.args[0]
    assert args.count("--platform") == 1
    assert args[args.index("--platform") + 1] == "linux/amd64"


def test_buildx_build_omits_platform_when_not_supplied():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".")
    assert "--platform" not in run.call_args.args[0]


def test_buildx_build_dict_args_emit_repeated_flags():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
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
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", push=True)
    assert "--push" in run.call_args.args[0]

    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", load=True)
    assert "--load" in run.call_args.args[0]


def test_buildx_build_cache_and_attestation_flags():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
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
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_build(context=".", secret=["id=npmrc,src=/home/user/.npmrc"], ssh=["default"])
    args = run.call_args.args[0]
    assert args[args.index("--secret") + 1] == "id=npmrc,src=/home/user/.npmrc"
    assert args[args.index("--ssh") + 1] == "default"


def test_buildx_build_returns_returncode_dict():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_fail("build failed", returncode=2)):
        result = buildx_build(context=".")
    assert result["returncode"] == 2
    assert result["stderr"] == "build failed"


def test_buildx_build_rejects_push_and_load_together():
    with pytest.raises(ToolInputError, match="`push` and `load` are mutually exclusive"):
        buildx_build(context=".", push=True, load=True)


# ---------- buildx_bake ----------


def test_buildx_bake_minimal_uses_progress_plain():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_bake()
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "bake", "--progress=plain"]


def test_buildx_bake_targets_appended_last():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_bake(targets=["app", "tests"], files=["docker-bake.hcl"], push=True)
    args = run.call_args.args[0]
    # Targets are positional, must come after all flags
    assert args[-2:] == ["app", "tests"]
    assert "--push" in args
    assert args[args.index("-f") + 1] == "docker-bake.hcl"


def test_buildx_bake_set_overrides_repeat():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_bake(set_overrides=["app.platform=linux/amd64", "tests.no-cache=true"])
    args = run.call_args.args[0]
    assert args.count("--set") == 2


# ---------- buildx_imagetools_inspect ----------


def test_buildx_imagetools_inspect_default_args():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("[ ... ]")) as run:
        result = buildx_imagetools_inspect("alpine:3.19")
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "imagetools", "inspect"]
    assert args[-1] == "alpine:3.19"
    assert "--raw" not in args
    assert "--format" not in args
    assert result["returncode"] == 0


def test_buildx_imagetools_inspect_raw_and_format():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_imagetools_inspect("alpine:3.19", raw=True)
    assert "--raw" in run.call_args.args[0]

    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_imagetools_inspect("alpine:3.19", format="{{json .}}")
    args = run.call_args.args[0]
    assert args[args.index("--format") + 1] == "{{json .}}"


def test_buildx_imagetools_inspect_rejects_raw_and_format_together():
    with pytest.raises(ToolInputError, match="`raw` and `format` are mutually exclusive"):
        buildx_imagetools_inspect("alpine:3.19", raw=True, format="{{json .}}")


# ---------- buildx_imagetools_create ----------


def test_buildx_imagetools_create_target_and_sources():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_imagetools_create(
            target="org/app:v1",
            sources=["org/app:v1-amd64", "org/app:v1-arm64"],
        )
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "imagetools", "create"]
    assert args[args.index("--tag") + 1] == "org/app:v1"
    assert args[-2:] == ["org/app:v1-amd64", "org/app:v1-arm64"]


def test_buildx_imagetools_create_append_dry_run_annotations():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
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
    with pytest.raises(ToolInputError, match="at least one source ref or file"):
        buildx_imagetools_create(target="org/app:v1", sources=[])


# ---------- buildx_list / buildx_du / buildx_inspect ----------


def test_buildx_ls_parses_ndjson():
    body = (
        '{"Name":"default","Driver":"docker","Current":true}\n'
        '{"Name":"remote","Driver":"docker-container","Current":false}\n'
    )
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok(body)) as run:
        result = buildx_list()
    args = run.call_args.args[0]
    assert args == ["buildx", "ls", "--format", "{{json .}}"]
    assert result == [
        {"Name": "default", "Driver": "docker", "Current": True},
        {"Name": "remote", "Driver": "docker-container", "Current": False},
    ]


def test_buildx_ls_raises_on_failure():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_fail("daemon error")):
        with pytest.raises(RemoteFailureError, match="daemon error"):
            buildx_list()


def test_buildx_ls_drops_partial_record_when_truncated():
    body = '{"Name":"default","Current":true}\n{"Name":"remote",'  # second record cut off
    truncated_result = CliResult(returncode=0, stdout=body, stderr="", truncated=True)
    with patch("docker_mcp.tools.buildx.run_docker", return_value=truncated_result):
        result = buildx_list()
    assert result == [{"Name": "default", "Current": True}]


def test_buildx_du_parses_ndjson():
    body = '{"ID":"abc","Size":"1MB"}\n{"ID":"def","Size":"2MB"}\n'
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok(body)) as run:
        result = buildx_du(builder="builder-x")
    args = run.call_args.args[0]
    assert args[:4] == ["buildx", "du", "--format", "{{json .}}"]
    assert args[args.index("--builder") + 1] == "builder-x"
    assert result == [{"ID": "abc", "Size": "1MB"}, {"ID": "def", "Size": "2MB"}]


def test_buildx_inspect_with_bootstrap():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("Name: default")) as run:
        buildx_inspect(bootstrap=True)
    args = run.call_args.args[0]
    assert "--bootstrap" in args


# ---------- buildx_prune ----------


def test_buildx_prune_always_passes_force():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_prune()
    args = run.call_args.args[0]
    assert args[:3] == ["buildx", "prune", "--force"]


def test_buildx_prune_filter_and_space_flags():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_prune(
            all=True,
            filters={"until": "24h", "type": "exec.cachemount"},
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


# ---------- buildx_create / buildx_use / buildx_remove ----------


def test_buildx_create_driver_opts_repeat():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
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
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_use("builder-x", default=True, global_default=True)
    args = run.call_args.args[0]
    assert "--default" in args
    assert "--global" in args
    assert args[-1] == "builder-x"


def test_buildx_rm_requires_target():
    with pytest.raises(ToolInputError, match="`name` or `all_inactive=True`"):
        buildx_remove()


def test_buildx_rm_rejects_name_and_all_inactive_together():
    with pytest.raises(ToolInputError, match="mutually exclusive"):
        buildx_remove(name="builder-x", all_inactive=True)


def test_buildx_rm_all_inactive():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_remove(all_inactive=True, keep_state=True)
    args = run.call_args.args[0]
    assert args[:2] == ["buildx", "rm"]
    assert "--all-inactive" in args
    assert "--keep-state" in args


def test_buildx_rm_named_with_force():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok()) as run:
        buildx_remove(name="builder-x", force=True)
    args = run.call_args.args[0]
    assert "--force" in args
    assert args[-1] == "builder-x"


# ---------- argument-injection defense ----------


def test_buildx_build_rejects_flag_like_context():
    with pytest.raises(ToolInputError, match="parses as a flag"):
        buildx_build(context="--output=type=local,dest=/etc")


def test_buildx_imagetools_inspect_rejects_flag_like_image():
    with pytest.raises(ToolInputError, match="parses as a flag"):
        buildx_imagetools_inspect(image="--raw")


def test_buildx_imagetools_create_rejects_flag_like_source():
    with pytest.raises(ToolInputError, match="parses as a flag"):
        buildx_imagetools_create(target="me/img:latest", sources=["ok/img:amd64", "--bad"])


# ---------- buildx_history_list / buildx_history_inspect ----------


def test_buildx_history_ls_parses_ndjson():
    ndjson = '{"ref":"a1","name":"build-a","status":"Completed"}\n{"ref":"b2","name":"build-b","status":"Error"}'
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok(ndjson)) as run:
        result = buildx_history_list()
    assert [r["ref"] for r in result] == ["a1", "b2"]
    argv = run.call_args.args[0]
    assert argv[:3] == ["buildx", "history", "ls"]
    assert argv[-2:] == ["--format", "{{json .}}"]


def test_buildx_history_ls_passes_builder():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("")) as run:
        buildx_history_list(builder="mybuilder")
    argv = run.call_args.args[0]
    assert "--builder" in argv and "mybuilder" in argv


def test_buildx_history_ls_raises_on_failure():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_fail('unknown command "history"')):
        with pytest.raises(RemoteFailureError, match="buildx history ls"):
            buildx_history_list()


def test_buildx_history_inspect_parses_json_object():
    body = '{"Name":"build-a","Ref":"a1","Duration":72500142,"Status":"completed"}'
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok(body)) as run:
        result = buildx_history_inspect(ref="a1")
    assert result["Ref"] == "a1"
    argv = run.call_args.args[0]
    assert argv[:3] == ["buildx", "history", "inspect"]
    assert "--format" in argv and "json" in argv
    assert argv[-1] == "a1"


def test_buildx_history_inspect_omits_ref_when_empty():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("{}")) as run:
        buildx_history_inspect()
    argv = run.call_args.args[0]
    # No trailing ref positional — buildx then inspects the most recent build.
    assert argv[-1] == "json"


def test_buildx_history_inspect_normalizes_qualified_ls_ref():
    # A `buildx history ls` ref is "<builder>/<node>/<id>", but inspect only accepts the bare id —
    # the tool reduces it and targets the builder named in the ref.
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("{}")) as run:
        buildx_history_inspect(ref="default/default/abc123")
    argv = run.call_args.args[0]
    assert argv[-1] == "abc123"  # bare id, not the qualified path
    assert "--builder" in argv and argv[argv.index("--builder") + 1] == "default"


def test_buildx_history_inspect_explicit_builder_overrides_ref_builder():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("{}")) as run:
        buildx_history_inspect(ref="default/default/abc123", builder="other")
    argv = run.call_args.args[0]
    assert argv[argv.index("--builder") + 1] == "other"
    assert argv[-1] == "abc123"


def test_buildx_history_inspect_caret_ref_passes_through():
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("{}")) as run:
        buildx_history_inspect(ref="^0")
    argv = run.call_args.args[0]
    assert argv[-1] == "^0"  # no "/", unchanged
    assert "--builder" not in argv  # nothing to derive


def test_buildx_history_inspect_non_object_wrapped_in_raw():
    # Valid JSON that isn't an object (e.g. an array) falls back to {"raw": <stdout>}.
    # (Genuinely unparseable output raises instead, surfacing the parse error.)
    with patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("[1, 2, 3]")):
        result = buildx_history_inspect(ref="a1")
    assert result == {"raw": "[1, 2, 3]"}


# ---------- remote-exec fallback ----------


class _FakeSession:
    """Records what a bespoke buildx staging session was asked to stage and run."""

    def __init__(self, root="/tmp/docker-mcp-server.stage.abc"):
        self.root = root
        self.contexts: list[tuple[str, str | None]] = []
        self.trees: list[str] = []
        self.files: list[str] = []
        self.calls: list[dict] = []

    def stage_build_context(self, context_dir, *, dockerfile=None):
        self.contexts.append((str(context_dir), dockerfile))
        return f"{self.root}/context1"

    def stage_tree(self, local_dir):
        self.trees.append(str(local_dir))
        return f"{self.root}/tree{len(self.trees)}"

    def stage_file(self, local_file):
        self.files.append(str(local_file))
        return f"{self.root}/file{len(self.files)}/{pathlib.Path(local_file).name}"

    def join(self, *parts):
        return "/".join(parts)

    def exec(self, argv, *, timeout, max_output_bytes, cwd=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        from docker_mcp.tools._ssh_proxy import RemoteExecResult

        return RemoteExecResult(returncode=0, stdout=b"", stderr=b"", truncated=False)


@contextlib.contextmanager
def _fake_session_ctx(session):
    yield session


def _remote_build(session):
    """Patch buildx onto the remote path with a fake staging session."""
    return (
        patch("docker_mcp.tools.buildx.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.buildx.remote_cli_session", lambda *a, **k: _fake_session_ctx(session)),
        patch("docker_mcp.tools.buildx.run_docker"),
    )


def _argv(session) -> list[str]:
    return session.calls[0]["argv"]


def test_buildx_queries_run_remotely_without_staging_anything():
    for call, expected_head in (
        (lambda: buildx_list(host="prod"), ["buildx", "ls"]),
        (lambda: buildx_inspect(host="prod"), ["buildx", "inspect"]),
        (lambda: buildx_du(host="prod"), ["buildx", "du"]),
        (lambda: buildx_imagetools_inspect("alpine:3.19", host="prod"), ["buildx", "imagetools", "inspect"]),
    ):
        with (
            patch("docker_mcp.tools.buildx.should_remote_exec", return_value=True),
            patch("docker_mcp.tools.buildx.remote_stage_and_exec") as staged,
            patch("docker_mcp.tools.buildx.remote_exec_cli", return_value=_ok("{}")) as remote,
        ):
            call()
        staged.assert_not_called()
        assert remote.call_args.args[1][: len(expected_head)] == expected_head


def test_buildx_bake_stages_its_working_directory_and_its_files():
    with (
        patch("docker_mcp.tools.buildx.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.buildx.remote_stage_and_exec", return_value=_ok("")) as staged,
    ):
        buildx_bake(targets=["app"], files=["docker-bake.hcl"], cwd="/srv/app", host="prod")
    assert staged.call_args.kwargs["cwd"] == "/srv/app"
    assert staged.call_args.kwargs["stage_cwd"] is True
    assert staged.call_args.kwargs["path_values"] == ["docker-bake.hcl"]


def test_buildx_bake_target_named_like_a_flag_is_refused():
    """
    Targets are appended verbatim to the argv, so an unvalidated one would be parsed as a flag — and it
    is also why `path_values` is passed explicitly rather than recovered by scanning for `-f`.
    """
    with pytest.raises(ToolInputError, match="parses as a flag"):
        buildx_bake(targets=["-f"], files=["docker-bake.hcl"], host="prod")


def test_buildx_create_and_imagetools_create_stage_only_the_files_they_name():
    with (
        patch("docker_mcp.tools.buildx.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.buildx.remote_stage_and_exec", return_value=_ok("")) as staged,
    ):
        buildx_create(name="builder", config="/etc/buildkitd.toml", host="prod")
    assert staged.call_args.kwargs["path_values"] == ["/etc/buildkitd.toml"]
    # No working directory is involved, so none is copied.
    assert staged.call_args.kwargs["stage_cwd"] is False

    with (
        patch("docker_mcp.tools.buildx.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.buildx.remote_stage_and_exec", return_value=_ok("")) as staged,
    ):
        buildx_imagetools_create("org/app:v1", ["org/app:amd64"], descriptor_files=["desc.json"], host="prod")
    assert staged.call_args.kwargs["path_values"] == ["desc.json"]


def test_buildx_create_without_a_config_needs_no_staging():
    with (
        patch("docker_mcp.tools.buildx.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.buildx.remote_stage_and_exec") as staged,
        patch("docker_mcp.tools.buildx.remote_exec_cli", return_value=_ok("")) as remote,
    ):
        buildx_create(name="builder", host="prod")
    staged.assert_not_called()
    remote.assert_called_once()


# ---------- buildx_build's bespoke staging ----------


def test_buildx_build_stages_the_context_and_points_the_build_at_it(tmp_path):
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(str(context), tags=["org/app:v1"], host="prod")
    assert session.contexts == [(str(context), None)]
    argv = _argv(session)
    assert argv[0] == "docker" and argv[1] == "buildx"
    assert argv[-1] == f"{session.root}/context1"  # the context positional points at the staged copy
    # Deliberately no remote working directory: see the in-context Dockerfile test below.
    assert session.calls[0]["cwd"] is None


def test_buildx_build_passes_a_url_context_through_untouched(tmp_path):
    """
    Staging keys off "is this an existing local directory", the inverse of trying to recognise URL
    syntax — which cannot be got right from the string alone. A Git/HTTP context is left for the remote
    CLI to fetch, exactly as the local one would.
    """
    session = _FakeSession()
    url = "https://github.com/org/repo.git#main"
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(url, host="prod")
    assert session.contexts == []
    assert _argv(session)[-1] == url
    assert session.calls[0]["cwd"] is None


def test_buildx_build_rewrites_an_in_context_dockerfile_to_an_absolute_staged_path(tmp_path, monkeypatch):
    """
    buildx resolves `--file` against the CLI's *working directory*, not the context (verified
    empirically), so the rewrite is to an absolute path under the staged context and the remote command
    gets no working directory at all. Running it *in* the staged context instead would let a relative
    `--file` that the local CLI could not find resolve inside the copied context — the same build failing
    locally and succeeding remotely. The relative path is still handed to the exclusion pass, so
    `.dockerignore` cannot drop the Dockerfile.
    """
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "app.dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(str(context), file="ctx/app.dockerfile", host="prod")
    assert session.contexts == [(str(context), "app.dockerfile")]
    argv = _argv(session)
    assert argv[argv.index("--file") + 1] == f"{session.root}/context1/app.dockerfile"
    assert session.calls[0]["cwd"] is None
    assert session.files == []  # already inside the staged context; not copied twice


def test_buildx_build_stages_a_dockerfile_outside_the_context(tmp_path):
    context = tmp_path / "ctx"
    context.mkdir()
    outside = tmp_path / "Dockerfile.shared"
    outside.write_text("FROM alpine\n", encoding="utf-8")
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(str(context), file=str(outside), host="prod")
    assert session.files == [str(outside)]
    argv = _argv(session)
    assert argv[argv.index("--file") + 1] == f"{session.root}/file1/Dockerfile.shared"
    assert session.contexts == [(str(context), None)]


def test_buildx_build_stages_paths_inside_composite_specs(tmp_path):
    """
    `--build-context name=path` and `--secret id=x,src=path` hide their paths inside the token, so
    whole-token matching would miss them and the build would read nothing (or the wrong thing) remotely.
    """
    context = tmp_path / "ctx"
    context.mkdir()
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text("//registry:_authToken=x\n", encoding="utf-8")
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(
            str(context),
            build_contexts={"deps": str(vendor)},
            secret=[f"id=npmrc,src={npmrc}"],
            host="prod",
        )
    argv = _argv(session)
    assert argv[argv.index("--build-context") + 1] == f"deps={session.root}/tree1"
    assert argv[argv.index("--secret") + 1] == f"id=npmrc,src={session.root}/file1/.npmrc"


def test_buildx_build_leaves_non_path_composite_values_alone(tmp_path):
    context = tmp_path / "ctx"
    context.mkdir()
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(
            str(context),
            build_contexts={"base": "docker-image://alpine:3.19"},
            secret=["id=token,env=NPM_TOKEN"],
            host="prod",
        )
    argv = _argv(session)
    assert argv[argv.index("--build-context") + 1] == "base=docker-image://alpine:3.19"
    assert argv[argv.index("--secret") + 1] == "id=token,env=NPM_TOKEN"
    assert session.files == [] and session.trees == []


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"output": ["type=local,dest=out"]}, "output="),
        ({"cache_to": ["type=local,dest=/tmp/cache"]}, "cache_to="),
        ({"cache_from": ["type=local,src=/tmp/cache"]}, "cache_from="),
        ({"ssh": ["default"]}, "ssh="),
        # The stdout exemption is `output`-only: a cache has no stdout form, so "-" is just a path
        # named "-" on the remote host — and for cache_from a missing import is non-fatal, i.e. a
        # silently uncached build.
        ({"cache_to": ["type=local,dest=-"]}, "cache_to="),
        ({"cache_from": ["type=local,src=-"]}, "cache_from="),
    ],
)
def test_buildx_build_refuses_flags_that_would_resolve_on_the_remote_host(tmp_path, kwargs, needle):
    """
    Each of these either loses work silently (an output written into a directory that is about to be
    deleted, a cache on the wrong disk, a *non-fatal* missing cache import) or uses the wrong
    credentials (`--ssh` reads the remote user's agent). The refusal happens before any connection.
    """
    context = tmp_path / "ctx"
    context.mkdir()
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        with pytest.raises(ToolInputError, match=needle):
            buildx_build(str(context), host="prod", **kwargs)
    assert session.calls == []
    assert session.contexts == []


def test_buildx_build_allows_a_registry_output_and_stdout_dest(tmp_path):
    # `dest=-` on `output` is stdout, captured identically on both paths — and buildx itself rejects it
    # for exporters where it makes no sense ("dest cannot be stdout for local exporter", verified), so
    # this needs no exporter allow-list of our own.
    context = tmp_path / "ctx"
    context.mkdir()
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(
            str(context), output=["type=oci,dest=-"], cache_to=["type=registry,ref=org/app:cache"], host="prod"
        )
    assert len(session.calls) == 1


def test_buildx_build_uses_the_local_cli_when_it_can(tmp_path):
    context = tmp_path / "ctx"
    context.mkdir()
    with (
        patch("docker_mcp.tools.buildx.should_remote_exec", return_value=False),
        patch("docker_mcp.tools.buildx.remote_cli_session") as session_factory,
        patch("docker_mcp.tools.buildx.run_docker", return_value=_ok("")) as run,
        patch("docker_mcp.tools.buildx.require_plugin") as require,
    ):
        buildx_build(str(context), output=["type=local,dest=out"], ssh=["default"], host="prod")
    session_factory.assert_not_called()
    require.assert_called_once_with("buildx")
    # The refusals are remote-only: locally these flags work exactly as before.
    assert "--output" in run.call_args.args[0]
    assert "--ssh" in run.call_args.args[0]


def test_buildx_build_refusals_name_the_consequence_for_that_flag(tmp_path):
    """
    One shared message for three flags was wrong: nothing is *written* for `cache_from`, and "deleted
    when the call returns" describes only the output case. Each refusal now explains its own failure.
    """
    context = tmp_path / "ctx"
    context.mkdir()
    session = _FakeSession()
    messages = {}
    # dict[str, Any] because each iteration targets a different parameter, so there is no single
    # precise type for the **kwargs -- the alternative is three near-identical copies of the body.
    cases: tuple[tuple[str, dict[str, Any]], ...] = (
        ("output", {"output": ["type=local,dest=out"]}),
        ("cache_to", {"cache_to": ["type=local,dest=/tmp/c"]}),
        ("cache_from", {"cache_from": ["type=local,src=/tmp/c"]}),
    )
    for flag, kwargs in cases:
        with contextlib.ExitStack() as stack:
            for patcher in _remote_build(session):
                stack.enter_context(patcher)
            with pytest.raises(ToolInputError) as excinfo:
                buildx_build(str(context), host="prod", **kwargs)
        messages[flag] = str(excinfo.value)
    assert "deleted when the call returns" in messages["output"]
    assert "nothing later reads it" in messages["cache_to"]
    assert "silently run uncached" in messages["cache_from"]
    assert "written" not in messages["cache_from"]  # nothing is written on an import


def test_buildx_build_stages_an_absolute_dockerfile_beside_a_url_context(tmp_path):
    """
    With a URL context an *absolute* `--file` is still read from this filesystem — buildx transfers it as
    a separate dockerfile context (observed: `transferring dockerfile: 46B` plus a parse error from the
    local file's own contents). So it has to be staged, even though the context is not.
    """
    dockerfile = tmp_path / "Dockerfile.remote"
    dockerfile.write_text("FROM alpine\n", encoding="utf-8")
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build("https://github.com/org/repo.git", file=str(dockerfile), host="prod")
    assert session.contexts == []  # the URL context is untouched
    assert session.files == [str(dockerfile)]
    argv = _argv(session)
    assert argv[argv.index("--file") + 1] == f"{session.root}/file1/Dockerfile.remote"


def test_buildx_build_leaves_a_relative_dockerfile_beside_a_url_context_alone(tmp_path, monkeypatch):
    """
    The mirror case: with a URL context a *relative* `--file` is resolved inside the fetched context, not
    here (verified — `-f Dockerfile <git-url>` reports "open Dockerfile: no such file" rather than using
    the identically-named file in the working directory). Resolving it locally would risk staging a
    same-named file that happens to sit in this server's cwd and silently building something else.
    """
    decoy = tmp_path / "Dockerfile"
    decoy.write_text("FROM decoy\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build("https://github.com/org/repo.git", file="Dockerfile", host="prod")
    assert session.files == []  # the decoy in the cwd is not staged
    argv = _argv(session)
    assert argv[argv.index("--file") + 1] == "Dockerfile"


def test_buildx_build_leaves_a_dockerfile_that_does_not_exist_locally(tmp_path):
    # Parity: a `--file` naming nothing here is reported by the remote CLI, not turned into a staging error.
    context = tmp_path / "ctx"
    context.mkdir()
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(str(context), file=str(tmp_path / "absent.dockerfile"), host="prod")
    assert session.files == []
    argv = _argv(session)
    assert argv[argv.index("--file") + 1] == str(tmp_path / "absent.dockerfile")


def test_buildx_build_does_not_let_a_relative_dockerfile_resolve_inside_the_staged_context(tmp_path, monkeypatch):
    """
    The divergence this design avoids. `-f Dockerfile ./ctx` with no `Dockerfile` beside the *server's*
    working directory fails locally, because buildx resolves `--file` against that directory. If the
    remote command ran inside the staged context, the copy's own `Dockerfile` would satisfy it and the
    build would succeed remotely — a different outcome for the same call. So the token is passed through
    untouched and the command gets no working directory.
    """
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # no Dockerfile here, so local buildx would fail
    session = _FakeSession()
    with contextlib.ExitStack() as stack:
        for patcher in _remote_build(session):
            stack.enter_context(patcher)
        buildx_build(str(context), file="Dockerfile", host="prod")
    argv = _argv(session)
    assert argv[argv.index("--file") + 1] == "Dockerfile"  # verbatim: nothing local to reconcile it with
    assert session.calls[0]["cwd"] is None  # ...and no cwd for it to resolve against remotely
    assert session.files == []

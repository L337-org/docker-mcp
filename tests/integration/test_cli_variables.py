# integration tests for values passed to the docker CLI through its environment (`vars`, and the values
# of `env` and `build_args`) - require a real Docker daemon with the compose and buildx plugins.
# run with: uv run pytest -m integration
#
# A shim `docker` placed first on PATH records every argv it is given and then runs the real binary, so
# these check both halves against the real CLIs: the value arrives where it is used, and it is in no
# command line the server started.

import os
import shutil
import stat
import uuid
from pathlib import Path

import pytest

from docker_mcp.tools import _cli
from docker_mcp.tools.buildx import buildx_bake, buildx_build
from docker_mcp.tools.compose import compose_down, compose_run
from docker_mcp.tools.containers import container_logs, container_remove
from docker_mcp.tools.system import system_wait

# Newlines, quotes, expansions and backslashes: what a shell or a `KEY=VALUE` token could mangle.
_SECRET = "s3 cret\nline2 \"q\" 'q' $(nope) `nope` \\ end"


@pytest.fixture
def argv_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    real = shutil.which("docker")
    if real is None:
        pytest.skip("no docker CLI on PATH")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log = tmp_path / "argv.log"
    shim = shim_dir / "docker"
    shim.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> "{log}"\nexec "{real}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(_cli, "_resolve", lambda binary: str(shim))
    _cli._clear_plugin_cache()
    return log


def _assert_never_on_a_command_line(log: Path, *values: str) -> None:
    recorded = log.read_text(encoding="utf-8")
    assert recorded, "the shim recorded no command line; the check would pass vacuously"
    for value in values:
        for line in value.splitlines():
            assert line not in recorded, f"{line!r} reached a docker command line"


def test_compose_run_delivers_vars_and_env_without_a_command_line(tmp_path: Path, argv_log: Path):
    if not _cli.has_plugin("compose"):
        pytest.skip("docker compose plugin not installed")
    (tmp_path / "compose.yaml").write_text(
        "services:\n  probe:\n    image: alpine:3\n    environment:\n      FROM_VARS: ${SUBSTITUTED:?required}\n",
        encoding="utf-8",
    )
    project = f"docker-mcp-it-{uuid.uuid4().hex[:8]}"
    env_value = _SECRET + " (env)"
    try:
        result = compose_run(
            service="probe",
            command=["sh", "-c", 'printf "%s|%s" "$FROM_VARS" "$FROM_ENV"'],
            project_dir=str(tmp_path),
            project_name=project,
            env={"FROM_ENV": env_value},
            vars={"SUBSTITUTED": _SECRET},
        )
        assert result["returncode"] == 0, result["stderr"]
        container = result["stdout"].strip()
        system_wait(kind="container", target=container, until="not-running", timeout_seconds=60)
        logs = container_logs(container)
        assert logs == f"{_SECRET}|{env_value}", logs
        container_remove(container, force=True)
        _assert_never_on_a_command_line(argv_log, _SECRET, env_value)
    finally:
        compose_down(project_dir=str(tmp_path), project_name=project, remove_orphans=True)


def test_buildx_build_delivers_a_secret_env_and_a_build_arg_without_a_command_line(tmp_path: Path, argv_log: Path):
    if not _cli.has_plugin("buildx"):
        pytest.skip("docker buildx plugin not installed")
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine:3\n"
        "ARG PLAIN\n"
        'RUN --mount=type=secret,id=tok,env=TOK printf "<%s|%s>" "$TOK" "$PLAIN" | tr " " _\n',
        encoding="utf-8",
    )
    result = buildx_build(
        context=str(tmp_path),
        no_cache=True,
        build_args={"PLAIN": "arg value"},
        secret=["id=tok,env=NPM_TOKEN"],
        vars={"NPM_TOKEN": "tok value"},
    )
    assert result["returncode"] == 0, result["stderr"]
    # Spaces become underscores, so the line differs from the secret and BuildKit does not mask it.
    assert "<tok_value|arg_value>" in result["stdout"] + result["stderr"]
    _assert_never_on_a_command_line(argv_log, "tok value", "arg value")


def test_buildx_bake_takes_a_variable_from_vars_without_a_command_line(tmp_path: Path, argv_log: Path):
    if not _cli.has_plugin("buildx"):
        pytest.skip("docker buildx plugin not installed")
    (tmp_path / "Dockerfile").write_text('FROM alpine:3\nARG FROM_BAKE\nRUN printf "<%s>" "$FROM_BAKE" | tr " " _\n')
    (tmp_path / "docker-bake.hcl").write_text(
        'variable "BAKE_VALUE" { default = "unset" }\n'
        'target "probe" {\n  args = { FROM_BAKE = BAKE_VALUE }\n  no-cache = true\n}\n'
    )
    result = buildx_bake(targets=["probe"], cwd=str(tmp_path), vars={"BAKE_VALUE": "bake value"})
    assert result["returncode"] == 0, result["stderr"]
    assert "<bake_value>" in result["stdout"] + result["stderr"]
    _assert_never_on_a_command_line(argv_log, "bake value")

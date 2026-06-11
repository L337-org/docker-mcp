from unittest.mock import patch

import pytest

from docker_mcp.tools._cli import CliResult
from docker_mcp.tools.stack import (
    stack_deploy,
    stack_ls,
    stack_ps,
    stack_rm,
    stack_services,
)


def _ok(stdout: str = "", stderr: str = "") -> CliResult:
    return CliResult(returncode=0, stdout=stdout, stderr=stderr, truncated=False)


def _fail(stderr: str, returncode: int = 1) -> CliResult:
    return CliResult(returncode=returncode, stdout="", stderr=stderr, truncated=False)


def _patch_run():
    return patch("docker_mcp.tools.stack.run_docker")


# ---------- stack_deploy (action tool: raw dict, never raises on non-zero) ----------


def test_stack_deploy_builds_args_and_returns_raw_dict():
    with _patch_run() as run:
        run.return_value = _ok(stdout="Creating service web")
        result = stack_deploy(
            "web",
            compose_files=["docker-compose.yml", "override.yml"],
            with_registry_auth=True,
            prune=True,
            resolve_image="changed",
            cwd="/srv/app",
        )
    assert result == {"returncode": 0, "stdout": "Creating service web", "stderr": "", "truncated": False}
    args, kwargs = run.call_args
    argv = args[0]
    assert argv[:2] == ["stack", "deploy"]
    # Each compose file is passed as its own -c.
    assert argv.count("-c") == 2
    assert "docker-compose.yml" in argv and "override.yml" in argv
    assert "--with-registry-auth" in argv
    assert "--prune" in argv
    assert "--resolve-image=changed" in argv
    assert "--detach=true" in argv  # default detach
    assert argv[-1] == "web"  # stack name is the trailing positional
    assert kwargs["cwd"] == "/srv/app"


def test_stack_deploy_detach_false_passes_flag():
    with _patch_run() as run:
        run.return_value = _ok()
        stack_deploy("web", compose_files=["c.yml"], detach=False)
    assert "--detach=false" in run.call_args.args[0]


def test_stack_deploy_does_not_raise_on_non_zero():
    with _patch_run() as run:
        run.return_value = _fail("this node is not a swarm manager")
        result = stack_deploy("web", compose_files=["c.yml"])
    assert result["returncode"] == 1
    assert "swarm manager" in result["stderr"]


def test_stack_deploy_requires_a_compose_file():
    with pytest.raises(ValueError, match="at least one"):
        stack_deploy("web", compose_files=[])


def test_stack_deploy_rejects_invalid_resolve_image():
    with pytest.raises(ValueError, match="resolve_image"):
        stack_deploy("web", compose_files=["c.yml"], resolve_image="sometimes")


def test_stack_deploy_rejects_flag_like_stack_name():
    with pytest.raises(ValueError, match="flag"):
        stack_deploy("--rm", compose_files=["c.yml"])


# ---------- stack_ls (parsed query: raises on non-zero) ----------


def test_stack_ls_parses_ndjson():
    ndjson = '{"Name":"web","Services":"3"}\n{"Name":"db","Services":"1"}'
    with _patch_run() as run:
        run.return_value = _ok(stdout=ndjson)
        result = stack_ls()
    assert [s["Name"] for s in result] == ["web", "db"]
    # JSON via the Go template, not the --format json shorthand (which needs docker >= ~v23).
    assert run.call_args.args[0] == ["stack", "ls", "--format", "{{json .}}"]


def test_stack_ls_single_object_wrapped_in_list():
    with _patch_run() as run:
        run.return_value = _ok(stdout='{"Name":"web","Services":"3"}')
        result = stack_ls()
    assert result == [{"Name": "web", "Services": "3"}]


def test_stack_ls_raises_on_failure():
    with _patch_run() as run:
        run.return_value = _fail("This node is not a swarm manager")
        with pytest.raises(RuntimeError, match="stack ls"):
            stack_ls()


# ---------- stack_ps / stack_services ----------


def test_stack_ps_builds_args_with_no_trunc_and_filters():
    with _patch_run() as run:
        run.return_value = _ok(stdout='{"Name":"web.1","CurrentState":"Running"}')
        result = stack_ps("web", no_trunc=True, filters=["desired-state=running"])
    assert result == [{"Name": "web.1", "CurrentState": "Running"}]
    argv = run.call_args.args[0]
    assert argv[:2] == ["stack", "ps"]
    assert "--no-trunc" in argv
    assert argv.count("--filter") == 1
    assert "desired-state=running" in argv
    assert argv[-1] == "web"


def test_stack_ps_raises_on_failure():
    with _patch_run() as run:
        run.return_value = _fail("nothing found in stack: web")
        with pytest.raises(RuntimeError, match="stack ps"):
            stack_ps("web")


def test_stack_services_builds_args_with_filters():
    with _patch_run() as run:
        run.return_value = _ok(stdout='{"Name":"web_api","Replicas":"3/3"}')
        result = stack_services("web", filters=["name=web_api"])
    assert result == [{"Name": "web_api", "Replicas": "3/3"}]
    argv = run.call_args.args[0]
    assert argv[:2] == ["stack", "services"]
    assert "name=web_api" in argv
    assert argv[-1] == "web"


def test_stack_services_rejects_flag_like_name():
    with pytest.raises(ValueError, match="flag"):
        stack_services("-x")


# ---------- stack_rm (action tool) ----------


def test_stack_rm_builds_args_for_multiple_stacks():
    with _patch_run() as run:
        run.return_value = _ok(stdout="Removing stack: web")
        result = stack_rm(["web", "db"])
    assert result["returncode"] == 0
    argv = run.call_args.args[0]
    assert argv[:2] == ["stack", "rm"]
    assert "--detach=true" in argv
    assert argv[-2:] == ["web", "db"]


def test_stack_rm_detach_false():
    with _patch_run() as run:
        run.return_value = _ok()
        stack_rm(["web"], detach=False)
    assert "--detach=false" in run.call_args.args[0]


def test_stack_rm_requires_a_stack_name():
    with pytest.raises(ValueError, match="at least one"):
        stack_rm([])


def test_stack_rm_rejects_flag_like_name():
    with pytest.raises(ValueError, match="flag"):
        stack_rm(["web", "-rf"])

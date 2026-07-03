from unittest.mock import patch

import pytest

from docker_mcp.tools._cli import CliResult
from docker_mcp.tools.context import (
    context_create,
    context_inspect,
    context_list,
    context_remove,
    context_use,
)


def _ok(stdout: str = "", stderr: str = "") -> CliResult:
    return CliResult(returncode=0, stdout=stdout, stderr=stderr, truncated=False)


def _fail(stderr: str, returncode: int = 1) -> CliResult:
    return CliResult(returncode=returncode, stdout="", stderr=stderr, truncated=False)


def test_context_ls_parses_json_lines():
    payload = (
        '{"Name":"default","Description":"docker desktop","DockerEndpoint":"unix:///var/run/docker.sock","Current":true}\n'
        '{"Name":"remote","Description":"prod","DockerEndpoint":"tcp://x:2376","Current":false}\n'
    )
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok(payload)) as run:
        result = context_list()
    assert result == [
        {
            "Name": "default",
            "Description": "docker desktop",
            "DockerEndpoint": "unix:///var/run/docker.sock",
            "Current": True,
        },
        {"Name": "remote", "Description": "prod", "DockerEndpoint": "tcp://x:2376", "Current": False},
    ]
    run.assert_called_once_with(["context", "ls", "--format", "{{json .}}"])


def test_context_ls_skips_blank_lines():
    payload = '{"Name":"a"}\n\n{"Name":"b"}\n'
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok(payload)):
        result = context_list()
    assert result == [{"Name": "a"}, {"Name": "b"}]


def test_context_ls_raises_on_failure():
    with patch("docker_mcp.tools.context.run_docker", return_value=_fail("permission denied")):
        with pytest.raises(RuntimeError, match="permission denied"):
            context_list()


def test_context_inspect_returns_first_array_entry():
    payload = '[{"Name":"remote","Metadata":{"Description":"prod"}}]'
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok(payload)) as run:
        result = context_inspect("remote")
    assert result == {"Name": "remote", "Metadata": {"Description": "prod"}}
    run.assert_called_once_with(["context", "inspect", "remote"])


def test_context_inspect_handles_bare_object_response():
    payload = '{"Name":"remote"}'
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok(payload)):
        assert context_inspect("remote") == {"Name": "remote"}


def test_context_inspect_raises_on_empty_array():
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok("[]")):
        with pytest.raises(RuntimeError, match="returned no entries"):
            context_inspect("remote")


def test_context_inspect_raises_on_failure():
    with patch("docker_mcp.tools.context.run_docker", return_value=_fail("context not found")):
        with pytest.raises(RuntimeError, match="context not found"):
            context_inspect("missing")


def test_context_create_minimal():
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok("remote\n")) as run:
        result = context_create("remote", docker_host="tcp://10.0.0.5:2376")
    run.assert_called_once_with(["context", "create", "remote", "--docker", "host=tcp://10.0.0.5:2376"])
    assert result == {"returncode": 0, "stdout": "remote\n", "stderr": "", "truncated": False}


def test_context_create_with_tls_and_description():
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok()) as run:
        context_create(
            "remote",
            docker_host="tcp://10.0.0.5:2376",
            description="prod swarm",
            tls_ca="/etc/docker/ca.pem",
            tls_cert="/etc/docker/cert.pem",
            tls_key="/etc/docker/key.pem",
        )
    args = run.call_args.args[0]
    assert args[:4] == ["context", "create", "remote", "--docker"]
    # tls flags joined into a single comma-separated --docker value
    docker_spec = args[4]
    assert "host=tcp://10.0.0.5:2376" in docker_spec
    assert "ca=/etc/docker/ca.pem" in docker_spec
    assert "cert=/etc/docker/cert.pem" in docker_spec
    assert "key=/etc/docker/key.pem" in docker_spec
    assert "--description" in args
    assert "prod swarm" in args


def test_context_create_skip_tls_verify():
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok()) as run:
        context_create("remote", docker_host="tcp://10.0.0.5:2376", skip_tls_verify=True)
    args = run.call_args.args[0]
    assert "skip-tls-verify=true" in args[4]


def test_context_create_returns_stderr_on_failure_without_raising():
    # Mutating ops return the CliResult dict so the agent can read stderr.
    with patch("docker_mcp.tools.context.run_docker", return_value=_fail("context already exists")):
        result = context_create("remote", docker_host="tcp://x:2376")
    assert result["returncode"] == 1
    assert "already exists" in result["stderr"]


def test_context_use_invokes_correct_args():
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok("Current context: remote\n")) as run:
        result = context_use("remote")
    run.assert_called_once_with(["context", "use", "remote"])
    assert result["returncode"] == 0


def test_context_rm_without_force():
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok("remote\n")) as run:
        context_remove("remote")
    run.assert_called_once_with(["context", "rm", "remote"])


def test_context_rm_with_force():
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok("remote\n")) as run:
        context_remove("remote", force=True)
    run.assert_called_once_with(["context", "rm", "remote", "--force"])


# ---------- argument-injection defense ----------


def test_context_use_rejects_flag_like_name():
    with pytest.raises(ValueError, match="parses as a flag"):
        context_use("--help")


def test_context_rm_rejects_flag_like_name():
    with pytest.raises(ValueError, match="parses as a flag"):
        context_remove("-x")

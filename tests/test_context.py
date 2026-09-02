from unittest.mock import patch

import pytest

from docker_mcp.exceptions import RemoteFailureError, ToolInputError
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
        with pytest.raises(RemoteFailureError, match="permission denied"):
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
        with pytest.raises(RemoteFailureError, match="returned no entries"):
            context_inspect("remote")


def test_context_inspect_raises_on_failure():
    with patch("docker_mcp.tools.context.run_docker", return_value=_fail("context not found")):
        with pytest.raises(RemoteFailureError, match="context not found"):
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


def test_context_create_refuses_to_smuggle_skip_tls_verify_through_docker_host():
    """
    The insecure switch must only be reachable through its own parameter.

    The `--docker` spec is one comma-separated argument, so a comma in `docker_host` appends a key
    rather than corrupting the value - turning TLS verification off while `skip_tls_verify` still
    reads False, which is exactly the visibility the explicit parameter exists to provide.
    """
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok()) as run:
        with pytest.raises(ToolInputError, match="contains ','"):
            context_create("remote", docker_host="tcp://10.0.0.5:2376,skip-tls-verify=true")
    run.assert_not_called()  # refused before the CLI ran at all


_COMMA_PATH = "/etc/docker/ca.pem,skip-tls-verify=true"
_HOST = "tcp://10.0.0.5:2376"


# Parametrized over calls rather than a field name and **kwargs: pyright cannot narrow a dynamic
# key back to the right parameter, so the kwargs form reports a spurious type error on
# skip_tls_verify. Explicit calls keep the checking that the generic @tool() decorator provides.
@pytest.mark.parametrize(
    "call",
    [
        lambda: context_create("remote", docker_host=_HOST, tls_ca=_COMMA_PATH),
        lambda: context_create("remote", docker_host=_HOST, tls_cert=_COMMA_PATH),
        lambda: context_create("remote", docker_host=_HOST, tls_key=_COMMA_PATH),
    ],
    ids=["tls_ca", "tls_cert", "tls_key"],
)
def test_context_create_refuses_a_comma_in_a_tls_path(call):
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok()) as run:
        with pytest.raises(ToolInputError, match="contains ','"):
            call()
    run.assert_not_called()


def test_context_create_still_allows_an_equals_sign_in_a_value():
    """`=` cannot create a new key, and a real path may contain one, so it must not be rejected."""
    with patch("docker_mcp.tools.context.run_docker", return_value=_ok()) as run:
        context_create("remote", docker_host="tcp://10.0.0.5:2376", tls_ca="/etc/docker/ca=1.pem")
    assert "ca=/etc/docker/ca=1.pem" in run.call_args.args[0][4]


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
    with pytest.raises(ToolInputError, match="parses as a flag"):
        context_use("--help")


def test_context_rm_rejects_flag_like_name():
    with pytest.raises(ToolInputError, match="parses as a flag"):
        context_remove("-x")


def test_a_failing_docker_command_carries_its_stderr_to_the_caller(on_the_wire):
    """`raise_on_cli_failure` is how docker's own error text reaches the model, and it only does so
    because the message is now on a translated type: the SDK would otherwise report the call as
    `Error executing tool context_list` and keep the CLI's explanation in the server log."""
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    with patch("docker_mcp.tools.context.run_docker", return_value=_fail("permission denied")):
        with pytest.raises(ToolError) as excinfo:
            on_the_wire("context_list", {})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "permission denied" in str(excinfo.value)

# integration tests for sending variables to a remote command on stdin - require a real Docker daemon,
# which provides the shells the remote host might have: dash, busybox ash and bash.
# run with: uv run pytest -m integration
#
# The real SSH path is covered by tests/integration/test_remote_exec.py when DOCKER_MCP_TEST_SSH_HOST is
# set; this runs the same wrapper script under each shell, fed exactly what `exec_remote` sends.

import shlex
import subprocess

import pytest

from docker_mcp.tools._ssh_proxy import PosixDialect, encode_env_value

_VALUES = {
    "MULTI": "line1\nline2\n\n",
    "QUOTED": 'it\'s "q" $(x) `y` ;&| \\ \\0101 %s %b',
    "UNICODE": "café ☃",
    "EMPTY": "",
    "m": "/tmp/victim",  # the wrapper's own marker variable: must not be redirected
}


@pytest.mark.parametrize(
    ("image", "shell"),
    [("debian:bookworm-slim", "sh"), ("alpine:3", "sh"), ("busybox:1.36", "sh"), ("bash:5", "bash")],
)
def test_variables_arrive_intact_under_each_shell(image: str, shell: str):
    names = list(_VALUES)
    printer = 'for n in "$@"; do eval "printf \'%s\\\\0\' \\"\\${$n}\\""; done'
    body = shlex.split(
        PosixDialect().wrap_with_timeout(["sh", "-c", printer, "sh", *names], timeout=30, env_names=names)
    )[2]
    script = f"touch /tmp/victim; {body}; e=$?; [ -e /tmp/victim ] || echo VICTIM-REMOVED >&2; exit $e"
    payload = "".join(f"{encode_env_value(v)}\n" for v in _VALUES.values()).encode()
    argv = ["docker", "run", "--rm", "-i", image, shell, "-c", script]
    # S603/S607: a fixed argv with no shell, and the docker CLI on PATH is what this suite targets.
    result = subprocess.run(argv, input=payload, capture_output=True, timeout=180)  # noqa: S603, S607
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"".join(v.encode() + b"\0" for v in _VALUES.values())
    assert b"VICTIM-REMOVED" not in result.stderr

# integration tests for the SSH remote-exec fallback — require a real remote Docker host over ssh://.
#
# These do NOT need a local Docker daemon (the point of the fallback is that there isn't one), so the
# daemon-required autouse fixture from tests/integration/conftest.py is overridden below. They need
# something CI cannot provide — a reachable host with a docker CLI and credentials already trusted in
# `~/.ssh/known_hosts` — so they are gated on an explicit env var and skip cleanly without it:
#
#   DOCKER_MCP_TEST_SSH_HOST=ssh://user@host[:port] uv run pytest -m integration tests/integration/test_remote_exec.py
#
# Everything here is read-only on the remote host: `docker version`, a staged directory read back with
# `cat`, and a `sleep` that gets killed. Nothing is created on the daemon.

import os
import subprocess

import pytest

from docker_mcp.tools._ssh_proxy import remote_staging_session, run_remote_exec

_CAP = 262_144
_HOST_ENV = "DOCKER_MCP_TEST_SSH_HOST"


@pytest.fixture(autouse=True, scope="module")
def skip_if_no_daemon():
    """Override the conftest fixture — the fallback exists for machines with no local daemon."""
    yield


@pytest.fixture(scope="module")
def ssh_host() -> str:
    host = (os.environ.get(_HOST_ENV) or "").strip()
    if not host:
        pytest.skip(f"set {_HOST_ENV}=ssh://user@host to run the remote-exec integration tests")
    return host


def test_remote_exec_runs_the_docker_cli_on_the_far_host(ssh_host):
    result = run_remote_exec(
        ssh_host, ["docker", "version", "--format", "{{.Server.Version}}"], max_output_bytes=_CAP, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "expected a server version from the remote daemon"


def test_remote_timeout_raises_the_same_exception_as_the_local_backend(ssh_host):
    # The watchdog is remote and reports 124; the mapping back to TimeoutExpired is what makes the two
    # backends interchangeable, and only a real remote can prove the whole chain.
    with pytest.raises(subprocess.TimeoutExpired):
        run_remote_exec(ssh_host, ["sleep", "30"], max_output_bytes=_CAP, timeout=2)


def test_staging_puts_a_tree_where_the_remote_command_can_read_it(ssh_host, tmp_path):
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / "top.txt").write_text("top\n", encoding="utf-8")
    (project / "sub" / "nested.txt").write_text("nested\n", encoding="utf-8")

    with remote_staging_session(ssh_host, timeout=60) as session:
        root = session.root
        staged = session.stage_tree(project)
        read_back = session.exec(
            ["sh", "-c", "cat top.txt sub/nested.txt"], timeout=60, max_output_bytes=_CAP, cwd=staged
        )
        assert read_back.returncode == 0, read_back.stderr
        assert read_back.stdout.decode() == "top\nnested\n"

    # Teardown is the part most easily broken by a later change, and it can only be checked from outside
    # the session — on a fresh connection, after the `finally` has run.
    check = run_remote_exec(
        ssh_host, ["sh", "-c", f"test -e {root} && echo PRESENT || echo GONE"], max_output_bytes=_CAP, timeout=60
    )
    assert check.stdout.decode().strip() == "GONE"


def test_staged_build_context_honours_dockerignore(ssh_host, tmp_path):
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM alpine:3.19\n", encoding="utf-8")
    (context / "keep.txt").write_text("keep\n", encoding="utf-8")
    (context / "secret.env").write_text("TOKEN=never-staged\n", encoding="utf-8")
    (context / ".dockerignore").write_text("secret.env\n", encoding="utf-8")

    with remote_staging_session(ssh_host, timeout=60) as session:
        staged = session.stage_build_context(context)
        listing = session.exec(["ls", "-A", staged], timeout=60, max_output_bytes=_CAP)
        assert listing.returncode == 0, listing.stderr
        names = set(listing.stdout.decode().split())
    assert {"Dockerfile", "keep.txt"} <= names
    assert "secret.env" not in names  # an excluded file must never reach the remote disk

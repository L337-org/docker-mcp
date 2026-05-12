import subprocess
from unittest.mock import MagicMock, patch

import pytest

import tools._cli as cli_module
from tools._cli import (
    MAX_CLI_OUTPUT_BYTES,
    CliResult,
    has_plugin,
    require_plugin,
    run_docker,
)


@pytest.fixture(autouse=True)
def _clear_plugin_cache():  # pyright: ignore[reportUnusedFunction]
    has_plugin.cache_clear()
    yield
    has_plugin.cache_clear()


def _fake_completed(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_run_docker_passes_argv_list_and_no_shell():
    with (
        patch("tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed(b"hi\n")) as run,
    ):
        result = run_docker(["ps", "-a"])
    assert isinstance(result, CliResult)
    assert result.returncode == 0
    assert result.stdout == "hi\n"
    args, kwargs = run.call_args
    assert args[0] == ["/usr/bin/docker", "ps", "-a"]
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True


def test_run_docker_raises_when_binary_missing():
    with patch("tools._cli.shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="was not found on PATH"):
            run_docker(["version"])


def test_run_docker_forwards_timeout_and_cwd_and_stdin():
    with (
        patch("tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["build", "-"], cwd="/tmp/ctx", timeout=300.0, stdin=b"FROM alpine")
    kwargs = run.call_args.kwargs
    assert kwargs["timeout"] == 300.0
    assert kwargs["cwd"] == "/tmp/ctx"
    assert kwargs["input"] == b"FROM alpine"


def test_run_docker_decodes_utf8_with_replace():
    # Half a UTF-8 surrogate followed by valid text — must not raise.
    payload = b"ok-\xff-end"
    with (
        patch("tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed(stdout=payload)),
    ):
        result = run_docker(["version"])
    assert result.stdout.startswith("ok-")
    assert result.stdout.endswith("-end")
    assert result.truncated is False


def test_run_docker_truncates_oversized_output():
    big = b"x" * (MAX_CLI_OUTPUT_BYTES + 100)
    with (
        patch("tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed(stdout=big)),
    ):
        result = run_docker(["logs", "x"])
    assert len(result.stdout) == MAX_CLI_OUTPUT_BYTES
    assert result.truncated is True


def test_run_docker_truncated_flag_set_when_only_stderr_overflows():
    big = b"e" * (MAX_CLI_OUTPUT_BYTES + 1)
    with (
        patch("tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed(stderr=big, returncode=1)),
    ):
        result = run_docker(["version"])
    assert result.truncated is True
    assert result.returncode == 1


def test_run_docker_env_allowlist_drops_unrelated_vars(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://example:2375")
    monkeypatch.setenv("MY_SECRET", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin")
    with (
        patch("tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["version"])
    env = run.call_args.kwargs["env"]
    assert env["DOCKER_HOST"] == "tcp://example:2375"
    assert env["PATH"] == "/usr/bin"
    assert "MY_SECRET" not in env


def test_run_docker_extra_env_overlays_allowlist(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    with (
        patch("tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["compose", "up"], extra_env={"COMPOSE_PROJECT_NAME": "demo"})
    env = run.call_args.kwargs["env"]
    assert env["COMPOSE_PROJECT_NAME"] == "demo"


def test_run_docker_windows_sets_create_no_window():
    fake_flag = 0x08000000  # actual value of CREATE_NO_WINDOW; arbitrary for the test
    with (
        patch("tools._cli.sys.platform", "win32"),
        patch.object(cli_module.subprocess, "CREATE_NO_WINDOW", fake_flag, create=True),
        patch("tools._cli.shutil.which", return_value=r"C:\Program Files\Docker\docker.exe"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["version"])
    assert run.call_args.kwargs["creationflags"] == fake_flag


def test_run_docker_non_windows_creationflags_zero():
    with (
        patch("tools._cli.sys.platform", "linux"),
        patch("tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["version"])
    assert run.call_args.kwargs["creationflags"] == 0


def test_has_plugin_true_when_version_exits_zero():
    with patch("tools._cli.run_docker", return_value=CliResult(0, "v2.30", "", False)):
        assert has_plugin("compose") is True


def test_has_plugin_false_when_version_exits_nonzero():
    with patch("tools._cli.run_docker", return_value=CliResult(1, "", "no plugin", False)):
        assert has_plugin("compose") is False


def test_has_plugin_false_when_binary_missing():
    with patch("tools._cli.run_docker", side_effect=FileNotFoundError("nope")):
        assert has_plugin("compose") is False


def test_has_plugin_false_on_timeout():
    with patch("tools._cli.run_docker", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10)):
        assert has_plugin("compose") is False


def test_has_plugin_is_cached():
    call_count = {"n": 0}

    def fake_run(*_a, **_k):
        call_count["n"] += 1
        return CliResult(0, "v1", "", False)

    with patch("tools._cli.run_docker", side_effect=fake_run):
        has_plugin("compose")
        has_plugin("compose")
        has_plugin("compose")
    assert call_count["n"] == 1


def test_require_plugin_raises_when_missing():
    with patch("tools._cli.has_plugin", return_value=False):
        with pytest.raises(RuntimeError, match="'buildx' is not installed"):
            require_plugin("buildx")


def test_require_plugin_silent_when_present():
    with patch("tools._cli.has_plugin", return_value=True):
        require_plugin("compose")


def test_cli_result_to_dict_is_serializable():
    r = CliResult(0, "out", "err", False)
    assert r.to_dict() == {"returncode": 0, "stdout": "out", "stderr": "err", "truncated": False}

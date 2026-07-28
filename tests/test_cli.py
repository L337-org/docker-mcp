import contextlib
import pathlib
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

import docker_mcp._hosts as _hosts_mod
import docker_mcp.tools._cli as cli_module
from docker_mcp._hosts import Host, parse_registry
from docker_mcp.tools._ssh_proxy import RemoteExecResult
from docker_mcp.tools._cli import (
    MAX_CLI_OUTPUT_BYTES,
    CliResult,
    has_plugin,
    parse_json_or_ndjson,
    parse_ndjson,
    raise_on_cli_failure,
    require_plugin,
    run_docker,
    safe_positional,
)


@pytest.fixture(autouse=True)
def _clear_plugin_cache():  # pyright: ignore[reportUnusedFunction]
    cli_module._clear_plugin_cache()
    yield
    cli_module._clear_plugin_cache()


def _fake_completed(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_run_docker_passes_argv_list_and_no_shell():
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed(b"hi\n")) as run,
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
    with patch("docker_mcp.tools._cli.shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="was not found on PATH"):
            run_docker(["version"])


def test_run_docker_forwards_timeout_and_cwd_and_stdin():
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
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
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed(stdout=payload)),
    ):
        result = run_docker(["version"])
    assert result.stdout.startswith("ok-")
    assert result.stdout.endswith("-end")
    assert result.truncated is False


def test_run_docker_truncates_oversized_output():
    big = b"x" * (MAX_CLI_OUTPUT_BYTES + 100)
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed(stdout=big)),
    ):
        result = run_docker(["logs", "x"])
    assert len(result.stdout) == MAX_CLI_OUTPUT_BYTES
    assert result.truncated is True


def test_run_docker_truncated_flag_set_when_only_stderr_overflows():
    big = b"e" * (MAX_CLI_OUTPUT_BYTES + 1)
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed(stderr=big, returncode=1)),
    ):
        result = run_docker(["version"])
    assert result.truncated is True
    assert result.returncode == 1


def test_run_docker_env_allowlist_drops_unrelated_vars(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://example:2375")
    monkeypatch.setenv("MY_SECRET", "leak-me")
    monkeypatch.setenv("PATH", "/usr/bin")
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["version"])
    env = run.call_args.kwargs["env"]
    assert env["DOCKER_HOST"] == "tcp://example:2375"
    assert env["PATH"] == "/usr/bin"
    assert "MY_SECRET" not in env


def test_run_docker_rewrites_ssh_docker_host_to_local_proxy(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "ssh://bob@example.com")
    fake_proxy = MagicMock()
    fake_proxy.port = 54321

    class FakeProxyCtx:
        def __enter__(self):
            return fake_proxy

        def __exit__(self, *exc_info):
            return False

    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
        patch("docker_mcp.tools._cli.ssh_proxy_for_docker_host", return_value=FakeProxyCtx()) as ssh_proxy,
    ):
        run_docker(["ps", "-a"])
    ssh_proxy.assert_called_once_with("ssh://bob@example.com", timeout=60.0)
    env = run.call_args.kwargs["env"]
    assert env["DOCKER_HOST"] == "tcp://127.0.0.1:54321"


def test_run_docker_passes_its_own_timeout_to_ssh_proxy_setup(monkeypatch):
    # The paramiko connect that stands up the proxy runs before subprocess.run's own timeout
    # enforcement kicks in, so it must be bounded by this call's timeout too — otherwise a slow or
    # unreachable ssh:// host could hang past the caller's deadline regardless of what's passed here.
    monkeypatch.setenv("DOCKER_HOST", "ssh://bob@example.com")
    fake_proxy = MagicMock()
    fake_proxy.port = 54321

    class FakeProxyCtx:
        def __enter__(self):
            return fake_proxy

        def __exit__(self, *exc_info):
            return False

    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()),
        patch("docker_mcp.tools._cli.ssh_proxy_for_docker_host", return_value=FakeProxyCtx()) as ssh_proxy,
    ):
        run_docker(["ps", "-a"], timeout=5.0)
    ssh_proxy.assert_called_once_with("ssh://bob@example.com", timeout=5.0)


def test_run_docker_leaves_non_ssh_docker_host_untouched(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://example:2375")
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
        patch("docker_mcp.tools._cli.ssh_proxy_for_docker_host") as ssh_proxy,
    ):
        run_docker(["ps", "-a"])
    ssh_proxy.assert_not_called()
    env = run.call_args.kwargs["env"]
    assert env["DOCKER_HOST"] == "tcp://example:2375"


def test_run_docker_drops_forwarded_tls_env_when_rewriting_ssh_host(monkeypatch):
    # A native ssh:// DOCKER_HOST ignores TLS entirely; if leftover DOCKER_TLS_VERIFY/
    # DOCKER_CERT_PATH from the environment survived the rewrite to tcp://127.0.0.1:<port>, the
    # CLI would attempt a TLS handshake against the plaintext local proxy and every call would fail.
    monkeypatch.setenv("DOCKER_HOST", "ssh://bob@example.com")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/certs")
    fake_proxy = MagicMock()
    fake_proxy.port = 54321

    class FakeProxyCtx:
        def __enter__(self):
            return fake_proxy

        def __exit__(self, *exc_info):
            return False

    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
        patch("docker_mcp.tools._cli.ssh_proxy_for_docker_host", return_value=FakeProxyCtx()),
    ):
        run_docker(["ps", "-a"])
    env = run.call_args.kwargs["env"]
    assert "DOCKER_TLS_VERIFY" not in env
    assert "DOCKER_CERT_PATH" not in env


def test_run_docker_keeps_tls_env_for_non_ssh_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://example:2376")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/certs")
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["ps", "-a"])
    env = run.call_args.kwargs["env"]
    assert env["DOCKER_TLS_VERIFY"] == "1"
    assert env["DOCKER_CERT_PATH"] == "/certs"


def test_run_docker_extra_env_overlays_allowlist(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["compose", "up"], extra_env={"COMPOSE_PROJECT_NAME": "demo"})
    env = run.call_args.kwargs["env"]
    assert env["COMPOSE_PROJECT_NAME"] == "demo"


def test_run_docker_extra_env_tls_survives_apply_host_env(monkeypatch):
    # extra_env is applied after _apply_host_env, so caller-provided TLS vars must not be stripped
    # even when the host has no (tls=) marker and DOCKER_TLS_VERIFY is absent from os.environ.
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("DOCKER_MCP_SERVER_HOSTS", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["version"], extra_env={"DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": "/certs"})
    env = run.call_args.kwargs["env"]
    assert env["DOCKER_TLS_VERIFY"] == "1"
    assert env["DOCKER_CERT_PATH"] == "/certs"


def test_run_docker_windows_sets_create_no_window():
    fake_flag = 0x08000000  # actual value of CREATE_NO_WINDOW; arbitrary for the test
    with (
        patch("docker_mcp.tools._cli.sys.platform", "win32"),
        patch.object(cli_module.subprocess, "CREATE_NO_WINDOW", fake_flag, create=True),
        patch("docker_mcp.tools._cli.shutil.which", return_value=r"C:\Program Files\Docker\docker.exe"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["version"])
    assert run.call_args.kwargs["creationflags"] == fake_flag


def test_run_docker_non_windows_creationflags_zero():
    with (
        patch("docker_mcp.tools._cli.sys.platform", "linux"),
        patch("docker_mcp.tools._cli.shutil.which", return_value="/usr/bin/docker"),
        patch("docker_mcp.tools._cli.subprocess.run", return_value=_fake_completed()) as run,
    ):
        run_docker(["version"])
    assert run.call_args.kwargs["creationflags"] == 0


def test_has_plugin_true_when_version_exits_zero():
    with patch("docker_mcp.tools._cli.run_docker", return_value=CliResult(0, "v2.30", "", False)):
        assert has_plugin("compose") is True


def test_has_plugin_false_when_version_exits_nonzero():
    with patch("docker_mcp.tools._cli.run_docker", return_value=CliResult(1, "", "no plugin", False)):
        assert has_plugin("compose") is False


def test_has_plugin_false_when_binary_missing():
    with patch("docker_mcp.tools._cli.run_docker", side_effect=FileNotFoundError("nope")):
        assert has_plugin("compose") is False


def test_has_plugin_false_on_timeout():
    with patch("docker_mcp.tools._cli.run_docker", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10)):
        assert has_plugin("compose") is False


def test_has_plugin_is_cached_within_ttl():
    call_count = {"n": 0}

    def fake_run(*_a, **_k):
        call_count["n"] += 1
        return CliResult(0, "v1", "", False)

    with patch("docker_mcp.tools._cli.run_docker", side_effect=fake_run):
        has_plugin("compose")
        has_plugin("compose")
        has_plugin("compose")
    assert call_count["n"] == 1


def test_has_plugin_reprobes_after_ttl_expires():
    call_count = {"n": 0}

    def fake_run(*_a, **_k):
        call_count["n"] += 1
        return CliResult(0, "v1", "", False)

    # Force the cached entry to look older than the TTL so the next call re-probes — this is what
    # lets a plugin installed mid-session become visible without restarting the server.
    with patch("docker_mcp.tools._cli.run_docker", side_effect=fake_run):
        has_plugin("compose")
        with patch("docker_mcp.tools._cli.time.monotonic", return_value=time.monotonic() + 10_000):
            has_plugin("compose")
    assert call_count["n"] == 2


def test_clear_plugin_cache_forces_reprobe():
    call_count = {"n": 0}

    def fake_run(*_a, **_k):
        call_count["n"] += 1
        return CliResult(0, "v1", "", False)

    with patch("docker_mcp.tools._cli.run_docker", side_effect=fake_run):
        has_plugin("compose")
        cli_module._clear_plugin_cache()
        has_plugin("compose")
    assert call_count["n"] == 2


def test_require_plugin_raises_when_missing():
    with patch("docker_mcp.tools._cli.has_plugin", return_value=False):
        with pytest.raises(RuntimeError, match="'buildx' is not installed"):
            require_plugin("buildx")


def test_require_plugin_silent_when_present():
    with patch("docker_mcp.tools._cli.has_plugin", return_value=True):
        require_plugin("compose")


def test_cli_result_to_dict_is_serializable():
    r = CliResult(0, "out", "err", False)
    assert r.to_dict() == {"returncode": 0, "stdout": "out", "stderr": "err", "truncated": False}


# ---------- safe_positional ----------


@pytest.mark.parametrize("value", ["alpine", "ghcr.io/org/repo:v1", "localhost:5000/x", "web", "my-context"])
def test_safe_positional_allows_normal_values(value):
    assert safe_positional(value, "image") == value


@pytest.mark.parametrize("value", ["-rf", "--follow", "--output=/etc/passwd", "-"])
def test_safe_positional_rejects_leading_dash(value):
    with pytest.raises(ValueError, match="parses as a flag"):
        safe_positional(value, "service")


def test_safe_positional_error_names_the_argument_kind():
    with pytest.raises(ValueError, match="service="):
        safe_positional("--rm", "service")


# ---------- raise_on_cli_failure ----------


def test_raise_on_cli_failure_silent_on_zero_exit():
    raise_on_cli_failure(CliResult(0, "ok", "", False), "buildx ls")


def test_raise_on_cli_failure_raises_with_command_and_stderr():
    with pytest.raises(RuntimeError, match=r"`docker buildx ls` failed with exit code 2: boom"):
        raise_on_cli_failure(CliResult(2, "", "boom", False), "buildx ls")


def test_raise_on_cli_failure_falls_back_to_stdout_then_placeholder():
    with pytest.raises(RuntimeError, match="only-on-stdout"):
        raise_on_cli_failure(CliResult(1, "only-on-stdout", "", False), "context inspect")
    with pytest.raises(RuntimeError, match="<no output>"):
        raise_on_cli_failure(CliResult(1, "", "", False), "context inspect")


# ---------- parse_ndjson ----------


def test_parse_ndjson_handles_ndjson():
    assert parse_ndjson('{"a": 1}\n{"a": 2}\n') == [{"a": 1}, {"a": 2}]


def test_parse_ndjson_skips_blank_lines():
    assert parse_ndjson('{"a": 1}\n\n{"a": 2}\n') == [{"a": 1}, {"a": 2}]


def test_parse_ndjson_empty_returns_empty_list():
    assert parse_ndjson("") == []


def test_parse_ndjson_drops_partial_last_line_when_truncated():
    body = '{"a": 1}\n{"a": 2}\n{"a": 3, "b":'
    assert parse_ndjson(body, truncated=True) == [{"a": 1}, {"a": 2}]


def test_parse_ndjson_raises_descriptively_on_garbage_when_not_truncated():
    body = '{"a": 1}\nnot-json-at-all'
    with pytest.raises(RuntimeError, match="Could not parse .* JSON.*line 2.*truncated=False"):
        parse_ndjson(body, truncated=False, what="buildx test output")


def test_parse_ndjson_truncated_always_drops_last_line():
    # When truncated=True the last line is dropped unconditionally — a conservative call,
    # since detecting completeness of a JSON fragment is brittle.
    assert parse_ndjson('{"a": 1}\n{"a": 2}', truncated=True) == [{"a": 1}]


# ---------- parse_json_or_ndjson ----------


def test_parse_json_or_ndjson_handles_array():
    assert parse_json_or_ndjson('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]


def test_parse_json_or_ndjson_handles_ndjson():
    assert parse_json_or_ndjson('{"a": 1}\n{"a": 2}\n') == [{"a": 1}, {"a": 2}]


def test_parse_json_or_ndjson_handles_single_object():
    assert parse_json_or_ndjson('{"a": 1}') == {"a": 1}


def test_parse_json_or_ndjson_empty_returns_none():
    assert parse_json_or_ndjson("") is None
    assert parse_json_or_ndjson("   \n  ") is None


def test_parse_json_or_ndjson_drops_partial_last_ndjson_line_when_truncated():
    # NDJSON whose final record was cut off by the output cap: the complete earlier
    # records must still parse, and the partial tail is dropped rather than crashing.
    body = '{"Name":"a"}\n{"Name":"b"}\n{"Name":"c","Sta'
    assert parse_json_or_ndjson(body, truncated=True) == [{"Name": "a"}, {"Name": "b"}]


def test_parse_json_or_ndjson_truncated_ndjson_without_drop_raises_descriptively():
    # Same body, but if we (wrongly) claimed it wasn't truncated, the partial line is a
    # hard parse error surfaced with a descriptive RuntimeError, not a raw JSONDecodeError.
    body = '{"Name":"a"}\n{"Name":"c","Sta'
    with pytest.raises(RuntimeError, match="Could not parse compose ls output as JSON.*line 2"):
        parse_json_or_ndjson(body, truncated=False, what="compose ls output")


# ---------- _apply_host_env: per-host DOCKER_HOST / TLS injection ----------


def test_apply_host_env_inert_for_legacy_single_host(monkeypatch):
    # DOCKER_MCP_SERVER_HOSTS unset + single host -> inherit the ambient docker env unchanged.
    monkeypatch.delenv("DOCKER_MCP_SERVER_HOSTS", raising=False)
    monkeypatch.setattr(_hosts_mod, "_registry", parse_registry(None))
    env = {"DOCKER_HOST": "ssh://ambient", "DOCKER_CONTEXT": "ctx"}
    cli_module._apply_host_env(env, None)
    assert env == {"DOCKER_HOST": "ssh://ambient", "DOCKER_CONTEXT": "ctx"}


def test_apply_host_env_injects_resolved_url_and_drops_context(monkeypatch):
    monkeypatch.setattr(_hosts_mod, "_registry", parse_registry("local=unix:///local.sock, prod=tcp://prod:2376"))
    env = {"DOCKER_HOST": "ssh://ambient", "DOCKER_CONTEXT": "ctx"}
    cli_module._apply_host_env(env, "prod")
    assert env["DOCKER_HOST"] == "tcp://prod:2376"
    assert "DOCKER_CONTEXT" not in env


def test_apply_host_env_sets_per_host_tls(monkeypatch, tmp_path):
    certs = tmp_path / "certs"
    certs.mkdir()
    for filename in ("ca.pem", "cert.pem", "key.pem"):
        (certs / filename).write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        _hosts_mod, "_registry", parse_registry(f"local=unix:///local.sock, prod=tcp://prod:2376(tls={certs})")
    )
    env: dict[str, str] = {}
    cli_module._apply_host_env(env, "prod")
    assert env["DOCKER_HOST"] == "tcp://prod:2376"
    assert env["DOCKER_CERT_PATH"] == str(certs)
    assert env["DOCKER_TLS_VERIFY"] == "1"


def test_apply_host_env_strips_inherited_tls_for_plaintext_host(monkeypatch):
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.setattr(_hosts_mod, "_registry", parse_registry("local=unix:///local.sock, prod=tcp://prod:2376"))
    env = {"DOCKER_CERT_PATH": "/stale", "DOCKER_TLS_VERIFY": "1"}  # inherited from the allow-list
    cli_module._apply_host_env(env, "prod")
    assert "DOCKER_CERT_PATH" not in env
    assert "DOCKER_TLS_VERIFY" not in env


def test_apply_host_env_platform_default_strips_ambient(monkeypatch):
    # An explicit host resolving to url=None must drop the ambient DOCKER_HOST/DOCKER_CONTEXT so the
    # child CLI uses the platform default rather than being retargeted by ambient settings.
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.setattr(_hosts_mod, "_registry", {"box": Host("box", None), "prod": Host("prod", "tcp://prod:2376")})
    env = {"DOCKER_HOST": "tcp://ambient", "DOCKER_CONTEXT": "ctx"}
    cli_module._apply_host_env(env, "box")
    assert "DOCKER_HOST" not in env
    assert "DOCKER_CONTEXT" not in env


# ---------- should_remote_exec: when a CLI call has to run on the remote host ----------


def _pin_hosts(monkeypatch, spec: str) -> None:
    monkeypatch.setattr(_hosts_mod, "_registry", parse_registry(spec))


@pytest.mark.parametrize(
    ("hosts_spec", "host", "which", "plugin_present", "plugin", "expected"),
    [
        # ssh:// target with nothing local to serve the call -> remote.
        ("prod=ssh://ops@prod", "prod", None, False, "scout", True),
        ("prod=ssh://ops@prod", "prod", None, False, None, True),
        # ssh:// target with a working local CLI/plugin -> local, unchanged behavior.
        ("prod=ssh://ops@prod", "prod", "/usr/bin/docker", True, "scout", False),
        ("prod=ssh://ops@prod", "prod", "/usr/bin/docker", True, None, False),
        # ssh:// target, local binary but the plugin is missing -> remote (a core-CLI call still local).
        ("prod=ssh://ops@prod", "prod", "/usr/bin/docker", False, "scout", True),
        ("prod=ssh://ops@prod", "prod", "/usr/bin/docker", False, None, False),
        # Never for a transport we cannot open a shell on, however broken the local CLI is.
        ("prod=tcp://prod:2376", "prod", None, False, "scout", False),
        ("prod=unix:///var/run/docker.sock", "prod", None, False, None, False),
        # host=None resolves to the default (first) entry, not to whichever entry is ssh://.
        ("local=unix:///local.sock, prod=ssh://ops@prod", None, None, False, "scout", False),
        ("prod=ssh://ops@prod, local=unix:///local.sock", None, None, False, "scout", True),
    ],
)
def test_should_remote_exec_matrix(monkeypatch, hosts_spec, host, which, plugin_present, plugin, expected):
    _pin_hosts(monkeypatch, hosts_spec)
    with (
        patch("docker_mcp.tools._cli.shutil.which", return_value=which),
        patch("docker_mcp.tools._cli.has_plugin", return_value=plugin_present) as has,
    ):
        assert cli_module.should_remote_exec(host, plugin=plugin) is expected
    if which is None or plugin is None:
        has.assert_not_called()  # no point probing a plugin when there is no binary (or no plugin needed)


def test_should_remote_exec_does_not_probe_for_a_non_ssh_host(monkeypatch):
    # The probe shells out (and, against an ssh:// default, would connect), so the cheap
    # transport check has to come first.
    _pin_hosts(monkeypatch, "prod=tcp://prod:2376")
    with (
        patch("docker_mcp.tools._cli.shutil.which") as which,
        patch("docker_mcp.tools._cli.has_plugin") as has,
    ):
        assert cli_module.should_remote_exec("prod", plugin="compose") is False
    which.assert_not_called()
    has.assert_not_called()


def test_should_remote_exec_false_for_platform_default_host(monkeypatch):
    # url=None is the platform socket/npipe, which is never reachable over SSH.
    monkeypatch.setattr(_hosts_mod, "_registry", {"box": Host("box", None)})
    with patch("docker_mcp.tools._cli.shutil.which", return_value=None):
        assert cli_module.should_remote_exec("box", plugin="scout") is False


# ---------- remote_exec_cli: the remote backend in run_docker's result shape ----------


def _remote_result(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, truncated: bool = False):
    return RemoteExecResult(returncode=returncode, stdout=stdout, stderr=stderr, truncated=truncated)


def test_remote_exec_cli_runs_docker_on_the_resolved_ssh_url(monkeypatch):
    _pin_hosts(monkeypatch, "local=unix:///local.sock, prod=ssh://ops@prod:2222")
    with patch(
        "docker_mcp.tools._cli.run_remote_exec", return_value=_remote_result(stdout=b"out\n", stderr=b"warn\n")
    ) as remote:
        result = cli_module.remote_exec_cli("prod", ["scout", "cves", "alpine"], timeout=42.0)
    assert remote.call_args.args == ("ssh://ops@prod:2222", ["docker", "scout", "cves", "alpine"])
    assert remote.call_args.kwargs == {"max_output_bytes": MAX_CLI_OUTPUT_BYTES, "timeout": 42.0}
    assert isinstance(result, CliResult)
    assert (result.returncode, result.stdout, result.stderr, result.truncated) == (0, "out\n", "warn\n", False)


def test_remote_exec_cli_decodes_utf8_with_replace(monkeypatch):
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    with patch("docker_mcp.tools._cli.run_remote_exec", return_value=_remote_result(stdout=b"ok-\xff-end")):
        result = cli_module.remote_exec_cli("prod", ["scout", "version"])
    assert result.stdout.startswith("ok-")
    assert result.stdout.endswith("-end")


def test_remote_exec_cli_carries_through_remote_truncation(monkeypatch):
    # The drain is the only place that saw the discarded bytes, so its flag has to survive even
    # though the retained output is (necessarily) within the cap by the time we decode it.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    with patch("docker_mcp.tools._cli.run_remote_exec", return_value=_remote_result(stdout=b"partial", truncated=True)):
        result = cli_module.remote_exec_cli("prod", ["scout", "sbom", "alpine"])
    assert result.truncated is True
    assert result.stdout == "partial"


def test_remote_exec_cli_preserves_nonzero_exit_without_raising(monkeypatch):
    # Action tools return the raw result and decide for themselves; the backend must not raise for them.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    with patch("docker_mcp.tools._cli.run_remote_exec", return_value=_remote_result(stderr=b"boom", returncode=17)):
        result = cli_module.remote_exec_cli("prod", ["scout", "cves", "alpine"])
    assert (result.returncode, result.stderr) == (17, "boom")


def test_remote_exec_cli_propagates_timeout_as_timeout_expired(monkeypatch):
    # Same exception the local subprocess path raises, so a tool sees one contract either way.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    with patch(
        "docker_mcp.tools._cli.run_remote_exec",
        side_effect=subprocess.TimeoutExpired(cmd=["docker", "scout", "cves"], timeout=5.0),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            cli_module.remote_exec_cli("prod", ["scout", "cves", "alpine"], timeout=5.0)


def test_remote_exec_cli_refuses_a_non_ssh_host(monkeypatch):
    _pin_hosts(monkeypatch, "prod=tcp://prod:2376")
    with patch("docker_mcp.tools._cli.run_remote_exec") as remote:
        with pytest.raises(RuntimeError, match="not reached over ssh://"):
            cli_module.remote_exec_cli("prod", ["scout", "cves", "alpine"])
    remote.assert_not_called()


def test_remote_exec_cli_refuses_stdin_and_extra_env(monkeypatch):
    # Silently dropping either would make the remote path diverge from the local one invisibly.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    with patch("docker_mcp.tools._cli.run_remote_exec") as remote:
        with pytest.raises(ValueError, match="stdin"):
            cli_module.remote_exec_cli("prod", ["build", "-"], stdin=b"FROM alpine")
        with pytest.raises(ValueError, match="COMPOSE_PROJECT_NAME"):
            cli_module.remote_exec_cli("prod", ["compose", "up"], extra_env={"COMPOSE_PROJECT_NAME": "demo"})
    remote.assert_not_called()


# ---------- flag_values ----------


def test_flag_values_recovers_the_paths_an_argv_names():
    from docker_mcp.tools._cli import flag_values

    args = ["compose", "-f", "a.yml", "-f", "sub/b.yml", "up", "-d"]
    assert flag_values(args, "-f") == ["a.yml", "sub/b.yml"]
    assert flag_values(args, "-c") == []


def test_flag_values_ignores_a_trailing_flag_with_no_value():
    from docker_mcp.tools._cli import flag_values

    assert flag_values(["stack", "deploy", "-c"], "-c") == []


# ---------- remote_stage_and_exec: staging the working directory ----------


class _FakeSession:
    """Records what a staging session was asked to stage and run."""

    def __init__(self, root="/tmp/docker-mcp-server.stage.abc"):
        self.root = root
        self.trees: list[str] = []
        self.files: list[str] = []
        self.calls: list[dict] = []
        self.result: RemoteExecResult | None = None

    def stage_tree(self, local_dir):
        self.trees.append(str(local_dir))
        return f"{self.root}/tree{len(self.trees)}"

    def stage_file(self, local_file):
        self.files.append(str(local_file))
        return f"{self.root}/file{len(self.files)}/{pathlib.Path(local_file).name}"

    def exec(self, argv, *, timeout, max_output_bytes, cwd=None):
        self.calls.append({"argv": list(argv), "timeout": timeout, "cwd": cwd, "cap": max_output_bytes})
        return self.result or RemoteExecResult(returncode=0, stdout=b"", stderr=b"", truncated=False)


@contextlib.contextmanager
def _fake_staging(session):
    yield session


def _stage_patched(session):
    return patch("docker_mcp.tools._cli.remote_staging_session", lambda *a, **k: _fake_staging(session))


def test_remote_stage_and_exec_stages_the_given_cwd_and_runs_in_it(monkeypatch, tmp_path):
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    project = tmp_path / "project"
    project.mkdir()
    session = _FakeSession()
    with _stage_patched(session):
        result = cli_module.remote_stage_and_exec("prod", ["compose", "up", "-d"], cwd=project, timeout=120.0)
    assert session.trees == [str(project)]
    call = session.calls[0]
    assert call["argv"] == ["docker", "compose", "up", "-d"]
    assert call["cwd"] == f"{session.root}/tree1"  # runs in the staged copy, not the login home dir
    assert (call["timeout"], call["cap"]) == (120.0, MAX_CLI_OUTPUT_BYTES)
    assert isinstance(result, CliResult)


def test_remote_stage_and_exec_treats_cwd_none_as_the_servers_own_directory(monkeypatch, tmp_path):
    """
    The trap this exists to avoid: `cwd=None` means the server's cwd on the local path (that is what
    `subprocess.run` uses), so resolving it to "stage nothing" would leave the command running in the
    SSH login home directory — quietly acting on whatever project happened to live there.
    """
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    workdir = tmp_path / "server-cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec("prod", ["compose", "ps"], cwd=None, timeout=60.0)
    assert session.trees == [str(workdir)]
    assert session.calls[0]["cwd"] == f"{session.root}/tree1"


def test_remote_stage_and_exec_leaves_a_relative_in_tree_path_alone(monkeypatch, tmp_path):
    # A relative token already resolves against the staged tree, which is the remote cwd.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / "sub" / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod",
            ["compose", "-f", "sub/compose.yml", "up"],
            cwd=project,
            timeout=60.0,
            path_values=["sub/compose.yml"],
        )
    assert session.calls[0]["argv"] == ["docker", "compose", "-f", "sub/compose.yml", "up"]
    assert session.files == []  # no second copy of a file the tree already carried


def test_remote_stage_and_exec_rewrites_an_absolute_in_tree_path_relative(monkeypatch, tmp_path):
    """
    An absolute local path is copied over as part of the tree, but the *token* still names a directory
    that does not exist on the remote host — so it has to become relative to the staged copy.
    """
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    project = tmp_path / "project"
    project.mkdir()
    compose_file = project / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod",
            ["compose", "-f", str(compose_file), "up"],
            cwd=project,
            timeout=60.0,
            path_values=[str(compose_file)],
        )
    assert session.calls[0]["argv"] == ["docker", "compose", "-f", "docker-compose.yml", "up"]
    assert session.files == []


def test_remote_stage_and_exec_stages_a_path_outside_the_tree(monkeypatch, tmp_path):
    # A shared override file next to (not inside) the project: staged on its own and pointed at.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    project = tmp_path / "project"
    project.mkdir()
    shared = tmp_path / "shared" / "base.yml"
    shared.parent.mkdir()
    shared.write_text("services: {}\n", encoding="utf-8")
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod",
            ["compose", "-f", str(shared), "config"],
            cwd=project,
            timeout=60.0,
            path_values=[str(shared)],
        )
    assert session.files == [str(shared)]
    assert session.calls[0]["argv"] == ["docker", "compose", "-f", f"{session.root}/file1/base.yml", "config"]


def test_remote_stage_and_exec_leaves_a_value_that_names_no_local_path(monkeypatch, tmp_path):
    """
    Nothing to stage and nothing to rewrite, so both backends report the path the caller passed.

    The in-tree case matters as much as the out-of-tree one: rewriting a missing `/proj/missing.yml` to
    `missing.yml` would make the remote CLI complain about a name the caller never wrote, where the
    local backend would have echoed the absolute path back.
    """
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    project = tmp_path / "project"
    project.mkdir()
    outside = "/nowhere/missing.yml"
    inside = str(project / "missing.yml")
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod",
            ["compose", "-f", outside, "-f", inside, "up"],
            cwd=project,
            timeout=60.0,
            path_values=[outside, inside],
        )
    assert session.calls[0]["argv"] == ["docker", "compose", "-f", outside, "-f", inside, "up"]
    assert session.files == []


def test_remote_stage_and_exec_refuses_an_unusable_working_directory(monkeypatch, tmp_path):
    # Two different mistakes land here, and the message has to distinguish them: a missing path, and a
    # file passed where a directory belongs (`is_dir()` is false for both).
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    a_file = tmp_path / "docker-compose.yml"
    a_file.write_text("services: {}\n", encoding="utf-8")
    session = _FakeSession()
    with _stage_patched(session):
        with pytest.raises(ValueError, match="nothing exists at that path"):
            cli_module.remote_stage_and_exec("prod", ["compose", "up"], cwd=tmp_path / "gone", timeout=60.0)
        with pytest.raises(ValueError, match="exists but is not a directory"):
            cli_module.remote_stage_and_exec("prod", ["compose", "up"], cwd=a_file, timeout=60.0)
    assert session.trees == []


def test_remote_stage_and_exec_refuses_stdin_extra_env_and_a_non_ssh_host(monkeypatch, tmp_path):
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod, local=unix:///local.sock")
    session = _FakeSession()
    with _stage_patched(session):
        with pytest.raises(ValueError, match="stdin"):
            cli_module.remote_stage_and_exec("prod", ["compose", "up"], cwd=tmp_path, stdin=b"x", timeout=60.0)
        with pytest.raises(ValueError, match="COMPOSE_FILE"):
            cli_module.remote_stage_and_exec(
                "prod", ["compose", "up"], cwd=tmp_path, extra_env={"COMPOSE_FILE": "x"}, timeout=60.0
            )
        with pytest.raises(RuntimeError, match="not reached over ssh://"):
            cli_module.remote_stage_and_exec("local", ["compose", "up"], cwd=tmp_path, timeout=60.0)
    assert session.trees == []  # every refusal lands before anything is connected or copied


def test_remote_stage_and_exec_returns_the_commands_own_failure(monkeypatch, tmp_path):
    # Action tools inspect returncode themselves, so a non-zero exit must come back, not raise.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    session = _FakeSession()
    session.result = RemoteExecResult(returncode=1, stdout=b"", stderr=b"no such service", truncated=True)
    with _stage_patched(session):
        result = cli_module.remote_stage_and_exec("prod", ["compose", "up"], cwd=tmp_path, timeout=60.0)
    assert (result.returncode, result.stderr, result.truncated) == (1, "no such service", True)


# ---------- filter_args ----------


def test_filter_args_renders_dict_as_repeated_filter_flags():
    from docker_mcp.tools._cli import filter_args

    assert filter_args({"name": "web"}) == ["--filter", "name=web"]
    assert filter_args(None) == []


def test_filter_args_list_value_repeats_the_filter():
    from docker_mcp.tools._cli import filter_args

    assert filter_args({"label": ["a=1", "b=2"]}) == ["--filter", "label=a=1", "--filter", "label=b=2"]


def test_filter_args_lowercases_booleans():
    from docker_mcp.tools._cli import filter_args

    assert filter_args({"dangling": True}) == ["--filter", "dangling=true"]


def test_remote_stage_and_exec_explains_an_unavailable_server_cwd(monkeypatch, tmp_path):
    """
    `Path.cwd()` raises a bare "No such file or directory" when the server's own working directory has
    been deleted underneath it — which says nothing about what to do, and the local backend tolerates
    the same situation (a process keeps its deleted cwd). Found by tripping over it live.
    """
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    session = _FakeSession()
    monkeypatch.setattr(
        cli_module.Path, "cwd", staticmethod(lambda: (_ for _ in ()).throw(FileNotFoundError(2, "No such file")))
    )
    with _stage_patched(session):
        with pytest.raises(ValueError, match="that is what would be copied over"):
            cli_module.remote_stage_and_exec("prod", ["compose", "ps"], cwd=None, timeout=60.0)
        # The same failure means something different when nothing is being staged as a working
        # directory: there it is only what relative paths resolve against, so the message says so — and
        # the remedy differs, because such a tool may expose no `cwd` for the caller to set.
        with pytest.raises(ValueError, match="Pass absolute paths instead"):
            cli_module.remote_stage_and_exec(
                "prod",
                ["buildx", "create", "--config", "rel.toml"],
                cwd=None,
                timeout=60.0,
                path_values=["rel.toml"],
                stage_cwd=False,
            )
    assert session.trees == []


def test_remote_stage_and_exec_needs_no_working_directory_for_absolute_paths_only(monkeypatch, tmp_path):
    """
    A tool in the no-staging mode may expose no `cwd` at all (`buildx_create --config /etc/…`), so
    demanding a usable server working directory would fail a call that needs none: the base is only ever
    read to resolve a *relative* value.
    """
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    config = tmp_path / "buildkitd.toml"
    config.write_text("[worker]\n", encoding="utf-8")
    monkeypatch.setattr(
        cli_module.Path, "cwd", staticmethod(lambda: (_ for _ in ()).throw(FileNotFoundError(2, "No such file")))
    )
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod",
            ["buildx", "create", "--config", str(config)],
            cwd=None,
            timeout=60.0,
            path_values=[str(config)],
            stage_cwd=False,
        )
    assert session.files == [str(config)]
    assert session.calls[0]["argv"][-1] == f"{session.root}/file1/buildkitd.toml"


def test_remote_stage_and_exec_does_not_expand_tilde_in_cwd_or_path_tokens(monkeypatch, tmp_path):
    """
    Parity guard. `subprocess.run(cwd=...)` does not expand `~` (verified: it raises FileNotFoundError
    for '~/proj'), the docker CLI does not expand argv tokens either, and the compose/stack docstrings
    promise paths are used verbatim. Expanding here would make the same call succeed remotely and fail
    locally — the one divergence this backend exists to avoid.
    """
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    session = _FakeSession()
    with _stage_patched(session):
        with pytest.raises(ValueError, match="nothing exists at that path"):
            cli_module.remote_stage_and_exec("prod", ["compose", "up"], cwd="~", timeout=60.0)
    # And a `~` token is passed through untouched rather than resolved to the server user's home.
    project = tmp_path / "project"
    project.mkdir()
    home_file = "~/docker-compose.yml"
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod", ["compose", "-f", home_file, "up"], cwd=project, timeout=60.0, path_values=[home_file]
        )
    assert session.calls[-1]["argv"] == ["docker", "compose", "-f", home_file, "up"]
    assert session.files == []


# ---------- remote_stage_and_exec: stage_cwd=False (only the named paths) ----------


def test_remote_stage_and_exec_without_staging_a_cwd_stages_each_named_path(monkeypatch, tmp_path):
    """
    The mode `buildx create --config` / `buildx imagetools create --file` use: nothing is copied as a
    working directory, the remote command gets no cwd, and each declared path that exists locally is
    staged on its own — there is no staged tree for it to be "inside".
    """
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    config = tmp_path / "buildkitd.toml"
    config.write_text("[worker]\n", encoding="utf-8")
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod",
            ["buildx", "create", "--config", str(config)],
            cwd=tmp_path,
            timeout=60.0,
            path_values=[str(config)],
            stage_cwd=False,
        )
    assert session.trees == []  # the working directory itself is not copied
    assert session.files == [str(config)]
    call = session.calls[0]
    assert call["cwd"] is None
    assert call["argv"] == ["docker", "buildx", "create", "--config", f"{session.root}/file1/buildkitd.toml"]


def test_remote_stage_and_exec_without_staging_a_cwd_resolves_relative_paths_against_it(monkeypatch, tmp_path):
    # `cwd` still says where a relative value resolves — the same place the local subprocess would have
    # resolved it — even though it is not itself copied.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    descriptor = tmp_path / "desc.json"
    descriptor.write_text("{}\n", encoding="utf-8")
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod",
            ["buildx", "imagetools", "create", "--file", "desc.json"],
            cwd=tmp_path,
            timeout=60.0,
            path_values=["desc.json"],
            stage_cwd=False,
        )
    assert session.files == [str(descriptor)]
    assert session.calls[0]["argv"][-1] == f"{session.root}/file1/desc.json"


def test_remote_stage_and_exec_without_staging_a_cwd_tolerates_a_missing_directory(monkeypatch, tmp_path):
    # With nothing being copied there is no reason to require the directory to exist: a value that names
    # nothing is simply left for the remote CLI to report, as on the local path.
    _pin_hosts(monkeypatch, "prod=ssh://ops@prod")
    session = _FakeSession()
    with _stage_patched(session):
        cli_module.remote_stage_and_exec(
            "prod",
            ["buildx", "create", "--config", "absent.toml"],
            cwd=tmp_path / "gone",
            timeout=60.0,
            path_values=["absent.toml"],
            stage_cwd=False,
        )
    assert session.files == []
    assert session.calls[0]["argv"][-1] == "absent.toml"

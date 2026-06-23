import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

import docker_mcp._hosts as _hosts_mod
import docker_mcp.tools._cli as cli_module
from docker_mcp._hosts import Host, parse_registry
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

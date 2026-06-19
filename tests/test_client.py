import hashlib
import json
import os
import sys
import threading
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import DockerException

import docker_mcp.tools.client as client_module
from docker_mcp.tools.client import _get_client, close, df, events, info, login, logout, ping, reconnect, version


class _BlockingStream:
    """A CancellableStream stand-in: __next__ blocks until close() is called from another thread."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.close_calls = 0

    def __iter__(self) -> _BlockingStream:
        return self

    def __next__(self) -> dict:
        # Wait (with a generous safety cap so a broken test can't hang) until close() fires, then
        # end iteration the way CancellableStream does once its socket is shut down.
        self._closed.wait(timeout=5)
        raise StopIteration

    def close(self) -> None:
        self.close_calls += 1
        self._closed.set()


def test_ping():
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        assert ping() is True
    mock_client.ping.assert_called_once()


def test_version():
    mock_client = MagicMock()
    mock_client.version.return_value = {"Version": "24.0.0"}
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        assert version() == {"Version": "24.0.0"}


def test_info():
    mock_client = MagicMock()
    mock_client.info.return_value = {"ID": "abc"}
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        assert info() == {"ID": "abc"}


def test_df():
    mock_client = MagicMock()
    mock_client.df.return_value = {"LayersSize": 1024}
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        assert df() == {"LayersSize": 1024}


def test_login():
    mock_client = MagicMock()
    mock_client.login.return_value = {"Status": "Login Succeeded"}
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        result = login("user", "pass", registry="https://example.com")
    assert result == {"Status": "Login Succeeded"}
    mock_client.login.assert_called_once_with(
        username="user",
        password="pass",
        email=None,
        registry="https://example.com",
        reauth=False,
        dockercfg_path=None,
    )


def _client_with_auths(auths: dict) -> MagicMock:
    """A mock client whose APIClient carries a real _auth_configs dict (login's storage shape)."""
    mock_client = MagicMock()
    mock_client.api._auth_configs = {"auths": auths}
    return mock_client


def test_logout_clears_all_cached_credentials_by_default():
    mock_client = _client_with_auths({"docker.io": {"username": "u"}, "ghcr.io": {"username": "v"}})
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        result = logout()
    assert sorted(result["cleared"]) == ["docker.io", "ghcr.io"]
    assert mock_client.api._auth_configs["auths"] == {}


def test_logout_clears_only_the_named_registry():
    mock_client = _client_with_auths({"docker.io": {"username": "u"}, "ghcr.io": {"username": "v"}})
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        result = logout(registry="ghcr.io")
    assert result == {"cleared": ["ghcr.io"]}
    # The other registry's credential is left intact.
    assert set(mock_client.api._auth_configs["auths"]) == {"docker.io"}


def test_logout_named_registry_not_present_is_a_noop():
    mock_client = _client_with_auths({"docker.io": {"username": "u"}})
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        result = logout(registry="quay.io")
    assert result == {"cleared": []}
    assert set(mock_client.api._auth_configs["auths"]) == {"docker.io"}


def test_logout_degrades_to_noop_when_internal_shape_is_absent():
    # If a future docker-py drops/renames _auth_configs, logout must not raise.
    mock_client = MagicMock()
    mock_client.api._auth_configs = None
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        assert logout() == {"cleared": []}


def test_events_collects_full_stream_when_under_limit():
    mock_client = MagicMock()
    mock_client.events.return_value = iter([{"event": "a"}, {"event": "b"}])
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        result = events(since="2024-01-01", until="2024-01-02")
    assert result == [{"event": "a"}, {"event": "b"}]
    mock_client.events.assert_called_once_with(since="2024-01-01", until="2024-01-02", filters=None, decode=True)


def test_events_stops_at_limit():
    mock_client = MagicMock()
    mock_client.events.return_value = iter([{"event": str(i)} for i in range(10)])
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        result = events(limit=3)
    assert result == [{"event": "0"}, {"event": "1"}, {"event": "2"}]


def test_events_returns_on_timeout_when_stream_is_quiet():
    # A quiet daemon (no events, no `until`) would block forever without the watchdog. The timer
    # closes the stream after `timeout_seconds`, unblocking iteration and returning what we have.
    stream = _BlockingStream()
    mock_client = MagicMock()
    mock_client.events.return_value = stream
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        result = events(timeout_seconds=0.1)
    assert result == []
    assert stream.close_calls >= 1


def test_get_client_wraps_daemon_unreachable():
    client_module._client = None
    with patch("docker_mcp.tools.client._build_default_client", side_effect=DockerException("connection refused")):
        with pytest.raises(RuntimeError, match="Cannot reach the Docker daemon"):
            _get_client()
    client_module._client = None


def test_close_resets_cached_client():
    fake_client = MagicMock()
    client_module._client = fake_client
    assert close() is True
    fake_client.close.assert_called_once()
    assert client_module._client is None


def test_close_when_no_cached_client():
    client_module._client = None
    assert close() is True
    assert client_module._client is None


def test_reconnect_with_explicit_host_swaps_and_closes_old():
    old_client = MagicMock()
    new_client = MagicMock()
    new_client.version.return_value = {"Version": "25.0.0"}
    client_module._client = old_client
    with patch("docker_mcp.tools.client.docker.DockerClient", return_value=new_client) as ctor:
        result = reconnect(docker_host="tcp://10.0.0.5:2376")
    ctor.assert_called_once_with(base_url="tcp://10.0.0.5:2376")
    assert result == {"Version": "25.0.0"}
    assert client_module._client is new_client
    old_client.close.assert_called_once()  # the previous client is torn down after the swap
    client_module._client = None


def test_reconnect_without_host_rebuilds_from_default():
    new_client = MagicMock()
    new_client.version.return_value = {"Version": "26.0.0"}
    client_module._client = None
    with patch("docker_mcp.tools.client._build_default_client", return_value=new_client) as build:
        result = reconnect()
    build.assert_called_once_with()
    assert result == {"Version": "26.0.0"}
    assert client_module._client is new_client
    client_module._client = None


def test_reconnect_keeps_old_client_when_new_endpoint_unreachable():
    old_client = MagicMock()
    new_client = MagicMock()
    new_client.version.side_effect = DockerException("connection refused")
    client_module._client = old_client
    with patch("docker_mcp.tools.client.docker.DockerClient", return_value=new_client):
        with pytest.raises(RuntimeError, match="daemon is unreachable"):
            reconnect(docker_host="tcp://unreachable:2376")
    # The working client must survive a failed reconnect, and the half-built one is closed.
    assert client_module._client is old_client
    new_client.close.assert_called_once()
    old_client.close.assert_not_called()
    client_module._client = None


def test_events_returns_collected_when_stream_close_raises():
    # On an ssh:// daemon CancellableStream.close() raises in the finally; the collected events must
    # still be returned rather than the close error replacing them.
    class _FiniteRaisingCloseStream:
        def __init__(self):
            self._it = iter([{"event": "a"}, {"event": "b"}])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._it)

        def close(self):
            raise DockerException("Cancellable streams not supported for the SSH protocol")

    mock_client = MagicMock()
    mock_client.events.return_value = _FiniteRaisingCloseStream()
    with patch("docker_mcp.tools.client._get_client", return_value=mock_client):
        result = events(limit=10)
    assert result == [{"event": "a"}, {"event": "b"}]


# ---------- self-termination guard ----------


def _fake_container(cid: str, name: str = "docker-mcp") -> Any:
    return types.SimpleNamespace(id=cid, short_id=cid[:12], name=name)


def test_guard_not_self_inert_when_identity_unknown(monkeypatch):
    monkeypatch.setattr(client_module, "_self_container_id", None)
    # No pinned identity → never raises, whatever the target.
    assert client_module.guard_not_self(_fake_container("abc123")) is None


def test_guard_not_self_allows_other_container(monkeypatch):
    monkeypatch.setattr(client_module, "_self_container_id", "self-full-id")
    assert client_module.guard_not_self(_fake_container("other-id")) is None


def test_guard_not_self_blocks_own_container(monkeypatch):
    monkeypatch.setattr(client_module, "_self_container_id", "self-full-id")
    with pytest.raises(RuntimeError, match="own container"):
        client_module.guard_not_self(_fake_container("self-full-id", name="docker-mcp"))


def test_guard_not_self_override_env_bypasses(monkeypatch):
    monkeypatch.setattr(client_module, "_self_container_id", "self-full-id")
    monkeypatch.setenv("DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE", "1")
    assert client_module.guard_not_self(_fake_container("self-full-id")) is None


def test_guard_not_self_override_via_deprecated_alias(monkeypatch, capsys):
    # The pre-rename DOCKER_MCP_ALLOW_SELF_TERMINATE spelling still bypasses the guard, and reading it
    # emits the one-time stderr deprecation notice so the migration stays observable at runtime.
    import docker_mcp._env as _env

    monkeypatch.setattr(_env, "_warned_aliases", set())
    monkeypatch.setattr(client_module, "_self_container_id", "self-full-id")
    monkeypatch.delenv("DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE", raising=False)
    monkeypatch.setenv("DOCKER_MCP_ALLOW_SELF_TERMINATE", "1")
    assert client_module.guard_not_self(_fake_container("self-full-id")) is None
    err = capsys.readouterr().err
    assert "DOCKER_MCP_ALLOW_SELF_TERMINATE is deprecated" in err
    assert "DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE" in err


# ---------- _detect_self_container_id ----------


def test_detect_self_container_id_from_hostname(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "deadbeef1234")
    mock_client = MagicMock()
    mock_client.containers.get.return_value = types.SimpleNamespace(id="deadbeef1234fullid")
    assert client_module._detect_self_container_id(mock_client) == "deadbeef1234fullid"
    mock_client.containers.get.assert_called_once_with("deadbeef1234")


def test_detect_self_container_id_none_when_lookup_fails(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "deadbeef1234")
    mock_client = MagicMock()
    mock_client.containers.get.side_effect = DockerException("not found")
    assert client_module._detect_self_container_id(mock_client) is None


# ---------- startup_preflight ----------


def test_startup_preflight_unreachable_prints_help_and_does_not_raise(monkeypatch, capsys):
    monkeypatch.setattr(client_module, "_self_container_id", None)
    monkeypatch.setattr(client_module, "in_container", lambda: False)
    monkeypatch.setattr(client_module, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("no daemon")))
    client_module.startup_preflight()  # must not raise
    err = capsys.readouterr().err
    assert "cannot reach the Docker daemon" in err
    assert client_module._self_container_id is None


def test_startup_preflight_in_container_help_is_os_aware(monkeypatch, capsys):
    monkeypatch.setattr(client_module, "in_container", lambda: True)
    monkeypatch.setattr(client_module, "classify_host_kernel", lambda: "docker-desktop")
    monkeypatch.setattr(client_module, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("no daemon")))
    client_module.startup_preflight()
    err = capsys.readouterr().err
    assert "Docker Desktop (macOS)" in err
    assert "docker.sock" in err


def test_startup_preflight_success_on_host_pins_nothing(monkeypatch, capsys):
    monkeypatch.setattr(client_module, "_self_container_id", None)
    monkeypatch.setattr(client_module, "in_container", lambda: False)
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.info.return_value = {"OperatingSystem": "Ubuntu 22.04", "SecurityOptions": []}
    monkeypatch.setattr(client_module, "_get_client", lambda: mock_client)
    client_module.startup_preflight()
    assert client_module._self_container_id is None
    assert "connected to Docker daemon — Ubuntu 22.04" in capsys.readouterr().err


def test_startup_preflight_success_in_container_pins_self(monkeypatch, capsys):
    monkeypatch.setattr(client_module, "_self_container_id", None)
    monkeypatch.setattr(client_module, "in_container", lambda: True)
    monkeypatch.setenv("HOSTNAME", "cafe1234")
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.containers.get.return_value = types.SimpleNamespace(id="cafe1234fullid")
    mock_client.info.return_value = {
        "OperatingSystem": "Docker Desktop",
        "SecurityOptions": ["name=seccomp", "name=rootless"],
    }
    monkeypatch.setattr(client_module, "_get_client", lambda: mock_client)
    client_module.startup_preflight()
    assert client_module._self_container_id == "cafe1234fullid"
    err = capsys.readouterr().err
    assert "(rootless)" in err
    assert "self-termination guard active" in err


def test_connection_help_ssh_endpoint_gives_ssh_specific_hints(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "ssh://user@remote")
    help_text = client_module._connection_help(RuntimeError("boom"))
    assert "ssh://" in help_text
    # The ssh branch must call out the paramiko-specific gotchas, not socket-mount advice.
    assert "known_hosts" in help_text
    assert "paramiko" in help_text
    # And it must not fall through to the unix-socket guidance.
    assert "docker.sock" not in help_text


def test_connection_help_non_ssh_endpoint_keeps_socket_guidance(monkeypatch):
    # Exercise the in-container branch so the socket-mount guidance is actually emitted — a non-ssh
    # endpoint must get the docker.sock hints, not the ssh-specific ones.
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setattr(client_module, "in_container", lambda: True)
    monkeypatch.setattr(client_module, "classify_host_kernel", lambda: "linux")
    help_text = client_module._connection_help(RuntimeError("boom"))
    assert "docker.sock" in help_text
    assert "known_hosts" not in help_text


def test_paramiko_is_available_for_ssh_transport():
    # The docker[ssh] extra must keep paramiko installed so ssh:// works via the pure-Python transport.
    import paramiko  # noqa: F401


# --- default-daemon resolution when DOCKER_HOST is unset -------------------------------------------
# docker-py's from_env() ignores Docker CLI contexts and falls back to the classic unix socket, which
# Docker Desktop 4.13+ no longer creates by default. _resolve_default_base_url() closes that gap by
# following the active context (like the docker CLI) and then probing known socket locations.


def test_scrub_unresolved_env_removes_placeholders(monkeypatch):
    # An MCP host that leaves an optional config field blank can pass the literal placeholder.
    monkeypatch.setenv("DOCKER_HOST", "${user_config.docker_host}")
    monkeypatch.setenv("DOCKER_MCP_SERVER_DISABLE", "${user_config.disable_domains}")
    client_module._scrub_unresolved_env()
    assert "DOCKER_HOST" not in os.environ
    assert "DOCKER_MCP_SERVER_DISABLE" not in os.environ


def test_scrub_unresolved_env_keeps_real_values(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.5:2375")
    monkeypatch.setenv("DOCKER_MCP_SERVER_READONLY", "true")
    client_module._scrub_unresolved_env()
    assert os.environ["DOCKER_HOST"] == "tcp://10.0.0.5:2375"
    assert os.environ["DOCKER_MCP_SERVER_READONLY"] == "true"


def test_scrub_unresolved_env_keeps_partial_template(monkeypatch):
    # Only a whole-value ${...} token is scrubbed; a value that merely contains braces is left alone.
    monkeypatch.setenv("SOME_VAR", "before ${x} after")
    client_module._scrub_unresolved_env()
    assert os.environ["SOME_VAR"] == "before ${x} after"


def test_startup_preflight_scrubs_unresolved_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "${user_config.docker_host}")
    monkeypatch.setattr(client_module, "in_container", lambda: False)
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.info.return_value = {"OperatingSystem": "Ubuntu 22.04", "SecurityOptions": []}
    monkeypatch.setattr(client_module, "_get_client", lambda: mock_client)
    client_module.startup_preflight()
    assert "DOCKER_HOST" not in os.environ  # cleared before the daemon connection is attempted


def test_active_context_name_prefers_env(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")
    assert client_module._active_context_name() == "colima"


def test_active_context_name_reads_current_context(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"currentContext": "desktop-linux"}), encoding="utf-8")
    assert client_module._active_context_name() == "desktop-linux"


def test_active_context_name_none_when_no_config(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))  # empty dir, no config.json
    assert client_module._active_context_name() is None


def test_context_host_reads_meta_json(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    name = "desktop-linux"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    meta_dir = tmp_path / "contexts" / "meta" / digest
    meta_dir.mkdir(parents=True)
    host = "unix:///Users/gavin/.docker/run/docker.sock"
    (meta_dir / "meta.json").write_text(
        json.dumps({"Name": name, "Endpoints": {"docker": {"Host": host}}}), encoding="utf-8"
    )
    assert client_module._context_host(name) == host


def test_context_host_none_when_meta_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    assert client_module._context_host("nonexistent") is None


def test_resolve_default_base_url_follows_context():
    with (
        patch("docker_mcp.tools.client._active_context_name", return_value="desktop-linux"),
        patch("docker_mcp.tools.client._context_host", return_value="unix:///home/u/.docker/run/docker.sock") as ch,
        patch("docker_mcp.tools.client._probe_default_socket") as probe,
    ):
        assert client_module._resolve_default_base_url() == "unix:///home/u/.docker/run/docker.sock"
    ch.assert_called_once_with("desktop-linux")
    probe.assert_not_called()  # a resolved context short-circuits the probe


def test_resolve_default_base_url_skips_default_context_and_probes():
    with (
        patch("docker_mcp.tools.client._active_context_name", return_value="default"),
        patch("docker_mcp.tools.client._context_host") as ch,
        patch("docker_mcp.tools.client._probe_default_socket", return_value="unix:///var/run/docker.sock"),
    ):
        assert client_module._resolve_default_base_url() == "unix:///var/run/docker.sock"
    ch.assert_not_called()  # the "default" context means the classic socket — no meta.json to read


def test_resolve_default_base_url_probes_when_context_has_no_host():
    with (
        patch("docker_mcp.tools.client._active_context_name", return_value="broken"),
        patch("docker_mcp.tools.client._context_host", return_value=None),
        patch("docker_mcp.tools.client._probe_default_socket", return_value="unix:///run/user/1000/docker.sock"),
    ):
        assert client_module._resolve_default_base_url() == "unix:///run/user/1000/docker.sock"


@pytest.mark.skipif(sys.platform == "win32", reason="socket probe is POSIX-only; Windows uses npipe via from_env()")
def test_probe_default_socket_returns_first_existing(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    # Only /var/run/docker.sock "exists"; the home-dir candidates ahead of it do not.
    monkeypatch.setattr(client_module.Path, "is_socket", lambda self: str(self) == "/var/run/docker.sock")
    assert client_module._probe_default_socket() == "unix:///var/run/docker.sock"


def test_probe_default_socket_none_on_windows(monkeypatch):
    # The probe short-circuits on Windows — the npipe default is handled by docker.from_env().
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    assert client_module._probe_default_socket() is None


def test_probe_default_socket_none_when_nothing_exists(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(client_module.Path, "is_socket", lambda _self: False)
    assert client_module._probe_default_socket() is None


def test_build_default_client_honors_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.5:2375")
    sentinel = MagicMock()
    with (
        patch("docker_mcp.tools.client.docker.from_env", return_value=sentinel) as from_env,
        patch("docker_mcp.tools.client.docker.DockerClient") as ctor,
    ):
        assert client_module._build_default_client() is sentinel
    from_env.assert_called_once_with()  # DOCKER_HOST goes through from_env (which applies its TLS env)
    ctor.assert_not_called()


def test_build_default_client_uses_resolved_base_url(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    sentinel = MagicMock()
    with (
        patch("docker_mcp.tools.client._resolve_default_base_url", return_value="unix:///x/docker.sock"),
        patch("docker_mcp.tools.client.docker.DockerClient", return_value=sentinel) as ctor,
    ):
        assert client_module._build_default_client() is sentinel
    ctor.assert_called_once_with(base_url="unix:///x/docker.sock")


def test_build_default_client_falls_back_to_from_env(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    sentinel = MagicMock()
    with (
        patch("docker_mcp.tools.client._resolve_default_base_url", return_value=None),
        patch("docker_mcp.tools.client.docker.from_env", return_value=sentinel) as from_env,
    ):
        assert client_module._build_default_client() is sentinel
    from_env.assert_called_once_with()

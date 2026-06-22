import os
import threading
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import DockerException

import docker_mcp._hosts as _hosts_mod
import docker_mcp.tools.client as client_module
from docker_mcp._hosts import Host, parse_registry
from docker_mcp.tools.client import _get_client, close, df, events, info, login, logout, ping, reconnect, version


def _set_multi(monkeypatch, spec="local=unix:///local.sock, prod=tcp://prod:2376"):
    """Pin a deterministic 2-host registry (explicit URLs, no auto/local resolution) → multi-host mode."""
    monkeypatch.setattr(_hosts_mod, "_registry", parse_registry(spec))


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
    client_module._clients.clear()
    with patch("docker_mcp.tools.client._build_default_client", side_effect=DockerException("connection refused")):
        with pytest.raises(RuntimeError, match="Cannot reach the Docker daemon"):
            _get_client()
    client_module._clients.clear()


def test_close_drops_all_pooled_clients():
    fake_client = MagicMock()
    client_module._clients["default"] = fake_client
    assert close() is True
    fake_client.close.assert_called_once()
    assert client_module._clients == {}


def test_close_when_pool_empty():
    client_module._clients.clear()
    assert close() is True
    assert client_module._clients == {}


def test_reconnect_rebuilds_default_from_pinned_endpoint():
    old_client = MagicMock()
    new_client = MagicMock()
    new_client.version.return_value = {"Version": "26.0.0"}
    client_module._clients["default"] = old_client
    with patch("docker_mcp.tools.client._build_default_client", return_value=new_client):
        result = reconnect()
    assert result == {"Version": "26.0.0"}
    assert client_module._clients["default"] is new_client
    old_client.close.assert_called_once()  # previous client torn down after the swap
    client_module._clients.clear()


def test_reconnect_keeps_old_client_when_rebuild_unreachable():
    old_client = MagicMock()
    new_client = MagicMock()
    new_client.version.side_effect = DockerException("connection refused")
    client_module._clients["default"] = old_client
    with patch("docker_mcp.tools.client._build_default_client", return_value=new_client):
        with pytest.raises(RuntimeError, match="daemon is unreachable"):
            reconnect()
    # The working client must survive a failed rebuild, and the half-built one is closed.
    assert client_module._clients["default"] is old_client
    new_client.close.assert_called_once()
    old_client.close.assert_not_called()
    client_module._clients.clear()


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
    monkeypatch.setattr(client_module, "_get_client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no daemon")))
    client_module.startup_preflight()  # must not raise
    err = capsys.readouterr().err
    assert "cannot reach the Docker daemon" in err
    assert client_module._self_container_id is None


def test_startup_preflight_in_container_help_is_os_aware(monkeypatch, capsys):
    monkeypatch.setattr(client_module, "in_container", lambda: True)
    monkeypatch.setattr(client_module, "classify_host_kernel", lambda: "docker-desktop")
    monkeypatch.setattr(client_module, "_get_client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no daemon")))
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
    monkeypatch.setattr(client_module, "_get_client", lambda *a, **k: mock_client)
    client_module.startup_preflight()
    assert client_module._self_container_id is None
    assert "connected to default host 'default' — Ubuntu 22.04" in capsys.readouterr().err


def test_startup_preflight_success_in_container_pins_self(monkeypatch, capsys):
    monkeypatch.setattr(client_module, "_self_container_id", None)
    monkeypatch.setattr(client_module, "_self_host_label", None)
    monkeypatch.setattr(client_module, "in_container", lambda: True)
    # Self-id is detected against the self host (first local-transport entry), pinned deterministically here.
    monkeypatch.setattr(client_module, "_self_host", lambda: Host("default", "unix:///var/run/docker.sock"))
    monkeypatch.setenv("HOSTNAME", "cafe1234")
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.containers.get.return_value = types.SimpleNamespace(id="cafe1234fullid")
    mock_client.info.return_value = {
        "OperatingSystem": "Docker Desktop",
        "SecurityOptions": ["name=seccomp", "name=rootless"],
    }
    monkeypatch.setattr(client_module, "_get_client", lambda *a, **k: mock_client)
    client_module.startup_preflight()
    assert client_module._self_container_id == "cafe1234fullid"
    assert client_module._self_host_label == "default"
    err = capsys.readouterr().err
    assert "(rootless)" in err
    assert "self-termination guard active" in err


def test_connection_help_ssh_endpoint_gives_ssh_specific_hints():
    help_text = client_module._connection_help(RuntimeError("boom"), Host("prod", "ssh://user@remote"))
    assert "ssh://" in help_text
    # The ssh branch must call out the paramiko-specific gotchas, not socket-mount advice.
    assert "known_hosts" in help_text
    assert "paramiko" in help_text
    # And it must not fall through to the unix-socket guidance.
    assert "docker.sock" not in help_text


def test_connection_help_non_ssh_endpoint_keeps_socket_guidance(monkeypatch):
    # Exercise the in-container branch so the socket-mount guidance is actually emitted — a non-ssh
    # endpoint must get the docker.sock hints, not the ssh-specific ones.
    monkeypatch.setattr(client_module, "in_container", lambda: True)
    monkeypatch.setattr(client_module, "classify_host_kernel", lambda: "linux")
    help_text = client_module._connection_help(RuntimeError("boom"), Host("local", "unix:///var/run/docker.sock"))
    assert "docker.sock" in help_text
    assert "known_hosts" not in help_text


def test_paramiko_is_available_for_ssh_transport():
    # The docker[ssh] extra must keep paramiko installed so ssh:// works via the pure-Python transport.
    import paramiko  # noqa: F401


def test_startup_preflight_scrubs_unresolved_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "${user_config.docker_host}")
    monkeypatch.setattr(client_module, "in_container", lambda: False)
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.info.return_value = {"OperatingSystem": "Ubuntu 22.04", "SecurityOptions": []}
    monkeypatch.setattr(client_module, "_get_client", lambda *a, **k: mock_client)
    client_module.startup_preflight()
    assert "DOCKER_HOST" not in os.environ  # cleared before the daemon connection is attempted


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
        patch("docker_mcp.tools.client.resolve_auto", return_value="unix:///x/docker.sock"),
        patch("docker_mcp.tools.client.docker.DockerClient", return_value=sentinel) as ctor,
    ):
        assert client_module._build_default_client() is sentinel
    ctor.assert_called_once_with(base_url="unix:///x/docker.sock")


def test_build_default_client_falls_back_to_from_env(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    sentinel = MagicMock()
    with (
        patch("docker_mcp.tools.client.resolve_auto", return_value=None),
        patch("docker_mcp.tools.client.docker.from_env", return_value=sentinel) as from_env,
    ):
        assert client_module._build_default_client() is sentinel
    from_env.assert_called_once_with()


# ---------- multi-host: pool routing, per-host TLS, scoped close, host-aware self-guard ----------


def test_get_client_routes_to_named_host(monkeypatch):
    _set_multi(monkeypatch)
    client_module._clients.clear()
    sentinel = MagicMock()
    with patch("docker_mcp.tools.client._build_client", return_value=sentinel) as build:
        assert _get_client("prod") is sentinel
        assert _get_client("prod") is sentinel  # cached: built once
    build.assert_called_once()
    assert build.call_args.args[0].label == "prod"
    assert client_module._clients["prod"] is sentinel
    client_module._clients.clear()


def test_build_client_uses_per_host_cert_dir(monkeypatch):
    _set_multi(monkeypatch)
    host = Host("prod", "tcp://prod:2376", cert_dir="/certs/prod")
    sentinel, tls_obj = MagicMock(), MagicMock()
    with (
        patch("docker_mcp.tools.client._tls_from_dir", return_value=tls_obj) as tls,
        patch("docker_mcp.tools.client.docker.DockerClient", return_value=sentinel) as ctor,
    ):
        assert client_module._build_client(host) is sentinel
    tls.assert_called_once_with("/certs/prod")
    ctor.assert_called_once_with(base_url="tcp://prod:2376", tls=tls_obj)


def test_build_client_falls_back_to_global_tls_env(monkeypatch):
    _set_multi(monkeypatch)
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/global/certs")
    host = Host("prod", "tcp://prod:2376")  # no per-host cert dir
    sentinel, tls_obj = MagicMock(), MagicMock()
    with (
        patch("docker_mcp.tools.client._tls_from_dir", return_value=tls_obj) as tls,
        patch("docker_mcp.tools.client.docker.DockerClient", return_value=sentinel) as ctor,
    ):
        assert client_module._build_client(host) is sentinel
    tls.assert_called_once_with("/global/certs")
    ctor.assert_called_once_with(base_url="tcp://prod:2376", tls=tls_obj)


def test_build_client_plaintext_when_no_tls(monkeypatch):
    _set_multi(monkeypatch)
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    host = Host("prod", "tcp://prod:2376")
    sentinel = MagicMock()
    with patch("docker_mcp.tools.client.docker.DockerClient", return_value=sentinel) as ctor:
        assert client_module._build_client(host) is sentinel
    ctor.assert_called_once_with(base_url="tcp://prod:2376")  # no tls kwarg


def test_close_one_host_leaves_others(monkeypatch):
    _set_multi(monkeypatch)
    local_client, prod_client = MagicMock(), MagicMock()
    client_module._clients.update({"local": local_client, "prod": prod_client})
    assert close(host="prod") is True
    prod_client.close.assert_called_once()
    local_client.close.assert_not_called()
    assert set(client_module._clients) == {"local"}
    client_module._clients.clear()


def test_guard_not_self_inert_on_a_non_self_host(monkeypatch):
    _set_multi(monkeypatch)  # self host is local; targeting prod can't be our own container
    monkeypatch.setattr(client_module, "_self_container_id", "self-full-id")
    monkeypatch.setattr(client_module, "_self_host_label", "local")
    assert client_module.guard_not_self(_fake_container("self-full-id"), host="prod") is None


def test_guard_not_self_enforced_on_the_self_host(monkeypatch):
    _set_multi(monkeypatch)
    monkeypatch.setattr(client_module, "_self_container_id", "self-full-id")
    monkeypatch.setattr(client_module, "_self_host_label", "local")
    with pytest.raises(RuntimeError, match="own container"):
        client_module.guard_not_self(_fake_container("self-full-id", name="mcp"), host="local")

import os
import socket
import threading
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.tools._ssh_proxy import (
    SshDialStdioProxy,
    SshTarget,
    connect_ssh_client,
    paramiko_dial_stdio_factory,
    parse_ssh_url,
    ssh_proxy_for_docker_host,
)

# ---------- parse_ssh_url ----------


def test_parse_ssh_url_basic(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda _path: False)
    target = parse_ssh_url("ssh://bob@example.com:2222")
    assert target == SshTarget(hostname="example.com", port=2222, username="bob", key_filename=None, proxycommand=None)


def test_parse_ssh_url_no_port_or_user(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda _path: False)
    target = parse_ssh_url("ssh://example.com")
    assert target.hostname == "example.com"
    assert target.port is None
    assert target.username is None


def test_parse_ssh_url_requires_hostname(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda _path: False)
    with pytest.raises(ValueError, match="Could not parse a hostname"):
        parse_ssh_url("ssh://")


def test_parse_ssh_url_applies_ssh_config_overrides(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text(
        "Host myhost\n"
        "    HostName 10.0.0.5\n"
        "    Port 2222\n"
        "    User deploy\n"
        "    IdentityFile ~/.ssh/id_deploy\n"
        "    ProxyCommand ssh -W %h:%p bastion\n"
    )
    monkeypatch.setattr("os.path.expanduser", lambda p: str(config) if p == "~/.ssh/config" else p)
    target = parse_ssh_url("ssh://myhost")
    assert target.hostname == "10.0.0.5"
    assert target.port == 2222
    assert target.username == "deploy"
    assert target.key_filename is not None and target.key_filename.endswith("id_deploy")
    assert target.proxycommand == "ssh -W 10.0.0.5:2222 bastion"


def test_parse_ssh_url_resolves_tilde_in_identity_file(tmp_path, monkeypatch):
    # paramiko's SSHConfig.lookup() tokenizes a literal "~" to os.path.expanduser("~") itself, so
    # by the time parse_ssh_url sees the value it should already be a real, usable path with no
    # literal "~" left — this pins that end-to-end behavior against a fake home directory.
    config = tmp_path / "config"
    config.write_text("Host myhost\n    IdentityFile ~/.ssh/id_deploy\n")
    real_expanduser = os.path.expanduser

    def fake_expanduser(p):
        if p == "~/.ssh/config":
            return str(config)
        if p == "~":
            return "/home/testuser"
        return real_expanduser(p)

    monkeypatch.setattr("os.path.expanduser", fake_expanduser)
    target = parse_ssh_url("ssh://myhost")
    assert target.key_filename == "/home/testuser/.ssh/id_deploy"


def test_parse_ssh_url_explicit_values_win_over_config(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text("Host myhost\n    User configuser\n    Port 9999\n")
    monkeypatch.setattr("os.path.expanduser", lambda p: str(config) if p == "~/.ssh/config" else p)
    target = parse_ssh_url("ssh://explicituser@myhost:1234")
    # The URL already specifies user/port, so the config-file values must not override them.
    assert target.username == "explicituser"
    assert target.port == 1234


# ---------- connect_ssh_client / paramiko_dial_stdio_factory ----------


def test_connect_ssh_client_mirrors_docker_py_defaults(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda _path: False)
    fake_client = MagicMock()
    with patch("docker_mcp.tools._ssh_proxy.paramiko.SSHClient", return_value=fake_client) as ssh_client_cls:
        result = connect_ssh_client("ssh://bob@example.com:2222")
    assert result is fake_client
    ssh_client_cls.assert_called_once_with()
    fake_client.load_system_host_keys.assert_called_once()
    fake_client.set_missing_host_key_policy.assert_called_once()
    fake_client.connect.assert_called_once_with(hostname="example.com", port=2222, username="bob")


def test_connect_ssh_client_omits_port_when_unresolved(monkeypatch):
    # No port in the URL and no ~/.ssh/config entry: passing port=None to paramiko would resolve
    # to port 0 (always refused) instead of paramiko's own default of 22, so the kwarg must be
    # left out entirely rather than passed through as None.
    monkeypatch.setattr("os.path.exists", lambda _path: False)
    fake_client = MagicMock()
    with patch("docker_mcp.tools._ssh_proxy.paramiko.SSHClient", return_value=fake_client):
        connect_ssh_client("ssh://bob@example.com")
    kwargs = fake_client.connect.call_args.kwargs
    assert "port" not in kwargs
    assert kwargs["hostname"] == "example.com"
    assert kwargs["username"] == "bob"


def test_connect_ssh_client_passes_key_filename_and_proxycommand(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text("Host myhost\n    IdentityFile ~/.ssh/id_deploy\n    ProxyCommand ssh -W %h:%p bastion\n")
    monkeypatch.setattr("os.path.expanduser", lambda p: str(config) if p == "~/.ssh/config" else p)
    fake_client = MagicMock()
    fake_proxy_command = MagicMock()
    with (
        patch("docker_mcp.tools._ssh_proxy.paramiko.SSHClient", return_value=fake_client),
        patch("docker_mcp.tools._ssh_proxy.paramiko.ProxyCommand", return_value=fake_proxy_command) as proxy_cmd_cls,
    ):
        connect_ssh_client("ssh://myhost")
    kwargs = fake_client.connect.call_args.kwargs
    assert kwargs["key_filename"].endswith("id_deploy")
    assert kwargs["sock"] is fake_proxy_command
    proxy_cmd_cls.assert_called_once_with("ssh -W myhost:22 bastion")


def test_connect_ssh_client_omits_timeout_kwargs_when_unset(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda _path: False)
    fake_client = MagicMock()
    with patch("docker_mcp.tools._ssh_proxy.paramiko.SSHClient", return_value=fake_client):
        connect_ssh_client("ssh://bob@example.com")
    kwargs = fake_client.connect.call_args.kwargs
    assert "timeout" not in kwargs
    assert "banner_timeout" not in kwargs
    assert "auth_timeout" not in kwargs


def test_connect_ssh_client_bounds_connect_banner_and_auth_phases_when_timeout_given(monkeypatch):
    # paramiko tracks the raw socket connect, the banner exchange, and authentication as separate
    # phases with separate (otherwise unbounded) timeouts; all three must be set or a slow host
    # could still hang past run_docker's own deadline in one of the un-bounded phases.
    monkeypatch.setattr("os.path.exists", lambda _path: False)
    fake_client = MagicMock()
    with patch("docker_mcp.tools._ssh_proxy.paramiko.SSHClient", return_value=fake_client):
        connect_ssh_client("ssh://bob@example.com", timeout=5.0)
    kwargs = fake_client.connect.call_args.kwargs
    assert kwargs["timeout"] == 5.0
    assert kwargs["banner_timeout"] == 5.0
    assert kwargs["auth_timeout"] == 5.0


def test_paramiko_dial_stdio_factory_opens_session_and_execs_dial_stdio():
    fake_client = MagicMock()
    fake_transport = MagicMock()
    fake_channel = MagicMock()
    fake_client.get_transport.return_value = fake_transport
    fake_transport.open_session.return_value = fake_channel

    factory = paramiko_dial_stdio_factory(fake_client)
    stream = factory()

    assert stream is fake_channel
    fake_channel.exec_command.assert_called_once_with("docker system dial-stdio")


def test_paramiko_dial_stdio_factory_raises_when_transport_missing():
    fake_client = MagicMock()
    fake_client.get_transport.return_value = None
    factory = paramiko_dial_stdio_factory(fake_client)
    with pytest.raises(RuntimeError, match="not connected"):
        factory()


# ---------- SshDialStdioProxy: accept/pump/teardown with an injected fake channel ----------


def _echo_socketpair_factory(made: list) -> Callable[[], socket.socket]:
    """Channel factory: a socketpair whose far end echoes back everything it receives."""

    def factory():
        local_end, remote_end = socket.socketpair()
        made.append((local_end, remote_end))

        def echo():
            try:
                while True:
                    data = remote_end.recv(4096)
                    if not data:
                        return
                    remote_end.sendall(data)
            except OSError:
                return
            finally:
                remote_end.close()

        threading.Thread(target=echo, daemon=True).start()
        return local_end

    return factory


def test_proxy_start_returns_a_real_listening_port():
    proxy = SshDialStdioProxy(channel_factory=MagicMock())
    port = proxy.start()
    try:
        assert isinstance(port, int)
        assert port > 0
        assert proxy.port == port
        # The port must actually be accepting connections.
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.close()
    finally:
        proxy.stop()


def test_proxy_pumps_data_full_duplex():
    made: list = []
    proxy = SshDialStdioProxy(channel_factory=_echo_socketpair_factory(made))
    port = proxy.start()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.sendall(b"hello world")
        assert sock.recv(4096) == b"hello world"
        sock.close()
    finally:
        proxy.stop()


def test_proxy_handles_concurrent_connections():
    made: list = []
    proxy = SshDialStdioProxy(channel_factory=_echo_socketpair_factory(made))
    port = proxy.start()
    results: dict[int, bytes] = {}
    try:

        def client(i: int) -> None:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            payload = f"msg-{i}".encode()
            sock.sendall(payload)
            results[i] = sock.recv(4096)
            sock.close()

        threads = [threading.Thread(target=client, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
    finally:
        proxy.stop()
    assert len(made) == 8
    for i in range(8):
        assert results[i] == f"msg-{i}".encode()


def test_proxy_stop_does_not_hang_with_open_connections():
    made: list = []
    proxy = SshDialStdioProxy(channel_factory=_echo_socketpair_factory(made))
    port = proxy.start()
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.sendall(b"keep-alive")
    assert sock.recv(4096) == b"keep-alive"
    # Local socket is left open (no explicit close) — stop() must still tear down promptly.
    proxy.stop()  # would hang if teardown didn't force-close blocked recv() calls
    sock.close()


def test_proxy_handles_channel_factory_failure_without_hanging():
    def failing_factory():
        raise RuntimeError("boom")

    proxy = SshDialStdioProxy(channel_factory=failing_factory)
    port = proxy.start()
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    # The remote end is closed because the factory raised; recv should return EOF, not hang.
    assert sock.recv(4096) == b""
    sock.close()
    proxy.stop()


def test_proxy_as_context_manager():
    made: list = []
    with SshDialStdioProxy(channel_factory=_echo_socketpair_factory(made)) as proxy:
        assert proxy.port is not None
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
        sock.sendall(b"ctx")
        assert sock.recv(4096) == b"ctx"
        sock.close()


# ---------- ssh_proxy_for_docker_host ----------


def test_ssh_proxy_for_docker_host_connects_starts_and_tears_down():
    fake_client = MagicMock()
    started = {}

    class FakeProxy:
        def __init__(self, channel_factory):
            self.channel_factory = channel_factory
            self.port = 12345

        def __enter__(self):
            started["entered"] = True
            return self

        def __exit__(self, *exc_info):
            started["exited"] = True

    with (
        patch("docker_mcp.tools._ssh_proxy.connect_ssh_client", return_value=fake_client) as connect,
        patch("docker_mcp.tools._ssh_proxy.SshDialStdioProxy", FakeProxy),
    ):
        with ssh_proxy_for_docker_host("ssh://example.com") as proxy:
            assert proxy.port == 12345
            assert started["entered"] is True
            assert "exited" not in started

    connect.assert_called_once_with("ssh://example.com", timeout=None)
    assert started["exited"] is True
    fake_client.close.assert_called_once()


def test_ssh_proxy_for_docker_host_forwards_timeout_to_connect():
    fake_client = MagicMock()

    class FakeProxy:
        def __init__(self, channel_factory):
            self.channel_factory = channel_factory
            self.port = 12345

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

    with (
        patch("docker_mcp.tools._ssh_proxy.connect_ssh_client", return_value=fake_client) as connect,
        patch("docker_mcp.tools._ssh_proxy.SshDialStdioProxy", FakeProxy),
    ):
        with ssh_proxy_for_docker_host("ssh://example.com", timeout=5.0):
            pass
    connect.assert_called_once_with("ssh://example.com", timeout=5.0)


def test_ssh_proxy_for_docker_host_closes_ssh_client_even_on_error():
    fake_client = MagicMock()
    with (
        patch("docker_mcp.tools._ssh_proxy.connect_ssh_client", return_value=fake_client),
        patch("docker_mcp.tools._ssh_proxy.SshDialStdioProxy") as proxy_cls,
    ):
        proxy_cls.return_value.__enter__.return_value = proxy_cls.return_value
        with pytest.raises(RuntimeError, match="boom"):
            with ssh_proxy_for_docker_host("ssh://example.com"):
                raise RuntimeError("boom")
    fake_client.close.assert_called_once()

import threading
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import DockerException

import docker_mcp.tools.client as client_module
from docker_mcp.tools.client import _get_client, close, df, events, info, login, ping, version


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
    with patch("docker_mcp.tools.client.docker.from_env", side_effect=DockerException("connection refused")):
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

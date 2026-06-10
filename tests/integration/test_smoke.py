# integration tests — require a real Docker daemon at $DOCKER_HOST (or the default unix socket).
# run with: uv run pytest -m integration

from docker_mcp.tools.client import df, info, ping, reconnect, version
from docker_mcp.tools.containers import list_containers
from docker_mcp.tools.images import list_images


def test_ping_real_daemon():
    assert ping() is True


def test_reconnect_from_env_returns_version():
    # No host → rebuild from the environment against the same daemon; validates connectivity.
    payload = reconnect()
    assert "Version" in payload
    assert "ApiVersion" in payload
    # The rebuilt client is live.
    assert ping() is True


def test_version_returns_keys():
    payload = version()
    assert "Version" in payload
    assert "ApiVersion" in payload


def test_info_returns_keys():
    payload = info()
    assert "ID" in payload
    assert "ServerVersion" in payload


def test_df_returns_layers_size():
    payload = df()
    assert "LayersSize" in payload


def test_list_containers_returns_list():
    assert isinstance(list_containers(all=True), list)


def test_list_images_returns_list():
    assert isinstance(list_images(), list)

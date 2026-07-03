# integration tests — require a real Docker daemon at $DOCKER_HOST (or the default unix socket).
# run with: uv run pytest -m integration

from docker_mcp.tools.system import system_df, system_info, system_ping, system_reconnect, system_version
from docker_mcp.tools.containers import container_list
from docker_mcp.tools.images import image_list


def test_ping_real_daemon():
    assert system_ping() is True


def test_reconnect_from_env_returns_version():
    # No host → rebuild from the environment against the same daemon; validates connectivity.
    payload = system_reconnect()
    assert "Version" in payload
    assert "ApiVersion" in payload
    # The rebuilt client is live.
    assert system_ping() is True


def test_version_returns_keys():
    payload = system_version()
    assert "Version" in payload
    assert "ApiVersion" in payload


def test_info_returns_keys():
    payload = system_info()
    assert "ID" in payload
    assert "ServerVersion" in payload


def test_df_returns_layers_size():
    payload = system_df()
    assert "LayersSize" in payload


def test_list_containers_returns_list():
    assert isinstance(container_list(all=True), list)


def test_list_images_returns_list():
    assert isinstance(image_list(), list)

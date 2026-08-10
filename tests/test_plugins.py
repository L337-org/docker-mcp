from unittest.mock import MagicMock, patch

from docker_mcp.tools.plugins import (
    plugin_configure,
    plugin_create,
    plugin_disable,
    plugin_enable,
    plugin_inspect,
    plugin_install,
    plugin_list,
    plugin_remove,
    plugin_upgrade,
)


def _patch():
    return patch("docker_mcp.tools.plugins._get_client")


def test_plugin_create():
    plugin = MagicMock()
    plugin.attrs = {"Id": "p1", "Enabled": False}
    with _patch() as mock_client:
        mock_client.return_value.plugins.create.return_value = plugin
        result = plugin_create("me/myplugin:latest", "/srv/plugin-data", gzip=True)
    assert result == {"Id": "p1", "Enabled": False}
    mock_client.return_value.plugins.create.assert_called_once_with("me/myplugin:latest", "/srv/plugin-data", gzip=True)


def test_plugin_create_expands_a_user_relative_data_dir():
    # The dir is read on this host, so it goes through host_read_path (expanduser + the
    # in-container "that path isn't a bind mount" guard) rather than reaching the SDK raw.
    plugin = MagicMock()
    plugin.attrs = {"Id": "p1"}
    with _patch() as mock_client:
        mock_client.return_value.plugins.create.return_value = plugin
        plugin_create("me/myplugin", "~/plugin-data")
    passed = mock_client.return_value.plugins.create.call_args.args[1]
    assert not passed.startswith("~")
    assert passed.endswith("plugin-data")


def test_plugin_inspect():
    plugin = MagicMock()
    plugin.attrs = {"Id": "p1"}
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_inspect("myplugin") == {"Id": "p1"}


def test_plugin_install():
    plugin = MagicMock()
    plugin.attrs = {"Id": "p1"}
    with _patch() as mock_client:
        mock_client.return_value.plugins.install.return_value = plugin
        result = plugin_install("vieux/sshfs", local_name="sshfs")
    assert result == {"Id": "p1"}
    mock_client.return_value.plugins.install.assert_called_once_with("vieux/sshfs", local_name="sshfs")


def test_plugin_list():
    plugin = MagicMock()
    plugin.attrs = {"Id": "p1"}
    with _patch() as mock_client:
        mock_client.return_value.plugins.list.return_value = [plugin]
        assert plugin_list() == [{"Id": "p1"}]


def test_plugin_configure():
    plugin = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_configure("myplugin", {"DEBUG": "1"}) is True
    plugin.configure.assert_called_once_with({"DEBUG": "1"})


def test_plugin_disable():
    plugin = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_disable("myplugin", force=True) is True
    plugin.disable.assert_called_once_with(force=True)


def test_plugin_enable():
    plugin = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_enable("myplugin", timeout_seconds=30) is True
    plugin.enable.assert_called_once_with(timeout=30)


def test_plugin_remove():
    plugin = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_remove("myplugin", force=True) is True
    plugin.remove.assert_called_once_with(force=True)


def test_upgrade_plugin_default_remote():
    plugin = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_upgrade("myplugin") is True
    plugin.upgrade.assert_called_once_with()


def test_upgrade_plugin_with_remote():
    plugin = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_upgrade("myplugin", remote="vieux/sshfs:v2") is True
    plugin.upgrade.assert_called_once_with("vieux/sshfs:v2")

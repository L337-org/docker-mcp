from unittest.mock import MagicMock, patch

from docker_mcp.tools.plugins import (
    plugin_configure,
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


_PRIVILEGES = [
    {"Name": "network", "Description": "", "Value": ["host"]},
    {"Name": "capabilities", "Description": "", "Value": ["CAP_SYS_ADMIN"]},
]


def test_plugin_install_dry_run_returns_the_requested_privileges_and_installs_nothing():
    # The daemon grants whatever this call returns without prompting, so the dry run has to ask the
    # very call PluginCollection.install makes - and must not reach the install at all.
    with _patch() as mock_client:
        mock_client.return_value.api.plugin_privileges.return_value = _PRIVILEGES
        result = plugin_install("vieux/sshfs:latest", dry_run=True)
    assert result == {"remote": "vieux/sshfs:latest", "privileges": _PRIVILEGES}
    mock_client.return_value.api.plugin_privileges.assert_called_once_with("vieux/sshfs:latest")
    mock_client.return_value.plugins.install.assert_not_called()
    mock_client.return_value.api.pull_plugin.assert_not_called()


def test_plugin_upgrade_dry_run_asks_about_the_reference_the_upgrade_would_pull():
    # `Plugin.upgrade` defaults its reference to the plugin's own name and grants exactly what
    # `plugin_privileges` returns for it, so a dry run with no `remote` must ask about that name.
    plugin = MagicMock()
    plugin.name = "vieux/sshfs:latest"
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        mock_client.return_value.api.plugin_privileges.return_value = _PRIVILEGES
        assert plugin_upgrade("sshfs", dry_run=True) == {"remote": "vieux/sshfs:latest", "privileges": _PRIVILEGES}
        assert plugin_upgrade("sshfs", remote="vieux/sshfs:next", dry_run=True) == {
            "remote": "vieux/sshfs:next",
            "privileges": _PRIVILEGES,
        }
    assert [c.args for c in mock_client.return_value.api.plugin_privileges.call_args_list] == [
        ("vieux/sshfs:latest",),
        ("vieux/sshfs:next",),
    ]
    plugin.upgrade.assert_not_called()


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

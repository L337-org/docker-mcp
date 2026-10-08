from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.exceptions import RemoteFailureError

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


def _upgrade_plugin(records=(), *, consumed=None):
    """A plugin whose `upgrade` is a real generator, as docker-py's is.

    A MagicMock method runs its body the moment it is called, which is exactly what hid the original
    bug: `Plugin.upgrade` does nothing until its stream is read, and a mock cannot show that.
    """
    plugin = MagicMock()
    calls = []

    def upgrade(*args):
        calls.append(args)
        for record in records:
            if consumed is not None:
                consumed.append(record)
            yield record

    plugin.upgrade.side_effect = upgrade
    return plugin, calls


def test_plugin_upgrade_reads_the_whole_stream_so_the_upgrade_actually_runs():
    consumed = []
    plugin, calls = _upgrade_plugin([{"status": "Pulling"}, {"status": "Done"}], consumed=consumed)
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_upgrade("myplugin") is True
    assert calls == [()]
    assert consumed == [{"status": "Pulling"}, {"status": "Done"}], "the upgrade stream was not read to the end"


def test_plugin_upgrade_passes_the_remote_through():
    plugin, calls = _upgrade_plugin([{"status": "Done"}])
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        assert plugin_upgrade("myplugin", remote="vieux/sshfs:v2") is True
    assert calls == [("vieux/sshfs:v2",)]


def test_plugin_upgrade_raises_an_error_the_daemon_reports_inside_the_stream():
    # The HTTP status is already 200 by the time the stream starts, so a failed pull arrives as an
    # `error` record - returning True after it would report a failed upgrade as a success.
    plugin, _ = _upgrade_plugin([{"status": "Pulling"}, {"error": "manifest unknown", "errorDetail": {}}])
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        with pytest.raises(RemoteFailureError, match=r"upgrading plugin 'myplugin' to 'x:v9': manifest unknown"):
            plugin_upgrade("myplugin", remote="x:v9")


def test_plugin_upgrade_gives_up_on_a_stream_that_runs_past_its_deadline(monkeypatch):
    from docker_mcp.tools import plugins

    clock = iter([0.0, 1.0, 10_000.0])
    monkeypatch.setattr(plugins.time, "monotonic", lambda: next(clock))
    plugin, _ = _upgrade_plugin([{"status": "Pulling"}, {"status": "Pulling"}, {"status": "Done"}])
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        with pytest.raises(RemoteFailureError, match="still streaming progress"):
            plugin_upgrade("myplugin")

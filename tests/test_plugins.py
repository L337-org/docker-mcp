import re
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import APIError

from docker_mcp.exceptions import RemoteFailureError, ToolInputError

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


def _plugin_data_dir(tmp_path):
    """Build a minimal plugin data directory: the `config.json` and `rootfs` the daemon requires.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        Path: the directory to pass as `plugin_data_dir`.
    """
    (tmp_path / "config.json").write_text('{"description": "test"}')
    (tmp_path / "rootfs" / "bin").mkdir(parents=True)
    (tmp_path / "rootfs" / "bin" / "entry").write_text("#!/bin/sh\n")
    return tmp_path


def test_plugin_create_uploads_the_directory_and_returns_the_new_plugins_attrs(tmp_path):
    data_dir = _plugin_data_dir(tmp_path)
    plugin = MagicMock()
    plugin.attrs = {"Id": "p1", "Name": "myorg/myplugin:v1", "Enabled": False}
    with _patch() as mock_client:
        mock_client.return_value.plugins.create.return_value = plugin
        result = plugin_create("myorg/myplugin:v1", str(data_dir))
    assert result == {"Id": "p1", "Name": "myorg/myplugin:v1", "Enabled": False}
    mock_client.return_value.plugins.create.assert_called_once_with("myorg/myplugin:v1", str(data_dir), gzip=False)


def test_plugin_create_maps_compress_onto_docker_pys_gzip(tmp_path):
    # The parameter follows the CLI's `--compress`; docker-py spells the same thing `gzip`, so a
    # rename on either side has to keep meeting here.
    data_dir = _plugin_data_dir(tmp_path)
    with _patch() as mock_client:
        plugin_create("myorg/myplugin:v1", str(data_dir), compress=True)
    assert mock_client.return_value.plugins.create.call_args.kwargs == {"gzip": True}


def test_plugin_create_refuses_a_directory_that_does_not_exist(tmp_path):
    with _patch() as mock_client:
        with pytest.raises(ToolInputError, match="No such plugin data directory"):
            plugin_create("myorg/myplugin:v1", str(tmp_path / "absent"))
    mock_client.return_value.plugins.create.assert_not_called()


def test_plugin_create_refuses_a_file_given_where_a_directory_is_required(tmp_path):
    not_a_dir = tmp_path / "config.json"
    not_a_dir.write_text("{}")
    with _patch() as mock_client:
        with pytest.raises(ToolInputError, match="No such plugin data directory"):
            plugin_create("myorg/myplugin:v1", str(not_a_dir))
    mock_client.return_value.plugins.create.assert_not_called()


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        (("config.json",), "it has no rootfs."),
        (("rootfs",), "it has no config.json."),
        ((), "it has no config.json and no rootfs."),
    ],
)
def test_plugin_create_names_what_the_data_directory_is_missing(tmp_path, present, expected):
    # docker-py tars whatever it finds and lets the daemon reject it, so without this guard a wrong
    # directory becomes an opaque daemon error instead of naming the entry that is absent.
    # Matched against the *variable* clause: the message's fixed second sentence names both entries,
    # so a `match=` on a bare name would pass however wrongly `missing` was computed.
    for entry in present:
        if entry == "rootfs":
            (tmp_path / "rootfs").mkdir()
        else:
            (tmp_path / entry).write_text("{}")
    with _patch() as mock_client:
        with pytest.raises(ToolInputError, match=re.escape(expected)):
            plugin_create("myorg/myplugin:v1", str(tmp_path))
    mock_client.return_value.plugins.create.assert_not_called()


def test_plugin_create_refuses_a_rootfs_that_is_a_regular_file(tmp_path):
    # `rootfs` has to be a directory; a file of that name would pass a bare existence check.
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "rootfs").write_text("not a directory")
    with _patch() as mock_client:
        with pytest.raises(ToolInputError, match=re.escape("it has no rootfs.")):
            plugin_create("myorg/myplugin:v1", str(tmp_path))
    mock_client.return_value.plugins.create.assert_not_called()


def test_plugin_create_turns_an_unreadable_tree_into_an_actionable_error(tmp_path):
    # Building the tar reads the tree as this process's user, and neither `walk`'s `PermissionError`
    # nor `create_archive`'s bare `OSError` is a `DockerException`, so `_LIBRARY_FAILURES` would
    # translate neither and the caller would lose the message naming the file.
    data_dir = _plugin_data_dir(tmp_path)
    with _patch() as mock_client:
        mock_client.return_value.plugins.create.side_effect = PermissionError(13, "Permission denied")
        with pytest.raises(ToolInputError, match="Could not read the plugin data directory"):
            plugin_create("myorg/myplugin:v1", str(data_dir))


def test_plugin_create_lets_a_daemon_failure_travel_as_itself(tmp_path):
    # `APIError` is an `OSError` as well as a `DockerException`, so the guard above must not catch
    # it and relabel a daemon refusal as a bad argument.
    data_dir = _plugin_data_dir(tmp_path)
    with _patch() as mock_client:
        mock_client.return_value.plugins.create.side_effect = APIError("plugin already exists")
        with pytest.raises(APIError):
            plugin_create("myorg/myplugin:v1", str(data_dir))


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
    plugin.enabled = False
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


def test_plugin_upgrade_refuses_an_enabled_plugin_with_the_step_to_take():
    # docker-py's own check raises `errors.DockerError`, which docker-py 7.2.0 does not define, so left
    # to it the caller would get an AttributeError with the explanation withheld.
    plugin, calls = _upgrade_plugin([{"status": "Done"}])
    plugin.enabled = True
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        with pytest.raises(ToolInputError, match="plugin_disable"):
            plugin_upgrade("myplugin")
    assert calls == [], "nothing is sent for an enabled plugin"


def test_plugin_upgrade_dry_run_works_on_an_enabled_plugin():
    # Previewing changes nothing, so it has no reason to require the plugin disabled first.
    plugin, _ = _upgrade_plugin()
    plugin.enabled = True
    plugin.name = "vieux/sshfs:latest"
    with _patch() as mock_client:
        mock_client.return_value.plugins.get.return_value = plugin
        mock_client.return_value.api.plugin_privileges.return_value = []
        assert plugin_upgrade("sshfs", dry_run=True) == {"remote": "vieux/sshfs:latest", "privileges": []}

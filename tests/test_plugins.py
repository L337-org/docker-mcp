import pytest
import urllib3

from unittest.mock import MagicMock, patch

from docker_mcp.tools.plugins import (
    plugin_configure,
    plugin_create,
    plugin_disable,
    plugin_enable,
    plugin_inspect,
    plugin_install,
    plugin_list,
    plugin_privileges,
    plugin_push,
    plugin_remove,
    plugin_upgrade,
)


def _patch():
    return patch("docker_mcp.tools.plugins._get_client")


def _push_api(records, *, spec_missing=()):
    """
    A stand-in for APIClient wired for plugin_push: `records` is what the stream yields.

    `spec_missing` deletes private helpers from the mock so `hasattr` reports them absent, letting
    the version-guard branch be exercised without a real old docker-py.
    """
    api = MagicMock()
    api._url.side_effect = lambda fmt, *args: "http://d/v1.45" + fmt.format(*args)
    api._stream_helper.return_value = iter(records)
    for attr in spec_missing:
        delattr(api, attr)
    return api


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


def test_plugin_push_targets_the_push_endpoint_not_docker_pys_broken_pull_url():
    # The whole reason this tool bypasses APIClient.push_plugin: that method POSTs to
    # /plugins/{name}/pull, a route the Engine does not define. Pin the correct path.
    api = _push_api([{"status": "Preparing"}, {"status": "Pushed"}])
    with _patch() as mock_client:
        mock_client.return_value.api = api
        result = plugin_push("me/myplugin:latest")
    assert api._url.call_args.args[0] == "/plugins/{0}/push"
    assert api._post.call_args.args[0].endswith("/plugins/me/myplugin:latest/push")
    # A chunked body has to be read incrementally, so the POST must be streamed.
    assert api._post.call_args.kwargs["stream"] is True
    assert result == {
        "name": "me/myplugin:latest",
        "progress": [{"status": "Preparing"}, {"status": "Pushed"}],
        "truncated": False,
        "error": None,
    }


def test_plugin_push_surfaces_a_registry_error_without_raising():
    # A rejected push arrives as a record in the stream, not an exception — the tool must not
    # report success, but it also must not raise (mirroring image_push).
    api = _push_api([{"status": "Preparing"}, {"error": "denied: requested access to the resource is denied"}])
    with _patch() as mock_client:
        mock_client.return_value.api = api
        result = plugin_push("me/myplugin")
    assert result["error"] == "denied: requested access to the resource is denied"


def test_plugin_push_caps_a_chatty_progress_stream():
    api = _push_api([{"status": f"layer {i}"} for i in range(500)])
    with _patch() as mock_client:
        mock_client.return_value.api = api
        result = plugin_push("me/myplugin")
    assert result["truncated"] is True
    assert len(result["progress"]) == 200


def test_plugin_push_sends_registry_auth_when_credentials_are_cached():
    api = _push_api([{"status": "Pushed"}])
    with _patch() as mock_client, patch("docker_mcp.tools.plugins.auth.get_config_header", return_value="dG9rZW4="):
        mock_client.return_value.api = api
        plugin_push("me/myplugin")
    assert api._post.call_args.kwargs["headers"] == {"X-Registry-Auth": "dG9rZW4="}


def test_plugin_push_omits_the_auth_header_when_no_credentials_are_cached():
    api = _push_api([{"status": "Pushed"}])
    with _patch() as mock_client, patch("docker_mcp.tools.plugins.auth.get_config_header", return_value=None):
        mock_client.return_value.api = api
        plugin_push("me/myplugin")
    assert api._post.call_args.kwargs["headers"] == {}


def test_plugin_push_returns_what_it_collected_when_the_watchdog_cuts_the_stream():
    # The timeout contract: closing the stream mid-push must yield the records gathered so far,
    # not an exception. Cancelling shuts the socket down, which surfaces to the reader as a
    # ProtocolError — the shape CancellableStream converts to StopIteration. Iterating the raw
    # _stream_helper generator (as this did before) let that error escape and lost the progress,
    # and closing the response didn't bound the wait at all: Response.close() leaves a read already
    # blocked on the socket blocked, then trips over its own None `fp` in _close_conn afterwards.
    def _cut_off():
        yield {"status": "Preparing"}
        yield {"status": "Pushing"}
        raise urllib3.exceptions.ProtocolError("Connection broken")

    api = _push_api([])
    api._stream_helper.return_value = _cut_off()
    with _patch() as mock_client:
        mock_client.return_value.api = api
        result = plugin_push("me/myplugin", timeout_seconds=0.05)
    assert result["progress"] == [{"status": "Preparing"}, {"status": "Pushing"}]
    assert result["error"] is None


def test_plugin_push_raises_a_clear_error_if_docker_py_drops_the_internals_it_uses():
    # The tool depends on undocumented docker-py helpers; a refactor upstream must surface as an
    # actionable message naming the escape hatch, not an AttributeError from inside the call.
    api = _push_api([], spec_missing=("_stream_helper",))
    with _patch() as mock_client:
        mock_client.return_value.api = api
        with pytest.raises(RuntimeError, match="_stream_helper"):
            plugin_push("me/myplugin")


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


def test_plugin_privileges_returns_the_requested_privileges():
    privileges = [
        {"Name": "network", "Description": "", "Value": ["host"]},
        {"Name": "capabilities", "Description": "", "Value": ["CAP_SYS_ADMIN"]},
    ]
    with _patch() as mock_client:
        mock_client.return_value.api.plugin_privileges.return_value = privileges
        assert plugin_privileges("vieux/sshfs:latest") == privileges
    mock_client.return_value.api.plugin_privileges.assert_called_once_with("vieux/sshfs:latest")


def test_plugin_privileges_installs_nothing():
    with _patch() as mock_client:
        mock_client.return_value.api.plugin_privileges.return_value = []
        assert plugin_privileges("vieux/sshfs") == []
    mock_client.return_value.plugins.install.assert_not_called()
    mock_client.return_value.api.pull_plugin.assert_not_called()


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

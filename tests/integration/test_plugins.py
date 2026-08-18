"""Integration coverage for `plugin_privileges`, which needs a reachable registry.

The call resolves a *remote* plugin through the daemon, so it exercises registry reachability and
auth rather than any local state. It installs nothing, which is the property worth pinning: the
whole point of the tool is to inspect what a plugin would be granted before `plugin_install` grants
it non-interactively.
"""

from docker.errors import DockerException

from docker_mcp.tools.plugins import plugin_list, plugin_privileges

from tests.integration.conftest import fail_unless_environmental_error

# A long-standing official plugin, used read-only here purely as a reference that resolves.
_REMOTE = "vieux/sshfs:latest"


def test_plugin_privileges_lists_requested_privileges_without_installing():
    installed_before = {plugin["Name"] for plugin in plugin_list()}

    try:
        privileges = plugin_privileges(_REMOTE)
    except (DockerException, RuntimeError) as exc:
        fail_unless_environmental_error(exc, what=f"plugin_privileges({_REMOTE})")
        return

    assert isinstance(privileges, list)
    # sshfs needs host access to be useful at all, so an empty list would mean we read the wrong
    # thing rather than that the plugin is unusually well behaved.
    assert privileges, "expected sshfs to request at least one privilege"
    for privilege in privileges:
        assert "Name" in privilege

    assert {plugin["Name"] for plugin in plugin_list()} == installed_before

"""Integration coverage for the two `plugins` paths that need a daemon rather than a mock.

`plugin_install(dry_run=True)` resolves a *remote* plugin through the daemon, so it exercises
registry reachability and auth rather than any local state. It installs nothing, which is the
property worth pinning: the whole point is to see what a plugin would be granted before
`plugin_install` grants it non-interactively.

`plugin_create` is the opposite path - it uploads a directory from this host and touches no
registry - so it is the one plugin tool that can be exercised end to end offline.
"""

import json

import pytest
from docker.errors import DockerException

from docker_mcp.tools.plugins import plugin_create, plugin_inspect, plugin_install, plugin_list, plugin_remove

from tests.integration.conftest import fail_unless_environmental_error

# A long-standing official plugin, used read-only here purely as a reference that resolves.
_REMOTE = "vieux/sshfs:latest"


def test_plugin_install_dry_run_lists_requested_privileges_without_installing():
    installed_before = {plugin["Name"] for plugin in plugin_list()}

    try:
        privileges = plugin_install(_REMOTE, dry_run=True)["privileges"]
    except (DockerException, RuntimeError) as exc:
        fail_unless_environmental_error(exc, what=f"plugin_install({_REMOTE}, dry_run=True)")
        return

    assert isinstance(privileges, list)
    # sshfs needs host access to be useful at all, so an empty list would mean we read the wrong
    # thing rather than that the plugin is unusually well behaved.
    assert privileges, "expected sshfs to request at least one privilege"
    for privilege in privileges:
        assert "Name" in privilege

    assert {plugin["Name"] for plugin in plugin_list()} == installed_before


# Named for this suite so a leaked plugin is attributable, and tagged so the reference is explicit
# rather than relying on the daemon's `:latest` defaulting.
_LOCAL = "docker-mcp-test/plugin-create:integration"

# The minimum `config.json` the daemon accepts, in the lower-case spelling Docker's plugin-config
# documentation uses (Go's json decoding matches field names case-insensitively, so this is the same
# document as the PascalCase `Plugin.Config` the Engine API spec defines). Every key here is on that
# definition's `required` list except the ones the daemon defaults; `entrypoint` names a binary that
# need not exist, because nothing executes until `plugin_enable`, which this test never calls.
_CONFIG = {
    "description": "docker-mcp integration test plugin",
    "documentation": "https://github.com/L337-org/docker-mcp",
    "entrypoint": ["/docker-mcp-test"],
    "interface": {"types": ["docker.volumedriver/1.0"], "socket": "docker-mcp-test.sock"},
    "network": {"type": "host"},
    "linux": {"capabilities": []},
}


@pytest.fixture
def plugin_data_dir(tmp_path):
    """A plugin data directory: the `config.json` manifest beside the `rootfs` it describes.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        Path: the directory to hand to `plugin_create`.
    """
    (tmp_path / "config.json").write_text(json.dumps(_CONFIG))
    (tmp_path / "rootfs" / "bin").mkdir(parents=True)
    (tmp_path / "rootfs" / "bin" / "docker-mcp-test").write_text("#!/bin/sh\nexit 0\n")
    return tmp_path


def test_plugin_create_installs_a_local_plugin_disabled(plugin_data_dir):
    # The property worth pinning is the one a caller acts on: the plugin arrives disabled rather than
    # running with whatever its config asked for. (That no registry is consulted is not asserted
    # here - nothing in this process can observe it.)
    if any(plugin["Name"] == _LOCAL for plugin in plugin_list()):
        plugin_remove(_LOCAL, force=True)

    try:
        created = plugin_create(_LOCAL, str(plugin_data_dir))
    except DockerException as exc:
        fail_unless_environmental_error(exc, what=f"plugin_create({_LOCAL})")
        return

    try:
        assert created["Name"] == _LOCAL
        assert created["Enabled"] is False
        assert created["Config"]["Interface"]["Socket"] == "docker-mcp-test.sock"
        # The daemon's own view, not just what the create call handed back.
        assert plugin_inspect(_LOCAL)["Enabled"] is False
        assert _LOCAL in {plugin["Name"] for plugin in plugin_list()}
    finally:
        plugin_remove(_LOCAL, force=True)

    assert _LOCAL not in {plugin["Name"] for plugin in plugin_list()}

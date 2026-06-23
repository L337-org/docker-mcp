# library of mcp tools relating to plugin management

from docker_mcp.server import tool
from docker_mcp.tools.client import _get_client


@tool()
def get_plugin(name: str, host: str | None = None) -> dict:
    """
    Get an installed plugin by name.

    args: name - The plugin name
    returns: dict - The plugin's attrs
    """
    return _get_client(host).plugins.get(name).attrs


@tool()
def install_plugin(remote_name: str, local_name: str | None = None, host: str | None = None) -> dict:
    """
    Install a plugin from a remote reference.

    args:
        remote_name - The remote plugin reference
        local_name - Optional local name for the plugin
    returns: dict - The installed plugin's attrs
    """
    return _get_client(host).plugins.install(remote_name, local_name=local_name).attrs


@tool()
def list_plugins(host: str | None = None) -> list:
    """
    List installed plugins.

    returns: list - A list of plugin attrs dicts
    """
    return [p.attrs for p in _get_client(host).plugins.list()]


@tool()
def configure_plugin(name: str, options: dict, host: str | None = None) -> bool:
    """
    Configure a plugin's settings.

    args:
        name - The plugin name
        options - Key/value plugin settings
    returns: bool - True after configuration
    """
    _get_client(host).plugins.get(name).configure(options)
    return True


@tool()
def disable_plugin(name: str, force: bool = False, host: str | None = None) -> bool:
    """
    Disable a plugin.

    args:
        name - The plugin name
        force - Force disable
    returns: bool - True after the plugin is disabled
    """
    _get_client(host).plugins.get(name).disable(force=force)
    return True


@tool()
def enable_plugin(name: str, timeout: int = 0, host: str | None = None) -> bool:
    """
    Enable a plugin.

    args:
        name - The plugin name
        timeout - Timeout in seconds (0 means no timeout)
    returns: bool - True after the plugin is enabled
    """
    _get_client(host).plugins.get(name).enable(timeout=timeout)
    return True


@tool()
def push_plugin(name: str, host: str | None = None) -> dict:
    """
    Push a plugin to a remote registry.

    args: name - The plugin name
    returns: dict - Push status returned by the daemon
    """
    return _get_client(host).plugins.get(name).push()


@tool()
def remove_plugin(name: str, force: bool = False, host: str | None = None) -> bool:
    """
    Remove a plugin.

    args:
        name - The plugin name
        force - Force removal even if the plugin is enabled
    returns: bool - True after removal
    """
    _get_client(host).plugins.get(name).remove(force=force)
    return True


@tool()
def upgrade_plugin(name: str, remote: str | None = None, host: str | None = None) -> bool:
    """
    Upgrade a plugin.

    args:
        name - The plugin name
        remote - Remote reference to upgrade from (defaults to current name)
    returns: bool - True after the upgrade completes
    """
    plugin = _get_client(host).plugins.get(name)
    if remote is None:
        plugin.upgrade()
    else:
        plugin.upgrade(remote)
    return True

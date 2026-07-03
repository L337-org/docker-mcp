# library of mcp tools relating to plugin management

from docker_mcp.server import tool
from docker_mcp.tools.system import _get_client


@tool()
def plugin_inspect(name: str, host: str | None = None) -> dict:
    """
    Get an installed plugin by name.

    args: name - The plugin name
    returns: dict - The plugin's attrs
    """
    return _get_client(host).plugins.get(name).attrs


@tool()
def plugin_install(remote_name: str, local_name: str | None = None, host: str | None = None) -> dict:
    """
    Install a plugin from Docker Hub.

    `remote_name` is a Docker Hub reference in `author/name:tag` form, e.g.
    `vieux/sshfs:latest`. The daemon handles permission grants non-interactively.
    After installation use `plugin_inspect` to confirm the plugin's enabled state, then call
    `plugin_enable` to activate it if needed, and optionally `plugin_configure` first if
    it requires settings. Use `plugin_list` to list all plugins, or `plugin_remove` to
    uninstall.

    args:
        remote_name - Docker Hub plugin reference, e.g. "vieux/sshfs:latest"
        local_name - Alias to refer to the plugin locally; defaults to remote_name
    returns: dict - The installed plugin's attrs
    """
    return _get_client(host).plugins.install(remote_name, local_name=local_name).attrs


@tool()
def plugin_list(host: str | None = None) -> list:
    """
    List installed plugins.

    returns: list - A list of plugin attrs dicts
    """
    return [p.attrs for p in _get_client(host).plugins.list()]


@tool()
def plugin_configure(name: str, options: dict, host: str | None = None) -> bool:
    """
    Set runtime configuration options on an installed plugin.

    Use `plugin_inspect` first to see which keys the plugin exposes under `Settings.Env`; pass
    those same keys as a plain dict, e.g. `{"DEBUG": "1", "SOCKET": "/run/x.sock"}`. The
    plugin must be disabled before reconfiguring — call `plugin_disable` first if it is
    currently active, then `plugin_enable` afterwards to apply the new settings.

    args:
        name - Plugin name or id (e.g. "vieux/sshfs:latest")
        options - Key/value settings to apply, matching the plugin's declared env keys
    returns: bool - True after configuration
    """
    _get_client(host).plugins.get(name).configure(options)
    return True


@tool()
def plugin_disable(name: str, force: bool = False, host: str | None = None) -> bool:
    """
    Disable a plugin so it stops intercepting Docker API calls; the plugin remains installed.

    A disabled plugin cannot be used by new containers but existing containers that already
    have it attached are unaffected. Use `force=True` to disable even if active containers
    are still using it — this may cause those containers to lose access to plugin-provided
    resources (e.g. a volume driver). Re-enable with `plugin_enable`.

    args:
        name - Plugin name or id
        force - Disable even if active containers are using the plugin (may disrupt them)
    returns: bool - True after the plugin is disabled
    """
    _get_client(host).plugins.get(name).disable(force=force)
    return True


@tool()
def plugin_enable(name: str, timeout: int = 0, host: str | None = None) -> bool:
    """
    Activate an installed plugin so Docker routes relevant API calls through it.

    Activates a plugin that is currently disabled — either freshly installed or previously
    disabled via `plugin_disable`. If the plugin exposes configuration (check via
    `plugin_inspect`), call `plugin_configure` while it is still disabled before enabling it.
    `timeout` controls how long Docker waits for the plugin process to become healthy;
    0 means wait indefinitely.

    args:
        name - Plugin name or id to enable
        timeout - Seconds to wait for the plugin to become healthy (0 = no timeout)
    returns: bool - True after the plugin is enabled
    """
    _get_client(host).plugins.get(name).enable(timeout=timeout)
    return True


@tool()
def plugin_push(name: str, host: str | None = None) -> dict:
    """
    Push a locally built or pulled plugin image to a remote registry.

    The daemon must already be authenticated with the target registry — call `login` first if
    needed. `name` must include the registry host for any registry other than Docker Hub,
    e.g. "registry.example.com/myplugin:1.0". The plugin must already exist locally
    (installed via `plugin_install` or built externally with `docker plugin create`).

    args: name - Plugin name including tag, e.g. "myorg/myplugin:latest"
    returns: dict - Push progress/status events returned by the daemon
    """
    return _get_client(host).plugins.get(name).push()


@tool()
def plugin_remove(name: str, force: bool = False, host: str | None = None) -> bool:
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
def plugin_upgrade(name: str, remote: str | None = None, host: str | None = None) -> bool:
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

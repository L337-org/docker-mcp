"""Tools for Engine plugins: installing and upgrading them, and controlling whether they are enabled."""

# library of mcp tools relating to plugin management

from docker_mcp.server import tool
from docker_mcp.tools.system import _get_client


@tool()
def plugin_inspect(name: str, host: str | None = None) -> dict:  # noqa: DOC101,DOC103
    """
    Return the full attrs for a single installed plugin.

    Use this to check a plugin's `Enabled` state before calling `plugin_enable` /
    `plugin_disable`, or to read the config keys it exposes under `Settings.Env` before
    calling `plugin_configure`. For the set of all installed plugins use `plugin_list`.

    Args:
        name: Plugin name, e.g. "vieux/sshfs:latest"

    Returns:
        dict: The plugin's full document (Id, Name, Enabled, Settings, Config)
    """
    return _get_client(host).plugins.get(name).attrs


@tool()
def plugin_install(  # noqa: DOC101,DOC103
    remote: str,
    local_name: str | None = None,
    dry_run: bool = False,
    host: str | None = None,
) -> dict:
    """
    Install a plugin from a registry, or with `dry_run=True` only show the host privileges it would get.

    The daemon grants every privilege a plugin requests without prompting, so for anything not
    already trusted call this with `dry_run=True` first: plugins routinely ask for host mounts,
    devices and elevated capabilities, and a granted privilege is host-level access, not
    container-scoped. A dry run reads the plugin from its registry and installs nothing; for a plugin
    already installed, read `Config` from `plugin_inspect` instead. Credentials come from
    `system_login`, or from `~/.docker/config.json` if the host ran `docker login`. After installing,
    `plugin_inspect` shows whether it is enabled; `plugin_configure` any settings it declares, then
    `plugin_enable` it. `plugin_list` lists every plugin and `plugin_remove` uninstalls.

    Args:
        remote: Registry plugin reference, `author/name:tag`, e.g. "vieux/sshfs:latest"; the `:latest` tag is optional
            and is the default if omitted
        local_name: Alias to refer to the plugin locally; defaults to remote
        dry_run: Return the privileges the plugin requests and install nothing

    Returns:
        dict: The installed plugin's full document ({"Id", "Name", "Enabled", "Settings", "Config"}); with `dry_run`,
            {"remote", "privileges"} where `privileges` holds one {"Name", "Description", "Value"} per requested
            privilege, e.g. Name "mount" with Value ["/data"], and is empty if the plugin requests none
    """
    client = _get_client(host)
    if dry_run:
        # The high-level PluginCollection exposes no privileges call; this is the documented low-level
        # one, and the same call `PluginCollection.install` makes before granting the result.
        return {"remote": remote, "privileges": client.api.plugin_privileges(remote)}
    return client.plugins.install(remote, local_name=local_name).attrs


@tool()
def plugin_list(host: str | None = None) -> list:  # noqa: DOC101,DOC103
    """
    List installed engine plugins with their full attrs.

    Covers managed engine plugins (volume/network/logging drivers installed via `plugin_install`)
    - not docker CLI plugins such as compose, buildx, or scout. Use it to find exact plugin names
    for `plugin_inspect`/`plugin_enable`/`plugin_disable`/`plugin_remove`; the `Enabled` key shows
    each plugin's state.

    Returns:
        list: One full document per installed plugin (Id, Name, Enabled, Settings, Config)
    """
    return [p.attrs for p in _get_client(host).plugins.list()]


@tool()
def plugin_configure(name: str, options: dict, host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Set runtime configuration options on an installed plugin.

    Use `plugin_inspect` first to see which keys the plugin exposes under `Settings.Env`; pass
    those same keys as a plain dict, e.g. `{"DEBUG": "1", "SOCKET": "/run/x.sock"}`. The
    plugin must be disabled before reconfiguring - call `plugin_disable` first if it is
    currently active, then `plugin_enable` afterwards to apply the new settings.

    Args:
        name: Plugin name, e.g. "vieux/sshfs:latest"
        options: Key/value settings to apply, matching the plugin's declared env keys

    Returns:
        bool: True after configuration
    """
    _get_client(host).plugins.get(name).configure(options)
    return True


@tool()
def plugin_disable(name: str, force: bool = False, host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Disable a plugin so it stops intercepting Docker API calls; the plugin remains installed.

    A disabled plugin cannot be used by new containers but existing containers that already
    have it attached are unaffected. Use `force=True` to disable even if active containers
    are still using it - this may cause those containers to lose access to plugin-provided
    resources (e.g. a volume driver). Re-enable with `plugin_enable`.

    Args:
        force: Disable even if active containers are using the plugin (may disrupt them)

    Returns:
        bool: True after the plugin is disabled
    """
    _get_client(host).plugins.get(name).disable(force=force)
    return True


@tool()
def plugin_enable(name: str, timeout_seconds: int = 0, host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Activate an installed plugin so Docker routes relevant API calls through it.

    Activates a plugin that is currently disabled - either freshly installed or previously
    disabled via `plugin_disable`. If the plugin exposes configuration (check via
    `plugin_inspect`), call `plugin_configure` while it is still disabled before enabling it.
    `timeout_seconds` controls how long Docker waits for the plugin process to become healthy;
    0 means wait indefinitely.

    Args:
        name: The plugin name to enable
        timeout_seconds: Seconds to wait for the plugin to become healthy (0 = no timeout)

    Returns:
        bool: True after the plugin is enabled
    """
    _get_client(host).plugins.get(name).enable(timeout=timeout_seconds)
    return True


@tool()
def plugin_remove(name: str, force: bool = False, host: str | None = None) -> bool:  # noqa: DOC101,DOC103
    """
    Uninstall an engine plugin from the daemon.

    Permanent removal - to deactivate but keep a plugin installed use `plugin_disable` instead. An
    enabled plugin must be disabled first unless `force=True`. Plugin names come from
    `plugin_list`.

    Args:
        name: The plugin name (e.g. "vieux/sshfs:latest")
        force: Remove even if the plugin is enabled

    Returns:
        bool: True after removal
    """
    _get_client(host).plugins.get(name).remove(force=force)
    return True


@tool()
def plugin_upgrade(  # noqa: DOC101,DOC103
    name: str,
    remote: str | None = None,
    dry_run: bool = False,
    host: str | None = None,
) -> bool | dict:
    """
    Upgrade an installed plugin to a newer version, or with `dry_run=True` only show the privileges it would get.

    The daemon grants whatever privileges the new version requests without prompting, exactly as
    `plugin_install` does, so a dry run first shows them and changes nothing. The plugin must be
    disabled first - call `plugin_disable` before this, then `plugin_enable` afterwards to bring it
    back up. `remote` lets you upgrade to a different reference (e.g. a newer tag) than the plugin's
    current name; omit it to re-pull the same reference. Existing settings and volumes created by the
    plugin persist across the upgrade.

    Args:
        name: The plugin name to upgrade
        remote: Reference to upgrade to, e.g. "vieux/sshfs:next" (default: same as name)
        dry_run: Return the privileges the upgrade would grant and change nothing

    Returns:
        bool | dict: True after the upgrade completes; with `dry_run`, {"remote", "privileges"} where `privileges`
            holds one {"Name", "Description", "Value"} per requested privilege
    """
    client = _get_client(host)
    plugin = client.plugins.get(name)
    if dry_run:
        # `Plugin.upgrade` defaults the reference to the plugin's own name and grants exactly what
        # `plugin_privileges` returns for it, so the preview asks about that same reference.
        target = remote if remote is not None else plugin.name
        return {"remote": target, "privileges": client.api.plugin_privileges(target)}
    if remote is None:
        plugin.upgrade()
    else:
        plugin.upgrade(remote)
    return True

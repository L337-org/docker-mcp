# library of mcp tools relating to plugin management

import threading

from docker import auth
from docker.types.daemon import CancellableStream

from docker_mcp.exceptions import CapabilityError
from docker_mcp.server import tool
from docker_mcp.tools._utils import close_stream_quietly, host_read_path
from docker_mcp.tools.system import _get_client

# Cap on progress records collected from a push stream, so a chatty registry can't grow the reply
# without bound. Reported back as "truncated" rather than silently dropped.
_MAX_PUSH_PROGRESS = 200


@tool()
def plugin_create(name: str, plugin_data_dir: str, gzip: bool = False, host: str | None = None) -> dict:
    """
    Build a plugin from a local plugin data directory and install it under `name`.

    The counterpart to `plugin_install`, which pulls an already-published plugin from a registry:
    use this only for a plugin rootfs you built yourself, and `plugin_install` for anything on a
    registry. `plugin_data_dir` is read on the machine running this server (not on the daemon
    host), must already contain a `config.json` manifest and a `rootfs` directory, and is tarred
    client-side and posted to the daemon - in a container it must be a bind mount or the path
    resolves to nothing. The new plugin is created **disabled**: call `plugin_configure` for any
    settings it declares, then `plugin_enable` to activate it. Raises if the directory is missing
    or lacks `config.json`/`rootfs`, or if `name` is already installed (remove it first with
    `plugin_remove`). Unlike the other create tools, this stamps no provenance labels - the Engine
    API's plugin-create call accepts none.

    args:
        name - Local name for the plugin, `author/name:tag`; the `:latest` tag is optional and
            is the default if omitted
        plugin_data_dir - Path on this server's filesystem to the plugin data directory
            (containing `config.json` and `rootfs`)
        gzip - Compress the uploaded directory with gzip (default False)
    returns: dict - The created plugin's attrs ({"Id", "Name", "Enabled", "Settings", "Config"})
    """
    path = host_read_path(plugin_data_dir)
    return _get_client(host).plugins.create(name, str(path), gzip=gzip).attrs


@tool()
def plugin_inspect(name: str, host: str | None = None) -> dict:
    """
    Return the full attrs for a single installed plugin.

    Use this to check a plugin's `Enabled` state before calling `plugin_enable` /
    `plugin_disable`, or to read the config keys it exposes under `Settings.Env` before
    calling `plugin_configure`. For the set of all installed plugins use `plugin_list`.

    args: name - Plugin name, e.g. "vieux/sshfs:latest"
    returns: dict - The plugin's attrs, including `Enabled` and `Settings`
    """
    return _get_client(host).plugins.get(name).attrs


@tool()
def plugin_install(remote: str, local_name: str | None = None, host: str | None = None) -> dict:
    """
    Install a plugin from Docker Hub.

    `remote` is a Docker Hub reference in `author/name:tag` form, e.g.
    `vieux/sshfs:latest`. The daemon handles permission grants non-interactively - call
    `plugin_privileges` first to see what host access the plugin is asking for.
    After installation use `plugin_inspect` to confirm the plugin's enabled state, then call
    `plugin_enable` to activate it if needed, and optionally `plugin_configure` first if
    it requires settings. Use `plugin_list` to list all plugins, or `plugin_remove` to
    uninstall.

    args:
        remote - Docker Hub plugin reference, e.g. "vieux/sshfs:latest"
        local_name - Alias to refer to the plugin locally; defaults to remote
    returns: dict - The installed plugin's attrs ({"Id", "Name", "Enabled", "Settings", "Config"})
    """
    return _get_client(host).plugins.install(remote, local_name=local_name).attrs


@tool()
def plugin_privileges(remote: str, host: str | None = None) -> list:
    """
    Ask the registry which host privileges a not-yet-installed plugin demands.

    The review step before `plugin_install`, which grants these privileges non-interactively (the
    daemon never prompts) - so this is the only chance to see what a plugin wants before it has it.
    Worth checking for anything not already trusted: plugins routinely request host mounts, devices,
    and elevated capabilities, and a granted privilege is host-level access, not container-scoped.
    Reads the *remote* plugin from its registry and installs nothing; for the privileges of a plugin
    already installed, read `Config` from `plugin_inspect` instead. Credentials come from
    `system_login`, or from `~/.docker/config.json` if the host ran `docker login`. Raises if the
    reference cannot be resolved in the registry.

    args: remote - Registry plugin reference, `author/name:tag`; the `:latest` tag is optional and
        is the default if omitted
    returns: list - One dict per requested privilege ({"Name", "Description", "Value"}), e.g. Name
        "mount" with Value ["/data"], or "capabilities" with Value ["CAP_SYS_ADMIN"]; empty if the
        plugin requests none
    """
    # The high-level PluginCollection exposes no privileges call; this is the documented low-level one.
    return _get_client(host).api.plugin_privileges(remote)


@tool()
def plugin_push(name: str, timeout_seconds: float = 300.0, host: str | None = None) -> dict:
    """
    Push an installed plugin to its registry.

    The write-side counterpart to `plugin_install` (which pulls) and the publish step after
    `plugin_create` builds a plugin locally: `name` must already be the registry-qualified name the
    plugin is installed under, since - unlike `image_push` - there is no plugin equivalent of
    `image_tag` to rename it first, so create it under the target name. The plugin does not need to
    be enabled. Credentials come from `system_login`, or from `~/.docker/config.json` if the host
    ran `docker login`. Does NOT raise when the registry rejects the push: an authentication or
    quota failure arrives as a final progress record and is surfaced as the `error` key, so check
    that key rather than assuming success. Raises `RuntimeError` if the installed docker-py is too
    old to expose the internals below, and `docker.errors.APIError` if the plugin isn't installed.

    Bypasses docker-py's `Plugin.push()`/`APIClient.push_plugin()`, which cannot work: both POST to
    `/plugins/{name}/pull`, a route the Engine does not define (push is `/plugins/{name}/push`), so
    they 404 against any daemon. Bug present since the method was written in 2017 and still in
    docker-py `main`; it survives because upstream has no test covering it. This calls the correct
    endpoint through docker-py's private request helpers, in the manner of `system_logout`'s
    `api._auth_configs` reach-in, and fails loudly if those internals change shape.

    Caveat for `ssh://` daemons: docker-py can't cancel an SSH stream, so the `timeout_seconds`
    watchdog can't interrupt a push that stalls with the connection still open - the same limitation
    `container_logs` carries in follow mode. The call still returns normally once the registry
    answers or the stream ends.

    args:
        name - Installed plugin name to push, `[registry/]author/name:tag`; `:latest` if the tag is
            omitted. A bare `author/name` pushes to Docker Hub
        timeout_seconds - Max wall-clock seconds to wait on the push stream before returning what
            was collected (default 300); raise it for a large plugin over a slow link
    returns: dict - {"name", "progress": [<decoded status dicts>], "truncated": bool, "error": str
        or None} - `error` is non-None only when the registry reported a failure
    """
    api = _get_client(host).api
    # docker-py exposes no working public path here (see docstring), so we drive its private request
    # helpers directly. Resolved via getattr - like system_logout's _auth_configs reach-in - so the
    # absence of any of them surfaces as the explicit message below rather than an AttributeError from
    # inside the call (and so a type checker isn't asked to vouch for a private attribute).
    build_url = getattr(api, "_url", None)
    post = getattr(api, "_post", None)
    raise_for_status = getattr(api, "_raise_for_status", None)
    stream_helper = getattr(api, "_stream_helper", None)
    if build_url is None or post is None or raise_for_status is None or stream_helper is None:
        missing = sorted(
            attr
            for attr, fn in (
                ("_url", build_url),
                ("_post", post),
                ("_raise_for_status", raise_for_status),
                ("_stream_helper", stream_helper),
            )
            if fn is None
        )
        raise CapabilityError(
            f"the installed docker-py no longer exposes {', '.join(missing)} on APIClient, which "
            "plugin_push needs to reach POST /plugins/{name}/push; push the plugin with "
            "`docker plugin push` until this tool is updated"
        )
    registry, _ = auth.resolve_repository_name(name)
    header = auth.get_config_header(api, registry)
    headers = {"X-Registry-Auth": header} if header else {}
    # stream=True is required: _stream_helper reads the chunked body incrementally. (push_plugin omits
    # it - a second latent bug there, harmless only because the wrong URL never returns a stream.)
    response = post(build_url("/plugins/{0}/push", name), headers=headers, stream=True)
    raise_for_status(response)
    progress: list = []
    truncated = False
    # Wrapped in CancellableStream (public, and what docker-py hands back from its own streaming
    # calls) because closing the *response* does not bound anything: Response.close() sets
    # http.client's `fp` to None without interrupting a read already blocked on the socket, so the
    # call still hangs until the registry answers and then dies in `_close_conn` with an
    # AttributeError on that None - losing the collected records. CancellableStream.close() shuts
    # the socket down, which does unblock the read, and turns the resulting ProtocolError/OSError
    # into StopIteration so the loop ends and the partial progress below is returned as documented.
    # This is also how container_logs/system_events get their bound - docker-py wraps those for us.
    stream = CancellableStream(stream_helper(response, decode=True), response)
    timer = threading.Timer(timeout_seconds, lambda: close_stream_quietly(stream))
    timer.start()
    try:
        for record in stream:
            progress.append(record)
            if len(progress) >= _MAX_PUSH_PROGRESS:
                truncated = True
                break
    finally:
        timer.cancel()
        # Both: the stream to tear the socket down on the truncation break, the response to release
        # the pooled connection. Either may already be shut; close_stream_quietly swallows that.
        close_stream_quietly(stream)
        close_stream_quietly(response)
    error = next((r.get("error") for r in reversed(progress) if isinstance(r, dict) and r.get("error")), None)
    return {"name": name, "progress": progress, "truncated": truncated, "error": error}


@tool()
def plugin_list(host: str | None = None) -> list:
    """
    List installed engine plugins with their full attrs.

    Covers managed engine plugins (volume/network/logging drivers installed via `plugin_install`)
    - not docker CLI plugins such as compose, buildx, or scout. Use it to find exact plugin names
    for `plugin_inspect`/`plugin_enable`/`plugin_disable`/`plugin_remove`; the `Enabled` key shows
    each plugin's state.

    returns: list - One attrs dict per installed plugin (Id, Name, Enabled, Settings, Config)
    """
    return [p.attrs for p in _get_client(host).plugins.list()]


@tool()
def plugin_configure(name: str, options: dict, host: str | None = None) -> bool:
    """
    Set runtime configuration options on an installed plugin.

    Use `plugin_inspect` first to see which keys the plugin exposes under `Settings.Env`; pass
    those same keys as a plain dict, e.g. `{"DEBUG": "1", "SOCKET": "/run/x.sock"}`. The
    plugin must be disabled before reconfiguring - call `plugin_disable` first if it is
    currently active, then `plugin_enable` afterwards to apply the new settings.

    args:
        name - Plugin name, e.g. "vieux/sshfs:latest"
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
    are still using it - this may cause those containers to lose access to plugin-provided
    resources (e.g. a volume driver). Re-enable with `plugin_enable`.

    args:
        name - The plugin name
        force - Disable even if active containers are using the plugin (may disrupt them)
    returns: bool - True after the plugin is disabled
    """
    _get_client(host).plugins.get(name).disable(force=force)
    return True


@tool()
def plugin_enable(name: str, timeout_seconds: int = 0, host: str | None = None) -> bool:
    """
    Activate an installed plugin so Docker routes relevant API calls through it.

    Activates a plugin that is currently disabled - either freshly installed or previously
    disabled via `plugin_disable`. If the plugin exposes configuration (check via
    `plugin_inspect`), call `plugin_configure` while it is still disabled before enabling it.
    `timeout_seconds` controls how long Docker waits for the plugin process to become healthy;
    0 means wait indefinitely.

    args:
        name - The plugin name to enable
        timeout_seconds - Seconds to wait for the plugin to become healthy (0 = no timeout)
    returns: bool - True after the plugin is enabled
    """
    _get_client(host).plugins.get(name).enable(timeout=timeout_seconds)
    return True


@tool()
def plugin_remove(name: str, force: bool = False, host: str | None = None) -> bool:
    """
    Uninstall an engine plugin from the daemon.

    Permanent removal - to deactivate but keep a plugin installed use `plugin_disable` instead. An
    enabled plugin must be disabled first unless `force=True`. Plugin names come from
    `plugin_list`.

    args:
        name - The plugin name (e.g. "vieux/sshfs:latest")
        force - Remove even if the plugin is enabled (default False)
    returns: bool - True after removal
    """
    _get_client(host).plugins.get(name).remove(force=force)
    return True


@tool()
def plugin_upgrade(name: str, remote: str | None = None, host: str | None = None) -> bool:
    """
    Upgrade an installed plugin to a newer version.

    The plugin must be disabled first - call `plugin_disable` before this, then
    `plugin_enable` afterwards to bring it back up. `remote` lets you upgrade to a
    different reference (e.g. a newer tag) than the plugin's current name; omit it to
    re-pull the same reference. Existing settings and volumes created by the plugin
    persist across the upgrade.

    args:
        name - The plugin name to upgrade
        remote - Reference to upgrade to, e.g. "vieux/sshfs:next" (default: same as name)
    returns: bool - True after the upgrade completes
    """
    plugin = _get_client(host).plugins.get(name)
    if remote is None:
        plugin.upgrade()
    else:
        plugin.upgrade(remote)
    return True

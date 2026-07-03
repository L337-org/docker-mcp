# library of mcp tools relating to image management

from docker_mcp.server import tool
from docker_mcp.tools._utils import MAX_PAYLOAD_BYTES, drop_none, host_read_path, join_bounded, stream_to_file
from docker_mcp.tools.system import _get_client


@tool()
def image_build(
    path: str | None = None,
    tag: str | None = None,
    quiet: bool = False,
    nocache: bool = False,
    rm: bool = True,
    pull: bool = False,
    forcerm: bool = False,
    dockerfile: str | None = None,
    buildargs: dict | None = None,
    container_limits: dict | None = None,
    shmsize: int | None = None,
    labels: dict | None = None,
    cache_from: list | None = None,
    target: str | None = None,
    network_mode: str | None = None,
    squash: bool = False,
    extra_hosts: dict | None = None,
    platform: str | None = None,
    isolation: str | None = None,
    use_config_proxy: bool = True,
    host: str | None = None,
) -> dict:
    """
    Build an image from a Dockerfile using the daemon's classic builder.

    Use this for simple single-platform builds from a local context. For multi-platform
    builds, BuildKit cache export/import, or advanced build features prefer `buildx_build`.
    `path` must be a directory accessible on the host running this server (it is the build
    context sent to the daemon). `dockerfile` is relative to `path`; omit to use the
    default `Dockerfile`.

    args:
        path - Build context directory path on the server host
        tag - Name and optional tag in "name:tag" format to apply to the built image
        quiet - Suppress verbose build output (final image id still returned)
        nocache - Ignore the layer cache and rebuild all layers
        rm - Remove intermediate containers on success (default True)
        pull - Always pull a newer version of each FROM base image before building
        forcerm - Remove intermediate containers even on build failure
        dockerfile - Dockerfile filename relative to path (default: "Dockerfile")
        buildargs - Build-time variables passed as `--build-arg`; dict of str→str
        container_limits - Resource limits for the build container, e.g. {"memory": 134217728}
        shmsize - Size of /dev/shm in bytes for build steps that need shared memory
        labels - Labels to apply to the resulting image; dict of str→str
        cache_from - List of image references to use as layer cache sources
        target - Stop at this named build stage (multi-stage Dockerfiles)
        network_mode - Network mode for RUN instructions during build (e.g. "host", "none")
        squash - Squash all new layers into one (experimental; requires daemon flag)
        extra_hosts - Additional /etc/hosts entries during build; dict of hostname→ip
        platform - Target platform, e.g. "linux/amd64" (single platform only; use buildx for multi)
        isolation - Windows isolation technology ("default", "process", "hyperv")
        use_config_proxy - Forward proxy env vars from Docker client config to build
    returns: dict - The built image's attrs
    """
    kwargs: dict = {
        "quiet": quiet,
        "nocache": nocache,
        "rm": rm,
        "pull": pull,
        "forcerm": forcerm,
        "squash": squash,
        "use_config_proxy": use_config_proxy,
        **drop_none(
            path=path,
            tag=tag,
            dockerfile=dockerfile,
            buildargs=buildargs,
            container_limits=container_limits,
            shmsize=shmsize,
            labels=labels,
            cache_from=cache_from,
            target=target,
            network_mode=network_mode,
            extra_hosts=extra_hosts,
            platform=platform,
            isolation=isolation,
        ),
    }
    image, _logs = _get_client(host).images.build(**kwargs)
    return image.attrs


@tool()
def image_inspect(name: str, host: str | None = None) -> dict:
    """
    Get an image by name or id.

    args: name - The image name or id
    returns: dict - The image's attrs
    """
    return _get_client(host).images.get(name).attrs


@tool()
def image_registry_data(name: str, auth_config: dict | None = None, host: str | None = None) -> dict:
    """
    Get registry data for an image without pulling it.

    Security: `auth_config` carries registry credentials, which many MCP clients log verbatim. Prefer
    `docker login` on the host so the `docker` module reuses credentials cached in
    `~/.docker/config.json`, and leave `auth_config` unset.

    args:
        name - Image reference
        auth_config - Optional registry authentication config
    returns: dict - Registry data attrs
    """
    return _get_client(host).images.get_registry_data(name, auth_config=auth_config).attrs


@tool()
def image_list(
    name: str | None = None, all: bool = False, filters: dict | None = None, host: str | None = None
) -> list:
    """
    List images on the server.

    args:
        name - Only show images of this repository
        all - Show intermediate image layers
        filters - Filter by attributes (label, dangling, before, since, etc.)
    returns: list - A list of image attrs dicts
    """
    return [i.attrs for i in _get_client(host).images.list(name=name, all=all, filters=filters)]


@tool()
def image_pull(
    repository: str,
    tag: str | None = None,
    all_tags: bool = False,
    platform: str | None = None,
    host: str | None = None,
) -> dict | list:
    """
    Pull an image of the given name.

    args:
        repository - The image repository
        tag - The image tag (ignored when all_tags=True)
        all_tags - Pull all tags from the repository
        platform - Platform in os/arch format
    returns: dict | list - Pulled image attrs (or a list of attrs if all_tags=True)
    """
    result = _get_client(host).images.pull(repository, tag=tag, all_tags=all_tags, platform=platform)
    if isinstance(result, list):
        return [i.attrs for i in result]
    return result.attrs


@tool()
def image_push(
    repository: str, tag: str | None = None, auth_config: dict | None = None, host: str | None = None
) -> str:
    """
    Push an image or repository to a registry.

    Security: `auth_config` carries registry credentials, which many MCP clients log verbatim. Prefer
    `docker login` on the host so the `docker` module reuses credentials cached in
    `~/.docker/config.json`, and leave `auth_config` unset.

    args:
        repository - The image repository
        tag - The tag to push
        auth_config - Optional registry authentication config
    returns: str - Push output as a string
    """
    output = _get_client(host).images.push(repository, tag=tag, stream=False, auth_config=auth_config, decode=False)
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


@tool()
def image_remove(image: str, force: bool = False, noprune: bool = False, host: str | None = None) -> bool:
    """
    Remove a local image by name or id.

    Fails without `force` if the image is tagged by multiple names (untag first with
    `image_tag`) or if stopped containers reference it. Running containers always block
    removal regardless of `force`. `noprune` keeps untagged parent layers that would
    otherwise be removed as a side-effect; leave False unless you need to preserve
    the parent layers for another purpose.

    args:
        image - Image name (with optional tag/digest) or id to remove
        force - Remove even if referenced by stopped containers or multiple tags
        noprune - Do not delete untagged intermediate parent layers
    returns: bool - True after removal completes
    """
    _get_client(host).images.remove(image=image, force=force, noprune=noprune)
    return True


@tool()
def image_search(term: str, limit: int | None = None, host: str | None = None) -> list:
    """
    Search Docker Hub for public images matching a term.

    Searches Docker Hub only — not GHCR, ECR, or other registries. For listing tags on a
    specific image from any OCI registry use `registry_tags` instead. Each result dict
    includes `name`, `description`, `star_count`, `is_official`, and `is_automated`.

    args:
        term - Search keyword, e.g. "nginx" or "python"
        limit - Maximum number of results to return (Docker Hub default is 25)
    returns: list - List of matching image dicts from Docker Hub
    """
    return _get_client(host).images.search(term=term, limit=limit)


@tool()
def image_prune(filters: dict | None = None, host: str | None = None) -> dict:
    """
    Remove unused local images to reclaim disk space.

    Without filters removes only "dangling" images — untagged layers not referenced by any
    tag or container. To remove all images not used by any container (including tagged ones)
    pass `filters={"dangling": False}`. Valid filter keys: `dangling` (bool as string
    "true"/"false"), `until` (RFC3339 timestamp or duration like "24h"), `label`
    (key or key=value). Use `df` first to see how much space is reclaimable.

    args: filters - Narrow which images to remove; omit to remove dangling images only
    returns: dict - {"ImagesDeleted": [...], "SpaceReclaimed": <bytes>}
    """
    return _get_client(host).images.prune(filters=filters)


@tool()
def image_load(data: bytes, host: str | None = None) -> list:
    """
    Load an image from a tarball produced by image_save.

    For a tarball already on the host running this server, prefer `image_load_from_file` — it streams
    from disk instead of carrying the (base64-encoded) bytes through the MCP protocol.

    args: data - Tarball contents
    returns: list - A list of loaded image attrs dicts
    """
    return [i.attrs for i in _get_client(host).images.load(data)]


@tool()
def image_load_from_file(file_path: str, host: str | None = None) -> list:
    """
    Load an image from a tar archive on the host running this MCP server.

    Streams the file straight to the daemon, so it handles arbitrarily large images that would be
    impractical to pass in band via `image_load`. The path is read by the server's user; `~` is expanded.

    args: file_path - Path to a tarball produced by `docker save` / `image_save_to_file`
    returns: list - A list of loaded image attrs dicts
    """
    path = host_read_path(file_path)
    with path.open("rb") as handle:
        return [i.attrs for i in _get_client(host).images.load(handle)]


@tool()
def image_save(name: str, named: bool = False, max_bytes: int = MAX_PAYLOAD_BYTES, host: str | None = None) -> bytes:
    """
    Save an image as a tar archive, returned in band.

    For anything but a small image prefer `image_save_to_file`, which streams to a host path; the
    in-band bytes here are capped (default 32 MiB) because MCP base64-encodes them into the agent's context.

    args:
        name - Image name or id
        named - Whether to keep the image name when saving
        max_bytes - Abort with ValueError if the tarball exceeds this many bytes (defaults to 32 MiB)
    returns: bytes - The tarball contents
    """
    image = _get_client(host).images.get(name)
    return join_bounded(image.save(named=named), max_bytes, f"save of image {name}")


@tool()
def image_save_to_file(
    name: str, dest_path: str, named: bool = False, overwrite: bool = False, host: str | None = None
) -> dict:
    """
    Save an image as a tar archive written to a file on the host running this MCP server.

    Streams the archive straight to disk (no in-band byte cap), so it handles large images. The file
    is written by the server's user; `~` is expanded and an existing file is refused unless `overwrite=True`.

    args:
        name - Image name or id
        dest_path - Destination path on the server host for the tarball
        named - Whether to keep the image name when saving
        overwrite - Replace dest_path if it already exists (default False)
    returns: dict - {"path": <resolved path>, "bytes_written": int}
    """
    image = _get_client(host).images.get(name)
    path, written = stream_to_file(image.save(named=named), dest_path, overwrite=overwrite)
    return {"path": str(path), "bytes_written": written}


@tool()
def image_tag(name: str, repository: str, tag: str | None = None, force: bool = False, host: str | None = None) -> bool:
    """
    Tag an image into a repository.

    args:
        name - The source image name or id
        repository - Target repository name
        tag - Optional tag for the new image
        force - Force the tag
    returns: bool - True if the image was tagged
    """
    image = _get_client(host).images.get(name)
    return image.tag(repository, tag=tag, force=force)


@tool()
def image_history(name: str, host: str | None = None) -> list:
    """
    Return the layer history of an image.

    Useful for auditing what commands built each layer and diagnosing image size. Each entry
    includes `Id` (layer digest or "<missing>" for imported layers), `Created` (unix
    timestamp), `CreatedBy` (the Dockerfile command that produced the layer, e.g. a RUN or
    COPY), `Size` (bytes added by that layer), and `Comment`. For full image metadata use
    `image_inspect` instead.

    args: name - Image name (with optional tag/digest) or id
    returns: list - Layer history entries, newest first
    """
    return _get_client(host).images.get(name).history()

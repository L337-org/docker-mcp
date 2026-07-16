# library of mcp tools relating to image management

from typing import cast

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
        labels - Labels to set on the resulting image (dict of str→str)
        cache_from - List of image references to use as layer cache sources
        target - Stop at this named build stage (multi-stage Dockerfiles)
        network_mode - Network mode for RUN instructions during build (e.g. "host", "none")
        squash - Squash all new layers into one (experimental; requires daemon flag)
        extra_hosts - Additional /etc/hosts entries during build; dict of hostname→ip
        platform - Target platform, e.g. "linux/amd64" (single platform only; use buildx for multi)
        isolation - Windows isolation technology ("default", "process", "hyperv")
        use_config_proxy - Forward proxy env vars from Docker client config to build
    returns: dict - The built image's full inspect payload (as `docker inspect`)
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
def image_inspect(id_or_name: str, host: str | None = None) -> dict:
    """
    Return the full inspect detail for a single local image.

    Includes config (env, entrypoint, exposed ports), size, layer digests (`RootFS.Layers`),
    and all tags/digests referencing it (`RepoTags`/`RepoDigests`). For a quick overview of
    many images use `image_list` instead. For the per-layer build history (which command
    produced each layer) use `image_history`. Only inspects images already present locally —
    for a remote image's manifest without pulling it use `image_registry_data` or
    `registry_manifest`.

    args: id_or_name - Image name (with optional tag/digest) or id
    returns: dict - Full image inspect attrs (equivalent to `docker inspect` on an image)
    """
    return _get_client(host).images.get(id_or_name).attrs


@tool()
def image_registry_data(repository: str, auth_config: dict | None = None, host: str | None = None) -> dict:
    """
    Get registry data for an image without pulling it, via the daemon's distribution endpoint.

    Uses the daemon (and its cached credentials) to resolve the remote descriptor and platform
    list. For direct registry access without a daemon use `registry_manifest`.

    Security: `auth_config` carries registry credentials, which many MCP clients log verbatim. Prefer
    `docker login` on the host so the `docker` module reuses credentials cached in
    `~/.docker/config.json`, and leave `auth_config` unset.

    args:
        repository - Image reference
        auth_config - Optional registry authentication config
    returns: dict - {"Descriptor", "Platforms"} — the OCI descriptor and the platforms available
        for the reference
    """
    return _get_client(host).images.get_registry_data(repository, auth_config=auth_config).attrs


@tool()
def image_list(
    repository: str | None = None, all: bool = False, filters: dict | None = None, host: str | None = None
) -> list:
    """
    List images in the daemon's local store.

    Local only — for a registry's contents use `registry_tags` / `hub_tags`, and `image_search`
    to find images on Docker Hub. Dangling (untagged) build leftovers show with
    filters={"dangling": True}.

    args:
        repository - Only show images of this repository
        all - Show intermediate image layers
        filters - Filter by attributes (label, dangling, before, since, etc.)
    returns: list - One full inspect payload (as `docker inspect`) per image
    """
    return [i.attrs for i in _get_client(host).images.list(name=repository, all=all, filters=filters)]


@tool()
def image_pull(
    repository: str,
    tag: str | None = None,
    all_tags: bool = False,
    platform: str | None = None,
    host: str | None = None,
) -> dict | list:
    """
    Pull an image from a registry to the daemon's local store.

    Private repositories need credentials — `system_login` (or `docker login` on the host) first.
    Use `image_load` for tarballs, and `registry_manifest` / `image_registry_data` to inspect a
    remote image without pulling it.

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

    The local image must already bear the target name — `image_tag` it with the
    registry-qualified `repository[:tag]` first; a bare name pushes to Docker Hub. Private
    registries need credentials (`system_login`, or `docker login` on the host).

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
def image_remove(id_or_name: str, force: bool = False, noprune: bool = False, host: str | None = None) -> bool:
    """
    Remove a local image by name or id.

    Fails without `force` if the image is tagged by multiple names (untag first with
    `image_tag`) or if stopped containers reference it. Running containers always block
    removal regardless of `force`. `noprune` keeps untagged parent layers that would
    otherwise be removed as a side-effect; leave False unless you need to preserve
    the parent layers for another purpose.

    args:
        id_or_name - Image name (with optional tag/digest) or id to remove
        force - Remove even if referenced by stopped containers or multiple tags
        noprune - Do not delete untagged intermediate parent layers
    returns: bool - True after removal completes
    """
    _get_client(host).images.remove(image=id_or_name, force=force, noprune=noprune)
    return True


@tool()
def image_search(term: str, limit: int | None = None, host: str | None = None) -> list:
    """
    Search Docker Hub for public images matching a term.

    Searches Docker Hub only — not GHCR, ECR, or other registries. For listing tags on a
    specific image from any OCI registry use `registry_tags` instead.

    args:
        term - Search keyword, e.g. "nginx" or "python"
        limit - Maximum number of results to return (Docker Hub default is 25)
    returns: list - Result dicts: {"name", "description", "star_count", "is_official",
        "is_automated"}
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
    (key or key=value). Use `system_df` first to see how much space is reclaimable.

    args: filters - Narrow which images to remove; omit to remove dangling images only
    returns: dict - {"ImagesDeleted": [...], "SpaceReclaimed": <bytes>}
    """
    return _get_client(host).images.prune(filters=filters)


@tool()
def image_load(data: bytes | None = None, from_file: str | None = None, host: str | None = None) -> list:
    """
    Load an image from a tarball produced by `image_save`, from in-band bytes or a file on the server host.

    Counterpart of `image_save`; when the image lives in a registry, `image_pull` is the normal
    route. Pass exactly one of `data` (tarball bytes in band) or `from_file` (a path on the server host,
    streamed straight to the daemon — preferred for anything but small images, since in-band bytes are
    base64-encoded by MCP). `from_file` is read by the server's user; `~` is expanded.

    args:
        data - Tarball contents; exactly one of data/from_file
        from_file - Path to a tarball produced by `docker save` / `image_save`; exactly one of data/from_file
    returns: list - One full inspect payload per loaded image
    """
    if (data is None) == (from_file is None):
        raise ValueError("Pass exactly one of `data` (in-band tarball bytes) or `from_file` (a server-host path).")
    if data is not None:
        return [i.attrs for i in _get_client(host).images.load(data)]
    path = host_read_path(cast(str, from_file))
    with path.open("rb") as handle:
        return [i.attrs for i in _get_client(host).images.load(handle)]


@tool()
def image_save(
    id_or_name: str,
    dest_path: str | None = None,
    named: bool = False,
    overwrite: bool = False,
    max_bytes: int = MAX_PAYLOAD_BYTES,
    host: str | None = None,
) -> bytes | dict:
    """
    Save an image as a tar archive: to a file on the server host, or in band.

    The archive keeps layers, tags, and metadata so `image_load` can restore it — different from
    `container_export`, which flattens one container's filesystem. With `dest_path` the archive
    streams straight to disk (no byte cap), so it handles large images — the file is written by
    the server's user, `~` is expanded, and an existing file is refused unless
    `overwrite=True`. Without `dest_path` the tar bytes are returned in band, capped at `max_bytes`
    (default 32 MiB) because MCP base64-encodes them — a fallback for when no writable host path
    exists (e.g. a containerized server without a bind mount).

    args:
        id_or_name - Image name or id
        dest_path - Destination path on the server host; omit to return the bytes in band
        named - Whether to retain repository/tag names in the saved archive
        overwrite - Replace dest_path if it already exists (default False)
        max_bytes - In-band mode: abort with ValueError beyond this many bytes (default 32 MiB)
    returns: bytes | dict - the tarball bytes (in band), or {"path": <resolved path>, "bytes_written": int}
    """
    image = _get_client(host).images.get(id_or_name)
    if dest_path is None:
        return join_bounded(image.save(named=named), max_bytes, f"save of image {id_or_name}")
    path, written = stream_to_file(image.save(named=named), dest_path, overwrite=overwrite)
    return {"path": str(path), "bytes_written": written}


@tool()
def image_tag(
    id_or_name: str, repository: str, tag: str | None = None, force: bool = False, host: str | None = None
) -> bool:
    """
    Tag an image into a repository (add a name to an existing local image).

    The image id stays the same and no data is copied — a tag is an alias. Typical flow: tag with
    the registry-qualified name, then `image_push`. `image_remove` on a tag merely untags while
    other names remain.

    args:
        id_or_name - The source image name or id
        repository - Target repository name (registry-qualified for pushing, e.g. "ghcr.io/o/r")
        tag - Optional tag for the new image (default "latest")
        force - Force the tag
    returns: bool - True if the image was tagged
    """
    image = _get_client(host).images.get(id_or_name)
    return image.tag(repository, tag=tag, force=force)


@tool()
def image_history(id_or_name: str, host: str | None = None) -> list:
    """
    Return the layer history of an image.

    Useful for auditing what commands built each layer and diagnosing image size. Each entry
    includes `Id` (layer digest or "<missing>" for imported layers), `Created` (unix
    timestamp), `CreatedBy` (the Dockerfile command that produced the layer, e.g. a RUN or
    COPY), `Size` (bytes added by that layer), and `Comment`. For full image metadata use
    `image_inspect` instead.

    args: id_or_name - Image name (with optional tag/digest) or id
    returns: list - Layer history entries, newest first
    """
    return _get_client(host).images.get(id_or_name).history()

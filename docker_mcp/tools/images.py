"""Tools for images.

Producing them, moving them between daemon and registry, and inspecting what is held locally.
"""

# library of mcp tools relating to image management

from typing import cast

from docker_mcp.exceptions import ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._utils import (
    MAX_PAYLOAD_BYTES,
    drop_none,
    host_read_path,
    join_bounded,
    open_host_read_file,
    stream_to_file,
)
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
    # Not an enum: alongside bridge/host/none/container:<id> this accepts any user-defined network
    # name, so the set is open by construction.
    network_mode: str | None = None,
    squash: bool = False,
    extra_hosts: dict | None = None,
    platform: str | None = None,
    # Not an enum: the accepted set is platform-dependent, and docker-py -- which is the call path
    # here -- documents no values at all, only "Isolation technology used during build". The
    # "default"/"process"/"hyperv" in the docstring are Docker's documented *Windows* values, given
    # as examples rather than as a closed set to validate against.
    isolation: str | None = None,
    use_config_proxy: bool = True,
    host: str | None = None,
) -> dict:
    """
    Build an image from a Dockerfile using the daemon's classic builder.

    Use this for simple single-platform builds from a local context. For multi-platform
    builds, BuildKit cache export/import, or advanced build features prefer `buildx_build`.
    `path` must be a directory accessible on the host running this server (it is the build
    context sent to the daemon). `dockerfile` is normally relative to `path`; omit to use the
    default `Dockerfile`.

    `dockerfile` is not confined to the context, despite the usual relative form: docker-py detects
    an absolute path, or a relative one escaping via `..`, reads that file from the **server host's**
    filesystem and injects its contents into the build. So it can read any file the server user can,
    like the other host-filesystem parameters (`dest_path`, `from_file`), and unlike them it is easy
    to mistake for a context-relative name. `buildx_build`'s `--file` resolves differently again
    (against the CLI's working directory) - see its docstring.

    Args:
        path: Build context directory path on the server host
        tag: Name and optional tag in "name:tag" format to apply to the built image
        quiet: Suppress verbose build output (final image id still returned)
        nocache: Ignore the layer cache and rebuild all layers
        rm: Remove intermediate containers on success (default True)
        pull: Always pull a newer version of each FROM base image before building
        forcerm: Remove intermediate containers even on build failure
        dockerfile: Dockerfile filename relative to path (default: "Dockerfile"); an absolute path or one containing
            ".." reads that file from the server host instead of the context
        buildargs: Build-time variables passed as `--build-arg`; dict of str to str
        container_limits: Resource limits for the build container, e.g. {"memory": 134217728}
        shmsize: Size of /dev/shm in bytes for build steps that need shared memory
        labels: Labels to set on the resulting image (dict of str to str)
        cache_from: List of image references to use as layer cache sources
        target: Stop at this named build stage (multi-stage Dockerfiles)
        network_mode: Network mode for RUN instructions during build (e.g. "host", "none")
        squash: Squash all new layers into one (experimental; requires daemon flag)
        extra_hosts: Additional /etc/hosts entries during build; dict of hostname to IP
        platform: Target platform, e.g. "linux/amd64" (single platform only; use buildx for multi)
        isolation: Isolation technology, passed to the daemon as given; platform-dependent, so not validated here
            (Windows documents "default", "process", "hyperv")
        use_config_proxy: Forward proxy env vars from Docker client config to build

    Returns:
        dict: The built image's full inspect payload (as `docker inspect`)
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
    produced each layer) use `image_history`. Only inspects images already present locally -
    for a remote image's manifest without pulling it use `image_registry_data` or
    `registry_manifest`.

    Args:
        id_or_name: Image name (with optional tag/digest) or id

    Returns:
        dict: Full image inspect attrs (equivalent to `docker inspect` on an image)
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

    Args:
        repository: Image reference
        auth_config: Optional registry authentication config

    Returns:
        dict: {"Descriptor", "Platforms"} - the OCI descriptor and the platforms available for the reference
    """
    return _get_client(host).images.get_registry_data(repository, auth_config=auth_config).attrs


@tool()
def image_list(
    repository: str | None = None, all: bool = False, filters: dict | None = None, host: str | None = None
) -> list:
    """
    List images in the daemon's local store.

    Local only - for a registry's contents use `registry_tags` / `hub_tags`, and `image_search`
    to find images on Docker Hub. Dangling (untagged) build leftovers show with
    filters={"dangling": True}.

    Args:
        repository: Only show images of this repository
        all: Show intermediate image layers
        filters: Filter by attributes (label, dangling, before, since, etc.)

    Returns:
        list: One summary dict per image ({"Id", "RepoTags", "RepoDigests", "Created", "Size", "Labels", ...}); use
            `image_inspect` for a full inspect payload
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

    Private repositories need credentials - `system_login` (or `docker login` on the host) first.
    Use `image_load` for tarballs, and `registry_manifest` / `image_registry_data` to inspect a
    remote image without pulling it.

    Args:
        repository: The image repository
        tag: The image tag (ignored when all_tags=True)
        all_tags: Pull all tags from the repository
        platform: Platform in os/arch format

    Returns:
        dict | list: Pulled image attrs (or a list of attrs if all_tags=True)
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

    The local image must already bear the target name - `image_tag` it with the
    registry-qualified `repository[:tag]` first; a bare name pushes to Docker Hub. Private
    registries need credentials (`system_login`, or `docker login` on the host).

    Security: `auth_config` carries registry credentials, which many MCP clients log verbatim. Prefer
    `docker login` on the host so the `docker` module reuses credentials cached in
    `~/.docker/config.json`, and leave `auth_config` unset.

    Args:
        repository: The image repository
        tag: The tag to push
        auth_config: Optional registry authentication config

    Returns:
        str: Push output as a string
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

    Args:
        id_or_name: Image name (with optional tag/digest) or id to remove
        force: Remove even if referenced by stopped containers or multiple tags
        noprune: Do not delete untagged intermediate parent layers

    Returns:
        bool: True after removal completes
    """
    _get_client(host).images.remove(image=id_or_name, force=force, noprune=noprune)
    return True


@tool()
def image_search(term: str, limit: int | None = None, host: str | None = None) -> list:
    """
    Search Docker Hub for public images matching a term.

    Searches Docker Hub only - not GHCR, ECR, or other registries. For listing tags on a
    specific image from any OCI registry use `registry_tags` instead.

    Args:
        term: Search keyword, e.g. "nginx" or "python"
        limit: Maximum number of results to return (Docker Hub default is 25)

    Returns:
        list: Result dicts: {"name", "description", "star_count", "is_official", "is_automated"} - `is_automated` is
            deprecated in the Engine API and is always false, so rank on `star_count`/`is_official` instead
    """
    return _get_client(host).images.search(term=term, limit=limit)


@tool()
def image_prune(filters: dict | None = None, host: str | None = None) -> dict:
    """
    Remove unused local images to reclaim disk space.

    Without filters removes only "dangling" images - untagged layers not referenced by any
    tag or container. To remove all images not used by any container (including tagged ones)
    pass `filters={"dangling": False}`. Valid filter keys: `dangling` (bool as string
    "true"/"false"), `until` (RFC3339 timestamp or duration like "24h"), `label`
    (key or key=value). Use `system_df` first to see how much space is reclaimable.

    Args:
        filters: Narrow which images to remove; omit to remove dangling images only

    Returns:
        dict: {"ImagesDeleted": [...], "SpaceReclaimed": <bytes>}
    """
    return _get_client(host).images.prune(filters=filters)


@tool()
def image_prune_builds(
    filters: dict | None = None,
    keep_storage: int | None = None,
    all: bool | None = None,
    host: str | None = None,
) -> dict:
    """
    Delete the daemon's build cache to reclaim disk space.

    Prunes the *build cache* - a separate Engine resource from the images `image_prune` removes,
    so run both to reclaim everything a build leaves behind. Prefer `buildx_prune` when the build
    ran on a non-default buildx builder (that builder keeps its own cache, invisible here) or when
    you need buildx's disk-ceiling flags; this tool needs no CLI plugin and works over any
    transport, including a daemon with no local `docker` binary. Inventory first with `system_df`
    (its `BuildCache` entry) or `buildx_du`. Destructive and immediate: later builds must re-run
    the steps whose cache was removed. Needs Docker API v1.31+; passing any of `filters`,
    `keep_storage`, or `all` needs v1.39+ and raises `InvalidVersion` on an older daemon - omit all
    three to prune with the daemon's own defaults.

    Args:
        filters: Narrow which cache records to remove, e.g. {"until": "24h"} (a duration or timestamp relative to the
            daemon's clock); also accepts `id`, `parent`, `type`, `description`, `inuse`, `shared`, `private`; omit to
            let the daemon prune unused cache
        keep_storage: Bytes of cache to keep, e.g. 5368709120 for 5 GiB; omit for no floor. The Engine renamed this
            `reserved-space` at API v1.48 and still honors the old name; the newer `max-used-space`/`min-free-space`
            ceilings are reachable only via `buildx_prune`
        all: Remove all types of build cache, not just the unused records

    Returns:
        dict: {"CachesDeleted": [...], "SpaceReclaimed": <bytes>}
    """
    return _get_client(host).images.prune_builds(**drop_none(filters=filters, keep_storage=keep_storage, all=all))


@tool()
def image_load(data: bytes | None = None, from_file: str | None = None, host: str | None = None) -> list:
    """
    Load an image from a tarball produced by `image_save`, from in-band bytes or a file on the server host.

    Counterpart of `image_save`; when the image lives in a registry, `image_pull` is the normal
    route, and for a flat rootfs archive that is not a `docker save` bundle use `image_import`.
    Pass exactly one of `data` (tarball bytes in band) or `from_file` (a path on the server host,
    streamed straight to the daemon - preferred for anything but small images, since in-band bytes are
    base64-encoded by MCP). `from_file` is read by the server's user; `~` is expanded.

    Args:
        data: Tarball contents; exactly one of data/from_file
        from_file: Path to a tarball produced by `docker save` / `image_save`; exactly one of data/from_file

    Returns:
        list: One full inspect payload per loaded image
    """
    if (data is None) == (from_file is None):
        raise ToolInputError("Pass exactly one of `data` (in-band tarball bytes) or `from_file` (a server-host path).")
    if data is not None:
        return [i.attrs for i in _get_client(host).images.load(data)]
    with open_host_read_file(cast(str, from_file)) as handle:
        return [i.attrs for i in _get_client(host).images.load(handle)]


@tool()
def image_import(
    repository: str | None = None,
    tag: str | None = None,
    from_file: str | None = None,
    data: bytes | None = None,
    from_url: str | None = None,
    from_image: str | None = None,
    changes: list | None = None,
    host: str | None = None,
) -> str:
    """
    Create an image from a flat root-filesystem tarball, like `docker import`.

    Imports a *filesystem* archive as a new single-layer image with no build history - not the same
    thing as `image_load`, which restores a `docker save` archive complete with its layers, tags and
    history, so prefer `image_load` for anything `image_save` produced. Use this for a rootfs that
    came from somewhere else: a `container_export` archive, a distro base tarball, a VM image dump.
    The result has an empty config - no `CMD`/`ENTRYPOINT`/`ENV` - unless you supply `changes`, so an
    imported image is usually not runnable until you set at least a command. Pass exactly one source
    (`from_file`, `data`, `from_url` or `from_image`); ToolInputError otherwise. `from_url` and
    `from_image` are fetched by the *daemon*, `from_file`/`data` are read here and uploaded; a
    `from_file` path that is not a readable file raises rather than being retried as a URL. Unlike
    the other image-creating tools this stamps no provenance labels: the Engine's import call accepts
    no labels field, and `changes` does not cover `LABEL`.

    Args:
        repository: Repository name to give the new image, e.g. "myorg/rootfs"; may include a tag (`myorg/rootfs:v1`),
            and defaults to `:latest` when it does not. Omit to import untagged, addressable only by the id in the
            returned progress (omit it entirely -- a blank string is a ToolInputError, not a shorthand for untagged). A
            digest reference is refused by the daemon. Required if `tag` is given
        tag: Tag to apply, e.g. "v1". **Overrides** a tag already in `repository` rather than being ignored, so passing
            `repository="myorg/rootfs:v1"` with `tag="v2"` yields `:v2`. Requires `repository` (ToolInputError without
            it - the daemon would otherwise silently drop the tag and import untagged). Blank is also a ToolInputError,
            not a shorthand for the default: the daemon would substitute `latest` without saying so
        from_file: Path to a rootfs tarball on the server host (`~` expanded), read by the server's user; refused if it
            is not an existing regular file; exactly one source
        data: Rootfs tarball contents in band (base64-encoded by MCP, so prefer `from_file` for anything but small
            archives); exactly one source
        from_url: URL the daemon fetches the tarball from; exactly one source
        from_image: Name of an existing image to import from, like a Dockerfile `FROM`; exactly one source
        changes: Dockerfile instructions applied to the new image, e.g. ['CMD ["/bin/sh"]']; only CMD, ENTRYPOINT, ENV,
            EXPOSE, ONBUILD, USER, VOLUME and WORKDIR are supported. Parsed as real Dockerfile syntax, so shell form is
            wrapped exactly as a Dockerfile would wrap it (`CMD /bin/sh` is stored as `["/bin/sh","-c","/bin/sh"]`) -
            use the exec form `CMD ["/bin/sh"]` to store a bare argv

    Returns:
        str: The daemon's raw newline-delimited JSON progress records; the final record carries the new image id as its
            `status`
    """
    sources = {"from_file": from_file, "data": data, "from_url": from_url, "from_image": from_image}
    supplied = [name for name, value in sources.items() if value is not None]
    if len(supplied) != 1:
        raise ToolInputError(
            "Pass exactly one of `from_file`, `data`, `from_url` or `from_image` "
            f"(got {', '.join(supplied) if supplied else 'none'})."
        )
    # A bare `tag` is refused rather than forwarded: the Engine returns early when `repo` is empty
    # (moby's httputils.RepoTagReference), so the tag is silently dropped and the image lands
    # untagged -- a caller asking for `:v1` would get no error and no tag. A blank `repository`
    # reaches that same early return, verified against a live daemon (repository="", tag=... imports
    # with RepoTags []), so it is refused rather than read as "no repository": passing one is never
    # meaningful, and treating it as absent would reopen the silent drop through the back door.
    if repository is not None and not repository.strip():
        raise ToolInputError("`repository` cannot be blank; omit it entirely to import untagged.")
    # A blank `tag` is the same silent substitution one step further on: `RepoTagReference` tests
    # `tag != ""`, so an empty tag skips the WithTag path and falls through to `TagNameOnly`, which
    # supplies `:latest`. The caller asked for one tag and would get a different one, with no error --
    # so it is refused alongside a blank `repository` rather than left as the asymmetric case.
    if tag is not None and not tag.strip():
        raise ToolInputError("`tag` cannot be blank; omit it to accept the daemon's default of `latest`.")
    if tag is not None and repository is None:
        raise ToolInputError(
            "`tag` needs a `repository` to attach to; pass `repository`, or omit `tag` to import untagged."
        )

    # The high-level ImageCollection has no import; these four are the documented low-level calls.
    api = _get_client(host).api
    common = drop_none(repository=repository, tag=tag, changes=changes)
    if from_file is not None:
        # `import_image_from_file` is a one-line delegation to `import_image(src=...)`, which sends
        # the path as `fromSrc` whenever `docker.utils.is_file(src)` is false -- so a missing path
        # (or a directory) is not an error but an instruction to the *daemon* to fetch that string
        # over HTTP, in the daemon's network namespace. Verified against a live daemon:
        # `from_file="127.0.0.1:9/rootfs.tar"` produced `Get "http://127.0.0.1:9/rootfs.tar"`.
        # `docker import` itself opens the file and reports ENOENT, so this guard restores CLI
        # parity and keeps `from_url` the only source that leaves the host.
        path = host_read_path(from_file)
        if not path.is_file():
            raise ToolInputError(
                f"No such rootfs tarball: {path}. Pass `from_url` to have the daemon fetch a URL, "
                "or `data` to send the archive in band."
            )
        return api.import_image_from_file(str(path), **common)
    if data is not None:
        return api.import_image_from_data(data, **common)
    if from_url is not None:
        return api.import_image_from_url(from_url, **common)
    return api.import_image_from_image(cast(str, from_image), **common)


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

    The archive keeps layers, tags, and metadata so `image_load` can restore it - different from
    `container_export`, which flattens one container's filesystem. With `dest_path` the archive
    streams straight to disk (no byte cap), so it handles large images - the file is written by
    the server's user, `~` is expanded, and an existing file is refused unless
    `overwrite=True`. Without `dest_path` the tar bytes are returned in band, capped at `max_bytes`
    (default 32 MiB) because MCP base64-encodes them - a fallback for when no writable host path
    exists (e.g. a containerized server without a bind mount).

    Args:
        id_or_name: Image name or id
        dest_path: Destination path on the server host; omit to return the bytes in band
        named: Whether to retain repository/tag names in the saved archive
        overwrite: Replace dest_path if it already exists (default False)
        max_bytes: In-band mode: abort with ToolInputError beyond this many bytes (default 32 MiB)

    Returns:
        bytes | dict: the tarball bytes (in band), or {"path": <resolved path>, "bytes_written": int}
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

    The image id stays the same and no data is copied - a tag is an alias. Typical flow: tag with
    the registry-qualified name, then `image_push`. `image_remove` on a tag merely untags while
    other names remain.

    Args:
        id_or_name: The source image name or id
        repository: Target repository name (registry-qualified for pushing, e.g. "ghcr.io/o/r")
        tag: Optional tag for the new image (default "latest")
        force: Force the tag

    Returns:
        bool: True if the image was tagged
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

    Args:
        id_or_name: Image name (with optional tag/digest) or id

    Returns:
        list: Layer history entries, newest first
    """
    return _get_client(host).images.get(id_or_name).history()

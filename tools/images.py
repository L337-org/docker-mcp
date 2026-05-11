# library of mcp tools relating to image management

from server import mcp
from tools._utils import MAX_PAYLOAD_BYTES, drop_none, join_bounded
from tools.client import _get_client


@mcp.tool()
def build_image(
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
) -> dict:
    """
    Build an image from a Dockerfile.

    args:
        path: str - Path to the build context directory
        tag: str - Tag to apply to the built image
        quiet: bool - Suppress build output
        nocache: bool - Do not use the build cache
        rm: bool - Remove intermediate containers
        pull: bool - Always pull a newer version of the base image
        forcerm: bool - Always remove intermediate containers, even on failure
        dockerfile: str - Name of the Dockerfile within the build context
        buildargs: dict - Build-time variables
        container_limits: dict - Resource limits for the build container
        shmsize: int - Size of /dev/shm in bytes
        labels: dict - Labels to apply to the image
        cache_from: list - Images used as cache sources
        target: str - Target build stage to stop at
        network_mode: str - Network mode for the run instructions during build
        squash: bool - Squash newly built layers into a single layer
        extra_hosts: dict - Extra hosts to add to /etc/hosts during build
        platform: str - Platform in the format os/arch
        isolation: str - Isolation technology
        use_config_proxy: bool - Use proxy values from the local config
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
    image, _logs = _get_client().images.build(**kwargs)
    return image.attrs


@mcp.tool()
def get_image(name: str) -> dict:
    """
    Get an image by name or id.

    args: name: str - The image name or id
    returns: dict - The image's attrs
    """
    return _get_client().images.get(name).attrs


@mcp.tool()
def get_registry_data(name: str, auth_config: dict | None = None) -> dict:
    """
    Get registry data for an image without pulling it.

    Security: `auth_config` carries registry credentials and many MCP clients log
    tool arguments verbatim. Prefer authenticating on the host running this MCP
    server with `docker login` so the `docker` module can reuse credentials cached
    in that host's Docker config (typically `~/.docker/config.json`), and leave
    `auth_config` unset.

    args:
        name: str - Image reference
        auth_config: dict - Optional registry authentication config
    returns: dict - Registry data attrs
    """
    return _get_client().images.get_registry_data(name, auth_config=auth_config).attrs


@mcp.tool()
def list_images(name: str | None = None, all: bool = False, filters: dict | None = None) -> list:
    """
    List images on the server.

    args:
        name: str - Only show images of this repository
        all: bool - Show intermediate image layers
        filters: dict - Filter by attributes (label, dangling, before, since, etc.)
    returns: list - A list of image attrs dicts
    """
    return [i.attrs for i in _get_client().images.list(name=name, all=all, filters=filters)]


@mcp.tool()
def pull_image(
    repository: str, tag: str | None = None, all_tags: bool = False, platform: str | None = None
) -> dict | list:
    """
    Pull an image of the given name.

    args:
        repository: str - The image repository
        tag: str - The image tag (ignored when all_tags=True)
        all_tags: bool - Pull all tags from the repository
        platform: str - Platform in os/arch format
    returns: dict | list - Pulled image attrs (or a list of attrs if all_tags=True)
    """
    result = _get_client().images.pull(repository, tag=tag, all_tags=all_tags, platform=platform)
    if isinstance(result, list):
        return [i.attrs for i in result]
    return result.attrs


@mcp.tool()
def push_image(repository: str, tag: str | None = None, auth_config: dict | None = None) -> str:
    """
    Push an image or repository to a registry.

    Security: `auth_config` carries registry credentials and many MCP clients log
    tool arguments verbatim. Prefer authenticating on the host running this MCP
    server with `docker login` so the `docker` module can reuse credentials cached
    in that host's Docker config (typically `~/.docker/config.json`), and leave
    `auth_config` unset.

    args:
        repository: str - The image repository
        tag: str - The tag to push
        auth_config: dict - Optional registry authentication config
    returns: str - Push output as a string
    """
    output = _get_client().images.push(repository, tag=tag, stream=False, auth_config=auth_config, decode=False)
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


@mcp.tool()
def remove_image(image: str, force: bool = False, noprune: bool = False) -> bool:
    """
    Remove an image.

    args:
        image: str - The image name or id
        force: bool - Force removal
        noprune: bool - Do not delete untagged parents
    returns: bool - True after removal completes
    """
    _get_client().images.remove(image=image, force=force, noprune=noprune)
    return True


@mcp.tool()
def search_images(term: str, limit: int | None = None) -> list:
    """
    Search Docker Hub for images.

    args:
        term: str - Search term
        limit: int - Maximum number of results
    returns: list - Search results
    """
    return _get_client().images.search(term=term, limit=limit)


@mcp.tool()
def prune_images(filters: dict | None = None) -> dict:
    """
    Remove unused images.

    args: filters: dict - Filters to apply (e.g. dangling, until, label)
    returns: dict - Information on deleted images and reclaimed space
    """
    return _get_client().images.prune(filters=filters)


@mcp.tool()
def load_image(data: bytes) -> list:
    """
    Load an image from a tarball produced by save_image.

    args: data: bytes - Tarball contents
    returns: list - A list of loaded image attrs dicts
    """
    return [i.attrs for i in _get_client().images.load(data)]


@mcp.tool()
def save_image(name: str, named: bool = False, max_bytes: int = MAX_PAYLOAD_BYTES) -> bytes:
    """
    Save an image as a tar archive.

    args:
        name: str - Image name or id
        named: bool - Whether to keep the image name when saving
        max_bytes: int - Abort with ValueError if the tarball exceeds this many bytes (defaults to 1 GiB)
    returns: bytes - The tarball contents
    """
    image = _get_client().images.get(name)
    return join_bounded(image.save(named=named), max_bytes, f"save of image {name}")


@mcp.tool()
def tag_image(name: str, repository: str, tag: str | None = None, force: bool = False) -> bool:
    """
    Tag an image into a repository.

    args:
        name: str - The source image name or id
        repository: str - Target repository name
        tag: str - Optional tag for the new image
        force: bool - Force the tag
    returns: bool - True if the image was tagged
    """
    image = _get_client().images.get(name)
    return image.tag(repository, tag=tag, force=force)


@mcp.tool()
def image_history(name: str) -> list:
    """
    Show the history of an image.

    args: name: str - The image name or id
    returns: list - History entries for each layer
    """
    return _get_client().images.get(name).history()

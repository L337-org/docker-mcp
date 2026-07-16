# library of mcp tools relating to volume management

from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.system import _get_client


@tool()
def volume_create(
    name: str | None = None,
    driver: str | None = None,
    driver_opts: dict | None = None,
    labels: dict | None = None,
    host: str | None = None,
) -> dict:
    """
    Create a volume managed by Docker.

    Named volumes persist after their containers stop or are removed; use them for
    databases, uploads, or any data that must outlive a container. Anonymous volumes
    (no `name`) are only removed automatically when the container was started with `--rm`
    or removed with `docker rm -v`; otherwise they accumulate and must be pruned manually.
    Common `driver_opts` for the default `local` driver: bind-mount an existing host path
    with `{"type": "none", "device": "/host/path", "o": "bind"}`, or mount an NFS share
    with `{"type": "nfs", "device": "server:/export", "o": "addr=server,rw"}`. Third-party
    drivers (e.g. `rexray`, `convoy`) accept their own option keys. List existing volumes with
    `volume_list`; reclaim unused ones with `volume_prune`. Created volumes are stamped with provenance
    labels.

    args:
        name - Volume name; auto-generated if omitted (creates an anonymous volume)
        driver - Volume driver to use (default: "local")
        driver_opts - Driver-specific options dict
        labels - Labels to set on the volume
    returns: dict - The created volume's attrs ({"Name", "Driver", "Mountpoint", "Labels", ...})
    """
    kwargs = drop_none(
        name=name, driver=driver, driver_opts=driver_opts, labels=with_provenance(labels, "volume_create")
    )
    return _get_client(host).volumes.create(**kwargs).attrs


@tool()
def volume_inspect(name: str, host: str | None = None) -> dict:
    """
    Get a volume's full inspect payload by name.

    Use it after `volume_list` to see a volume's on-disk location, driver, and labels — e.g.
    before a backup or `volume_remove`. Volumes are addressed purely by name; they have no
    separate id.

    args: name - The volume name (volumes have no ids)
    returns: dict - The volume's attrs (Name, Driver, Mountpoint, CreatedAt, Labels, Options, Scope)
    """
    return _get_client(host).volumes.get(name).attrs


@tool()
def volume_list(filters: dict | None = None, managed_only: bool = False, host: str | None = None) -> list:
    """
    List volumes.

    Volumes are addressed by name only — feed a Name to `volume_inspect` for detail or
    `volume_remove` / `volume_prune` to clean up. filters={"dangling": True} finds volumes that no
    container references.

    args:
        filters - Filter by attributes (e.g. dangling, name, label)
        managed_only - Only return volumes created by this MCP server (filters on the
                             docker-mcp-server.managed label); combines with any `filters` given
    returns: list - One volume document ({"Name", "Driver", "Mountpoint", ...}) per volume
    """
    if managed_only:
        filters = managed_filter(filters)
    return [v.attrs for v in _get_client(host).volumes.list(**drop_none(filters=filters))]


@tool()
def volume_prune(filters: dict | None = None, host: str | None = None) -> dict:
    """
    Remove volumes not referenced by any container, running or stopped.

    A volume used by even one stopped container is not "unused" and survives the prune —
    remove the container first (or use `container_prune`, then this) to reclaim its
    volumes. Valid filter keys: `label` (key or key=value), `all` ("true" as a string —
    without it only anonymous volumes are eligible, matching `docker volume prune`'s
    default). Use `volume_list` first to see what currently exists.

    args: filters - Narrow which unused volumes to remove; omit to remove all anonymous ones
    returns: dict - {"VolumesDeleted": [...], "SpaceReclaimed": <bytes>}
    """
    return _get_client(host).volumes.prune(filters=filters)


@tool()
def volume_remove(name: str, force: bool = False, host: str | None = None) -> bool:
    """
    Remove a single volume by name.

    Fails if any container, running or stopped, still references the volume — remove or
    recreate those containers first, or pass `force=True` to remove it anyway (the
    containers keep their reference but lose the underlying data). For bulk cleanup of
    volumes with no container references at all, use `volume_prune` instead.

    args:
        name - Volume name to remove
        force - Remove even if a container still references the volume
    returns: bool - True after removal
    """
    _get_client(host).volumes.get(name).remove(force=force)
    return True

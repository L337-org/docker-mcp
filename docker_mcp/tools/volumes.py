# library of mcp tools relating to volume management

from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.client import _get_client


@tool()
def create_volume(
    name: str | None = None,
    driver: str | None = None,
    driver_opts: dict | None = None,
    labels: dict | None = None,
    host: str | None = None,
) -> dict:
    """
    Create a volume.

    args:
        name - Volume name (auto-generated if omitted)
        driver - Volume driver name
        driver_opts - Driver-specific options
        labels - Labels to set on the volume
    returns: dict - The created volume's attrs
    """
    kwargs = drop_none(
        name=name, driver=driver, driver_opts=driver_opts, labels=with_provenance(labels, "create_volume")
    )
    return _get_client(host).volumes.create(**kwargs).attrs


@tool()
def get_volume(volume_id: str, host: str | None = None) -> dict:
    """
    Get a volume by name.

    args: volume_id - The volume name
    returns: dict - The volume's attrs
    """
    return _get_client(host).volumes.get(volume_id).attrs


@tool()
def list_volumes(filters: dict | None = None, managed_only: bool = False, host: str | None = None) -> list:
    """
    List volumes.

    args:
        filters - Filter by attributes (e.g. dangling, name, label)
        managed_only - Only return volumes created by this MCP server (filters on the
                             docker-mcp-server.managed label); combines with any `filters` given
    returns: list - A list of volume attrs dicts
    """
    if managed_only:
        filters = managed_filter(filters)
    return [v.attrs for v in _get_client(host).volumes.list(**drop_none(filters=filters))]


@tool()
def prune_volumes(filters: dict | None = None, host: str | None = None) -> dict:
    """
    Remove unused volumes.

    args: filters - Filters to apply
    returns: dict - Information on deleted volumes and reclaimed space
    """
    return _get_client(host).volumes.prune(filters=filters)


@tool()
def remove_volume(volume_id: str, force: bool = False, host: str | None = None) -> bool:
    """
    Remove a volume.

    args:
        volume_id - The volume name
        force - Force removal
    returns: bool - True after removal
    """
    _get_client(host).volumes.get(volume_id).remove(force=force)
    return True

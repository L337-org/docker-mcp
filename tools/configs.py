# library of mcp tools relating to swarm config management

from server import mcp
from tools._utils import drop_none
from tools.client import _get_client


@mcp.tool()
def create_config(name: str, data: bytes, labels: dict | None = None, templating: dict | None = None) -> dict:
    """
    Create a swarm config.

    args:
        name: str - The name of the config
        data: bytes - The config payload
        labels: dict - Labels to apply
        templating: dict - Templating driver configuration
    returns: dict - The created config's attrs
    """
    kwargs: dict = {"name": name, "data": data, **drop_none(labels=labels, templating=templating)}
    return _get_client().configs.create(**kwargs).attrs


@mcp.tool()
def get_config(config_id: str) -> dict:
    """
    Get a swarm config by id.

    args: config_id: str - The config id
    returns: dict - The config's attrs
    """
    return _get_client().configs.get(config_id).attrs


@mcp.tool()
def list_configs(filters: dict | None = None) -> list:
    """
    List swarm configs.

    args: filters: dict - Filter by attributes (e.g. id, name, label)
    returns: list - A list of config attrs dicts
    """
    return [c.attrs for c in _get_client().configs.list(**drop_none(filters=filters))]


@mcp.tool()
def remove_config(config_id: str) -> bool:
    """
    Remove a swarm config.

    args: config_id: str - The config id
    returns: bool - True after removal
    """
    _get_client().configs.get(config_id).remove()
    return True

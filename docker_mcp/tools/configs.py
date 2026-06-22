# library of mcp tools relating to swarm config management

from docker_mcp.server import tool
from docker_mcp.tools._labels import with_provenance
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.client import _get_client


@tool()
def create_config(
    name: str, data: bytes, labels: dict | None = None, templating: dict | None = None, host: str | None = None
) -> dict:
    """
    Create a swarm config.

    args:
        name - The name of the config
        data - The config payload
        labels - Labels to apply
        templating - Templating driver configuration
    returns: dict - The created config's attrs
    """
    kwargs: dict = {
        "name": name,
        "data": data,
        **drop_none(labels=with_provenance(labels, "create_config"), templating=templating),
    }
    return _get_client(host).configs.create(**kwargs).attrs


@tool()
def get_config(config_id: str, host: str | None = None) -> dict:
    """
    Get a swarm config by id.

    args: config_id - The config id
    returns: dict - The config's attrs
    """
    return _get_client(host).configs.get(config_id).attrs


@tool()
def list_configs(filters: dict | None = None, host: str | None = None) -> list:
    """
    List swarm configs.

    args: filters - Filter by attributes (e.g. id, name, label)
    returns: list - A list of config attrs dicts
    """
    return [c.attrs for c in _get_client(host).configs.list(**drop_none(filters=filters))]


@tool()
def remove_config(config_id: str, host: str | None = None) -> bool:
    """
    Remove a swarm config.

    args: config_id - The config id
    returns: bool - True after removal
    """
    _get_client(host).configs.get(config_id).remove()
    return True

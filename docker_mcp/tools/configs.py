# library of mcp tools relating to swarm config management

from docker_mcp.server import tool
from docker_mcp.tools._labels import with_provenance
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.system import _get_client


@tool()
def config_create(
    name: str, data: bytes, labels: dict | None = None, templating: dict | None = None, host: str | None = None
) -> dict:
    """
    Create an immutable Swarm config object; requires a swarm manager.

    Configs store non-sensitive configuration files (nginx.conf, app.yaml, etc.) and mount
    them into service containers at a specified path. Unlike secrets, config data is not
    encrypted at rest — use `secret_create` for credentials or keys. `data` is raw bytes;
    encode strings first (e.g. `"my config".encode()`). Once created, a config is immutable:
    to update it, create a new config with a new name and update the service to reference it,
    then remove the old config with `config_remove`.

    args:
        name - Unique config name within the swarm
        data - Raw bytes content of the config file
        labels - Labels to apply to the config object
        templating - Templating driver config (e.g. {"Name": "golang"} for Go template syntax)
    returns: dict - The created config's attrs including its id
    """
    kwargs: dict = {
        "name": name,
        "data": data,
        **drop_none(labels=with_provenance(labels, "config_create"), templating=templating),
    }
    return _get_client(host).configs.create(**kwargs).attrs


@tool()
def config_inspect(config_id: str, host: str | None = None) -> dict:
    """
    Get a swarm config by id.

    args: config_id - The config id
    returns: dict - The config's attrs
    """
    return _get_client(host).configs.get(config_id).attrs


@tool()
def config_list(filters: dict | None = None, host: str | None = None) -> list:
    """
    List swarm configs.

    args: filters - Filter by attributes (e.g. id, name, label)
    returns: list - A list of config attrs dicts
    """
    return [c.attrs for c in _get_client(host).configs.list(**drop_none(filters=filters))]


@tool()
def config_remove(config_id: str, host: str | None = None) -> bool:
    """
    Remove a swarm config.

    args: config_id - The config id
    returns: bool - True after removal
    """
    _get_client(host).configs.get(config_id).remove()
    return True

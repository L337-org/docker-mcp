# library of mcp tools relating to swarm secrets management

from docker_mcp.server import tool
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.client import _get_client


@tool()
def create_secret(name: str, data: bytes, labels: dict | None = None, driver: dict | None = None) -> dict:
    """
    Create a swarm secret.

    args:
        name: str - The name of the secret
        data: bytes - The secret payload
        labels: dict - Labels to apply
        driver: dict - Optional secret driver configuration
    returns: dict - The created secret's attrs
    """
    kwargs: dict = {"name": name, "data": data, **drop_none(labels=labels, driver=driver)}
    return _get_client().secrets.create(**kwargs).attrs


@tool()
def get_secret(secret_id: str) -> dict:
    """
    Get a swarm secret by id.

    args: secret_id: str - The secret id
    returns: dict - The secret's attrs
    """
    return _get_client().secrets.get(secret_id).attrs


@tool()
def list_secrets(filters: dict | None = None) -> list:
    """
    List swarm secrets.

    args: filters: dict - Filter by attributes (e.g. id, name, label)
    returns: list - A list of secret attrs dicts
    """
    return [s.attrs for s in _get_client().secrets.list(**drop_none(filters=filters))]


@tool()
def remove_secret(secret_id: str) -> bool:
    """
    Remove a swarm secret.

    args: secret_id: str - The secret id
    returns: bool - True after removal
    """
    _get_client().secrets.get(secret_id).remove()
    return True

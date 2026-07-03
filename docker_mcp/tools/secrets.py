# library of mcp tools relating to swarm secrets management

from docker_mcp.server import tool
from docker_mcp.tools._labels import with_provenance
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.system import _get_client


@tool()
def secret_create(
    name: str, data: bytes, labels: dict | None = None, driver: dict | None = None, host: str | None = None
) -> dict:
    """
    Create a swarm secret.

    args:
        name - The name of the secret
        data - The secret payload
        labels - Labels to apply
        driver - Optional secret driver configuration
    returns: dict - The created secret's attrs
    """
    kwargs: dict = {
        "name": name,
        "data": data,
        **drop_none(labels=with_provenance(labels, "secret_create"), driver=driver),
    }
    return _get_client(host).secrets.create(**kwargs).attrs


@tool()
def secret_inspect(secret_id: str, host: str | None = None) -> dict:
    """
    Get a swarm secret by id.

    args: secret_id - The secret id
    returns: dict - The secret's attrs
    """
    return _get_client(host).secrets.get(secret_id).attrs


@tool()
def secret_list(filters: dict | None = None, host: str | None = None) -> list:
    """
    List swarm secrets.

    args: filters - Filter by attributes (e.g. id, name, label)
    returns: list - A list of secret attrs dicts
    """
    return [s.attrs for s in _get_client(host).secrets.list(**drop_none(filters=filters))]


@tool()
def secret_remove(secret_id: str, host: str | None = None) -> bool:
    """
    Remove a Swarm secret; requires a swarm manager.

    Removing a secret does not immediately affect running service tasks — tasks that already
    have the secret mounted retain access until they are restarted or the service is updated.
    Use `service_list` and inspect each service's spec via `service_inspect` to identify
    services that mount the secret before removing it (service filters do not support
    filtering by secret reference). The secret id (not name) is required; retrieve it from `secret_list`
    or `secret_inspect`.

    args: secret_id - The secret id to remove
    returns: bool - True after removal
    """
    _get_client(host).secrets.get(secret_id).remove()
    return True

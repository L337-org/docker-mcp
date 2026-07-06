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
        labels - Labels to set on the secret
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
def secret_inspect(id_or_name: str, host: str | None = None) -> dict:
    """
    Get a swarm secret's metadata by id or name; requires a swarm manager.

    The returned attrs never include the secret's actual data (`Spec.Data` is write-only —
    the daemon accepts it on `secret_create` but never returns it back, by design). Use this
    to check a secret's `CreatedAt`, `Labels`, or which driver created it, not to read its
    contents. To see which services reference it, inspect each service's spec via
    `service_inspect` (there is no server-side filter for "services using this secret").

    args: id_or_name - The secret id or name
    returns: dict - The secret's attrs, excluding the actual secret data
    """
    return _get_client(host).secrets.get(id_or_name).attrs


@tool()
def secret_list(filters: dict | None = None, host: str | None = None) -> list:
    """
    List swarm secrets' metadata; requires a swarm manager.

    Like `secret_inspect`, results never include secret data, only metadata (name, id,
    labels, timestamps). Valid filter keys: `id`, `name`, `names`, `label` (key or
    key=value).

    args: filters - Narrow the list; omit to return every secret
    returns: list - A list of secret attrs dicts (data-free)
    """
    return [s.attrs for s in _get_client(host).secrets.list(**drop_none(filters=filters))]


@tool()
def secret_remove(id_or_name: str, host: str | None = None) -> bool:
    """
    Remove a Swarm secret; requires a swarm manager.

    Removing a secret does not immediately affect running service tasks — tasks that already
    have the secret mounted retain access until they are restarted or the service is updated.
    Use `service_list` and inspect each service's spec via `service_inspect` to identify
    services that mount the secret before removing it (service filters do not support
    filtering by secret reference).

    args: id_or_name - The secret id or name to remove
    returns: bool - True after removal
    """
    _get_client(host).secrets.get(id_or_name).remove()
    return True

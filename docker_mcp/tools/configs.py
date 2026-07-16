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
        labels - Labels to set on the config
        templating - Templating driver config (e.g. {"Name": "golang"} for Go template syntax)
    returns: dict - The created config's attrs ({"ID", "Version", "CreatedAt", "Spec", ...})
    """
    kwargs: dict = {
        "name": name,
        "data": data,
        **drop_none(labels=with_provenance(labels, "config_create"), templating=templating),
    }
    return _get_client(host).configs.create(**kwargs).attrs


@tool()
def config_inspect(id_or_name: str, host: str | None = None) -> dict:
    """
    Get a swarm config's full inspect payload by id or name.

    Requires a swarm manager. Unlike a secret, a config's payload IS readable after creation:
    `Spec.Data` in the result holds the base64-encoded contents. Use `config_list` to enumerate
    configs; use this to read one config's contents and metadata.

    args: id_or_name - The config id or name
    returns: dict - The config's attrs (ID, CreatedAt, UpdatedAt, Spec{Name, Labels, Data base64})
    """
    return _get_client(host).configs.get(id_or_name).attrs


@tool()
def config_list(filters: dict | None = None, host: str | None = None) -> list:
    """
    List swarm configs; requires a swarm manager.

    Unlike secrets, config attrs include the actual config data (`Spec.Data`, base64-encoded)
    since configs are not treated as sensitive. Valid filter keys: `id`, `name`, `names`,
    `label` (key or key=value). Fetch a single config by id/name with `config_inspect`.

    args: filters - Narrow the list; omit to return every config
    returns: list - One full config document ({"ID", "Spec", ...}) per config
    """
    return [c.attrs for c in _get_client(host).configs.list(**drop_none(filters=filters))]


@tool()
def config_remove(id_or_name: str, host: str | None = None) -> bool:
    """
    Remove a swarm config.

    Requires a swarm manager, and fails while any service still references the config — update or
    remove those services first. The last step of the rotation flow described in `config_create`.

    args: id_or_name - The config id or name
    returns: bool - True after removal
    """
    _get_client(host).configs.get(id_or_name).remove()
    return True

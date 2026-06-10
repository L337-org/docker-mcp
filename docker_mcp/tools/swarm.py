# library of mcp tools relating to docker swarm

from docker_mcp.server import tool
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.client import _get_client


@tool()
def init_swarm(
    advertise_addr: str | None = None,
    listen_addr: str = "0.0.0.0:2377",
    force_new_cluster: bool = False,
    default_addr_pool: list | None = None,
    subnet_size: int | None = None,
    data_path_addr: str | None = None,
    data_path_port: int | None = None,
    name: str | None = None,
    labels: dict | None = None,
    autolock_managers: bool | None = None,
    log_driver: dict | None = None,
) -> str:
    """
    Initialize a new swarm on this Engine.

    args:
        advertise_addr: str - Externally reachable address advertised to other nodes
        listen_addr: str - Listen address used for inter-manager communication
        force_new_cluster: bool - Force a new cluster from current state
        default_addr_pool: list - IP address pools for swarm overlay networks
        subnet_size: int - Subnet size for the IP pool
        data_path_addr: str - Address to use for data path traffic
        data_path_port: int - Port number for data path traffic
        name: str - Name of the swarm
        labels: dict - User-defined key/value metadata
        autolock_managers: bool - Encrypt manager keys at rest
        log_driver: dict - Default log driver configuration
    returns: str - The node id of the newly created swarm manager
    """
    kwargs: dict = {
        "listen_addr": listen_addr,
        "force_new_cluster": force_new_cluster,
        **drop_none(
            advertise_addr=advertise_addr,
            default_addr_pool=default_addr_pool,
            subnet_size=subnet_size,
            data_path_addr=data_path_addr,
            data_path_port=data_path_port,
            name=name,
            labels=labels,
            autolock_managers=autolock_managers,
            log_driver=log_driver,
        ),
    }
    return _get_client().swarm.init(**kwargs)


@tool()
def join_swarm(
    remote_addrs: list,
    join_token: str,
    listen_addr: str = "0.0.0.0:2377",
    advertise_addr: str | None = None,
    data_path_addr: str | None = None,
) -> bool:
    """
    Join an existing swarm.

    args:
        remote_addrs: list - Addresses of swarm managers to connect to
        join_token: str - The swarm join token
        listen_addr: str - Listen address for inter-manager communication
        advertise_addr: str - Advertised address
        data_path_addr: str - Data path address
    returns: bool - True after the engine joins the swarm
    """
    kwargs: dict = {
        "remote_addrs": remote_addrs,
        "join_token": join_token,
        "listen_addr": listen_addr,
        **drop_none(advertise_addr=advertise_addr, data_path_addr=data_path_addr),
    }
    return _get_client().swarm.join(**kwargs)


@tool()
def leave_swarm(force: bool = False) -> bool:
    """
    Leave the current swarm.

    args: force: bool - Force leave even if the node is a manager
    returns: bool - True after leaving the swarm
    """
    return _get_client().swarm.leave(force=force)


@tool()
def update_swarm(
    rotate_worker_token: bool = False,
    rotate_manager_token: bool = False,
    rotate_manager_unlock_key: bool = False,
) -> bool:
    """
    Update the swarm configuration.

    args:
        rotate_worker_token: bool - Rotate the worker join token
        rotate_manager_token: bool - Rotate the manager join token
        rotate_manager_unlock_key: bool - Rotate the manager unlock key
    returns: bool - True after the update completes
    """
    return _get_client().swarm.update(
        rotate_worker_token=rotate_worker_token,
        rotate_manager_token=rotate_manager_token,
        rotate_manager_unlock_key=rotate_manager_unlock_key,
    )


@tool()
def reload_swarm() -> dict:
    """
    Reload the swarm and return its current attrs.

    returns: dict - The swarm's current attrs
    """
    swarm = _get_client().swarm
    swarm.reload()
    return swarm.attrs


@tool()
def unlock_swarm(key: str) -> bool:
    """
    Unlock a locked swarm.

    args: key: str - The unlock key
    returns: bool - True after the swarm is unlocked
    """
    return _get_client().swarm.unlock(key)


@tool()
def get_swarm_unlock_key() -> dict:
    """
    Return the swarm unlock key.

    returns: dict - The unlock key info
    """
    return _get_client().swarm.get_unlock_key()

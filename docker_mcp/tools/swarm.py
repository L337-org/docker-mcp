# library of mcp tools relating to docker swarm

from docker_mcp.server import tool
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.system import _get_client


@tool()
def swarm_init(
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
    host: str | None = None,
) -> str:
    """
    Initialize a new swarm on this Engine.

    args:
        advertise_addr - Externally reachable address advertised to other nodes
        listen_addr - Listen address used for inter-manager communication
        force_new_cluster - Force a new cluster from current state
        default_addr_pool - IP address pools for swarm overlay networks
        subnet_size - Subnet size for the IP pool
        data_path_addr - Address to use for data path traffic
        data_path_port - Port number for data path traffic
        name - Name of the swarm
        labels - User-defined key/value metadata
        autolock_managers - Encrypt manager keys at rest
        log_driver - Default log driver configuration
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
    return _get_client(host).swarm.init(**kwargs)


@tool()
def swarm_join(
    remote_addrs: list,
    join_token: str,
    listen_addr: str = "0.0.0.0:2377",
    advertise_addr: str | None = None,
    data_path_addr: str | None = None,
    host: str | None = None,
) -> bool:
    """
    Join an existing swarm.

    args:
        remote_addrs - Addresses of swarm managers to connect to
        join_token - The swarm join token
        listen_addr - Listen address for inter-manager communication
        advertise_addr - Advertised address
        data_path_addr - Data path address
    returns: bool - True after the engine joins the swarm
    """
    kwargs: dict = {
        "remote_addrs": remote_addrs,
        "join_token": join_token,
        "listen_addr": listen_addr,
        **drop_none(advertise_addr=advertise_addr, data_path_addr=data_path_addr),
    }
    return _get_client(host).swarm.join(**kwargs)


@tool()
def swarm_leave(force: bool = False, host: str | None = None) -> bool:
    """
    Leave the current swarm.

    args: force - Force leave even if the node is a manager
    returns: bool - True after leaving the swarm
    """
    return _get_client(host).swarm.leave(force=force)


@tool()
def swarm_update(
    rotate_worker_token: bool = False,
    rotate_manager_token: bool = False,
    rotate_manager_unlock_key: bool = False,
    host: str | None = None,
) -> bool:
    """
    Update swarm-wide settings; currently exposes token and unlock-key rotation.

    Must be called on a swarm manager node. Token rotation invalidates the old join token
    immediately — nodes that have not yet joined using the old token must use the new one.
    Existing joined nodes are unaffected. Use `swarm_join_tokens` to retrieve the new
    tokens after rotation. Rotating the unlock key requires all managers to be re-unlocked
    on restart with the new key; retrieve it immediately via `swarm_unlock_key`.

    args:
        rotate_worker_token - Issue a new worker join token, invalidating the current one
        rotate_manager_token - Issue a new manager join token, invalidating the current one
        rotate_manager_unlock_key - Issue a new autolock unlock key for manager restart
    returns: bool - True after the update completes
    """
    return _get_client(host).swarm.update(
        rotate_worker_token=rotate_worker_token,
        rotate_manager_token=rotate_manager_token,
        rotate_manager_unlock_key=rotate_manager_unlock_key,
    )


@tool()
def swarm_inspect(host: str | None = None) -> dict:
    """
    Inspect the swarm this daemon belongs to (id, spec, join-token config, CA info).

    returns: dict - The swarm's attrs, as returned by the daemon's swarm inspect endpoint
    """
    swarm = _get_client(host).swarm
    swarm.reload()
    return swarm.attrs


@tool()
def swarm_unlock(key: str, host: str | None = None) -> bool:
    """
    Unlock a manager node that is locked after restart due to autolock being enabled.

    When autolock is enabled (via `swarm_init` or `swarm_update`), manager nodes require
    the unlock key after every restart before they can rejoin the swarm and resume
    scheduling. Must be called on the locked manager node directly. Retrieve the current
    unlock key with `swarm_unlock_key` from any unlocked manager — store it securely
    when enabling autolock. A locked node cannot serve API requests and cannot return its
    own key while locked; other unlocked managers in the swarm can still serve the key.
    Once unlocked the manager resumes automatically.

    args: key - The swarm unlock key (from `swarm_unlock_key`)
    returns: bool - True after the swarm is unlocked
    """
    return _get_client(host).swarm.unlock(key)


@tool()
def swarm_unlock_key(host: str | None = None) -> dict:
    """
    Return the swarm unlock key.

    returns: dict - The unlock key info
    """
    return _get_client(host).swarm.get_unlock_key()


def _read_join_tokens(swarm: object) -> dict:
    """Pull the {Worker, Manager} join tokens out of a (freshly reloaded) swarm's raw attrs."""
    tokens = getattr(swarm, "attrs", {}).get("JoinTokens", {})
    return {"Worker": tokens.get("Worker"), "Manager": tokens.get("Manager")}


@tool()
def swarm_join_tokens(host: str | None = None) -> dict:
    """
    Return the swarm's worker and manager join tokens.

    These are the tokens a new node passes to `swarm_join` — without one, `swarm_join` cannot be
    called, so this closes the init -> join loop. The tokens are secret bearer credentials (anyone
    holding the manager token can join as a manager); treat the result as sensitive and avoid logging
    it. Reads `swarm.attrs["JoinTokens"]` after a reload, so it always reflects the current tokens.

    returns: dict - {"Worker": <worker join token>, "Manager": <manager join token>}
    """
    swarm = _get_client(host).swarm
    swarm.reload()
    return _read_join_tokens(swarm)


@tool()
def swarm_join_token_rotate(rotate_worker: bool = False, rotate_manager: bool = False, host: str | None = None) -> dict:
    """
    Rotate the worker and/or manager join token, then return the fresh tokens.

    Rotating invalidates the old token immediately — nodes that have already joined are unaffected,
    but any pending invitations using the old token will fail. At least one of `rotate_worker` /
    `rotate_manager` must be True. Wraps `swarm.update(rotate_*_token=...)` and re-reads the tokens so
    the caller gets the new value in one step.

    args:
        rotate_worker - Rotate the worker join token
        rotate_manager - Rotate the manager join token
    returns: dict - {"Worker": <worker join token>, "Manager": <manager join token>} after rotation
    """
    if not (rotate_worker or rotate_manager):
        raise ValueError("Set rotate_worker and/or rotate_manager to True — nothing to rotate otherwise.")
    swarm = _get_client(host).swarm
    swarm.update(rotate_worker_token=rotate_worker, rotate_manager_token=rotate_manager)
    swarm.reload()
    return _read_join_tokens(swarm)

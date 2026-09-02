# library of mcp tools relating to docker swarm

from docker_mcp.exceptions import RemoteFailureError
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
    Initialize a new swarm, making this Engine its first manager node.

    Fails if the Engine is already part of a swarm - call `swarm_leave` first to reset it.
    `advertise_addr` only needs setting when the host has multiple network interfaces or is
    behind NAT (otherwise it is auto-detected); it must be reachable by every other node
    that will join. To add more nodes afterwards, retrieve join tokens with
    `swarm_join_tokens` and call `swarm_join` on each one. Set `autolock_managers=True` to
    require the unlock key (`swarm_unlock_key`) on every manager restart - store that key
    securely immediately, since it is only shown once autolock is enabled.

    args:
        advertise_addr - Externally reachable address advertised to other nodes
        listen_addr - Listen address used for inter-manager communication
        force_new_cluster - Force a new single-node cluster from this node's current state
                             (disaster recovery when a majority of managers is lost)
        default_addr_pool - IP address pools for swarm overlay networks
        subnet_size - Subnet size for the IP pool
        data_path_addr - Address to use for data path traffic
        data_path_port - Port number for data path traffic
        name - Name of the swarm
        labels - Labels to set on the swarm
        autolock_managers - Require the unlock key after every manager restart
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
    Join this Engine to an existing swarm as a worker or manager.

    Fails if the Engine is already part of a swarm. Whether this node joins as a worker or
    a manager is determined entirely by which token is passed - `join_token` must be one of
    the two tokens from `swarm_join_tokens`, called against any existing manager. `advertise_addr`
    only needs setting when this host has multiple network interfaces or is behind NAT
    (otherwise it is auto-detected from the interface used to reach `remote_addrs`); it must
    be reachable by every other node in the swarm.

    args:
        remote_addrs - Address(es) of existing swarm managers to connect to
        join_token - The worker or manager join token (from `swarm_join_tokens`) - determines
                     the role this node joins as
        listen_addr - Listen address for inter-manager communication
        advertise_addr - Externally reachable address advertised to other nodes
        data_path_addr - Address to use for data path traffic
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

    The daemon's service tasks are rescheduled to the remaining nodes. A manager refuses to leave
    without force=True, since leaving can break raft quorum. The departed node lingers as "down"
    in `node_list` until a manager runs `node_remove`.

    args: force - Force leave even if the node is a manager
    returns: bool - True after leaving the swarm
    """
    return _get_client(host).swarm.leave(force=force)


@tool()
def swarm_update(
    rotate_worker_token: bool = False,
    rotate_manager_token: bool = False,
    rotate_manager_unlock_key: bool = False,
    updates: dict | None = None,
    host: str | None = None,
) -> bool:
    """
    Update swarm-wide settings: the single home for join-token rotation and cluster spec changes.

    Must be called on a swarm manager node. Token rotation invalidates the old join token
    immediately - nodes that have not yet joined using the old token must use the new one.
    Existing joined nodes are unaffected. Use `swarm_join_tokens` to retrieve the new
    tokens after rotation. Rotating the unlock key requires all managers to be re-unlocked
    on restart with the new key; retrieve it immediately via `swarm_unlock_key`. Rotation and
    `updates` are independent and may be combined in one call; `swarm_init` sets these same
    fields when the swarm is first created, and `swarm_inspect` reads the current values back.

    The Engine replaces the whole cluster spec on every update, so this reads the current spec
    first and resubmits it, merging `updates` over it - omitting `updates` therefore changes
    nothing but the requested rotation.

    args:
        rotate_worker_token - Issue a new worker join token, invalidating the current one
        rotate_manager_token - Issue a new manager join token, invalidating the current one
        rotate_manager_unlock_key - Issue a new autolock unlock key for manager restart
        updates - Engine SwarmSpec fields to change, merged over the current spec one top-level
            key at a time, so a named block is replaced whole rather than field by field: keys are
            "Name", "Labels", "Orchestration", "Raft", "Dispatcher", "CAConfig",
            "EncryptionConfig" and "TaskDefaults", e.g. {"EncryptionConfig": {"AutoLockManagers":
            True}} to turn manager autolock on. Read the current blocks from `swarm_inspect`
    returns: bool - True after the update completes
    """
    client = _get_client(host)
    swarm = client.swarm
    # `POST /swarm/update` replaces the cluster spec outright: the Engine's own note on the handler
    # is "client should provide the complete spec of the swarm, including Name and Labels. If a
    # field is specified with 0 or nil, then the default value will be used", so anything left out
    # is reset. Read the live spec and send it back, which is what `docker swarm update` itself does.
    swarm.reload()
    spec = {**(swarm.attrs.get("Spec") or {}), **(updates or {})}
    # `Version.Index` is the optimistic-concurrency token the endpoint requires, and the reload above
    # is what keeps it current. Taken from the inspect document rather than the `swarm.version`
    # property, which returns this same value but is typed `str | None` by typeshed - contradicting
    # `update_swarm(version: int)` in that same stub set.
    version = (swarm.attrs.get("Version") or {}).get("Index")
    if not isinstance(version, int):
        raise RemoteFailureError(
            "Swarm inspect returned no Version.Index, so the update cannot be version-guarded. "
            "Check `swarm_inspect` - this node may not be a swarm manager."
        )
    # Stays on the low-level `client.api`, and deliberately: the high-level `Swarm.update()` builds
    # the outgoing spec from *docker-py kwargs* via `create_swarm_spec`, so it cannot resubmit the
    # daemon's own spec document. Called with no spec kwargs it sends exactly
    # {"CAConfig": {"NodeCertExpiry": 7776000000000000}} (verified against docker-py 7.2.0) - which,
    # against a replace-semantics endpoint, silently clears manager autolock, cluster labels, the
    # default task log driver and any Raft/Orchestration/Dispatcher tuning. Round-tripping the
    # document through its kwargs would also be lossy (`SwarmSpec` drops the whole `Raft` block
    # unless one of its five values is truthy, and knows nothing of Engine fields added later), so
    # the documented `APIClient.update_swarm` is the only faithful path.
    client.api.update_swarm(
        version=version,
        swarm_spec=spec,
        rotate_worker_token=rotate_worker_token,
        rotate_manager_token=rotate_manager_token,
        rotate_manager_unlock_key=rotate_manager_unlock_key,
    )
    return True


@tool()
def swarm_inspect(host: str | None = None) -> dict:
    """
    Inspect the swarm this daemon belongs to (id, spec, join-token config, CA info).

    Works on a manager node only. Cluster-level configuration - for per-node state use
    `node_list`; for the tokens new nodes need, `swarm_join_tokens`.

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
    unlock key with `swarm_unlock_key` from any unlocked manager - store it securely
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
    Return the swarm's current unlock key.

    The key only serves a purpose when autolock is enabled (see `swarm_init`'s /
    `swarm_update`'s `autolock_managers` / `rotate_manager_unlock_key`). Must be called
    against an unlocked manager - a locked manager cannot serve API requests, including
    this one. Feed the result's key to `swarm_unlock` to unlock a manager after restart.
    Treat the key as a sensitive credential.

    returns: dict - {"UnlockKey": <the current unlock key>}
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

    These are the tokens a new node passes to `swarm_join` - without one, `swarm_join` cannot be
    called, so this closes the init -> join loop. The tokens are secret bearer credentials (anyone
    holding the manager token can join as a manager); treat the result as sensitive and avoid logging
    it. Reads `swarm.attrs["JoinTokens"]` after a reload, so it always reflects the current tokens.

    returns: dict - {"Worker": <worker join token>, "Manager": <manager join token>}
    """
    swarm = _get_client(host).swarm
    swarm.reload()
    return _read_join_tokens(swarm)


# --- cluster-wide task queries ---
#
# Tasks are swarm objects, so these live here and share the `swarm` domain gate: a user who sets
# DOCKER_MCP_SERVER_DISABLE=swarm expects everything named `swarm_*` to go with it. They are the
# one part of this module that is not cluster lifecycle, and they read what `services.py` writes --
# `service_ps` is the per-service view of the same task documents.
#
# Both stay on the low-level `client.api`: docker-py has no task collection at all (there is no
# `client.tasks`), so these documented `APIClient` methods are the only public path.


@tool()
def swarm_task_list(filters: dict | None = None, host: str | None = None) -> list:
    """
    List tasks across the whole swarm, like `docker service ps` with no service to scope it.

    The cluster-wide view of what is actually scheduled. `service_ps` covers one service and
    `stack_ps` one stack, so answering "what is failing anywhere" or "what is running on this node"
    through those means looping over every service; this is one call. Filter by `node` for a node's
    workload (the CLI's `docker node ps`), `desired-state` to separate what should be running from
    what is shutting down, or `service` for a single service -- for which `service_ps` is the
    simpler call. Each task carries its full `Spec`, including the `ContainerSpec` (image, command,
    env), so this returns much more per task than the `service-tasks://{id_or_name}` resource's
    computed rollout summary. Read-only. Requires a swarm manager: on any other node the daemon
    refuses, and its refusal is what comes back.

    args:
        filters - Filter dict; keys: id, name, service, node, label, desired-state
            (running|shutdown|accepted); omit for every task in the cluster
    returns: list - One full task document per task (ID, ServiceID, NodeID, Slot, Spec, Status,
        DesiredState), the same shape `service_ps` returns
    """
    return _get_client(host).api.tasks(filters=filters)


@tool()
def swarm_task_inspect(id_or_name: str, host: str | None = None) -> dict:
    """
    Inspect a single swarm task, like `docker inspect --type task`.

    For when you already hold a task reference -- from a `swarm_task_list` or `service_ps` row, a
    service event, or an error message -- and want just that task. `swarm_task_list` returns the
    same document for every task, so prefer it when scanning; this is the single-object fetch.
    To reach the container behind a running task, read `Status.ContainerStatus.ContainerID` and pass
    it to `container_inspect` / `container_logs` -- but note the container may be on another node,
    where those tools cannot see it, and `service_logs` aggregates across tasks instead. Read-only.
    Requires a swarm manager; reports the daemon's own error if the task does not exist, if a
    prefix matches more than one task, or if this node is not a manager.

    args:
        id_or_name - The task id, an unambiguous id prefix, or the task's full name -- which is the
            container-name form `<service>.<slot>.<taskid>` (`<service>.<nodeid>.<taskid>` for a
            global service), NOT the shorter `<service>.<slot>` that `docker service ps` prints in
            its NAME column, which does not resolve. The daemon tries full id, then full name, then
            prefix, and rejects an ambiguous prefix rather than picking a match
    returns: dict - Full task inspect payload, as `docker inspect --type task`. Carries no name
        field of its own; compose one from `ServiceID`/`Slot` if you need it
    """
    return _get_client(host).api.inspect_task(id_or_name)

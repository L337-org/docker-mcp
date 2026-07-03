# library of mcp tools relating to network management

from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.system import _get_client


@tool()
def network_create(
    name: str,
    driver: str | None = None,
    options: dict | None = None,
    ipam: dict | None = None,
    check_duplicate: bool | None = None,
    internal: bool = False,
    labels: dict | None = None,
    enable_ipv6: bool = False,
    attachable: bool | None = None,
    scope: str | None = None,
    ingress: bool | None = None,
    host: str | None = None,
) -> dict:
    """
    Create a network.

    args:
        name - The name of the network
        driver - Driver name (e.g. bridge, overlay)
        options - Driver-specific options
        ipam - IPAM configuration as a dict
        check_duplicate - Reject creation if a duplicate name exists
        internal - Restrict external access
        labels - Labels to set on the network
        enable_ipv6 - Enable IPv6 networking
        attachable - Allow standalone containers to attach (swarm)
        scope - Network scope (local, global, swarm)
        ingress - Make this an ingress network for swarm routing-mesh
    returns: dict - The created network's attrs
    """
    kwargs: dict = {
        "internal": internal,
        "enable_ipv6": enable_ipv6,
        **drop_none(
            driver=driver,
            options=options,
            ipam=ipam,
            check_duplicate=check_duplicate,
            labels=with_provenance(labels, "network_create"),
            attachable=attachable,
            scope=scope,
            ingress=ingress,
        ),
    }
    return _get_client(host).networks.create(name, **kwargs).attrs


@tool()
def network_inspect(network_id: str, host: str | None = None) -> dict:
    """
    Get a network by id or name.

    args: network_id - The network id or name
    returns: dict - The network's attrs
    """
    return _get_client(host).networks.get(network_id).attrs


@tool()
def network_list(
    names: list | None = None,
    ids: list | None = None,
    filters: dict | None = None,
    greedy: bool = False,
    managed_only: bool = False,
    host: str | None = None,
) -> list:
    """
    List networks.

    args:
        names - Filter by network names
        ids - Filter by network ids
        filters - Additional filters
        greedy - Fetch extended details per network
        managed_only - Only return networks created by this MCP server (filters on the
                             docker-mcp-server.managed label); combines with any `filters` given
    returns: list - A list of network attrs dicts
    """
    if managed_only:
        filters = managed_filter(filters)
    kwargs: dict = {"greedy": greedy, **drop_none(names=names, ids=ids, filters=filters)}
    return [n.attrs for n in _get_client(host).networks.list(**kwargs)]


@tool()
def network_prune(filters: dict | None = None, host: str | None = None) -> dict:
    """
    Remove networks that have no active container endpoints.

    Built-in networks (bridge, host, none) are never removed. Only networks with zero
    connected containers are eligible. Valid filter keys: `until` (RFC3339 timestamp or
    duration — removes networks created before that point), `label` (key or key=value).

    args: filters - Narrow which networks to remove; omit to remove all unused custom networks
    returns: dict - {"NetworksDeleted": [...]}
    """
    return _get_client(host).networks.prune(filters=filters)


@tool()
def network_remove(network_id: str, host: str | None = None) -> bool:
    """
    Remove a network.

    args: network_id - The network id or name
    returns: bool - True after removal
    """
    _get_client(host).networks.get(network_id).remove()
    return True


@tool()
def network_connect(
    network_id: str,
    container: str,
    aliases: list | None = None,
    links: list | None = None,
    ipv4_address: str | None = None,
    ipv6_address: str | None = None,
    link_local_ips: list | None = None,
    driver_opt: dict | None = None,
    host: str | None = None,
) -> bool:
    """
    Attach a running container to an additional network without restarting it.

    Use this to give a container access to services on a network it was not started with.
    `aliases` sets extra DNS names for this container within the network (other containers
    can reach it by those names in addition to its container name). `ipv4_address` /
    `ipv6_address` assign a specific IP on the network; omit to let the driver assign one.
    `links` is a legacy feature (deprecated; prefer DNS aliases). Use `network_disconnect`
    to undo.

    args:
        network_id - Network id or name to connect the container to
        container - Container id or name to attach
        aliases - Additional DNS names for this container within the network
        links - Legacy container links (deprecated)
        ipv4_address - Static IPv4 address to assign on this network
        ipv6_address - Static IPv6 address to assign on this network
        link_local_ips - Link-local IP addresses to assign
        driver_opt - Driver-specific endpoint options
    returns: bool - True after the container is connected
    """
    network = _get_client(host).networks.get(network_id)
    network.connect(
        container,
        aliases=aliases,
        links=links,
        ipv4_address=ipv4_address,
        ipv6_address=ipv6_address,
        link_local_ips=link_local_ips,
        driver_opt=driver_opt,
    )
    return True


@tool()
def network_disconnect(network_id: str, container: str, force: bool = False, host: str | None = None) -> bool:
    """
    Disconnect a container from a network.

    args:
        network_id - The network id or name
        container - The container id or name
        force - Force disconnect
    returns: bool - True after the container is disconnected
    """
    network = _get_client(host).networks.get(network_id)
    network.disconnect(container, force=force)
    return True

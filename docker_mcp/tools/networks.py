# library of mcp tools relating to network management

from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import drop_none
from docker_mcp.tools.client import _get_client


@tool()
def create_network(
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
            labels=with_provenance(labels, "create_network"),
            attachable=attachable,
            scope=scope,
            ingress=ingress,
        ),
    }
    return _get_client().networks.create(name, **kwargs).attrs


@tool()
def get_network(network_id: str) -> dict:
    """
    Get a network by id or name.

    args: network_id - The network id or name
    returns: dict - The network's attrs
    """
    return _get_client().networks.get(network_id).attrs


@tool()
def list_networks(
    names: list | None = None,
    ids: list | None = None,
    filters: dict | None = None,
    greedy: bool = False,
    managed_only: bool = False,
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
    return [n.attrs for n in _get_client().networks.list(**kwargs)]


@tool()
def prune_networks(filters: dict | None = None) -> dict:
    """
    Remove unused networks.

    args: filters - Filters to apply
    returns: dict - Information on deleted networks
    """
    return _get_client().networks.prune(filters=filters)


@tool()
def remove_network(network_id: str) -> bool:
    """
    Remove a network.

    args: network_id - The network id or name
    returns: bool - True after removal
    """
    _get_client().networks.get(network_id).remove()
    return True


@tool()
def connect_network(
    network_id: str,
    container: str,
    aliases: list | None = None,
    links: list | None = None,
    ipv4_address: str | None = None,
    ipv6_address: str | None = None,
    link_local_ips: list | None = None,
    driver_opt: dict | None = None,
) -> bool:
    """
    Connect a container to a network.

    args:
        network_id - The network id or name
        container - The container id or name
        aliases - Endpoint aliases for the container in this network
        links - Links to other containers
        ipv4_address - IPv4 address to assign
        ipv6_address - IPv6 address to assign
        link_local_ips - Link-local addresses
        driver_opt - Network driver options
    returns: bool - True after the container is connected
    """
    network = _get_client().networks.get(network_id)
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
def disconnect_network(network_id: str, container: str, force: bool = False) -> bool:
    """
    Disconnect a container from a network.

    args:
        network_id - The network id or name
        container - The container id or name
        force - Force disconnect
    returns: bool - True after the container is disconnected
    """
    network = _get_client().networks.get(network_id)
    network.disconnect(container, force=force)
    return True

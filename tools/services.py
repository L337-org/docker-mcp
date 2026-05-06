# library of mcp tools relating to swarm service management

from typing import Literal

from server import mcp
from tools._utils import drop_none
from tools.client import _get_client


@mcp.tool()
def create_service(image: str, command: str | list | None = None, extra_kwargs: dict | None = None) -> dict:
    """
    Create a swarm service.

    args:
        image: str - The image for the service
        command: str | list - The command to run in service tasks
        extra_kwargs: dict - Additional ServiceCollection.create kwargs (name, env, mode, etc.)
    returns: dict - The created service's attrs
    """
    kwargs = extra_kwargs or {}
    return _get_client().services.create(image, command=command, **kwargs).attrs


@mcp.tool()
def get_service(service_id: str, insert_defaults: bool | None = None) -> dict:
    """
    Get a swarm service by id or name.

    args:
        service_id: str - The service id or name
        insert_defaults: bool - Merge default values into the output
    returns: dict - The service's attrs
    """
    return _get_client().services.get(service_id, insert_defaults=insert_defaults).attrs


@mcp.tool()
def list_services(filters: dict | None = None) -> list:
    """
    List swarm services.

    args: filters: dict - Filter by attributes (id, name, label, mode)
    returns: list - A list of service attrs dicts
    """
    return [s.attrs for s in _get_client().services.list(**drop_none(filters=filters))]


@mcp.tool()
def update_service(service_id: str, updates: dict) -> bool:
    """
    Update a swarm service's configuration.

    args:
        service_id: str - The service id or name
        updates: dict - Fields to update on the service
    returns: bool - True after the update
    """
    service = _get_client().services.get(service_id)
    service.update(**updates)
    return True


@mcp.tool()
def remove_service(service_id: str) -> bool:
    """
    Stop and remove a swarm service.

    args: service_id: str - The service id or name
    returns: bool - True after the service is removed
    """
    _get_client().services.get(service_id).remove()
    return True


@mcp.tool()
def service_tasks(service_id: str, filters: dict | None = None) -> list:
    """
    List the tasks of a swarm service.

    args:
        service_id: str - The service id or name
        filters: dict - Filter by id, name, node, label, desired-state
    returns: list - A list of task dicts
    """
    service = _get_client().services.get(service_id)
    return service.tasks(filters=filters)


@mcp.tool()
def service_logs(
    service_id: str,
    details: bool = False,
    follow: bool = False,
    stdout: bool = True,
    stderr: bool = True,
    since: int = 0,
    timestamps: bool = False,
    tail: int | Literal["all"] = "all",
) -> str:
    """
    Get the log stream of a swarm service.

    The default `tail="all"` returns the entire log buffer, which can be very large
    on long-running services and may exceed the agent's context. Pass an integer
    (e.g. `tail=500`) to constrain output, or use `since` to bound the time range.

    args:
        service_id: str - The service id or name
        details: bool - Show extra details
        follow: bool - Follow the log stream
        stdout: bool - Include stdout
        stderr: bool - Include stderr
        since: int - Show logs since this Unix timestamp
        timestamps: bool - Include timestamps
        tail: int | "all" - Number of lines from the end, or the literal "all"
    returns: str - Decoded log output
    """
    service = _get_client().services.get(service_id)
    output = service.logs(
        details=details,
        follow=follow,
        stdout=stdout,
        stderr=stderr,
        since=since,
        timestamps=timestamps,
        tail=tail,
    )
    chunks = []
    for chunk in output:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode("utf-8", errors="replace"))
        else:
            chunks.append(str(chunk))
    return "".join(chunks)


@mcp.tool()
def scale_service(service_id: str, replicas: int) -> bool:
    """
    Scale a swarm service to a number of replicas.

    args:
        service_id: str - The service id or name
        replicas: int - The desired number of replicas
    returns: bool - True after scaling
    """
    return _get_client().services.get(service_id).scale(replicas)


@mcp.tool()
def force_update_service(service_id: str) -> bool:
    """
    Force update a swarm service even if its config has not changed.

    args: service_id: str - The service id or name
    returns: bool - True after the force update
    """
    _get_client().services.get(service_id).force_update()
    return True

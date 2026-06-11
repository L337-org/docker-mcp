# library of mcp tools relating to swarm service management

from collections.abc import Iterable
from typing import Literal, cast

from docker_mcp.server import tool
from docker_mcp.tools._utils import MAX_PAYLOAD_BYTES, drop_none, join_bounded
from docker_mcp.tools.client import _get_client


@tool()
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


@tool()
def get_service(service_id: str, insert_defaults: bool | None = None) -> dict:
    """
    Get a swarm service by id or name.

    args:
        service_id: str - The service id or name
        insert_defaults: bool - Merge default values into the output
    returns: dict - The service's attrs
    """
    return _get_client().services.get(service_id, insert_defaults=insert_defaults).attrs


@tool()
def list_services(filters: dict | None = None) -> list:
    """
    List swarm services.

    args: filters: dict - Filter by attributes (id, name, label, mode)
    returns: list - A list of service attrs dicts
    """
    return [s.attrs for s in _get_client().services.list(**drop_none(filters=filters))]


@tool()
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


@tool()
def remove_service(service_id: str) -> bool:
    """
    Stop and remove a swarm service.

    args: service_id: str - The service id or name
    returns: bool - True after the service is removed
    """
    _get_client().services.get(service_id).remove()
    return True


@tool()
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


@tool()
def service_logs(
    service_id: str,
    details: bool = False,
    stdout: bool = True,
    stderr: bool = True,
    since: int = 0,
    timestamps: bool = False,
    tail: int | Literal["all"] = "all",
    max_bytes: int = MAX_PAYLOAD_BYTES,
) -> str:
    """
    Get a bounded snapshot of a swarm service's logs (never follows).

    `follow` is intentionally not exposed: this tool joins the whole stream into one string before
    returning, so following would block forever and grow the buffer without limit. Collection is
    capped at `max_bytes` (raising ValueError if exceeded) so a noisy service can't OOM the server.
    The default `tail="all"` returns the entire buffer, which can be very large on long-running
    services and may exceed the agent's context — pass an integer (e.g. `tail=500`) or use `since`
    to constrain output.

    args:
        service_id: str - The service id or name
        details: bool - Show extra details
        stdout: bool - Include stdout
        stderr: bool - Include stderr
        since: int - Show logs since this Unix timestamp
        timestamps: bool - Include timestamps
        tail: int | "all" - Number of lines from the end, or the literal "all"
        max_bytes: int - Abort with ValueError if the buffered logs exceed this many bytes (default 32 MiB)
    returns: str - Decoded log output
    """
    service = _get_client().services.get(service_id)
    output = service.logs(
        details=details,
        follow=False,
        stdout=stdout,
        stderr=stderr,
        since=since,
        timestamps=timestamps,
        tail=tail,
    )

    def _as_bytes(chunks: Iterable) -> Iterable[bytes]:
        for chunk in chunks:
            yield chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", errors="replace")

    raw = join_bounded(_as_bytes(cast(Iterable, output)), max_bytes, f"logs of service {service_id}")
    return raw.decode("utf-8", errors="replace")


@tool()
def scale_service(service_id: str, replicas: int) -> bool:
    """
    Scale a swarm service to a number of replicas.

    args:
        service_id: str - The service id or name
        replicas: int - The desired number of replicas
    returns: bool - True after scaling
    """
    return _get_client().services.get(service_id).scale(replicas)


@tool()
def force_update_service(service_id: str) -> bool:
    """
    Force update a swarm service even if its config has not changed.

    args: service_id: str - The service id or name
    returns: bool - True after the force update
    """
    _get_client().services.get(service_id).force_update()
    return True


@tool()
def rollback_service(service_id: str) -> dict:
    """
    Roll a swarm service back to its previous spec (the docker `service rollback` equivalent).

    Re-applies the service's `PreviousSpec` — the spec from before the most recent `update_service` /
    `scale_service` / `force_update_service`. Raises ValueError if the service has no PreviousSpec
    (it has never been updated, or was already rolled back). The high-level SDK exposes no rollback,
    so this reads the current version and previous spec via the low-level APIClient and submits them
    with `update_service`.

    args: service_id: str - The service id or name
    returns: dict - The daemon response (a dict with a "Warnings" key)
    """
    api = _get_client().api
    info = api.inspect_service(service_id)
    previous = info.get("PreviousSpec")
    if not previous:
        raise ValueError(
            f"Service {service_id} has no PreviousSpec to roll back to (never updated, or already rolled back)."
        )
    version = info["Version"]["Index"]
    return api.update_service(
        service_id,
        version,
        task_template=previous.get("TaskTemplate"),
        name=previous.get("Name"),
        labels=previous.get("Labels"),
        mode=previous.get("Mode"),
        update_config=previous.get("UpdateConfig"),
        rollback_config=previous.get("RollbackConfig"),
        networks=previous.get("Networks"),
        endpoint_spec=previous.get("EndpointSpec"),
    )

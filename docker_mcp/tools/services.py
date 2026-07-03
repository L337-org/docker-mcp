# library of mcp tools relating to swarm service management

from collections.abc import Iterable
from typing import Literal, cast

from docker_mcp.server import tool
from docker_mcp.tools._labels import managed_filter, with_provenance
from docker_mcp.tools._utils import MAX_PAYLOAD_BYTES, drop_none, join_bounded
from docker_mcp.tools.system import _get_client


@tool()
def service_create(
    image: str, command: str | list | None = None, extra_kwargs: dict | None = None, host: str | None = None
) -> dict:
    """
    Create a Swarm service; requires a swarm manager node.

    Use this instead of `container_run` when you need replicated or global scheduling,
    rolling updates, or automatic restart across the swarm. Common `extra_kwargs` keys:
    `name` (str), `env` (list of "KEY=VAL"), `mode` ({"Replicated": {"Replicas": N}} or
    {"Global": {}}), `networks` (list of network names/ids), `endpoint_spec`
    ({"Ports": [{"PublishedPort": 80, "TargetPort": 8080}]}), `labels` (dict),
    `restart_policy` ({"Condition": "on-failure", "MaxAttempts": 3}),
    `resources` ({"Limits": {"NanoCPUs": 500000000, "MemoryBytes": 134217728}}).

    args:
        image - Image to run service tasks from (e.g. "nginx:alpine")
        command - Override the image's default command; string or list of strings
        extra_kwargs - Additional docker-py ServiceCollection.create keyword arguments
    returns: dict - The created service's attrs
    """
    kwargs = dict(extra_kwargs or {})
    # Stamp the service-level `labels`; leave any caller `container_labels` untouched.
    labels = with_provenance(kwargs.get("labels"), "service_create")
    if labels is not None:
        kwargs["labels"] = labels
    return _get_client(host).services.create(image, command=command, **kwargs).attrs


@tool()
def service_inspect(id_or_name: str, insert_defaults: bool | None = None, host: str | None = None) -> dict:
    """
    Get a swarm service by id or name.

    args:
        id_or_name - The service id or name
        insert_defaults - Merge default values into the output
    returns: dict - The service's attrs
    """
    return _get_client(host).services.get(id_or_name, insert_defaults=insert_defaults).attrs


@tool()
def service_list(filters: dict | None = None, managed_only: bool = False, host: str | None = None) -> list:
    """
    List swarm services.

    args:
        filters - Filter by attributes (id, name, label, mode)
        managed_only - Only return services created by this MCP server (filters on the
                             docker-mcp-server.managed label); combines with any `filters` given
    returns: list - A list of service attrs dicts
    """
    if managed_only:
        filters = managed_filter(filters)
    return [s.attrs for s in _get_client(host).services.list(**drop_none(filters=filters))]


@tool()
def service_update(id_or_name: str, updates: dict | None = None, force: bool = False, host: str | None = None) -> bool:
    """
    Update a swarm service's configuration, or force a redeploy with no spec change.

    Pass exactly one of `updates` (fields to change, same parameters as `service_create`) or
    `force=True` (the `docker service update --force` equivalent: bumps the ForceUpdate counter so
    the service's tasks redeploy with an unchanged spec — e.g. to reschedule after a node change or
    re-pull a mutable tag).

    args:
        id_or_name - The service id or name
        updates - Fields to update on the service; exactly one of updates/force
        force - Redeploy the service without changing its spec; exactly one of updates/force
    returns: bool - True after the update
    """
    if (updates is None) == (not force):
        raise ValueError("Pass exactly one of `updates` (fields to change) or `force=True` (redeploy unchanged).")
    service = _get_client(host).services.get(id_or_name)
    if force:
        service.force_update()
        return True
    service.update(**cast(dict, updates))
    return True


@tool()
def service_remove(id_or_name: str, host: str | None = None) -> bool:
    """
    Stop and remove a swarm service.

    args: id_or_name - The service id or name
    returns: bool - True after the service is removed
    """
    _get_client(host).services.get(id_or_name).remove()
    return True


@tool()
def service_ps(id_or_name: str, filters: dict | None = None, host: str | None = None) -> list:
    """
    List the tasks of a swarm service.

    args:
        id_or_name - The service id or name
        filters - Filter by id, name, node, label, desired-state
    returns: list - A list of task dicts
    """
    service = _get_client(host).services.get(id_or_name)
    return service.tasks(filters=filters)


@tool()
def service_logs(
    id_or_name: str,
    details: bool = False,
    stdout: bool = True,
    stderr: bool = True,
    since: int = 0,
    timestamps: bool = False,
    tail: int | Literal["all"] = 200,
    max_bytes: int = MAX_PAYLOAD_BYTES,
    host: str | None = None,
) -> str:
    """
    Get a bounded snapshot of a swarm service's logs (never follows).

    `follow` is intentionally not exposed: the stream is joined into one string before returning, so
    following would block forever and grow unbounded. Collection is capped at `max_bytes` (ValueError
    if exceeded) so a noisy service can't OOM the server. The default is a bounded `tail=200`;
    `tail="all"` returns the whole buffer, which can be huge on long-running services and exceed
    the agent's context — prefer an integer, or `since`, to constrain output.

    args:
        id_or_name - The service id or name
        details - Show extra details
        stdout - Include stdout
        stderr - Include stderr
        since - Show logs since this Unix timestamp
        timestamps - Include timestamps
        tail - Number of lines from the end (default 200), or the literal "all" for everything
        max_bytes - Abort with ValueError if the buffered logs exceed this many bytes (default 32 MiB)
    returns: str - Decoded log output
    """
    service = _get_client(host).services.get(id_or_name)
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

    raw = join_bounded(_as_bytes(cast(Iterable, output)), max_bytes, f"logs of service {id_or_name}")
    return raw.decode("utf-8", errors="replace")


@tool()
def service_scale(id_or_name: str, replicas: int, host: str | None = None) -> bool:
    """
    Scale a swarm service to a number of replicas.

    args:
        id_or_name - The service id or name
        replicas - The desired number of replicas
    returns: bool - True after scaling
    """
    return _get_client(host).services.get(id_or_name).scale(replicas)


@tool()
def service_rollback(id_or_name: str, host: str | None = None) -> dict:
    """
    Roll a swarm service back to its previous spec (the docker `service rollback` equivalent).

    Re-applies the service's `PreviousSpec` — the spec from before the most recent `service_update` /
    `service_scale`. Raises ValueError if the service has no PreviousSpec
    (it has never been updated, or was already rolled back). The high-level SDK exposes no rollback,
    so this reads the current version and previous spec via the low-level APIClient and submits them
    with the low-level `update_service` API call.

    args: id_or_name - The service id or name
    returns: dict - The daemon response (a dict with a "Warnings" key)
    """
    api = _get_client(host).api
    info = api.inspect_service(id_or_name)
    previous = info.get("PreviousSpec")
    if not previous:
        raise ValueError(
            f"Service {id_or_name} has no PreviousSpec to roll back to (never updated, or already rolled back)."
        )
    version = info["Version"]["Index"]
    # fetch_current_spec=False is the docker-py default, but pass it explicitly: rollback must *replace*
    # the service with PreviousSpec, not merge PreviousSpec over the current spec. With it False the
    # daemon-side base is empty, so fields absent from PreviousSpec are genuinely unset (the intended
    # rollback), not silently carried over from the spec we're rolling away from.
    return api.update_service(
        id_or_name,
        version,
        task_template=previous.get("TaskTemplate"),
        name=previous.get("Name"),
        labels=previous.get("Labels"),
        mode=previous.get("Mode"),
        update_config=previous.get("UpdateConfig"),
        rollback_config=previous.get("RollbackConfig"),
        networks=previous.get("Networks"),
        endpoint_spec=previous.get("EndpointSpec"),
        fetch_current_spec=False,
    )

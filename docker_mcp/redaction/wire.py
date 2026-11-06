"""The wire: applying redaction to every tool result, resource body and error a redacted host produces.

`server.tool()` and `server.resource()` wrap each registered callable with `wrap`, outermost, outside the
failure translation, because that is where an error becomes the text a client sees.  The function the
decorators return is the unwrapped one, so the server never acts on a redacted value itself.

Each call is judged by the host it reaches: a tool by its `host` argument, or the default host when it
names none; a resource by the host in its URI, or the default host for the forms without one.  A call to
a host that is not redacted passes through untouched, even where its output holds a value harvested
from a redacted host, since redacting it would confirm the value is reused there.  Tools and resources
that reach no daemon are classified as such and never redacted; a test checks that every registered one
is classified, so a new one cannot slip through.
"""

import enum
import functools
import inspect
import json
import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from docker_mcp import _hosts
from docker_mcp.exceptions import DockerMcpError
from docker_mcp.redaction import settings
from docker_mcp.redaction.harvest import HostHarvest, credential_values
from docker_mcp.redaction.redactor import Redactor
from docker_mcp.redaction.store import BUDGET_MESSAGE

logger = logging.getLogger("docker_mcp.redaction")

REDACTOR = Redactor()

# Set on every wrapper `wrap` builds, with the scope it was given; tests/test_server.py walks the built
# server and fails any registered tool or resource without it.
REDACTION_SCOPE = "docker_mcp_redaction_scope"


class Scope(enum.Enum):
    """Which host a call's output belongs to, for deciding whether to redact it.

    Attributes:
        HOST_ARGUMENT: the `host` argument, or the default host when the call names none.
        DEFAULT_HOST: always the default host (a resource whose URI names no host).
        NO_DAEMON: the call reaches no daemon and returns no daemon data; never redacted.
    """

    HOST_ARGUMENT = "host_argument"
    DEFAULT_HOST = "default_host"
    NO_DAEMON = "no_daemon"


# Tools with no `host` argument, each because it reaches no daemon: the CLI context registry on this
# machine, registries and Docker Hub over HTTPS, the documentation, and the server's own configuration.
# tests/test_server.py holds this to the registered tools that take no `host`, so adding one means
# deciding here whether its output is daemon data.
NO_DAEMON_TOOLS = frozenset(
    {
        "context_create",
        "context_inspect",
        "context_list",
        "context_remove",
        "context_use",
        "docs_lookup",
        "host_list",
        "hub_rate_limit",
        "hub_repo_info",
        "hub_tags",
        "registry_image_config",
        "registry_manifest",
        "registry_tags",
        "tool_list",
    }
)

# Tools whose in-band output is raw bytes, which cannot be redacted without corrupting the archive.  The
# value is the argument that sends the output to a file instead, or None where there is none.  Under the
# global switch a tool with no such argument is not registered at all; on a redacted host the in-band
# form of each is refused.
BYTE_TOOLS: dict[str, str | None] = {
    "container_archive_get": None,
    "container_export": "dest_path",
    "image_save": "dest_path",
}

# A Compose project's `.env` file larger than this is not read for the harvest.
_MAX_ENV_FILE_BYTES = 1024 * 1024
# Keys under which a result names a container or service, for asking the harvest about ones it has not
# seen.
_ID_KEYS = frozenset({"Id", "ID", "ContainerID", "ServiceID", "Container"})
_ID_VALUE = re.compile(r"[0-9a-f]{12,64}")

_harvests: dict[str, HostHarvest] = {}
_harvests_lock = threading.Lock()


def _client_for(label: str) -> Any:
    # Imported here: docker_mcp.tools imports docker_mcp.server, which imports this module.
    from docker_mcp.tools.system import _get_client

    return _get_client(label)


def _harvest_for(label: str) -> HostHarvest:
    with _harvests_lock:
        harvest = _harvests.get(label)
        if harvest is None:
            harvest = HostHarvest(label, _client_for, REDACTOR.remember)
            _harvests[label] = harvest
        return harvest


def _label_for(scope: Scope, arguments: dict[str, Any]) -> str | None:
    if scope is Scope.NO_DAEMON:
        return None
    named = arguments.get("host") if scope is Scope.HOST_ARGUMENT else None
    if named is None:
        return _hosts.default().label
    # An unknown label is left for the call itself to refuse, with its own message.
    return named if named in _hosts.labels() else None


def _refuse_bytes_in_band(name: str, arguments: dict[str, Any], label: str) -> str | None:
    if name not in BYTE_TOOLS:
        return None
    to_file = BYTE_TOOLS[name]
    if to_file is not None and arguments.get(to_file):
        return None
    alternative = (
        f"pass `{to_file}` to write it to a file on the server host instead"
        if to_file
        else "use `container_archive_get_to_file`, which writes it to a file on the server host"
    )
    return (
        f"{name} cannot return bytes in band on host {label!r}: its output there is redacted, and replacing "
        f"text inside an archive would corrupt it. {alternative[0].upper()}{alternative[1:]}; the file is not "
        "redacted. See REDACTION.md."
    )


def _harvest_compose_env(arguments: dict[str, Any], label: str) -> None:
    directory = arguments.get("project_dir") or os.getcwd()
    env_file = Path(directory) / ".env"
    try:
        if not env_file.is_file() or env_file.stat().st_size > _MAX_ENV_FILE_BYTES:
            return
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.info("redaction harvest: could not read %s: %s", env_file, exc)
        return
    assignments = [line.strip().removeprefix("export ").strip() for line in lines if not line.lstrip().startswith("#")]
    for value in credential_values(assignments):
        REDACTOR.remember(value.strip().strip("'\""), label)


def _mentioned_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _ID_KEYS and isinstance(item, str) and _ID_VALUE.fullmatch(item):
                found.add(item)
            else:
                found |= _mentioned_ids(item)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= _mentioned_ids(item)
    return found


def _redact_result(result: Any, label: str, json_text: bool) -> Any:
    if json_text and isinstance(result, str):
        # A JSON body is redacted as data, so a value holding quotes or newlines is matched as itself
        # rather than in its escaped form, then written back the way the resources write it.
        try:
            parsed = json.loads(result)
        except ValueError:
            return REDACTOR.redact(result, label)
        return json.dumps(REDACTOR.redact(parsed, label), indent=2)
    return REDACTOR.redact(result, label)


def _prepare(  # noqa: DOC501,DOC503 - raises the caller's error_cls, which no Raises section can name
    name: str, scope: Scope, error_cls: type[Exception], arguments: dict[str, Any]
) -> str | None:
    """Everything a redacted call needs before it runs: refusals first, then a fresh harvest.

    Args:
        name: the tool's name, or the resource's URI
        scope: which host the call's output belongs to
        error_cls: the SDK error to refuse with
        arguments: the call's arguments, by name

    Returns:
        str | None: the redacted host's label, or None when the call's output is not redacted
    """
    label = _label_for(scope, arguments)
    if label is None or not settings.host_redacted(label):
        return None
    try:
        if REDACTOR.budget_reached():
            raise error_cls(BUDGET_MESSAGE)
        if (refusal := _refuse_bytes_in_band(name, arguments, label)) is not None:
            raise error_cls(refusal)
        _harvest_for(label).ensure_fresh()
        if name.startswith("compose_"):
            _harvest_compose_env(arguments, label)
    except DockerMcpError as exc:
        # A redaction refusal: turned into the client-visible type here, outside the translation.
        raise error_cls(str(exc)) from None
    return label


def _failed(exc: Exception, label: str, error_cls: type[Exception]) -> Exception:
    # The translated failure is the text the client sees, and can carry daemon or Compose output.  Raised
    # without its cause, which holds the same text unredacted.
    try:
        return error_cls(REDACTOR.redact(str(exc), label))
    except DockerMcpError as refusal:
        return error_cls(str(refusal))


def _finish(result: Any, name: str, label: str, error_cls: type[Exception], json_text: bool) -> Any:
    try:
        if _harvest_for(label).harvest_if_unseen(_mentioned_ids(result)):
            logger.debug("redaction harvest: refreshed for %s on host %r after an unseen ID", name, label)
        return _redact_result(result, label, json_text)
    except DockerMcpError as exc:
        raise error_cls(str(exc)) from None


def wrap[F: Callable[..., Any]](
    func: F, name: str, scope: Scope, error_cls: type[Exception], *, json_text: bool = False
) -> F:
    """Wrap a registered tool or resource so its output and errors are redacted on a redacted host.

    Preserves the signature (the SDK builds the input schema from it) and the sync/async-ness, as the
    other registration wrappers do.  Redaction itself fails closed: any failure in it propagates, so the
    call fails rather than returning unredacted output.

    Args:
        func: the callable to register, already wrapped by the failure translation
        name: the tool's name, or the resource's URI
        scope: which host the call's output belongs to
        error_cls: the SDK error the translation raises (`ToolError` or `ResourceError`)
        json_text: True for a resource whose body is JSON text, which is redacted as data

    Returns:
        F: the wrapped callable, carrying its scope as `REDACTION_SCOPE`
    """
    signature = inspect.signature(func)

    def arguments_of(args: tuple, kwargs: dict) -> dict[str, Any]:
        try:
            return dict(signature.bind_partial(*args, **kwargs).arguments)
        except TypeError:
            return dict(kwargs)

    if inspect.iscoroutinefunction(func):
        import anyio.to_thread

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            arguments = arguments_of(args, kwargs)
            # Harvesting and redacting block, so they run off the event loop.
            label = await anyio.to_thread.run_sync(lambda: _prepare(name, scope, error_cls, arguments))
            if label is None:
                return await func(*args, **kwargs)
            try:
                result = await func(*args, **kwargs)
            except error_cls as exc:
                failure = exc
                raise await anyio.to_thread.run_sync(lambda: _failed(failure, label, error_cls)) from None
            return await anyio.to_thread.run_sync(lambda: _finish(result, name, label, error_cls, json_text))

        async_wrapper.__signature__ = signature  # pyright: ignore[reportAttributeAccessIssue]
        setattr(async_wrapper, REDACTION_SCOPE, scope)
        return cast(F, async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        label = _prepare(name, scope, error_cls, arguments_of(args, kwargs))
        if label is None:
            return func(*args, **kwargs)
        try:
            result = func(*args, **kwargs)
        except error_cls as exc:
            raise _failed(exc, label, error_cls) from None
        return _finish(result, name, label, error_cls, json_text)

    wrapper.__signature__ = signature  # pyright: ignore[reportAttributeAccessIssue]
    setattr(wrapper, REDACTION_SCOPE, scope)
    return cast(F, wrapper)


def disable_core_dumps() -> None:
    """Stop a crash from writing the process's memory, and so the stored values, to disk.

    Called at startup while redaction is on.  POSIX only; elsewhere there is nothing to set, and macOS
    crash reports do not include memory by default.
    """
    try:
        import resource
    except ImportError:
        return
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

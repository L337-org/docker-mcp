"""The harvest: reading credential values directly from where a redacted host's daemon reports them.

A value is harvested when it sits under a credential name (the built-in patterns plus
`DOCKER_MCP_SERVER_REDACT_NAMES`) in a container, image or service environment, in a build argument
recorded in an image's history, or as the content of a Swarm config with a credential-like name.  A
harvested value is redacted wherever it appears, however word-like, which is what the shape rules alone
cannot do.

The harvest must be fresh: a value read before a container was created would not be known when that
container's output arrives.  Rather than a background thread watching each daemon, every call asks the
daemon for the events since the last check, a bounded query that returns at once, and harvests again
if anything that can carry a credential changed.  The wire also asks for a fresh harvest when a result
names a container or service the harvest has not seen.  Image histories are read once per image, since
an image never changes.
"""

import base64
import binascii
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import docker.errors

from docker_mcp.redaction import settings

logger = logging.getLogger("docker_mcp.redaction")

# The events that can bring a new credential into what the daemon reports, as (type, action).  Only these:
# a running container emits a stream of others (exec, attach, start, health checks), and harvesting again
# on each would make every call pay for a full harvest.  An environment is fixed when a container is
# created; an image's when it arrives.
_RELEVANT_EVENTS = frozenset(
    {
        ("container", "create"),
        ("image", "pull"),
        ("image", "load"),
        ("image", "import"),
        ("image", "tag"),
        ("image", "create"),
        ("service", "create"),
        ("service", "update"),
        ("config", "create"),
    }
)

# `|2 NPM_TOKEN=abc BUILD=1 /bin/sh -c ...`: the number of build arguments, then that many assignments.
_HISTORY_BUILD_ARGS = re.compile(r"\|(\d+)\s+(.*)", re.DOTALL)

# Swarm config content larger than this is a configuration file, not a credential value, and is left to
# the shape rules.
_MAX_CONFIG_VALUE = 16 * 1024


def credential_values(assignments: Iterable[str]) -> Iterable[str]:
    """The values of `NAME=value` assignments whose name is a credential name.

    Args:
        assignments: strings of the form `NAME=value`, as Docker reports environments

    Yields:
        str: each non-empty value under a credential name
    """
    names = settings.names()
    for assignment in assignments:
        if not isinstance(assignment, str):
            continue
        name, sep, value = assignment.partition("=")
        if sep and value and names.matches(name):
            yield value


def history_build_args(created_by: str) -> list[str]:
    """The `NAME=value` build arguments a history entry's `CreatedBy` records.

    Args:
        created_by: one history entry's `CreatedBy` text

    Returns:
        list: the assignments, possibly empty
    """
    match = _HISTORY_BUILD_ARGS.search(created_by or "")
    if match is None:
        return []
    count = int(match.group(1))
    tokens = match.group(2).split()
    return [token for token in tokens[:count] if "=" in token]


class HostHarvest:
    """The harvest of one redacted host, kept fresh by the daemon's events.  Thread-safe."""

    def __init__(self, label: str, client_for: Callable[[str], Any], remember: Callable[[str, str], None]) -> None:
        """Start with nothing harvested.

        Args:
            label: the host's label
            client_for: returns the docker-py client for a host label
            remember: stores a harvested value for a host
        """
        self._label = label
        self._client_for = client_for
        self._remember = remember
        self._lock = threading.Lock()
        self._checked_at: int | None = None
        self._known_ids: set[str] = set()
        self._histories_read: set[str] = set()
        self._tolerated: set[str] = set()

    def ensure_fresh(self) -> None:
        """Harvest if nothing has been harvested yet, or if the daemon reports a relevant change since."""
        with self._lock:
            now = int(time.time())
            if self._checked_at is None or self._changed_since(self._checked_at, now):
                self._harvest()
            self._checked_at = now

    def harvest_if_unseen(self, ids: Iterable[str]) -> bool:
        """Harvest again if any of these container or service IDs is not one the harvest has seen.

        Args:
            ids: IDs a result mentions, full or abbreviated

        Returns:
            bool: True if it harvested again
        """
        with self._lock:
            unseen = [i for i in ids if not any(known.startswith(i) for known in self._known_ids)]
            if not unseen:
                return False
            self._harvest()
            self._checked_at = int(time.time())
            return True

    def _changed_since(self, since: int, until: int) -> bool:
        # `until` makes this a bounded query that returns rather than a stream that waits, and it must not
        # be in the future: the daemon holds the query open until that moment arrives, which cost every
        # call a second.  The window starts a second early instead, so an event in the second of the last
        # check is still seen.
        stream = self._client_for(self._label).api.events(since=since - 1, until=until, decode=True)
        try:
            return any((event.get("Type"), event.get("Action")) in _RELEVANT_EVENTS for event in stream)
        finally:
            stream.close()

    def _harvest(self) -> None:
        client = self._client_for(self._label)
        known: set[str] = set()
        for container in client.containers.list(all=True):
            known.add(container.id)
            self._store(credential_values((container.attrs.get("Config") or {}).get("Env") or []))
        for image in client.images.list():
            self._store(credential_values((image.attrs.get("Config") or {}).get("Env") or []))
            if image.id not in self._histories_read:
                for entry in image.history():
                    self._store(credential_values(history_build_args(entry.get("CreatedBy") or "")))
                self._histories_read.add(image.id)
        known |= self._harvest_swarm(client)
        self._known_ids = known

    def _harvest_swarm(self, client: Any) -> set[str]:
        # Services and configs exist only on a Swarm manager; elsewhere the daemon refuses the call, which
        # is expected rather than a fault, and is logged once per host.
        known: set[str] = set()
        try:
            for service in client.services.list():
                known.add(service.id)
                spec = ((service.attrs.get("Spec") or {}).get("TaskTemplate") or {}).get("ContainerSpec") or {}
                self._store(credential_values(spec.get("Env") or []))
            names = settings.names()
            for config in client.configs.list():
                name = (config.attrs.get("Spec") or {}).get("Name") or ""
                if names.matches(name):
                    self._store_config(client.configs.get(config.id).attrs)
        except docker.errors.APIError as exc:
            if self._label not in self._tolerated:
                self._tolerated.add(self._label)
                logger.info("redaction harvest: no Swarm services or configs read on host %r: %s", self._label, exc)
        return known

    def _store_config(self, attrs: dict) -> None:
        data = (attrs.get("Spec") or {}).get("Data") or ""
        try:
            text = base64.b64decode(data).decode("utf-8")
        except binascii.Error, UnicodeDecodeError:
            return  # binary content is left to the shape rules
        if text and len(text) <= _MAX_CONFIG_VALUE:
            self._store([text])

    def _store(self, values: Iterable[str]) -> None:
        for value in values:
            self._remember(value, self._label)

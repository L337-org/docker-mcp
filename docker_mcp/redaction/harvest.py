"""The harvest: reading credential values directly from where a redacted host's daemon reports them.

A value is harvested when it sits under a credential name (the built-in patterns plus
`DOCKER_MCP_SERVER_REDACT_NAMES`) in a container, image or service environment, in a build argument
recorded in an image's history, or as the content of a Swarm config with a credential-like name.  A
harvested value is redacted wherever it appears, however word-like, which is what the shape rules alone
cannot do.

The harvest must be fresh: a value read before a container was created would not be known when that
container's output arrives.  Rather than a background thread watching each daemon, every call takes a
snapshot of the IDs that can carry a credential (containers, images, service versions and configs), a
few cheap list requests, and harvests again when it differs from the last.  The daemon's event history
cannot do this job: it keeps only the most recent events, so on a busy host a `create` can fall out of
it between two calls, and it filters by the daemon's clock, which need not agree with this one.  Image
histories are read once per image, since an image never changes.
"""

import base64
import binascii
import logging
import re
import threading
from collections.abc import Callable, Iterable
from typing import Any

import docker.errors

from docker_mcp.redaction import settings

logger = logging.getLogger("docker_mcp.redaction")

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
    """The harvest of one redacted host, kept fresh by comparing ID snapshots.  Thread-safe."""

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
        self._snapshot: frozenset[tuple[str, ...]] | None = None
        self._known_ids: frozenset[str] = frozenset()
        self._checked_ids: set[str] = set()
        self._histories_read: set[str] = set()
        self._swarm_refusal_logged = False

    def ensure_fresh(self) -> None:
        """Harvest if nothing has been harvested yet, or if the daemon's IDs differ from the last snapshot.

        Raises whatever the daemon client raises: the caller decides what a failed harvest means.
        """
        with self._lock:
            self._refresh()

    def harvest_if_unseen(self, ids: Iterable[str]) -> bool:
        """Check freshness again if any of these IDs is neither a known container or service nor already checked.

        A result can name a container created during the call itself, after the snapshot taken before
        it.  Network, plugin and removed containers' IDs look the same, so each ID is checked once: a
        snapshot comparison rather than a harvest, which runs only if the daemon really changed.

        Args:
            ids: IDs a result mentions, full or abbreviated

        Returns:
            bool: True if it harvested again
        """
        with self._lock:
            unseen = [
                i
                for i in ids
                if i not in self._checked_ids and not any(known.startswith(i) for known in self._known_ids)
            ]
            if not unseen:
                return False
            self._checked_ids.update(unseen)
            return self._refresh()

    def _refresh(self) -> bool:
        client = self._client_for(self._label)
        # Taken before harvesting: a container created between the two is then harvested now and seen as
        # a change next time, rather than counted as seen without being harvested.
        snapshot = self._take_snapshot(client)
        if snapshot == self._snapshot:
            return False
        self._harvest(client)
        self._snapshot = snapshot
        self._known_ids = frozenset(entry[1] for entry in snapshot if entry[0] in ("container", "service"))
        return True

    def _take_snapshot(self, client: Any) -> frozenset[tuple[str, ...]]:
        api = client.api
        entries: set[tuple[str, ...]] = {("container", c["Id"]) for c in api.containers(all=True, quiet=True)}
        entries |= {("image", image_id) for image_id in api.images(quiet=True)}
        try:
            # A service's environment can change under the same ID, so its version is part of the entry.
            entries |= {("service", s["ID"], str((s.get("Version") or {}).get("Index"))) for s in api.services()}
            entries |= {("config", c["ID"]) for c in api.configs()}
        except docker.errors.APIError as exc:
            self._log_swarm_refusal(exc)
        return frozenset(entries)

    def _log_swarm_refusal(self, exc: Exception) -> None:
        # Services and configs exist only on a Swarm manager; elsewhere the daemon refuses the call, which
        # is expected rather than a fault, and is logged once per host.
        if not self._swarm_refusal_logged:
            self._swarm_refusal_logged = True
            logger.info("redaction harvest: no Swarm services or configs read on host %r: %s", self._label, exc)

    def _harvest(self, client: Any) -> None:
        # A container or image removed while this runs is skipped rather than failing the harvest: what is
        # gone can no longer produce output.
        for container in client.containers.list(all=True, ignore_removed=True):
            self._store(credential_values((container.attrs.get("Config") or {}).get("Env") or []))
        for image_id in client.api.images(quiet=True):
            try:
                image = client.images.get(image_id)
                self._store(credential_values((image.attrs.get("Config") or {}).get("Env") or []))
                if image.id not in self._histories_read:
                    for entry in image.history():
                        self._store(credential_values(history_build_args(entry.get("CreatedBy") or "")))
                    self._histories_read.add(image.id)
            except docker.errors.NotFound:
                continue
        self._harvest_swarm(client)

    def _harvest_swarm(self, client: Any) -> None:
        try:
            for service in client.services.list():
                spec = ((service.attrs.get("Spec") or {}).get("TaskTemplate") or {}).get("ContainerSpec") or {}
                self._store(credential_values(spec.get("Env") or []))
            names = settings.names()
            for config in client.configs.list():
                name = (config.attrs.get("Spec") or {}).get("Name") or ""
                if names.matches(name):
                    try:
                        self._store_config(client.configs.get(config.id).attrs)
                    except docker.errors.NotFound:
                        continue
        except docker.errors.APIError as exc:
            self._log_swarm_refusal(exc)

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

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

from docker_mcp.redaction import rules, settings

logger = logging.getLogger("docker_mcp.redaction")

# `|2 NPM_TOKEN=abc BUILD=1 /bin/sh -c ...`: the number of build arguments, then that many assignments.
# A `RUN` history entry with build arguments starts its arguments with `|N `, then lists the N of them as
# `NAME=value` joined by single spaces, unquoted, before the command (`RUN |2 A=x y B=z /bin/sh -c ...`).
_RUN_BUILD_ARGS = re.compile(r"\|(\d+) ")
_ARGUMENT_NAME = re.compile(r"(?:^| )([A-Za-z_][A-Za-z0-9_]*)=")

# The most result IDs remembered as checked between snapshot changes.  They are external input, so the set
# is bounded; forgetting them costs only a repeated snapshot comparison.
_MAX_CHECKED_IDS = 10_000

# Swarm config content larger than this is a configuration file, not a credential value, and is left to
# the shape rules.
_MAX_CONFIG_VALUE = 16 * 1024


def credential_values(assignments: Iterable[str]) -> Iterable[str]:
    """The values of `NAME=value` assignments whose name is a credential name.

    A value that is an absolute path is skipped: under a credential name it says where a credential is
    (`SSL_CERT_FILE`, `POSTGRES_PASSWORD_FILE=/run/secrets/db`), it is not one, and a harvested value is
    replaced wherever it appears, so the path would be lost from mounts, commands and logs too.

    Args:
        assignments: strings of the form `NAME=value`, as Docker reports environments

    Yields:
        str: each non-empty value under a credential name that is not an absolute path
    """
    names = settings.names()
    for assignment in assignments:
        if not isinstance(assignment, str):
            continue
        name, sep, value = assignment.partition("=")
        if sep and value and names.matches(name) and not rules.is_absolute_path(value):
            yield value


def history_build_args(created_by: Iterable[str]) -> list[str]:
    """The `NAME=value` build arguments an image's history records, values with spaces kept whole.

    A value may hold spaces, and a `RUN` entry joins the arguments with single spaces, unquoted.  Every
    value but the last ends where the next `NAME=` begins.  The last runs straight into the command, so
    it is read from the image's own `ARG` entry instead (`ARG PASSPHRASE=correct horse battery staple`),
    where it ends at the end of the line or where another argument the `RUN` entries name begins
    (`ARG A=x y B`).  A last argument with no `ARG` entry falls back to its first word.  A value holding
    ` NAME=` itself is misread; accepted, as the format gives no way to tell.

    Args:
        created_by: every history entry's `CreatedBy` text, for one image

    Returns:
        list: the assignments, possibly empty
    """
    entries = [text or "" for text in created_by]
    values: dict[str, str] = {}
    names: set[str] = set()
    last: dict[str, str] = {}
    for text in entries:
        match = _RUN_BUILD_ARGS.search(text)
        if match is None:
            continue
        rest = text[match.end() :]
        found = list(_ARGUMENT_NAME.finditer(rest))[: int(match.group(1))]
        names.update(argument.group(1) for argument in found)
        for argument, following in zip(found, found[1:], strict=False):
            values.setdefault(argument.group(1), rest[argument.end() : following.start()])
        if found:
            final = found[-1]
            last.setdefault(final.group(1), rest[final.end() :].split(" ", 1)[0])
    for name, first_word in last.items():
        if name in values:
            continue
        # BuildKit's `ARG` entry carries the value passed, as the `RUN` entry does (checked against a real
        # build, with and without `--build-arg`).  It is trusted only where it begins as the `RUN` entry's
        # value does, so a builder that recorded a declaration instead falls back to the first word.
        whole = _arg_entry_value(entries, name, names - {name})
        values[name] = whole if whole and whole.split(" ", 1)[0] == first_word else first_word
    return [f"{name}={value}" for name, value in values.items() if value]


def _arg_entry_value(entries: list[str], name: str, others: set[str]) -> str | None:
    for text in entries:
        if not text.startswith("ARG "):
            continue
        match = re.search(rf"(?:^| ){re.escape(name)}=", text[4:])
        if match is None:
            continue
        tail = text[4:][match.end() :]
        stops = [
            other.start()
            for other_name in others
            if (other := re.search(rf" {re.escape(other_name)}(?:=| |$)", tail)) is not None
        ]
        return tail[: min(stops, default=len(tail))]
    return None


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
        # The snapshot entries already harvested, so a change reads only what is new.
        self._harvested: frozenset[tuple[str, ...]] = frozenset()
        self._known_ids: frozenset[str] = frozenset()
        # The known IDs by their first 12 characters, the shortest form a result abbreviates one to, so an
        # abbreviated ID is matched without scanning every known one.
        self._known_by_prefix: dict[str, list[str]] = {}
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
            ids: IDs a result mentions, full or abbreviated to at least 12 characters, as Docker abbreviates them

        Returns:
            bool: True if it harvested again
        """
        with self._lock:
            unseen = [i for i in ids if i not in self._checked_ids and not self._is_known(i)]
            if not unseen:
                return False
            if len(self._checked_ids) + len(unseen) > _MAX_CHECKED_IDS:
                self._checked_ids.clear()
            # One result can name more IDs than the bound; those beyond it are simply checked again later.
            self._checked_ids.update(unseen[:_MAX_CHECKED_IDS])
            return self._refresh()

    def _is_known(self, mentioned: str) -> bool:
        if mentioned in self._known_ids:
            return True
        return any(known.startswith(mentioned) for known in self._known_by_prefix.get(mentioned[:12], ()))

    def _refresh(self) -> bool:
        client = self._client_for(self._label)
        # Taken before harvesting: a container created between the two is then harvested now and seen as
        # a change next time, rather than counted as seen without being harvested.
        snapshot = self._take_snapshot(client)
        if snapshot == self._snapshot:
            return False
        # Only what the snapshot shows to be new is read: a container's or image's environment cannot change
        # under the same ID, and a service's entry carries its version, so an update is a new entry.  A
        # full read on every change cost seconds a call on a large daemon.
        self._harvest(client, snapshot - self._harvested)
        self._harvested = snapshot
        self._snapshot = snapshot
        self._known_ids = frozenset(entry[1] for entry in snapshot if entry[0] in ("container", "service"))
        self._known_by_prefix = {}
        for known in self._known_ids:
            self._known_by_prefix.setdefault(known[:12], []).append(known)
        # Each checked ID is now either known or still not a container or service; a later change to the
        # daemon shows in the snapshot whatever names it.
        self._checked_ids.clear()
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

    def _harvest(self, client: Any, entries: Iterable[tuple[str, ...]]) -> None:
        # An object removed since the snapshot is skipped rather than failing the harvest: what is gone can
        # no longer produce output.
        for kind, object_id, *_ in sorted(entries):
            try:
                if kind == "container":
                    container = client.containers.get(object_id)
                    self._store(credential_values((container.attrs.get("Config") or {}).get("Env") or []))
                elif kind == "image":
                    self._harvest_image(client.images.get(object_id))
                else:
                    self._harvest_swarm_object(client, kind, object_id)
            except docker.errors.NotFound:
                continue

    def _harvest_image(self, image: Any) -> None:
        self._store(credential_values((image.attrs.get("Config") or {}).get("Env") or []))
        if image.id not in self._histories_read:
            history = [entry.get("CreatedBy") or "" for entry in image.history()]
            self._store(credential_values(history_build_args(history)))
            self._histories_read.add(image.id)

    def _harvest_swarm_object(self, client: Any, kind: str, object_id: str) -> None:
        # Any failure but NotFound propagates, as a container's does, so the snapshot is not marked read and
        # the next call reads the object again; a node that is not a manager lists no services or configs in
        # the snapshot, so there is no refusal to tolerate here.
        if kind == "service":
            attrs = client.services.get(object_id).attrs
            spec = ((attrs.get("Spec") or {}).get("TaskTemplate") or {}).get("ContainerSpec") or {}
            self._store(credential_values(spec.get("Env") or []))
        elif kind == "config":
            attrs = client.configs.get(object_id).attrs
            if settings.names().matches((attrs.get("Spec") or {}).get("Name") or ""):
                self._store_config(attrs)

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

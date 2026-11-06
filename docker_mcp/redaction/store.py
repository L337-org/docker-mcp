"""The one store of values the redactor replaces, with its limits.

Each distinct value is held once per host, with its tier: harvested (read directly from where the
daemon reports it, or named by the names setting) or discovered (found by a shape rule).  Matching and
swapping back both use this store; the matcher's index is derived from it, not a second copy.  It
lives in memory only, is never written anywhere, and nothing is ever evicted, so a marker stays good
for the life of the process.

Every limit is a named constant here, with its reason, so it can be changed in one edit; they are
deliberately not settings.  `architecture/redaction.md` describes what happens at each.
"""

import enum
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from docker_mcp.exceptions import RedactionBudgetError
from docker_mcp.redaction import forms

logger = logging.getLogger("docker_mcp.redaction")

# --- limits ----------------------------------------------------------------------------------------

# The shape rules target credential-sized values, not data.  A service-account JSON is about 3 KB in
# base64 and a kubeconfig with embedded certificates 7 to 11 KB; a longer run is more likely an archive
# or a compressed file, and replacing it whole would cost the agent the data for the chance it holds a
# credential.  Harvested values have no such limit: they are known exactly.
MAX_DISCOVERED_MATCH_LENGTH = 16 * 1024

# Discovered values stored, by count and by bytes (each value plus its matcher forms).  A flood of
# one-off random strings in logs must not grow the store without bound, and must never push out or
# block a harvested value, so discovered values have limits of their own; past them a new one is
# still redacted where a rule finds it, but not stored.
MAX_DISCOVERED_VALUES = 100_000
MAX_DISCOVERED_BYTES = 64 * 1024 * 1024

# Both kinds together, forms included.  A harvested value cannot be turned away - the matcher is built
# from the store, so an unstored harvested value would not be redacted at all - so only an abnormal
# harvest reaches this, and then every call on a redacted host is refused until a restart.
OVERALL_BUDGET_BYTES = 256 * 1024 * 1024

# Identifiers are four bytes.  The last is reserved for a value that was redacted but not stored, so
# its marker looks like any other and simply has no entry behind it.
UNSTORED_VALUE_ID = 0xFFFFFFFF
_MAX_VALUE_ID = UNSTORED_VALUE_ID - 1


class Tier(enum.Enum):
    """How a value came to be stored.

    Attributes:
        HARVESTED: read directly from where the daemon reports it; always stored.
        DISCOVERED: found by a shape rule; stored within its own limits.
    """

    HARVESTED = "harvested"
    DISCOVERED = "discovered"


class Secret:
    """A stored value whose text form never shows it.

    A traceback that shows local variables, an exception message or a debug line that logs an object
    prints the placeholder, never the credential.  `reveal()` is the one way to the value.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Hold a value.

        Args:
            value: the credential
        """
        self._value = value

    def reveal(self) -> str:
        """The value itself.

        Returns:
            str: the credential
        """
        return self._value

    def __repr__(self) -> str:
        """The placeholder, never the value.

        Returns:
            str: the placeholder
        """
        return "<redacted value>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        """The placeholder, whatever format is asked for.

        Args:
            spec: the format specification, ignored

        Returns:
            str: the placeholder
        """
        del spec
        return "<redacted value>"

    def __reduce__(self) -> tuple:  # noqa: DOC201 - it never returns
        """Refuse to be pickled, which would copy the value out of this process's memory.

        Raises:
            TypeError: always
        """
        raise TypeError("a redacted value cannot be serialised")


@dataclass(frozen=True)
class Entry:
    """One stored value.

    Attributes:
        value_id: its identifier, never reused.
        host_index: the host it was stored for; markers for it are swapped back only there.
        tier: how it came to be stored.
        secret: the value.
        cost: bytes it counts against the limits: the value and its matcher forms.
    """

    value_id: int
    host_index: int
    tier: Tier
    secret: Secret
    cost: int

    def __repr__(self) -> str:
        """The entry's identifiers, never its value.

        Returns:
            str: identifiers and tier only
        """
        return f"Entry(value_id={self.value_id}, host_index={self.host_index}, tier={self.tier.value})"


class IdentifiersExhaustedError(Exception):
    """Every value identifier has been issued; the redactor rotates the key and empties the store."""


BUDGET_MESSAGE = (
    "the credential store used by redaction is full, so calls to redacted hosts are refused; restart the "
    "server to clear it (every marker issued so far will then stop working)"
)


@dataclass
class _Usage:
    values: int = 0
    bytes: int = 0


class ValueStore:
    """The values, keyed by host and text, with their limits.  Thread-safe."""

    def __init__(self, on_add: Callable[[Entry, dict[str, int]], None] | None = None) -> None:
        """Start empty.

        Args:
            on_add: called with each new entry and its matcher forms, outside the store's lock
        """
        self._lock = threading.RLock()
        self._by_key: dict[tuple[int, str], Entry] = {}
        self._by_id: dict[int, Entry] = {}
        self._next_id = 0
        self._usage = {Tier.HARVESTED: _Usage(), Tier.DISCOVERED: _Usage()}
        self._host_usage: dict[int, dict[Tier, _Usage]] = {}
        self._budget_reached = False
        self._warned_hosts: set[int] = set()
        # Values stored but still being indexed, so a concurrent add of the same value can wait for them.
        self._indexing: dict[tuple[int, str], threading.Event] = {}
        self._on_add = on_add

    def add(  # noqa: DOC503 - raised by the helper that stores, re-raised after indexing fails
        self, value: str, host_index: int, tier: Tier, *, host_label: str = ""
    ) -> Entry | None:
        """Store a value for a host, or return the entry already holding it.

        A discovered value later harvested becomes harvested.  A discovered value past its kind's
        limits is not stored and None is returned, so the caller issues an unstored marker; this is
        logged once at WARNING per host.

        Args:
            value: the value
            host_index: the host it is stored for
            tier: how it was found
            host_label: the host's label, for the log line only

        Returns:
            Entry | None: the entry, or None for a discovered value that was not stored

        Raises:
            RedactionBudgetError: storing a harvested value would pass the overall budget, or already did
            IdentifiersExhaustedError: every identifier has been issued
        """
        if not value:
            return None
        key = (host_index, value)
        # One lock block both checks for the value and stores it: a gap between the two lets a concurrent
        # call store the same value a second time.
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                result = (
                    self._promote(existing) if existing.tier is Tier.DISCOVERED and tier is Tier.HARVESTED else existing
                )
                pending = self._indexing.get(key)
            else:
                stored = self._store_new(key, value, host_index, tier, host_label)
        if existing is not None:
            # Another call stored this value and is still indexing it.  Returning now would let this call
            # redact against an index that does not hold it yet, and show it in clear.
            if pending is None:
                return result
            pending.wait()
            with self._lock:
                still_stored = self._by_key.get(key)
            # If the other call's indexing failed it removed the value again; store it afresh here rather
            # than report a value that is neither stored nor indexed.
            return (
                still_stored if still_stored is not None else self.add(value, host_index, tier, host_label=host_label)
            )
        if stored is None:
            return None
        entry, value_forms, indexed = stored
        try:
            if self._on_add is not None:
                # Outside the store's lock: indexing can take a while near the limits, and must not hold up
                # every other lookup.  A concurrent add of the same value waits on `indexed` instead.
                self._on_add(entry, value_forms)
        except BaseException:
            # Not indexed means not redacted, so the value must not look stored: the next add retries.
            with self._lock:
                self._by_key.pop(key, None)
                # A concurrent add may have promoted it meanwhile; undo the accounting of what is stored now.
                current = self._by_id.pop(entry.value_id, entry)
                self._account(current, -1)
            raise
        finally:
            with self._lock:
                self._indexing.pop(key, None)
            indexed.set()
        return entry

    def _store_new(
        self, key: tuple[int, str], value: str, host_index: int, tier: Tier, host_label: str
    ) -> tuple[Entry, dict[str, int], threading.Event] | None:
        # Called with the lock held.  None for a discovered value past its limits.
        if self._budget_reached:
            raise RedactionBudgetError(BUDGET_MESSAGE)
        value_forms = forms.forms_of(value)
        cost = sum(len(text.encode("utf-8", errors="surrogateescape")) for text in value_forms)
        total = self._usage[Tier.HARVESTED].bytes + self._usage[Tier.DISCOVERED].bytes
        if tier is Tier.DISCOVERED:
            discovered = self._usage[Tier.DISCOVERED]
            if (
                len(value) > MAX_DISCOVERED_MATCH_LENGTH
                or discovered.values >= MAX_DISCOVERED_VALUES
                or discovered.bytes + cost > MAX_DISCOVERED_BYTES
                or total + cost > OVERALL_BUDGET_BYTES
            ):
                self._warn_discovered_full(host_index, host_label)
                return None
        elif total + cost > OVERALL_BUDGET_BYTES:
            self._budget_reached = True
            logger.error(
                "credential store reached its overall budget of %d bytes (harvested: %d values, %d bytes; "
                "discovered: %d values, %d bytes); every call on a redacted host is refused until restart",
                OVERALL_BUDGET_BYTES,
                self._usage[Tier.HARVESTED].values,
                self._usage[Tier.HARVESTED].bytes,
                self._usage[Tier.DISCOVERED].values,
                self._usage[Tier.DISCOVERED].bytes,
            )
            raise RedactionBudgetError(BUDGET_MESSAGE)
        if self._next_id > _MAX_VALUE_ID:
            raise IdentifiersExhaustedError
        entry = Entry(self._next_id, host_index, tier, Secret(value), cost)
        self._next_id += 1
        self._by_key[key] = entry
        self._by_id[entry.value_id] = entry
        self._account(entry, +1)
        indexed = threading.Event()
        self._indexing[key] = indexed
        return entry, value_forms, indexed

    def _promote(self, entry: Entry) -> Entry:
        promoted = Entry(entry.value_id, entry.host_index, Tier.HARVESTED, entry.secret, entry.cost)
        self._account(entry, -1)
        self._by_key[(entry.host_index, entry.secret.reveal())] = promoted
        self._by_id[entry.value_id] = promoted
        self._account(promoted, +1)
        return promoted

    def _account(self, entry: Entry, sign: int) -> None:
        for usage in (self._usage[entry.tier], self._host_usage_for(entry.host_index)[entry.tier]):
            usage.values += sign
            usage.bytes += sign * entry.cost

    def _host_usage_for(self, host_index: int) -> dict[Tier, _Usage]:
        return self._host_usage.setdefault(host_index, {Tier.HARVESTED: _Usage(), Tier.DISCOVERED: _Usage()})

    def _warn_discovered_full(self, host_index: int, host_label: str) -> None:
        if host_index in self._warned_hosts:
            return
        self._warned_hosts.add(host_index)
        logger.warning(
            "credential store: the limit on discovered values is reached on host %r; new ones found by a "
            "rule are still redacted but not stored, so their other forms are not matched and their "
            "markers cannot be used in writes",
            host_label or host_index,
        )

    def get(self, value_id: int) -> Entry | None:
        """The entry with this identifier.

        Args:
            value_id: an identifier from a marker

        Returns:
            Entry | None: the entry, or None when nothing holds that identifier
        """
        with self._lock:
            return self._by_id.get(value_id)

    def get_for(self, value: str, host_index: int) -> Entry | None:
        """The entry holding this value for this host, if any.

        Args:
            value: the value
            host_index: the host

        Returns:
            Entry | None: the entry, or None
        """
        with self._lock:
            return self._by_key.get((host_index, value))

    def entries(self) -> list[Entry]:
        """Every entry, in identifier order.

        Returns:
            list: the entries
        """
        with self._lock:
            return list(self._by_id.values())

    @property
    def budget_reached(self) -> bool:
        """Whether the overall budget has been reached.

        Returns:
            bool: True once a harvested value would have passed it
        """
        return self._budget_reached

    def report(self, host_index: int) -> dict:
        """The store's state for one host, without any value.

        Args:
            host_index: the host

        Returns:
            dict: entries and bytes for each kind, and whether the overall budget is reached
        """
        with self._lock:
            usage = self._host_usage_for(host_index)
            return {
                "harvested_values": usage[Tier.HARVESTED].values,
                "harvested_bytes": usage[Tier.HARVESTED].bytes,
                "discovered_values": usage[Tier.DISCOVERED].values,
                "discovered_bytes": usage[Tier.DISCOVERED].bytes,
                "budget_reached": self._budget_reached,
            }

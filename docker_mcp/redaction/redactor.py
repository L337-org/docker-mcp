"""The redactor: replaces credentials in what a redacted host returns, and reads markers back.

`redact` walks a tool result or resource body - dict keys and values, lists, strings - and replaces
every stored value's forms and everything a shape rule finds with a marker.  `remember` stores a value
read directly from the daemon (the harvest, wired in by the change that redacts output).  `resolve`
turns a marker back into the text it replaced, for swapping back in writes, or says why it cannot.

Everything fails closed: an exception here propagates, and the caller returns the error rather than
the unredacted output.
"""

import enum
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from docker_mcp import _hosts
from docker_mcp.redaction import forms, rules
from docker_mcp.redaction.markers import MARKER_PATTERN, CounterExhaustedError, MarkerCodec, MarkerFault
from docker_mcp.redaction.matcher import Matcher
from docker_mcp.redaction.store import (
    UNSTORED_VALUE_ID,
    BudgetReachedError,
    Entry,
    IdentifiersExhaustedError,
    Tier,
    ValueStore,
)


class StateResetError(Exception):
    """The redactor rotated its key and emptied its store partway through a call.

    Raised rather than finishing the call against the empty store, where a harvested value the rules
    cannot see would come back unredacted.  The call is refused; the next one re-reads the values.
    """


class Refusal(enum.Enum):
    """Why a marker cannot be swapped back on the host a call targets.

    Attributes:
        MALFORMED: not a marker this server could have written.
        NOT_THIS_PROCESS: altered, or issued before the server last started.
        OTHER_HOST: issued for a different host.
        UNSTORED: issued for a value that was redacted but not kept, because the store was full.
    """

    MALFORMED = "malformed"
    NOT_THIS_PROCESS = "not_this_process"
    OTHER_HOST = "other_host"
    UNSTORED = "unstored"


@dataclass(frozen=True)
class _Candidate:
    start: int
    end: int
    known: Entry | None
    form_id: int


class _State:
    """One generation of the redactor's state: a key, a store and the index built from it."""

    def __init__(self) -> None:
        self.codec = MarkerCodec()
        self.matcher = Matcher()
        self.store = ValueStore(on_add=lambda entry, value_forms: self.matcher.add(value_forms, entry.value_id))


class Redactor:
    """Holds the redaction state and applies it.  Thread-safe; one per process."""

    def __init__(self, host_labels: Callable[[], list[str]] = _hosts.labels) -> None:
        """Start with an empty store and a fresh key.

        Args:
            host_labels: the configured host labels in order; a host's index is its position here
        """
        self._lock = threading.Lock()
        self._state = _State()
        self._host_labels = host_labels

    # --- hosts ------------------------------------------------------------------------------------

    def _host_index(self, host: str) -> int:
        return self._host_labels().index(host)

    def _host_label(self, index: int) -> str:
        labels = self._host_labels()
        return labels[index] if index < len(labels) else str(index)

    # --- state rotation ---------------------------------------------------------------------------

    def _rotate(self, stale: _State) -> None:
        # Every identifier or counter value has been issued.  A new key and an empty store mean every
        # outstanding marker is refused rather than any identifier being reused; values are stored again
        # as they are read.  Neither limit is reachable before the overall budget in practice; this keeps
        # the rule that nothing is ever reused even so.
        with self._lock:
            if self._state is stale:
                self._state = _State()

    # --- storing ----------------------------------------------------------------------------------

    def remember(self, value: str, host: str, tier: Tier = Tier.HARVESTED) -> None:  # noqa: DOC502
        """Store a value read directly from where the daemon reports it, so it is redacted everywhere.

        Args:
            value: the value
            host: the host it was read from
            tier: how it was found; harvested unless a rule found it

        Raises:
            BudgetReachedError: storing it would pass the overall budget
        """
        index = self._host_index(host)
        state = self._state
        try:
            state.store.add(value, index, tier, host_label=host)
        except IdentifiersExhaustedError:
            # Storing only adds, so it is safe to do again in the new generation.
            self._rotate(state)
            self._state.store.add(value, index, tier, host_label=host)

    def budget_reached(self) -> bool:
        """Whether the overall budget is reached, so every call on a redacted host must be refused.

        Returns:
            bool: True once reached; only a restart clears it
        """
        return self._state.store.budget_reached

    def report(self, host: str) -> dict:
        """The store's state for one host, without any value.

        Args:
            host: the host label

        Returns:
            dict: entries and bytes for each kind, and whether the overall budget is reached
        """
        return self._state.store.report(self._host_index(host))

    # --- redacting --------------------------------------------------------------------------------

    def redact(self, value: Any, host: str) -> Any:
        """Replace every credential in a tool result or resource body with a marker.

        Strings are redacted wherever they are, dict keys included; other scalars and bytes are
        returned as they are.  Under a public field (`rules.PUBLIC_FIELDS`) the shape rules are not
        applied, but stored values still are.

        Args:
            value: the result: a string, dict, list or tuple, nested to any depth, or any other value
            host: the host the call targeted

        Returns:
            Any: the same shape with credentials replaced

        Raises:
            BudgetReachedError: the overall budget is reached
            StateResetError: the redactor rotated partway through, so this call is refused rather than
                finished against an empty store
        """
        if self.budget_reached():
            raise BudgetReachedError
        index = self._host_index(host)
        state = self._state
        try:
            return self._walk(state, value, index, shape_rules=True)
        except IdentifiersExhaustedError, CounterExhaustedError:
            self._rotate(state)
            raise StateResetError from None

    def _walk(self, state: _State, value: Any, index: int, *, shape_rules: bool) -> Any:
        if isinstance(value, str):
            return self._redact_text(state, value, index, shape_rules=shape_rules)
        if isinstance(value, dict):
            return {
                self._walk(state, key, index, shape_rules=shape_rules): self._walk(
                    state, item, index, shape_rules=shape_rules and key not in rules.PUBLIC_FIELDS
                )
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            walked = []
            for position, item in enumerate(value):
                previous = value[position - 1] if position else None
                if (
                    shape_rules
                    and isinstance(item, str)
                    and isinstance(previous, str)
                    and rules.is_credential_flag(previous)
                    and item
                    and not item.startswith("-")
                ):
                    walked.append(self._replace_whole(state, item, index))
                else:
                    walked.append(self._walk(state, item, index, shape_rules=shape_rules))
            return walked if isinstance(value, list) else tuple(walked)
        return value

    def _replace_whole(self, state: _State, text: str, index: int) -> str:
        # The element after a credential-named flag in an exec-form command: the whole string is the value.
        known = state.store.get_for(text, index)
        candidate = _Candidate(0, len(text), known, forms.RAW)
        return self._marker_for(state, candidate, text, index)

    def _redact_text(self, state: _State, text: str, index: int, *, shape_rules: bool) -> str:
        candidates = [
            _Candidate(hit.start, hit.end, entry, hit.form_id)
            for hit in state.matcher.find(text)
            if (entry := state.store.get(hit.value_id)) is not None
        ]
        if shape_rules:
            candidates.extend(_Candidate(span.start, span.end, None, forms.RAW) for span in rules.find(text))
        if not candidates:
            return text
        pieces: list[str] = []
        position = 0
        for candidate in _choose(candidates):
            pieces.append(text[position : candidate.start])
            pieces.append(self._marker_for(state, candidate, text[candidate.start : candidate.end], index))
            position = candidate.end
        pieces.append(text[position:])
        return "".join(pieces)

    def _marker_for(self, state: _State, candidate: _Candidate, found: str, index: int) -> str:
        if candidate.known is not None:
            # A value stored for another host, found in this host's output, is stored for this host too,
            # so the marker names an entry that can be swapped back here.
            entry = candidate.known
            if entry.host_index != index:
                entry = state.store.add(entry.secret.reveal(), index, entry.tier, host_label=self._host_label(index))
            value_id = entry.value_id if entry is not None else UNSTORED_VALUE_ID
            return state.codec.issue(value_id, candidate.form_id, index)
        entry = state.store.add(found, index, Tier.DISCOVERED, host_label=self._host_label(index))
        return state.codec.issue(entry.value_id if entry is not None else UNSTORED_VALUE_ID, forms.RAW, index)

    # --- reading markers back ---------------------------------------------------------------------

    def resolve(self, token: str, host: str) -> str | Refusal:
        """The text a marker replaced, if it can be swapped back on this host.

        Args:
            token: the marker's token, the 22 characters inside `[REDACTED:...]`
            host: the host the write targets

        Returns:
            str | Refusal: the text, in the form it was replaced in, or why it cannot be used
        """
        state = self._state
        fields = state.codec.read(token)
        if fields is MarkerFault.MALFORMED:
            return Refusal.MALFORMED
        if isinstance(fields, MarkerFault):
            return Refusal.NOT_THIS_PROCESS
        if fields.host_index != self._host_index(host):
            return Refusal.OTHER_HOST
        if fields.value_id == UNSTORED_VALUE_ID:
            return Refusal.UNSTORED
        entry = state.store.get(fields.value_id)
        if entry is None or fields.form_id not in forms.form_ids():
            return Refusal.NOT_THIS_PROCESS
        return forms.render(entry.secret.reveal(), fields.form_id)


def find_markers(text: str) -> list[str]:
    """The tokens of every marker in a piece of text.

    Args:
        text: the text to search

    Returns:
        list: the 22-character tokens, in order
    """
    return MARKER_PATTERN.findall(text)


def _choose(candidates: list[_Candidate]) -> list[_Candidate]:
    # Longest wins where two overlap, and a known value wins a tie, since its form is exact.
    candidates.sort(key=lambda c: (c.start, -(c.end - c.start), c.known is None))
    chosen: list[_Candidate] = []
    for candidate in candidates:
        if chosen and candidate.start < chosen[-1].end:
            previous = chosen[-1]
            if candidate.end - candidate.start > previous.end - previous.start:
                chosen[-1] = candidate
            continue
        chosen.append(candidate)
    return chosen

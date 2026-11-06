"""The index that finds every stored value's forms in a piece of text in one pass.

An Aho-Corasick automaton (`pyahocorasick`), because Python's `re` with thousands of literal
alternatives took seconds per 100 KB of output where an automaton takes milliseconds.  Building one is
not cheap - about 19 seconds for the 700,000 forms of 100,000 values - so it is not rebuilt on every
new value: new forms go into a small automaton that is rebuilt quickly, and are merged into the large
one once the small one holds a quarter as many, so total rebuild work stays proportional to the store.
A merge near the limits stalls the call that triggers it; that is an accepted cost of a store that
size, which only an abnormal harvest or flood reaches.

The index is replaced whole, never changed in place, so a reader holding the old one is never given a
half-built one.
"""

import threading
from dataclasses import dataclass

import ahocorasick

# Below this many pending forms the small automaton is simply rebuilt; above it, and above a quarter of
# the large one, everything is merged into the large one.
_MIN_MERGE_THRESHOLD = 2_000


@dataclass(frozen=True)
class Hit:
    """One form of one stored value, found in text.

    Attributes:
        start: index of its first character.
        end: index one past its last character.
        value_id: the stored value's identifier.
        form_id: which form of it was found.
    """

    start: int
    end: int
    value_id: int
    form_id: int


# Each form's payload: (value identifier, form identifier, length).  The length is there because
# `iter_long` reports only where a match ends.
_Payload = tuple[int, int, int]


def _build(forms: dict[str, _Payload]) -> ahocorasick.Automaton | None:
    if not forms:
        return None
    automaton = ahocorasick.Automaton()
    for text, payload in forms.items():
        automaton.add_word(text, payload)
    automaton.make_automaton()
    return automaton


@dataclass(frozen=True)
class _Index:
    large: ahocorasick.Automaton | None
    small: ahocorasick.Automaton | None


class Matcher:
    """Finds stored values' forms in text.  Thread-safe."""

    def __init__(self) -> None:
        """Start empty."""
        self._lock = threading.Lock()
        self._large_forms: dict[str, _Payload] = {}
        self._small_forms: dict[str, _Payload] = {}
        self._index = _Index(None, None)

    def add(self, forms: dict[str, int], value_id: int) -> None:
        """Index one stored value's forms.

        Args:
            forms: text -> form identifier
            value_id: the stored value's identifier
        """
        with self._lock:
            for text, form_id in forms.items():
                if text not in self._large_forms:
                    self._small_forms.setdefault(text, (value_id, form_id, len(text)))
            if len(self._small_forms) > max(_MIN_MERGE_THRESHOLD, len(self._large_forms) // 4):
                self._large_forms.update(self._small_forms)
                self._small_forms = {}
                self._index = _Index(_build(self._large_forms), None)
            else:
                self._index = _Index(self._index.large, _build(self._small_forms))

    def clear(self) -> None:
        """Forget every value, for a rotation that empties the store."""
        with self._lock:
            self._large_forms = {}
            self._small_forms = {}
            self._index = _Index(None, None)

    def find(self, text: str) -> list[Hit]:
        """Every non-overlapping occurrence of a stored form, longest first where two overlap.

        Args:
            text: the text to search

        Returns:
            list: the hits, in order of position
        """
        index = self._index
        hits: list[Hit] = []
        for automaton in (index.large, index.small):
            if automaton is None:
                continue
            for end, (value_id, form_id, length) in automaton.iter_long(text):
                hits.append(Hit(end + 1 - length, end + 1, value_id, form_id))
        return _longest_non_overlapping(hits)


def _longest_non_overlapping(hits: list[Hit]) -> list[Hit]:
    hits.sort(key=lambda hit: (hit.start, -(hit.end - hit.start)))
    chosen: list[Hit] = []
    for hit in hits:
        if chosen and hit.start < chosen[-1].end:
            previous = chosen[-1]
            if hit.end - hit.start > previous.end - previous.start:
                chosen[-1] = hit
            continue
        chosen.append(hit)
    return chosen

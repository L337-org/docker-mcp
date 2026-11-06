"""The index that finds every stored value's forms in a piece of text in one pass per automaton.

An Aho-Corasick automaton (`pyahocorasick`), because Python's `re` with thousands of literal alternatives
takes seconds per 100 KB of output where an automaton takes milliseconds.  Building an automaton costs
time in proportion to its forms, so the index is a few automata of geometrically falling size rather than
one: a new value's forms become a small automaton of their own, and whenever an automaton reaches a
quarter of the size of the one before it, the two are merged and rebuilt as one.  Each form is rebuilt
about log4(n) times over the life of the store, and a search scans at most about ten automata.

The index is replaced whole, never changed in place, so a reader holding the old one is never given a
half-built one.
"""

import threading
from dataclasses import dataclass

import ahocorasick

# An automaton is merged into the one before it once it holds at least this fraction of that one's forms.
_MERGE_RATIO = 4

# Each form's payload: (value identifier, form identifier, length).  The length is there because the
# automaton reports where a match ends, not where it starts.
_Payload = tuple[int, int, int]


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


@dataclass(frozen=True)
class _Level:
    forms: dict[str, _Payload]
    automaton: ahocorasick.Automaton


def _build(forms: dict[str, _Payload]) -> _Level:
    automaton = ahocorasick.Automaton()
    for text, payload in forms.items():
        automaton.add_word(text, payload)
    automaton.make_automaton()
    return _Level(forms, automaton)


class Matcher:
    """Finds stored values' forms in text.  Thread-safe."""

    def __init__(self) -> None:
        """Start empty."""
        self._lock = threading.Lock()
        self._levels: tuple[_Level, ...] = ()

    def add(self, forms: dict[str, int], value_id: int) -> None:
        """Index one stored value's forms.

        A form already indexed keeps the value it was first indexed for, and adding nothing new does not
        rebuild anything.

        Args:
            forms: text -> form identifier
            value_id: the stored value's identifier
        """
        with self._lock:
            new = {
                text: (value_id, form_id, len(text))
                for text, form_id in forms.items()
                if not any(text in level.forms for level in self._levels)
            }
            if not new:
                return
            levels = [*self._levels, _build(new)]
            while len(levels) >= 2 and len(levels[-1].forms) * _MERGE_RATIO >= len(levels[-2].forms):
                merged = {**levels[-1].forms, **levels[-2].forms}
                levels[-2:] = [_build(merged)]
            self._levels = tuple(levels)

    def clear(self) -> None:
        """Forget every value, for a rotation that empties the store."""
        with self._lock:
            self._levels = ()

    def find(self, text: str) -> list[Hit]:
        """Every occurrence of a stored form, overlapping ones included.

        Overlaps are resolved by the caller, which needs to see them all: where two stored values
        partly overlap, reporting only the first would leave the second's tail in the text.

        Args:
            text: the text to search

        Returns:
            list: the hits, in order of position
        """
        hits = [
            Hit(end + 1 - length, end + 1, value_id, form_id)
            for level in self._levels
            for end, (value_id, form_id, length) in level.automaton.iter(text)
        ]
        hits.sort(key=lambda hit: (hit.start, hit.end))
        return hits

    def levels(self) -> int:
        """How many automata a search scans.

        Returns:
            int: the number of levels
        """
        return len(self._levels)

"""Markers: the text that replaces a redacted value, and reading one back.

A marker is `[REDACTED:<token>]`, where the token is one 16-byte block encrypted with AES under a key
generated when the server starts, written in URL-safe base64 without padding (22 characters).  The
block holds the stored value's identifier (4 bytes), the form that was replaced (1 byte), the host
(2 bytes), a counter (5 bytes) and a fixed check field (4 bytes).  The counter rises on every marker,
so two markers for the same credential never look alike; without the key they are unrelated random
strings, and nothing else about the value - its format, how it was found, whether it was stored - is
in the marker.

Encrypting one block at a time under a single key uses AES as a pseudo-random permutation: every
input block is distinct because the counter is, so no two markers repeat, and decrypting gives the
fields back directly with no table of issued markers to keep.
"""

import base64
import enum
import os
import re
import threading
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MARKER_PREFIX = "[REDACTED:"
_TOKEN_LENGTH = 22
MARKER_PATTERN = re.compile(r"\[REDACTED:([A-Za-z0-9_-]{22})\]")

_CHECK = b"DMSR"
_MAX_COUNTER = (1 << 40) - 1


class MarkerFault(enum.Enum):
    """Why a marker could not be read back.

    Attributes:
        MALFORMED: not a marker this server could have written.
        NOT_THIS_PROCESS: altered, or written by an earlier run of the server (under another key);
            the two cannot be told apart, since both fail the check field.
    """

    MALFORMED = "malformed"
    NOT_THIS_PROCESS = "not_this_process"


@dataclass(frozen=True)
class MarkerFields:
    """What a marker records.

    Attributes:
        value_id: the stored value's identifier, or `store.UNSTORED_VALUE_ID`.
        form_id: the form of the value that was replaced.
        host_index: the host the marker was issued for.
    """

    value_id: int
    form_id: int
    host_index: int


class MarkerCodec:
    """Issues markers and reads them back, holding the key and the counter.

    Thread-safe: the counter is advanced under a lock, so concurrent calls never share a value.
    """

    def __init__(self) -> None:
        """Start with a fresh random key and a counter at zero."""
        self._lock = threading.Lock()
        self._counter = 0
        # S305: ECB is what one block needs.  Each marker is exactly one 16-byte block, and ECB on a single
        # block is the raw block cipher that every other mode is built from; its known weakness, equal
        # plaintext blocks giving equal ciphertext, cannot arise because no two blocks are ever equal.
        self._cipher = Cipher(algorithms.AES(os.urandom(32)), modes.ECB())  # noqa: S305

    def issue(self, value_id: int, form_id: int, host_index: int) -> str:
        """Write a marker for one replacement.

        Args:
            value_id: the stored value's identifier, or the unstored identifier
            form_id: the form that was replaced
            host_index: the host the replacement happened on

        Returns:
            str: the marker text

        Raises:
            CounterExhaustedError: the counter has issued every value it can; the caller rotates
        """
        with self._lock:
            if self._counter > _MAX_COUNTER:
                raise CounterExhaustedError
            counter = self._counter
            self._counter += 1
            encryptor = self._cipher.encryptor()
        block = (
            value_id.to_bytes(4, "big")
            + form_id.to_bytes(1, "big")
            + host_index.to_bytes(2, "big")
            + counter.to_bytes(5, "big")
            + _CHECK
        )
        token = encryptor.update(block) + encryptor.finalize()
        return f"{MARKER_PREFIX}{base64.urlsafe_b64encode(token).decode('ascii').rstrip('=')}]"

    def read(self, token: str) -> MarkerFields | MarkerFault:
        """Read a marker's token back.

        Args:
            token: the 22 characters between `[REDACTED:` and `]`

        Returns:
            MarkerFields | MarkerFault: the fields, or why they could not be read
        """
        if len(token) != _TOKEN_LENGTH:
            return MarkerFault.MALFORMED
        try:
            raw = base64.urlsafe_b64decode(token + "==")
        except ValueError:
            return MarkerFault.MALFORMED
        # 22 characters carry 132 bits for a 128-bit block, so the last one has 4 spare bits the decoder
        # ignores.  Only the spelling `issue` writes is accepted, or a marker altered in those bits would
        # read back as the original.
        if len(raw) != 16 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
            return MarkerFault.MALFORMED
        with self._lock:
            decryptor = self._cipher.decryptor()
        block = decryptor.update(raw) + decryptor.finalize()
        if block[12:] != _CHECK:
            return MarkerFault.NOT_THIS_PROCESS
        return MarkerFields(
            value_id=int.from_bytes(block[0:4], "big"),
            form_id=block[4],
            host_index=int.from_bytes(block[5:7], "big"),
        )


class CounterExhaustedError(Exception):
    """The marker counter has run out; the redactor rotates the key and empties the store."""

"""The encoded forms of a stored value that the matcher looks for, and how to render each one again.

A credential the server knows about is redacted in the forms it is commonly printed in, not only raw.
Each form has a one-byte identifier that a marker records, so swapping a marker back renders exactly
the text it replaced, a base64 occurrence as base64.  Adding a form is one row in `_FORMS` plus its
case in tests/test_redaction_forms.py.
"""

import base64
import binascii
import urllib.parse
from collections.abc import Callable

# Encoded forms shorter than this, padding aside, are not matched: a short run of base64 or hex turns
# up in ordinary output by chance, and replacing it would damage data an agent needs.  The raw value is
# matched at any length, because it is the credential itself.  An accepted consequence: a value of six
# characters or fewer, base64-encoded inside a larger blob at an unlucky alignment, is not found there,
# because the part of its encoding that does not depend on its neighbours is shorter than this.
MIN_ENCODED_FORM_LENGTH = 8

RAW = 0


def _b64_core(data: bytes, shift: int, encoder: Callable[[bytes], bytes]) -> str:
    """The characters of `data`'s base64 encoding that do not depend on the bytes around it.

    When a value sits inside a larger blob, its bytes start at one of three positions within a
    base64 group, and the characters at either edge mix its bits with its neighbours'.  Encoding the
    value between two different sets of neighbours and keeping only the characters that agree leaves
    exactly the part that is the same wherever the value appears at that alignment.

    Args:
        data: the value's bytes
        shift: how many bytes before the value share its first base64 group (0, 1 or 2)
        encoder: the base64 alphabet's encoder

    Returns:
        str: the stable characters, or "" when there are none
    """
    low = encoder(b"\x00" * shift + data + b"\x00\x00").decode("ascii").rstrip("=")
    high = encoder(b"\xff" * shift + data + b"\xff\xff").decode("ascii").rstrip("=")
    # The longest run of agreeing characters.  Padding is stripped first because it agrees in both
    # encodings without saying anything about the value, and would otherwise join the run to characters
    # that depend on the neighbours.
    best_start = best_end = start = 0
    for index, (a, b) in enumerate(zip(low, high, strict=True)):
        if a != b:
            start = index + 1
        elif index + 1 - start > best_end - best_start:
            best_start, best_end = start, index + 1
    return low[best_start:best_end]


def _whole(encoder: Callable[[bytes], bytes], *, padded: bool) -> Callable[[bytes], str]:
    def render(data: bytes) -> str:
        text = encoder(data).decode("ascii")
        return text if padded else text.rstrip("=")

    return render


def _core(encoder: Callable[[bytes], bytes], shift: int) -> Callable[[bytes], str]:
    def render(data: bytes) -> str:
        return _b64_core(data, shift, encoder)

    return render


def _url_quoted(data: bytes) -> str:
    return urllib.parse.quote(data.decode("utf-8", errors="surrogateescape"), safe="")


# Identifier -> renderer.  Identifiers are recorded in markers, so they are never reused or renumbered.
_FORMS: dict[int, Callable[[bytes], str]] = {
    1: _whole(base64.b64encode, padded=True),
    2: _whole(base64.b64encode, padded=False),
    3: _whole(base64.urlsafe_b64encode, padded=True),
    4: _whole(base64.urlsafe_b64encode, padded=False),
    5: _core(base64.b64encode, 0),
    6: _core(base64.b64encode, 1),
    7: _core(base64.b64encode, 2),
    8: _core(base64.urlsafe_b64encode, 0),
    9: _core(base64.urlsafe_b64encode, 1),
    10: _core(base64.urlsafe_b64encode, 2),
    11: _whole(base64.b32encode, padded=True),
    12: _whole(base64.b32encode, padded=False),
    13: lambda data: binascii.hexlify(data).decode("ascii"),
    14: lambda data: binascii.hexlify(data).decode("ascii").upper(),
    15: _url_quoted,
}


def render(value: str, form_id: int) -> str:  # noqa: DOC502
    """The text of one form of a value, as the matcher found it and as swapping back restores it.

    Args:
        value: the stored value
        form_id: the form's identifier, `RAW` for the value itself

    Returns:
        str: the rendered text

    Raises:
        KeyError: an identifier no form has, which a marker that decrypted correctly cannot carry
    """
    if form_id == RAW:
        return value
    return _FORMS[form_id](value.encode("utf-8", errors="surrogateescape"))


def forms_of(value: str) -> dict[str, int]:
    """Every distinct text the matcher should look for to find this value, with its form identifier.

    The raw value always comes first.  An encoded form shorter than `MIN_ENCODED_FORM_LENGTH`, or
    identical to one already listed, is left out; where two forms render the same text the first
    identifier is kept, and rendering either gives that same text back.

    Args:
        value: the stored value

    Returns:
        dict: text -> form identifier
    """
    found: dict[str, int] = {value: RAW} if value else {}
    for form_id in _FORMS:
        text = render(value, form_id)
        # Padding does not count towards the length: `MFRA====` is four characters of information.
        if len(text.rstrip("=")) >= MIN_ENCODED_FORM_LENGTH and text not in found:
            found[text] = form_id
    return found


def form_ids() -> list[int]:
    """Every form identifier, the raw one included.

    Returns:
        list: the identifiers, in order
    """
    return [RAW, *_FORMS]

"""Shape rules: finding credentials the server was never told about, by their form and their context.

Each rule returns the spans of text it judges to be a credential.  A span found here is a discovered
value: it is redacted, and stored within the discovered limits so its other forms are matched from then
on.  Every rule is a row in `_RULES`, so adding one is a row plus its cases in
tests/test_redaction_rules.py; the public field names and the flag pattern are tables too.

REDACTION.md describes each rule for users; `_RULES` below is the list.
"""

import base64
import binascii
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from docker_mcp.redaction import settings
from docker_mcp.redaction.store import MAX_DISCOVERED_MATCH_LENGTH

# Docker's own public fields whose values look random but are not secret.  The shape rules never
# replace the value of one of these; known values are still matched in them.  Kept short on purpose: a
# field listed here is never redacted by a rule even if a credential ends up in it.  REDACTION.md lists
# it, and a test keeps the two in step.
PUBLIC_FIELDS = frozenset({"TrustRoot", "CertIssuerPublicKey", "CertIssuerSubject", "SandboxKey"})

_GENERIC_MIN_LENGTH = 32
_GENERIC_MIN_ENTROPY = 4.5
_ASSIGNMENT_MIN_LENGTH = 8
_ASSIGNMENT_MIN_ENTROPY = 4.0


@dataclass(frozen=True)
class Span:
    """A credential found in text.

    Attributes:
        start: index of its first character.
        end: index one past its last character.
    """

    start: int
    end: int


def entropy(text: str) -> float:
    """Shannon entropy per character.

    Args:
        text: the string

    Returns:
        float: bits per character; 0 for an empty string
    """
    if not text:
        return 0.0
    counts = Counter(text)
    length = len(text)
    return -sum(n / length * math.log2(n / length) for n in counts.values())


def _group_spans(pattern: re.Pattern[str], group: int = 1) -> Callable[[str], Iterator[Span]]:
    def find(text: str) -> Iterator[Span]:
        for match in pattern.finditer(text):
            if match.group(group):
                yield Span(match.start(group), match.end(group))

    return find


# --- known formats ---------------------------------------------------------------------------------

_PEM = re.compile(
    r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----.*?-----END \1-----",
    re.DOTALL,
)
_GITHUB = re.compile(r"\b((?:gh[pousr]_[A-Za-z0-9]{36,255})|(?:github_pat_[A-Za-z0-9_]{22,255}))\b")
_STRIPE = re.compile(r"\b((?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,})\b")
_SLACK = re.compile(r"\b(xox[abposr]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,})\b")
_AWS_KEY_ID = re.compile(r"\b((?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16})\b")
_JWT = re.compile(r"\b(eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{8,})")


def _pem(text: str) -> Iterator[Span]:
    for match in _PEM.finditer(text):
        yield Span(match.start(), match.end())


def _jwt(text: str) -> Iterator[Span]:
    for match in _JWT.finditer(text):
        header = match.group(1).split(".", 1)[0]
        try:
            decoded = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
        except ValueError, binascii.Error:
            continue
        if isinstance(decoded, dict) and "alg" in decoded:
            yield Span(match.start(1), match.end(1))


# --- context: headers, URLs, connection strings ---------------------------------------------------

_AUTH_HEADER = re.compile(
    r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*\"?(?:(?:basic|bearer|token|digest|negotiate)\s+)?([^\s\",;]+)"
)
_API_KEY_HEADER = re.compile(r"(?i)\bx-(?:api|auth|access)-(?:key|token)\s*[:=]\s*\"?([^\s\",;]+)")
_COOKIE = re.compile(r"(?i)\b(?:set-)?cookie\s*[:=]\s*\"?([^\r\n\"]+)")
# A pattern that opens with a repeated class must neither start inside a run of that class nor backtrack
# out of it, or a long run of base64 or hex is scanned again from every position: quadratic in its length.
# So each starts only where the run begins (the look-behind) and repeats possessively (`*+`), which finds
# the same matches here because the class cannot contain the character that must follow it.
_URL_USERINFO = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*+://[^/\s:@]++:([^@\s/]+)@", re.IGNORECASE)
_CONNECTION_PASSWORD = re.compile(r"(?i)\b(?:password|pwd)\s*=\s*([^;'\"\s]+)")

# --- credential-named assignments and command-line flags -------------------------------------------

# The name is the whole run, so a leading digit stays part of it (`2FA_SECRET`); the credential-name
# globs match a word anywhere in it, so that is still a credential name.
_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<name>[A-Za-z0-9_.-]++)[\"']?\s*(?:=|:)\s*[\"']?(?P<value>[^\s\"',;}\]]+)",
)
# Only the flag itself is matched, so scanning never consumes the next flag (`-p --secret x` must see
# `--secret`); its value is read from where the flag ends.
_FLAG = re.compile(r"(?<![\w-])--?(?P<name>[A-Za-z][A-Za-z0-9-]*)(?:=|[ \t]+)")
_FLAG_VALUE = re.compile(r"\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s\"']+)")


def _is_credential_name(name: str) -> bool:
    normalised = name.upper().replace("-", "_").replace(".", "_")
    return settings.names().matches(normalised)


def _assignments(text: str) -> Iterator[Span]:
    for match in _ASSIGNMENT.finditer(text):
        value = match.group("value")
        if (
            _is_credential_name(match.group("name"))
            and len(value) >= _ASSIGNMENT_MIN_LENGTH
            and entropy(value) >= _ASSIGNMENT_MIN_ENTROPY
        ):
            yield Span(match.start("value"), match.end("value"))


_BARE_FLAG = re.compile(r"--?(?P<name>[A-Za-z][A-Za-z0-9-]*)")


def is_credential_flag(element: str) -> bool:
    """Whether one element of an exec-form command is a credential-named flag with no value of its own.

    Docker reports `Cmd`, `Args` and `Entrypoint` as lists, so `["app", "--password", "s3cret"]` puts
    the flag and its value in separate strings; the redactor uses this to treat the next element as the
    value, which the text rule alone cannot see.

    Args:
        element: one string from a list

    Returns:
        bool: True for a bare `--password`-style flag
    """
    match = _BARE_FLAG.fullmatch(element)
    return match is not None and _is_credential_name(match.group("name"))


def _flags(text: str) -> Iterator[Span]:
    # Unlike an assignment, a value after a credential-named flag is taken whatever its shape: the flag
    # says what it is, and a word-like password on a command line is exactly the miss this closes.
    for match in _FLAG.finditer(text):
        if not _is_credential_name(match.group("name")):
            continue
        value = _FLAG_VALUE.match(text, match.end())
        if value is None:
            continue
        group = next(name for name in ("dq", "sq", "bare") if value.group(name) is not None)
        if group == "bare" and value.group(group).startswith("-"):
            continue  # the next flag, not a value
        if value.group(group):
            yield Span(value.start(group), value.end(group))


# --- generic: long random runs ----------------------------------------------------------------------

_RUN = re.compile(r"[A-Za-z0-9+/_-]{4,}={0,2}")
_HEX = re.compile(r"[0-9A-Fa-f]+")
_LINE_BREAK = re.compile(r"\r?\n")
# How far back from a run to look for the `://` that would make it part of a URL.
_URL_LOOKBACK = 2048
# The shortest line width `base64`-style wrapping uses (PEM uses 64, coreutils `base64` 76).
_MIN_WRAP_WIDTH = 60


def _runs(text: str) -> Iterator[tuple[int, int, str]]:
    """Runs of base64 characters, with lines wrapped by a base64 encoder joined into one run.

    Wrapped output is lines of one width followed by one shorter final line, so a run continues onto the
    next line only while every line so far has the same width of at least `_MIN_WRAP_WIDTH`, and the
    first shorter line ends it.  Without the joining, each line of a wrapped blob would be a separate
    value, and a large blob would fill the store with hundreds of them.

    Args:
        text: the text to search

    Yields:
        tuple: start, end, and the run's characters with the line breaks removed
    """
    position = 0
    while (match := _RUN.search(text, position)) is not None:
        start, end, joined = match.start(), match.end(), match.group()
        width = end - start
        while width >= _MIN_WRAP_WIDTH and len(joined) % width == 0:
            gap = _LINE_BREAK.match(text, end)
            following = _RUN.match(text, gap.end()) if gap else None
            if following is None or following.end() - following.start() > width:
                break
            end, joined = following.end(), joined + following.group()
            if following.end() - following.start() < width:
                break  # the shorter final line
        yield start, end, joined
        position = end


def _looks_like_path(joined: str) -> bool:
    # Rooted, at least two segments, and none of base64's other symbols.  Not "contains //": random base64
    # holds a `//` about one time in five at 800 characters, so that test would skip whole random blobs.
    return (
        joined.startswith(("/", "./", "~/"))
        and "+" not in joined
        and "=" not in joined
        and sum(1 for segment in joined.split("/") if segment) >= 2
    )


def _in_url_or_path(text: str, start: int, joined: str) -> bool:
    if _looks_like_path(joined):
        return True
    # Back to the start of the whitespace-delimited token: a `://` there makes this part of a URL.
    # A bounded look-back: a URL's scheme is close to its path, and searching back to the start of a long
    # text for every run would make this quadratic in the text's length.
    window = max(0, start - _URL_LOOKBACK)
    token_start = max(text.rfind(c, window, start) for c in " \t\r\n\"'=") + 1
    return "://" in text[max(token_start, window) : start]


# Public keys look random but are public, so the generic rule leaves them alone: the body of an OpenSSH
# public key, after its key type, and the inside of a PEM public key or certificate block.  A private key
# is still caught, by the PEM rule above.
_OPENSSH_KEY_TYPE = re.compile(
    r"(?:ssh-(?:rsa|dss|ed25519)|ecdsa-sha2-nistp(?:256|384|521)|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)"
    r" $"
)
_PEM_PUBLIC = re.compile(
    r"-----BEGIN ((?:[A-Z0-9]+ )*(?:PUBLIC KEY|CERTIFICATE))-----.*?-----END \1-----",
    re.DOTALL,
)


def _public_key_spans(text: str) -> list[tuple[int, int]]:
    if "-----BEGIN" not in text:
        return []
    return [match.span() for match in _PEM_PUBLIC.finditer(text)]


def _is_public_key(text: str, start: int, joined: str, public: list[tuple[int, int]]) -> bool:
    if joined.startswith("AAAA") and _OPENSSH_KEY_TYPE.search(text, max(0, start - 40), start):
        return True
    return any(begin <= start < end for begin, end in public)


def _generic(text: str) -> Iterator[Span]:
    public = _public_key_spans(text)
    for start, end, joined in _runs(text):
        if not _GENERIC_MIN_LENGTH <= len(joined) <= MAX_DISCOVERED_MATCH_LENGTH:
            continue
        if _HEX.fullmatch(joined.rstrip("=")):
            continue  # digests and identifiers; hex carries at most 4 bits per character anyway
        if _in_url_or_path(text, start, joined):
            continue
        if _is_public_key(text, start, joined, public):
            continue
        if entropy(joined) >= _GENERIC_MIN_ENTROPY:
            yield Span(start, end)


# Rule name -> finder.  Order does not matter: the redactor replaces each cluster of overlapping spans
# whole.
_RULES: dict[str, Callable[[str], Iterator[Span]]] = {
    "pem": _pem,
    "github": _group_spans(_GITHUB),
    "stripe": _group_spans(_STRIPE),
    "slack": _group_spans(_SLACK),
    "aws_key_id": _group_spans(_AWS_KEY_ID),
    "jwt": _jwt,
    "authorization": _group_spans(_AUTH_HEADER),
    "api_key_header": _group_spans(_API_KEY_HEADER),
    "cookie": _group_spans(_COOKIE),
    "url_userinfo": _group_spans(_URL_USERINFO),
    "connection_password": _group_spans(_CONNECTION_PASSWORD),
    "assignment": _assignments,
    "flag": _flags,
    "generic": _generic,
}


def find(text: str) -> list[Span]:
    """Every span any rule judges to be a credential, possibly overlapping.

    Args:
        text: the text to search

    Returns:
        list: the spans, in no particular order
    """
    found = []
    for rule in _RULES.values():
        for span in rule(text):
            if 0 < span.end - span.start <= MAX_DISCOVERED_MATCH_LENGTH:
                found.append(span)
    return found

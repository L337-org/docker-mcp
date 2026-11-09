import base64
import json
import re
import random
import secrets
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from docker_mcp.redaction import rules, settings
from docker_mcp.redaction.settings import NameRules, parse_names
from docker_mcp.redaction.store import MAX_DISCOVERED_MATCH_LENGTH


@pytest.fixture(autouse=True)
def _default_names(monkeypatch):
    monkeypatch.setattr(settings, "_names", NameRules())


def _found(text: str) -> list[str]:
    return sorted({text[span.start : span.end] for span in rules.find(text)})


def _jwt(header: dict) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part(header)}.{part({'sub': 'someone', 'iat': 1700000000})}.{secrets.token_urlsafe(32)}"


# Token-shaped fixtures are built at run time from random characters, never written out whole: a
# realistic token in source is a secret as far as any scanner is concerned (SK.1.3), fake or not.
def _alnum(n: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


_PEM_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + _alnum(40) + "\n-----END RSA PRIVATE KEY-----"
_GITHUB = "gh" + "p_" + _alnum(36)
_STRIPE = "sk_" + "live_" + _alnum(24)
_SLACK = "xox" + "b-" + "".join(secrets.choice("0123456789") for _ in range(12)) + "-" + _alnum(16)
_AWS_KEY_ID = "AK" + "IA" + "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))
# Distinct characters, so the entropy is exactly log2 of the length (4.32 bits at 20, 5.32 at 40) and
# always meets the rules' thresholds; a plain random draw falls under them by chance a few times in a
# hundred at these lengths, which is the rules' known behaviour, not something these tests examine.
_RANDOM_40 = "".join(
    secrets.SystemRandom().sample("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", 40)
)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"key:\n{_PEM_KEY}\nend", _PEM_KEY),
        (f"token {_GITHUB} here", _GITHUB),
        (f"key {_STRIPE} x", _STRIPE),
        (f"slack {_SLACK} x", _SLACK),
        (f"aws {_AWS_KEY_ID} x", _AWS_KEY_ID),
        ("Authorization: Bearer abc.def-ghi_jkl", "abc.def-ghi_jkl"),
        ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("Proxy-Authorization: Negotiate YIIBhgYGKwYBBQUCoIIBejCCAXag", "YIIBhgYGKwYBBQUCoIIBejCCAXag"),
        ("X-Api-Key: plainkeyvalue", "plainkeyvalue"),
        ("Cookie: session=abc123; theme=dark", "session=abc123; theme=dark"),
        ("postgres://app:s3cretpw@db:5432/app", "s3cretpw"),
        ("Server=db;User Id=sa;Password=Pa55word!;", "Pa55word!"),
        (f"API_TOKEN={_RANDOM_40[:20]}", _RANDOM_40[:20]),
        (f'{{"client_secret": "{_RANDOM_40[:24]}"}}', _RANDOM_40[:24]),
        (f"blob {_RANDOM_40}", _RANDOM_40),
    ],
)
def test_each_rule_finds_its_credential(text, secret):
    assert secret in _found(text)


def test_a_jwt_is_found_only_with_a_real_header():
    real = _jwt({"alg": "HS256", "typ": "JWT"})
    assert real in _found(f"token={real}")
    fake = _jwt({"not": "a header"})
    assert fake not in _found(f"value {fake}")


def test_a_certificate_is_public_and_left_alone():
    cert = "-----BEGIN CERTIFICATE-----\nMIIBszCC\n-----END CERTIFICATE-----"
    assert _found(cert) == []


@pytest.mark.parametrize(
    "text",
    [
        "sha256:" + "a3f5" * 16,  # a digest: hex is never matched by the generic rule
        str(uuid.uuid4()),
        "/var/lib/docker/overlay2/" + "Zq8xT2mR9vLk4WnB7yHc3PdF6sJg1QaE",  # a path
        "https://registry.example.com/v2/Zq8xT2mR9vLk4WnB7yHc3PdF6sJg1QaE5uXo0Itb",
        "a" * 64,  # long but low entropy
        "API_TOKEN=short",  # under the assignment rule's length
        "API_TOKEN=aaaaaaaaaaaaaaaa",  # long enough, no entropy
        "SORT_ORDER=Zq8xT2mR9vLk4WnB",  # not a credential name
    ],
)
def test_what_is_not_a_credential_is_left_alone(text):
    assert _found(text) == []


def _b64(n: int) -> str:
    return base64.b64encode(secrets.token_bytes(n)).decode()


@pytest.mark.parametrize(
    "text",
    [
        f"SSH_PUBLIC_KEY=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI{_b64(32)[:43]} deploy@ci",
        f"authorized: ssh-rsa AAAAB3NzaC1yc2E{_b64(256)} user@host",
        f"ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY{_b64(80)}",
        "-----BEGIN PUBLIC KEY-----\n" + "\n".join(textwrap.wrap(_b64(270), 64)) + "\n-----END PUBLIC KEY-----",
        "-----BEGIN CERTIFICATE-----\n" + "\n".join(textwrap.wrap(_b64(700), 64)) + "\n-----END CERTIFICATE-----",
    ],
)
def test_a_public_key_is_left_alone(text):
    # Public keys look random but are public; the PEM rule still catches a private key.
    assert _found(text) == []


def test_a_random_run_beside_a_public_key_is_still_found():
    secret = _b64(40)
    text = f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI{_b64(32)[:43]} me\ntoken {secret}"
    assert _found(text) == [secret]


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("app --password hunter2 --verbose", "hunter2"),  # a word-like password: the flag says what it is
        ("app --password=hunter2", "hunter2"),
        ("app --api-key 'two words'", "two words"),
        ('app --token="t0k3n"', "t0k3n"),
        ("mysql -p --secret s", "s"),
        ("psql --db-password letmein", "letmein"),
    ],
)
def test_the_value_after_a_credential_named_flag_is_found(text, secret):
    assert secret in _found(text)


@pytest.mark.parametrize("text", ["app --name hunter2", "app --password --verbose", "app --log-level debug"])
def test_a_flag_that_is_not_a_credential_or_has_no_value_is_left_alone(text):
    assert _found(text) == []


def test_the_names_setting_extends_the_flag_and_assignment_rules(monkeypatch):
    monkeypatch.setattr(settings, "_names", parse_names("UPSTREAM_*"))
    assert "wordlike" in _found("tool --upstream-url wordlike")


@pytest.mark.parametrize("seed", range(20))
def test_wrapped_base64_lines_are_one_credential(seed):
    blob = base64.b64encode(random.Random(seed).randbytes(600)).decode()  # noqa: S311 - reproducible data
    wrapped = "\n".join(textwrap.wrap(blob, 76))
    spans = rules.find(f"cert:\n{wrapped}\ndone")
    generic = [span for span in spans if span.end - span.start > 76]
    assert len(generic) == 1
    assert generic[0].end - generic[0].start == len(wrapped)


def test_a_run_longer_than_the_match_limit_is_left_alone():
    blob = base64.b64encode(secrets.token_bytes(MAX_DISCOVERED_MATCH_LENGTH)).decode()
    assert len(blob) > MAX_DISCOVERED_MATCH_LENGTH
    assert _found(f"data {blob} end") == []


def test_redaction_md_lists_exactly_the_public_fields():
    text = (Path(__file__).parent.parent / "REDACTION.md").read_text(encoding="utf-8")
    section = text.split("## Fields the rules leave alone", 1)[1].split("\n## ", 1)[0]
    assert set(re.findall(r"`([A-Za-z]+)`", section)) == rules.PUBLIC_FIELDS


def test_random_base64_containing_slashes_is_not_mistaken_for_a_path():
    # Random base64 holds `//` about one time in five at 800 characters, so `//` cannot mean "a path".
    token = "Zq8xT2mR9vLk//4WnB7yHc3PdF6sJg1QaE5uXo0Itb"
    assert token in _found(f"blob {token}")
    rooted = "/Zq8xT2mR9vLk//4WnB7+yHc3PdF6sJg1QaE5uXo0Itb"
    assert rooted in _found(f"blob {rooted}")  # rooted, but a `+` says it is not a path


def test_a_real_path_with_a_random_looking_segment_is_left_alone():
    assert _found("/var/lib/docker/overlay2/Zq8xT2mR9vLk4WnB7yHc3PdF6sJg1QaE/diff") == []


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(secrets.token_urlsafe, id="base64url"),
        pytest.param(secrets.token_hex, id="hex"),
        pytest.param(lambda n: "a.b-c_d" * n, id="dotted"),
    ],
)
def test_the_rules_take_linear_time_on_long_runs(make):
    # A pattern retried from every position in a run is quadratic in its length: ten times the input would
    # take about a hundred times as long.  Comparing two sizes rather than asserting a wall-clock bound
    # keeps this independent of how fast the runner is.
    def timed(n: int) -> float:
        text = make(n)
        started = time.perf_counter()
        rules.find(text)
        return time.perf_counter() - started

    timed(1_000)  # warm up
    small, large = min(timed(10_000) for _ in range(3)), min(timed(100_000) for _ in range(3))
    assert large < small * 30, f"10x the input took {large / small:.0f}x as long"


def test_a_credential_name_after_a_digit_is_still_found():
    value = _RANDOM_40[:20]
    assert value in _found(f"2FA_SECRET={value}")


def test_an_excluded_flag_name_stops_the_flag_rule_taking_its_value(monkeypatch):
    # `--token-ttl 3600` matches `*TOKEN*`, and the value would then be replaced wherever it stands alone;
    # REDACTION.md gives the exclusion as the remedy, so it has to reach the flag rule.
    assert _found("app --token-ttl 3600") == ["3600"]
    monkeypatch.setattr(settings, "_names", parse_names("!TOKEN_TTL"))
    assert _found("app --token-ttl 3600") == []

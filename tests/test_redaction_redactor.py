import base64
import os
import threading

import pytest

from docker_mcp.exceptions import DockerMcpError, RedactionBudgetError, RedactionResetError, ToolRefusalError
from docker_mcp.redaction import forms, markers, rules, settings
from docker_mcp.redaction import store as store_module
from docker_mcp.redaction.redactor import Redactor, Refusal, find_markers
from docker_mcp.redaction.settings import NameRules
from docker_mcp.redaction.store import Tier

_HOSTS = ["local", "prod"]
_SECRET = "correct-horse-battery-staple-42"


@pytest.fixture(autouse=True)
def _default_names(monkeypatch):
    monkeypatch.setattr(settings, "_names", NameRules())


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(host_labels=lambda: _HOSTS)


def _swap_back(r: Redactor, text: str, host: str) -> str:
    out = text
    for token in find_markers(text):
        resolved = r.resolve(token, host)
        assert isinstance(resolved, str), resolved
        out = out.replace(f"[REDACTED:{token}]", resolved, 1)
    return out


def test_a_stored_value_is_replaced_and_swaps_back_to_exactly_what_it_replaced(redactor):
    redactor.remember(_SECRET, "prod")
    text = f"DB_PASS={_SECRET} and again {_SECRET}"
    redacted = redactor.redact(text, "prod")
    assert _SECRET not in redacted
    assert len(find_markers(redacted)) == 2
    assert _swap_back(redactor, redacted, "prod") == text


def test_two_markers_for_the_same_value_differ(redactor):
    redactor.remember(_SECRET, "prod")
    first, second = find_markers(redactor.redact(f"{_SECRET} {_SECRET}", "prod"))
    assert first != second


@pytest.mark.parametrize("form_id", [1, 2, 3, 4, 11, 13, 14])
def test_an_encoded_form_is_replaced_and_swaps_back_in_that_form(redactor, form_id):
    redactor.remember(_SECRET, "prod")
    encoded = forms.render(_SECRET, form_id)
    redacted = redactor.redact(f"x {encoded} y", "prod")
    assert encoded not in redacted
    assert _swap_back(redactor, redacted, "prod") == f"x {encoded} y"


@pytest.mark.parametrize("shift", [0, 1, 2])
def test_a_value_inside_a_larger_base64_blob_is_replaced(redactor, shift):
    # Zero bytes around the value keep the blob's entropy under the generic rule's threshold, so the
    # replacement here is the stored value's form, not the rule catching the whole blob.
    redactor.remember(_SECRET, "prod")
    blob = base64.b64encode(b"\x00" * (30 + shift) + _SECRET.encode() + b"\x00" * 30).decode()
    redacted = redactor.redact(f"[{blob}]", "prod")
    [token] = find_markers(redacted)
    restored = redactor.resolve(token, "prod")
    assert isinstance(restored, str) and restored in blob and restored != _SECRET
    assert _swap_back(redactor, redacted, "prod") == f"[{blob}]"


def test_a_marker_is_refused_on_another_host(redactor):
    redactor.remember(_SECRET, "prod")
    [token] = find_markers(redactor.redact(_SECRET, "prod"))
    assert redactor.resolve(token, "local") is Refusal.OTHER_HOST


def test_a_value_stored_for_one_host_is_redacted_in_anothers_output_with_that_hosts_marker(redactor):
    redactor.remember(_SECRET, "prod")
    redacted = redactor.redact(f"echo {_SECRET}", "local")
    [token] = find_markers(redacted)
    assert redactor.resolve(token, "local") == _SECRET
    assert redactor.resolve(token, "prod") is Refusal.OTHER_HOST


def test_a_marker_from_another_process_or_altered_is_refused(redactor):
    other = Redactor(host_labels=lambda: _HOSTS)
    other.remember(_SECRET, "prod")
    [token] = find_markers(other.redact(_SECRET, "prod"))
    assert redactor.resolve(token, "prod") is Refusal.NOT_THIS_PROCESS
    assert redactor.resolve("not-a-marker", "prod") is Refusal.MALFORMED


def test_a_discovered_value_is_stored_so_its_other_forms_are_matched(redactor):
    token = "Zq8xT2mR9vLk4WnB7yHc3PdF6sJg1QaE5uXo0Itb"
    first = redactor.redact(f"blob {token}", "prod")
    assert token not in first
    hexed = forms.render(token, 13)  # hex is never matched by the rules alone
    assert hexed not in redactor.redact(f"hex {hexed}", "prod")


def test_an_unstored_value_gets_an_ordinary_marker_that_is_refused_in_writes(redactor, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_DISCOVERED_VALUES", 0)
    redacted = redactor.redact("blob Zq8xT2mR9vLk4WnB7yHc3PdF6sJg1QaE5uXo0Itb", "prod")
    [token] = find_markers(redacted)
    assert markers.MARKER_PATTERN.search(redacted)
    assert redactor.resolve(token, "prod") is Refusal.UNSTORED


def test_structured_results_are_walked_keys_and_all(redactor):
    redactor.remember(_SECRET, "prod")
    result = {
        "Config": {"Env": [f"DB_PASS={_SECRET}", "PATH=/usr/bin"]},
        _SECRET: (1, _SECRET),
        "count": 3,
        "raw": b"bytes stay as they are",
        "none": None,
    }
    redacted = redactor.redact(result, "prod")
    assert _SECRET not in repr(redacted)
    assert redacted["Config"]["Env"][1] == "PATH=/usr/bin"
    assert (redacted["count"], redacted["raw"], redacted["none"]) == (3, b"bytes stay as they are", None)
    assert isinstance(next(v for k, v in redacted.items() if k.startswith("[REDACTED:")), tuple)


def test_a_public_field_is_left_to_known_values_only(redactor):
    redactor.remember(_SECRET, "prod")
    random_looking = "Zq8xT2mR9vLk4WnB7yHc3PdF6sJg1QaE5uXo0Itb"
    redacted = redactor.redact({"SandboxKey": random_looking, "TrustRoot": f"x {_SECRET}"}, "prod")
    assert redacted["SandboxKey"] == random_looking
    assert _SECRET not in redacted["TrustRoot"]


def test_at_the_overall_budget_every_call_is_refused(redactor, monkeypatch):
    monkeypatch.setattr(store_module, "OVERALL_BUDGET_BYTES", 400)
    redactor.remember("first-harvested-value", "prod")
    with pytest.raises(RedactionBudgetError):
        redactor.remember("y" * 300, "prod")
    assert redactor.budget_reached()
    with pytest.raises(RedactionBudgetError):
        redactor.redact("anything", "local")


def test_running_out_of_identifiers_refuses_the_call_and_old_markers(redactor, monkeypatch):
    redactor.remember(_SECRET, "prod")
    [old] = find_markers(redactor.redact(_SECRET, "prod"))
    monkeypatch.setattr(store_module, "_MAX_VALUE_ID", 0)
    # The call that runs out is refused, since the values it stored before are in the old generation.
    with pytest.raises(RedactionResetError) as caught:
        redactor.remember("a-second-harvested-value", "prod")
    assert isinstance(caught.value, DockerMcpError) and not isinstance(caught.value, ToolRefusalError)
    assert "retry it as it is" in str(caught.value)
    assert redactor.resolve(old, "prod") is Refusal.NOT_THIS_PROCESS
    # The retried call works in the new generation.
    redactor.remember("a-second-harvested-value", "prod")
    [new] = find_markers(redactor.redact("a-second-harvested-value", "prod"))
    assert redactor.resolve(new, "prod") == "a-second-harvested-value"


def test_running_out_of_marker_counter_refuses_the_call_rather_than_repeating_or_leaking(redactor, monkeypatch):
    redactor.remember(_SECRET, "prod")
    [old] = find_markers(redactor.redact(_SECRET, "prod"))
    monkeypatch.setattr(redactor._state.codec, "_counter", markers._MAX_COUNTER + 1)
    with pytest.raises(RedactionResetError):
        redactor.redact(f"x {_SECRET}", "prod")
    # The next call works under a new key once the value is stored again; old markers are refused.
    redactor.remember(_SECRET, "prod")
    assert _SECRET not in redactor.redact(f"x {_SECRET}", "prod")
    assert redactor.resolve(old, "prod") is Refusal.NOT_THIS_PROCESS


def test_a_failure_inside_redaction_propagates_rather_than_returning_the_output(redactor, monkeypatch):
    def broken(_text):
        raise RuntimeError("rule failed")

    monkeypatch.setattr(rules, "find", broken)
    with pytest.raises(RuntimeError, match="rule failed"):
        redactor.redact("anything", "prod")


def test_concurrent_redaction_and_storing_never_leak_or_mix_values(redactor):
    values = [f"harvested-value-{i:04d}-{os.urandom(6).hex()}" for i in range(200)]
    failures: list[str] = []

    def work(chunk: list[str]) -> None:
        for value in chunk:
            redactor.remember(value, "prod")
            redacted = redactor.redact(f"out {value} end", "prod")
            if value in redacted:
                failures.append("leaked")
            [token] = find_markers(redacted)
            if redactor.resolve(token, "prod") != value:
                failures.append("wrong value")

    threads = [threading.Thread(target=work, args=(values[i::8],)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert redactor.report("prod")["harvested_values"] == 200


def test_remembering_takes_the_tier_given(redactor):
    redactor.remember("a-value-found-by-a-rule", "prod", Tier.DISCOVERED)
    assert redactor.report("prod")["discovered_values"] == 1


@pytest.mark.parametrize("flag", ["--password", "-password", "--api-key", "--token"])
def test_a_flag_and_its_value_in_separate_elements_of_a_command_list(redactor, flag):
    result = {"Cmd": ["app", flag, "wordpass", "--verbose"], "Args": (flag, "wordpass")}
    redacted = redactor.redact(result, "prod")
    assert "wordpass" not in repr(redacted)
    assert redacted["Cmd"][0] == "app" and redacted["Cmd"][1] == flag and redacted["Cmd"][3] == "--verbose"
    assert isinstance(redacted["Args"], tuple)


def test_a_flag_followed_by_another_flag_or_a_non_credential_flag_leaves_the_list_alone(redactor):
    result = ["app", "--password", "--verbose", "--name", "plainword"]
    assert redactor.redact(result, "prod") == result


def test_a_path_after_a_credential_flag_is_left_alone_in_a_list_and_in_text(redactor):
    # `--tlscert /certs/server/cert.pem` (the dind entrypoint) and `--password-file` name where a credential
    # is; taken as one, the path would be stored and replaced in every mount and command on the host.
    result = {
        "Args": ["dockerd", "--tlscert", "/certs/server/cert.pem", "--tlskey", "/certs/server/key.pem"],
        "Cmd": 'app --password-file /run/secrets/db --password "/Summer 2025!"',
    }
    redacted = redactor.redact(result, "prod")
    assert redacted["Args"] == result["Args"]
    assert "--password-file /run/secrets/db" in redacted["Cmd"]
    assert "Summer" not in redacted["Cmd"]  # not a plain path, so still taken as the flag's value
    assert redactor.report("prod")["discovered_values"] == 1


def test_partly_overlapping_credentials_leave_nothing_in_clear(redactor):
    # A known value immediately followed by a random run the generic rule finds: the run starts inside
    # the known value's text, so replacing either alone would leave part of the other.
    redactor.remember("pass:word123", "prod")
    tail = "Zq8xT2mR9vLk4WnB7yHc3PdF6sJg1QaE5uXo0Itb"
    text = f"x pass:word123{tail} y"
    redacted = redactor.redact(text, "prod")
    assert "pass:" not in redacted and "word123" not in redacted and tail[:10] not in redacted
    assert _swap_back(redactor, redacted, "prod") == text


def test_two_stored_values_that_partly_overlap_are_both_covered(redactor):
    redactor.remember("alpha-bravo-charlie", "prod")
    redactor.remember("charlie-delta-echo", "prod")
    text = "x alpha-bravo-charlie-delta-echo y"
    redacted = redactor.redact(text, "prod")
    assert "alpha" not in redacted and "echo" not in redacted
    assert _swap_back(redactor, redacted, "prod") == text


def test_the_budget_refusal_tells_the_client_what_to_do(redactor, monkeypatch):
    monkeypatch.setattr(store_module, "OVERALL_BUDGET_BYTES", 400)
    redactor.remember("first-harvested-value", "prod")
    with pytest.raises(RedactionBudgetError):
        redactor.remember("y" * 300, "prod")
    with pytest.raises(RedactionBudgetError) as caught:
        redactor.redact("anything", "local")
    assert isinstance(caught.value, ToolRefusalError) and "restart the server" in str(caught.value)


@pytest.mark.parametrize(
    ("text", "replaced"),
    [
        ("DB_PASSWORD=pw", True),
        ('{"password": "pw"}', True),
        ("login with pw now", True),
        ("pw", True),
        ("[pw]", True),
        ("(pw)", True),
        ("the password is pw.", True),
        ("a=pw; b=1", True),
        ("/pw/", True),
        ("user@pw", True),
        ("cd /home/pwd/x", False),  # inside another word: left alone, and nothing points at the value
        ("pwned", False),
        ("my_pw", False),  # an underscore joins words in an identifier
    ],
)
def test_a_short_harvested_value_is_replaced_only_as_a_whole_token(redactor, text, replaced):
    redactor.remember("pw", "prod")
    out = redactor.redact(text, "prod")
    assert ("[REDACTED:" in out) is replaced
    if not replaced:
        assert out == text


def test_a_value_of_the_short_length_or_more_is_still_replaced_inside_words(redactor):
    redactor.remember("hunter22", "prod")  # eight characters: matched anywhere
    assert "hunter22" not in redactor.redact("xhunter22x", "prod")

import threading

import pytest

from docker_mcp.redaction import markers
from docker_mcp.redaction.markers import MARKER_PATTERN, CounterExhaustedError, MarkerCodec, MarkerFault, MarkerFields
from docker_mcp.redaction.store import UNSTORED_VALUE_ID


def _token(marker: str) -> str:
    match = MARKER_PATTERN.fullmatch(marker)
    assert match, marker
    return match.group(1)


def test_a_marker_reads_back_to_what_it_recorded():
    codec = MarkerCodec()
    marker = codec.issue(41, 13, 2)
    assert codec.read(_token(marker)) == MarkerFields(value_id=41, form_id=13, host_index=2)


def test_two_markers_for_the_same_value_cannot_be_linked():
    codec = MarkerCodec()
    first, second = codec.issue(7, 0, 0), codec.issue(7, 0, 0)
    assert first != second
    # Nothing shared beyond the fixed prefix: the tokens are unrelated.
    assert sum(a == b for a, b in zip(_token(first), _token(second), strict=True)) < 8


def test_a_marker_for_an_unstored_value_has_the_same_shape_as_any_other():
    codec = MarkerCodec()
    stored, unstored = codec.issue(3, 0, 0), codec.issue(UNSTORED_VALUE_ID, 0, 0)
    assert len(stored) == len(unstored) == len("[REDACTED:]") + 22
    assert MARKER_PATTERN.fullmatch(unstored)


def test_a_marker_from_another_process_or_altered_is_not_this_processes():
    codec, other = MarkerCodec(), MarkerCodec()
    token = _token(other.issue(1, 0, 0))
    assert codec.read(token) is MarkerFault.NOT_THIS_PROCESS
    own = _token(codec.issue(1, 0, 0))
    altered = own[:5] + ("A" if own[5] != "A" else "B") + own[6:]
    assert codec.read(altered) is MarkerFault.NOT_THIS_PROCESS


@pytest.mark.parametrize("token", ["", "short", "A" * 21, "A" * 23, "!" * 22])
def test_a_token_that_is_not_a_marker_is_malformed(token):
    assert MarkerCodec().read(token) is MarkerFault.MALFORMED


def test_the_counter_refuses_to_wrap(monkeypatch):
    codec = MarkerCodec()
    monkeypatch.setattr(codec, "_counter", markers._MAX_COUNTER + 1)
    with pytest.raises(CounterExhaustedError):
        codec.issue(1, 0, 0)


def test_concurrent_issuing_never_repeats_a_marker():
    codec = MarkerCodec()
    issued: list[list[str]] = [[] for _ in range(8)]

    def work(slot: list[str]) -> None:
        slot.extend(codec.issue(1, 0, 0) for _ in range(2_000))

    threads = [threading.Thread(target=work, args=(slot,)) for slot in issued]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    everything = [marker for slot in issued for marker in slot]
    assert len(set(everything)) == len(everything) == 16_000

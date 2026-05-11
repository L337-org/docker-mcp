import pytest

from tools._utils import drop_none, join_bounded


def test_drop_none_filters_none_values():
    assert drop_none(a=1, b=None, c="x", d=None) == {"a": 1, "c": "x"}


def test_drop_none_keeps_falsy_non_none_values():
    assert drop_none(zero=0, empty="", emptylist=[], emptydict={}, false=False) == {
        "zero": 0,
        "empty": "",
        "emptylist": [],
        "emptydict": {},
        "false": False,
    }


def test_drop_none_with_no_kwargs_returns_empty_dict():
    assert drop_none() == {}


def test_drop_none_with_all_none_returns_empty_dict():
    assert drop_none(a=None, b=None) == {}


def test_join_bounded_concatenates_chunks_under_cap():
    assert join_bounded(iter([b"foo", b"bar"]), max_bytes=1024, what="test") == b"foobar"


def test_join_bounded_at_exact_cap_succeeds():
    assert join_bounded(iter([b"abc", b"def"]), max_bytes=6, what="test") == b"abcdef"


def test_join_bounded_raises_when_cap_exceeded():
    with pytest.raises(ValueError, match="exceeded max_bytes=4"):
        join_bounded(iter([b"abc", b"de"]), max_bytes=4, what="test")


def test_join_bounded_empty_stream_returns_empty_bytes():
    assert join_bounded(iter([]), max_bytes=10, what="test") == b""


def test_join_bounded_rejects_negative_max_bytes():
    with pytest.raises(ValueError, match="max_bytes must be non-negative"):
        join_bounded(iter([b"x"]), max_bytes=-1, what="test")


def test_join_bounded_does_not_extend_past_cap():
    # Stream yields one huge chunk that would push the buffer over the cap on the very first
    # iteration — verify we raise *before* extending so the bytearray never grows past max_bytes.
    consumed = []

    def stream():
        consumed.append("big")
        yield b"x" * 1000

    with pytest.raises(ValueError, match="exceeded max_bytes=10"):
        join_bounded(stream(), max_bytes=10, what="test")
    assert consumed == ["big"]

import pytest

from docker_mcp.tools._utils import MAX_PAYLOAD_BYTES, drop_none, join_bounded, stream_to_file


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


def test_in_band_cap_is_32_mib():
    # The in-band default was lowered from 1 GiB so large payloads use the *_to_file variants.
    assert MAX_PAYLOAD_BYTES == 32 * 1024 * 1024


def test_stream_to_file_writes_chunks_and_counts_bytes(tmp_path):
    dest = tmp_path / "out.bin"
    path, written = stream_to_file(iter([b"ab", b"cde"]), str(dest))
    assert path == dest
    assert written == 5
    assert dest.read_bytes() == b"abcde"


def test_stream_to_file_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path, _ = stream_to_file(iter([b"x"]), "~/out.bin")
    assert path == tmp_path / "out.bin"
    assert path.read_bytes() == b"x"


def test_stream_to_file_refuses_existing_without_overwrite(tmp_path):
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")
    with pytest.raises(FileExistsError, match="already exists"):
        stream_to_file(iter([b"new"]), str(dest))
    assert dest.read_bytes() == b"old"


def test_stream_to_file_overwrite_replaces(tmp_path):
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")
    _, written = stream_to_file(iter([b"new!"]), str(dest), overwrite=True)
    assert written == 4
    assert dest.read_bytes() == b"new!"


def _raising_chunks():
    yield b"partial-data"
    raise RuntimeError("daemon disconnect")


def test_stream_to_file_leaves_no_file_on_midstream_failure(tmp_path):
    dest = tmp_path / "out.bin"
    with pytest.raises(RuntimeError, match="daemon disconnect"):
        stream_to_file(_raising_chunks(), str(dest))
    assert not dest.exists()
    # the sibling temp file is cleaned up too — no .partial leftovers
    assert list(tmp_path.iterdir()) == []


def test_stream_to_file_preserves_original_on_failure_even_with_overwrite(tmp_path):
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"original")
    with pytest.raises(RuntimeError, match="daemon disconnect"):
        stream_to_file(_raising_chunks(), str(dest), overwrite=True)
    # temp+replace means the original is never truncated until a complete write succeeds
    assert dest.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]


class _ClosingChunks:
    """An iterator that records whether close() was called — to assert streams aren't leaked."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._it = iter(chunks)
        self.closed = False

    def __iter__(self) -> _ClosingChunks:
        return self

    def __next__(self) -> bytes:
        return next(self._it)

    def close(self) -> None:
        self.closed = True


def test_join_bounded_closes_stream_on_success():
    chunks = _ClosingChunks([b"a", b"b"])
    join_bounded(chunks, max_bytes=100, what="test")
    assert chunks.closed


def test_join_bounded_closes_stream_on_abort():
    chunks = _ClosingChunks([b"x" * 10, b"y" * 10])
    with pytest.raises(ValueError, match="exceeded max_bytes"):
        join_bounded(chunks, max_bytes=5, what="test")
    assert chunks.closed


def test_stream_to_file_closes_stream(tmp_path):
    chunks = _ClosingChunks([b"a", b"b"])
    stream_to_file(chunks, str(tmp_path / "o.bin"))
    assert chunks.closed

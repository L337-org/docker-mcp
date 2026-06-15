import types
from pathlib import Path

import pytest
from docker.errors import DockerException

from docker_mcp.tools import _utils
from docker_mcp.tools._utils import (
    MAX_PAYLOAD_BYTES,
    assert_host_writable,
    classify_host_kernel,
    close_stream_quietly,
    drop_none,
    env_flag,
    host_read_path,
    in_container,
    join_bounded,
    stream_to_file,
)


# ---------- close_stream_quietly ----------


def test_close_stream_quietly_noop_without_close_method():
    close_stream_quietly(object())  # no .close() — must not raise


def test_close_stream_quietly_calls_close():
    class _S:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    s = _S()
    close_stream_quietly(s)
    assert s.closed


@pytest.mark.parametrize(
    "exc",
    [
        OSError("shut down"),  # already-closed socket
        AttributeError("_sock"),  # transport internals differ
        DockerException("Cancellable streams not supported for the SSH protocol"),  # ssh:// daemon
    ],
)
def test_close_stream_quietly_swallows_any_close_error(exc):
    # These are the real CancellableStream.close() failure modes; none may escape this best-effort
    # helper. DockerException in particular is what docker-py raises on every ssh:// close.
    class _S:
        def close(self):
            raise exc

    close_stream_quietly(_S())  # must not raise


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


# ---------- env_flag ----------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", "  on  "])
def test_env_flag_truthy(monkeypatch, value):
    monkeypatch.setenv("DOCKER_MCP_X", value)
    assert env_flag("DOCKER_MCP_X") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_env_flag_falsy(monkeypatch, value):
    monkeypatch.setenv("DOCKER_MCP_X", value)
    assert env_flag("DOCKER_MCP_X") is False


def test_env_flag_unset_is_false(monkeypatch):
    monkeypatch.delenv("DOCKER_MCP_X", raising=False)
    assert env_flag("DOCKER_MCP_X") is False


# ---------- in_container ----------
# NOTE: the autouse `_force_host_install` fixture patches `_utils.in_container`, but `from ... import
# in_container` here binds the real function, so these exercise the genuine logic.


def test_in_container_true_via_env(monkeypatch):
    monkeypatch.setenv("DOCKER_MCP_IN_CONTAINER", "1")
    assert in_container() is True


def test_in_container_true_via_dockerenv(monkeypatch):
    monkeypatch.delenv("DOCKER_MCP_IN_CONTAINER", raising=False)
    monkeypatch.setattr(_utils.Path, "exists", lambda self: str(self) == "/.dockerenv")
    assert in_container() is True


def test_in_container_false_when_no_signal(monkeypatch):
    monkeypatch.delenv("DOCKER_MCP_IN_CONTAINER", raising=False)
    monkeypatch.setattr(_utils.Path, "exists", lambda self: False)
    assert in_container() is False


# ---------- _host_backed ----------


def _mounts(*pairs):
    return list(pairs)


def test_host_backed_true_for_real_bind_mount(monkeypatch):
    monkeypatch.setattr(_utils, "_read_mountinfo", lambda: _mounts(("/", "overlay"), ("/host/data", "ext4")))
    assert _utils._host_backed(Path("/host/data/sub/img.tar")) is True


def test_host_backed_false_for_overlay_root(monkeypatch):
    monkeypatch.setattr(_utils, "_read_mountinfo", lambda: _mounts(("/", "overlay")))
    assert _utils._host_backed(Path("/tmp/img.tar")) is False


def test_host_backed_false_for_tmpfs_mount(monkeypatch):
    # A tmpfs is in-memory and also lost on exit, so it must not count as host-backed.
    monkeypatch.setattr(_utils, "_read_mountinfo", lambda: _mounts(("/", "overlay"), ("/scratch", "tmpfs")))
    assert _utils._host_backed(Path("/scratch/img.tar")) is False


def test_host_backed_longest_prefix_wins(monkeypatch):
    # /host is host-backed but the nested /host/cache is tmpfs — the longest match decides.
    monkeypatch.setattr(
        _utils, "_read_mountinfo", lambda: _mounts(("/", "overlay"), ("/host", "ext4"), ("/host/cache", "tmpfs"))
    )
    assert _utils._host_backed(Path("/host/keep/x")) is True
    assert _utils._host_backed(Path("/host/cache/x")) is False


def test_host_backed_false_when_mountinfo_unavailable(monkeypatch):
    monkeypatch.setattr(_utils, "_read_mountinfo", list)  # empty
    assert _utils._host_backed(Path("/anything")) is False


def test_read_mountinfo_parses_and_unescapes(monkeypatch):
    content = (
        "36 35 98:0 / / rw,noatime - overlay overlay rw\n"
        "41 36 0:5 / /host/my\\040dir rw - ext4 /dev/sda1 rw\n"
        "garbage line without separator\n"
    )
    monkeypatch.setattr(_utils.Path, "read_text", lambda self, **kw: content)
    assert _utils._read_mountinfo() == [("/", "overlay"), ("/host/my dir", "ext4")]


# ---------- assert_host_writable ----------


def test_assert_host_writable_noop_on_host():
    # autouse fixture already pins in_container False; an unmapped path must be allowed.
    assert assert_host_writable("/nowhere/img.tar") is None


def test_assert_host_writable_allows_mapped_path_in_container(monkeypatch):
    monkeypatch.setattr(_utils, "in_container", lambda: True)
    monkeypatch.setattr(_utils, "_host_backed", lambda path: True)
    assert assert_host_writable("/host/img.tar") is None


def test_assert_host_writable_rejects_unmapped_path_in_container(monkeypatch):
    monkeypatch.setattr(_utils, "in_container", lambda: True)
    monkeypatch.setattr(_utils, "_host_backed", lambda path: False)
    with pytest.raises(RuntimeError, match="bind-mounted in"):
        assert_host_writable("/scratch/img.tar")


def test_stream_to_file_guard_blocks_unmapped_write_in_container(monkeypatch, tmp_path):
    monkeypatch.setattr(_utils, "in_container", lambda: True)
    monkeypatch.setattr(_utils, "_host_backed", lambda path: False)
    dest = tmp_path / "out.bin"
    with pytest.raises(RuntimeError, match="lost when the container exits"):
        stream_to_file(iter([b"data"]), str(dest))
    assert not dest.exists()  # refused before any bytes were written


# ---------- host_read_path ----------


def test_host_read_path_noop_on_host_returns_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # missing file on host: still returned (the natural open() error surfaces later)
    assert host_read_path("~/missing.tar") == tmp_path / "missing.tar"


def test_host_read_path_existing_file_allowed_in_container(monkeypatch, tmp_path):
    monkeypatch.setattr(_utils, "in_container", lambda: True)
    monkeypatch.setattr(_utils, "_host_backed", lambda path: False)
    existing = tmp_path / "img.tar"
    existing.write_bytes(b"x")
    assert host_read_path(str(existing)) == existing


def test_host_read_path_missing_unmapped_raises_in_container(monkeypatch, tmp_path):
    monkeypatch.setattr(_utils, "in_container", lambda: True)
    monkeypatch.setattr(_utils, "_host_backed", lambda path: False)
    with pytest.raises(RuntimeError, match="no such path is visible"):
        host_read_path(str(tmp_path / "missing.tar"))


def test_host_read_path_missing_but_mapped_allowed_in_container(monkeypatch, tmp_path):
    monkeypatch.setattr(_utils, "in_container", lambda: True)
    monkeypatch.setattr(_utils, "_host_backed", lambda path: True)
    target = tmp_path / "missing.tar"
    assert host_read_path(str(target)) == target


# ---------- classify_host_kernel ----------


@pytest.mark.parametrize(
    "release, expected",
    [
        ("5.15.0-91-generic", "linux"),
        ("5.15.153.1-microsoft-standard-WSL2", "wsl2"),
        ("6.6.31-linuxkit", "docker-desktop"),
    ],
)
def test_classify_host_kernel(monkeypatch, release, expected):
    monkeypatch.setattr(_utils.os, "uname", lambda: types.SimpleNamespace(release=release))
    assert classify_host_kernel() == expected


def test_classify_host_kernel_unknown_without_uname(monkeypatch):
    def _no_uname():
        raise AttributeError("os.uname is POSIX-only")

    monkeypatch.setattr(_utils.os, "uname", _no_uname)
    assert classify_host_kernel() == "unknown"

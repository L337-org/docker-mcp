# internal helpers shared across tool modules

import os
import tempfile
from collections.abc import Iterable
from functools import lru_cache
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import IO, Any

# Alias-aware env lookups, re-exported so tool modules can keep importing them from _utils.
from docker_mcp.exceptions import ToolInputError
from docker_mcp._env import env_flag, read_env  # noqa: F401

# Default cap (32 MiB) for tools that buffer a daemon-side byte stream *in band* and return it
# through the MCP protocol (where it is base64-encoded into the agent's context). Anything larger
# is impractical in band - use the `*_to_file` / `*_from_file` tool variants, which stream to/from
# a host path instead. The cap is per-call and overridable via each tool's `max_bytes` argument.
MAX_PAYLOAD_BYTES = 33_554_432

# Set in our published container images so the in-container guards engage even if /.dockerenv is
# ever absent (e.g. an unusual runtime). On the host (uvx) install neither signal is present, so
# every guard below is a no-op and the existing behaviour is unchanged.
IN_CONTAINER_ENV = "DOCKER_MCP_SERVER_IN_CONTAINER"

# /proc/self/mountinfo fstypes that never represent a host bind mount: the container's own overlay
# root and the assorted pseudo / in-memory filesystems. A path whose nearest mount is one of these
# is NOT backed by the host, so a write there is lost when the container exits. Real bind mounts
# (ext4, xfs, virtiofs, fuse.grpcfuse on Docker Desktop, ...) fall outside this set.
_PSEUDO_FSTYPES = frozenset(
    {
        "overlay",
        "tmpfs",
        "proc",
        "sysfs",
        "cgroup",
        "cgroup2",
        "devpts",
        "mqueue",
        "shm",
        "devtmpfs",
        "fuse.lxcfs",
        "nsfs",
        "tracefs",
        "debugfs",
        "securityfs",
        "pstore",
        "bpf",
        "configfs",
        "hugetlbfs",
        "fusectl",
        "ramfs",
        "binfmt_misc",
    }
)


@lru_cache(maxsize=1)
def package_version() -> str:
    """
    Installed docker-mcp-server version, or 'unknown' when it can't be resolved.

    The single version resolver for every externally-visible surface (User-Agent strings, provenance
    labels), so they always agree. Called at import time to build module-level User-Agent constants,
    so it must never raise - any metadata-lookup failure (not just a missing package) falls back to
    'unknown' rather than crashing the importing module. Cached: the metadata lookup is done once.
    """
    try:
        return _pkg_version("docker-mcp-server")
    except Exception:  # noqa: BLE001 - import-time helper must never raise; any failure -> safe fallback
        return "unknown"


def in_container() -> bool:
    """
    True when this server is running inside a container.

    Docker writes `/.dockerenv` into every container, and our published images additionally set
    `DOCKER_MCP_SERVER_IN_CONTAINER=1`. Either signal flips on the in-container filesystem and
    self-termination guards; on the host install neither is present so those guards are inert.
    """
    return env_flag(IN_CONTAINER_ENV) or Path("/.dockerenv").exists()


def _unescape_mountinfo_field(field: str) -> str:
    r"""Decode the octal escapes the kernel applies to mountinfo path fields (space/tab/newline/\\)."""
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _read_mountinfo() -> list[tuple[str, str]]:
    """
    Return (mount_point, fstype) pairs from /proc/self/mountinfo, or [] if it can't be read.

    The format is `ID PARENT MAJ:MIN ROOT MOUNT_POINT OPTIONS... - FSTYPE SOURCE SUPER_OPTS`; the
    number of optional fields before the literal ` - ` separator varies, so we split on it.
    """
    try:
        raw = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    mounts: list[tuple[str, str]] = []
    for line in raw.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or not right_fields:
            continue
        mounts.append((_unescape_mountinfo_field(left_fields[4]), right_fields[0]))
    return mounts


def _host_backed(path: Path) -> bool:
    """
    True if `path` falls under a real host bind mount inside the container.

    Finds the longest mount point in /proc/self/mountinfo that is a prefix of the path (matching the
    directory it would live in, since the file itself may not exist yet) and checks that mount's
    fstype is not a pseudo / overlay filesystem. Conservative: returns False when mountinfo is
    unavailable, so the guards prefer a (recoverable) false alarm over a silent data-loss write.
    """
    mounts = _read_mountinfo()
    if not mounts:
        return False
    try:
        target = str(path.resolve())
    except OSError:
        target = str(path)
    best_len = -1
    best_fstype = ""
    for mount_point, fstype in mounts:
        mp = mount_point.rstrip("/") or "/"
        covers = mp == "/" or target == mp or target.startswith(mp + "/")
        if covers and len(mp) > best_len:
            best_len, best_fstype = len(mp), fstype
    return best_len >= 0 and best_fstype not in _PSEUDO_FSTYPES


def _unmapped_path_message(path: Path, *, for_write: bool) -> str:
    """Actionable error text telling the user how to bind-mount a host directory into the container."""
    if for_write:
        verb, consequence = (
            "write to",
            ("it would land in the container's ephemeral filesystem and be lost when the container exits"),
        )
    else:
        verb, consequence = "read from", "no such path is visible inside the container"
    parent = path.parent
    return (
        f"Cannot {verb} {path}: {consequence}. This docker-mcp-server is running in a container, so "
        f"host paths must be bind-mounted in. Add a mount for the directory to the `docker run` args "
        f"in your MCP client config - e.g. `-v {parent}:{parent}` (using the same path inside and out "
        f"keeps host and container paths identical) - then retry. Small payloads can use the in-band "
        f"byte tools instead, which need no mount."
    )


def assert_host_writable(dest_path: str) -> None:
    """
    Pre-flight guard for the `*_to_file` tools: refuse a destination that isn't on a host bind mount.

    A no-op outside a container. Inside one, a write to a non-mounted path silently lands in the
    container's overlay layer and vanishes on `--rm`, so we fail up front with mount instructions
    rather than reporting a phantom success.
    """
    if not in_container():
        return
    if not _host_backed(Path(dest_path).expanduser()):
        raise ToolInputError(_unmapped_path_message(Path(dest_path).expanduser(), for_write=True))


def host_read_path(file_path: str) -> Path:
    """
    Resolve a host read path, enriching the "missing file" case with mount guidance in a container.

    Returns the expanded path unchanged on the host install, or when the file genuinely exists (it
    may legitimately live inside the container's image). Only when running in a container, the file
    is absent, and its location isn't a host bind mount do we raise the actionable mount message
    instead of letting a bare FileNotFoundError surface.
    """
    path = Path(file_path).expanduser()
    if in_container() and not path.exists() and not _host_backed(path):
        raise ToolInputError(_unmapped_path_message(path, for_write=False))
    return path


def open_host_read_file(file_path: str) -> IO[bytes]:
    """Open a caller-supplied file for reading, as a `ToolInputError` when it cannot be opened.

    `host_read_path` resolves the path and explains an unmapped one inside a container; this adds the
    open itself, because a missing file, a directory passed where a file belongs, or a permission
    denial all raise a builtin `OSError` subclass - which the SDK reports as a crash with the text
    withheld, leaving the caller nothing to act on for an argument only they control.

    Converting `OSError` wholesale is safe here in a way it is not for writes: every failure mode of
    opening a named file for reading is something the caller can address by naming a different one.

    :param file_path: the caller's path
    :returns: IO[bytes] - the open binary handle, for use as a context manager and for handing
        straight to a docker-py call that wants a binary file-like
    """
    path = host_read_path(file_path)
    try:
        return path.open("rb")
    except OSError as exc:
        raise ToolInputError(
            f"Cannot read {str(path)!r}: {exc.strerror or exc}. Pass a path to an existing, "
            f"readable file on the host running this server."
        ) from exc


def classify_host_kernel() -> str:
    """
    Best-effort host-OS classification from the shared kernel string (containers share the host
    kernel), used to tailor socket-mount hints when the daemon is unreachable.

    Returns 'wsl2' (Windows/WSL2), 'docker-desktop' (LinuxKit VM - usually macOS), 'linux' (a native
    Linux daemon), or 'unknown' when os.uname() is unavailable (non-POSIX).
    """
    try:
        release = os.uname().release.lower()
    except AttributeError:  # os.uname() is POSIX-only
        return "unknown"
    if "microsoft" in release or "wsl" in release:
        return "wsl2"
    if "linuxkit" in release:
        return "docker-desktop"
    return "linux"


def close_stream_quietly(stream: Any) -> None:
    """
    Best-effort close of a docker `CancellableStream` (or anything with `.close()`).

    Used by the log/event tools that arm a watchdog timer to `stream.close()` on a wall-clock
    deadline: when the tool finishes first we still close to free the socket, and a close that
    races with the timer (or a stream that's already shut down) must not surface as an error.

    Catches broadly on purpose: docker-py's `CancellableStream.close()` reaches into the response's
    private socket internals, so beyond the expected `OSError` (already-shut-down socket) it can
    raise `AttributeError` on transports whose internals differ, and it *always* raises
    `DockerException` for an `ssh://` daemon (SSH streams aren't cancellable). This helper runs in a
    watchdog-timer thread and in tool `finally` blocks, so none of those may escape - mirrors
    `client.py:_close_client_quietly`.
    """
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: S110, BLE001 - best-effort close; see docstring for why it's broad
        pass


def drop_none(**kwargs: Any) -> dict[str, Any]:
    """
    Return a dict containing only the kwargs whose value is not None.

    Used at `docker` module call sites where None means "let the SDK pick the default"
    and passing the key explicitly with value=None would override that default.
    """
    return {k: v for k, v in kwargs.items() if v is not None}


def stream_to_file(chunks: Iterable[bytes], dest_path: str, *, overwrite: bool = False) -> tuple[Path, int]:
    """
    Stream byte chunks to a host file, returning the resolved path and the number of bytes written.

    Used by the `*_to_file` tool variants so a large daemon-side payload (image save, container
    export/archive) is written straight to disk instead of being buffered in memory and returned
    through MCP. `~` in `dest_path` is expanded; an existing file is refused unless `overwrite=True`,
    so the agent can't silently clobber a file the server's user can write.

    Writes to a sibling temp file and `os.replace()`s it into place on success, so a mid-stream
    failure (daemon disconnect, disk full, iterator error) never leaves a partial/corrupt file at
    `dest_path` and never truncates an existing file it ends up not replacing. The source iterator
    is best-effort closed either way, so an aborted docker stream doesn't leak its socket.

    When running in a container, refuses up front if `dest_path` isn't on a host bind mount (the
    write would otherwise be silently discarded on `--rm`); see `assert_host_writable`.
    """
    assert_host_writable(dest_path)
    path = Path(dest_path).expanduser()
    if path.exists() and not overwrite:
        raise ToolInputError(f"{path} already exists; pass overwrite=True to replace it.")
    # Checked rather than caught: `mkstemp` below raises `FileNotFoundError`/`NotADirectoryError`
    # for a parent that is missing or is a file, which is the caller's path to fix, but it raises
    # `OSError` for a full or read-only disk too - and that is not the caller's doing. Naming this
    # case explicitly keeps the message actionable without classifying a disk failure as a bad
    # argument.
    if not path.parent.is_dir():
        raise ToolInputError(
            f"Cannot write {str(path)!r}: {str(path.parent)!r} is not a directory. Create it first, "
            f"or choose a dest_path inside one that exists."
        )
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".partial")
    tmp_path = Path(tmp_name)
    written = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
                written += len(chunk)
        # The handle is closed (with-block exited) before replace, so this is safe on Windows too.
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        close_stream_quietly(chunks)
    return path, written


def as_byte_chunks(chunks: Iterable | bytes | bytearray | str) -> Iterable[bytes]:
    """
    Normalize a docker log/stream payload to bytes chunks, ready for `join_bounded`.

    Accepts either a stream of chunks or a whole payload. A whole `bytes`/`bytearray` is yielded as a
    single chunk rather than iterated, because iterating one yields ints that would stringify to
    decimal text and silently corrupt the result. `bytearray` is handled alongside `bytes` for the
    same reason at chunk level: it is not a subclass of `bytes`, so `str()` on one produces the
    literal text "bytearray(b'...')". Anything else is encoded.

    Closes the source in a `finally`. This generator sits between the caller's stream and
    `join_bounded`, and `join_bounded` can only close what it is handed - this wrapper - so without
    this the underlying docker stream's socket leaks whenever the byte cap aborts iteration, which
    is exactly the guarantee `join_bounded`'s own docstring makes.
    """
    if isinstance(chunks, (bytes, bytearray)):
        yield bytes(chunks)
        return
    if isinstance(chunks, str):
        yield chunks.encode("utf-8", errors="replace")
        return
    try:
        for chunk in chunks:
            if isinstance(chunk, (bytes, bytearray)):
                yield bytes(chunk)
            else:
                yield str(chunk).encode("utf-8", errors="replace")
    finally:
        close_stream_quietly(chunks)


def join_bounded(chunks: Iterable[bytes], max_bytes: int, what: str, remedy: str | None = None) -> bytes:
    """
    Concatenate bytes chunks, aborting with ToolInputError if the running total would exceed
    max_bytes. A negative `max_bytes` is a caller bug rather than an answer to give a model, and
    stays a `ValueError`.

    `remedy` is what the message tells the caller to do about it, and it has to be passed by any
    caller whose own cap is fixed. The default names `max_bytes`, which is right for the tools that
    expose it as a parameter and useless for the ones that do not - being told to raise something
    they cannot reach is worse guidance than none, because it reads as actionable.

    Wraps the `b"".join(stream)` pattern used by tools that buffer a whole daemon-side
    payload (container export, image save, container archive) so a pathological input can't
    OOM the MCP server process. The cap is checked *before* the next chunk is appended, so
    the in-memory buffer never grows past `max_bytes`. The source iterator is best-effort closed
    in a finally so aborting on the cap doesn't leak the underlying docker stream's socket.
    """
    if max_bytes < 0:
        raise ValueError(f"max_bytes must be non-negative, got {max_bytes}")
    collected = bytearray()
    try:
        for chunk in chunks:
            if len(collected) + len(chunk) > max_bytes:
                advice = remedy or "Raise max_bytes if a larger payload is intended."
                raise ToolInputError(
                    f"{what} exceeded max_bytes={max_bytes}; aborted to prevent memory exhaustion. {advice}"
                )
            collected.extend(chunk)
    finally:
        close_stream_quietly(chunks)
    return bytes(collected)

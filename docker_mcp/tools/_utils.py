# internal helpers shared across tool modules

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Default cap (32 MiB) for tools that buffer a daemon-side byte stream *in band* and return it
# through the MCP protocol (where it is base64-encoded into the agent's context). Anything larger
# is impractical in band — use the `*_to_file` / `*_from_file` tool variants, which stream to/from
# a host path instead. The cap is per-call and overridable via each tool's `max_bytes` argument.
MAX_PAYLOAD_BYTES = 33_554_432


def close_stream_quietly(stream: Any) -> None:
    """
    Best-effort close of a docker `CancellableStream` (or anything with `.close()`).

    Used by the log/event tools that arm a watchdog timer to `stream.close()` on a wall-clock
    deadline: when the tool finishes first we still close to free the socket, and a close that
    races with the timer (or a stream that's already shut down) must not surface as an error.
    """
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        close()
    except OSError:
        # Socket already shut down (e.g. our close() raced the watchdog timer) — nothing to do.
        pass  # noqa: S110 — intentional: a redundant close is a no-op, not a failure


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
    """
    path = Path(dest_path).expanduser()
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists; pass overwrite=True to replace it.")
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


def join_bounded(chunks: Iterable[bytes], max_bytes: int, what: str) -> bytes:
    """
    Concatenate bytes chunks, aborting with ValueError if the running total would exceed max_bytes.

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
                raise ValueError(
                    f"{what} exceeded max_bytes={max_bytes}; aborted to prevent memory exhaustion. "
                    f"Raise max_bytes if a larger payload is intended."
                )
            collected.extend(chunk)
    finally:
        close_stream_quietly(chunks)
    return bytes(collected)

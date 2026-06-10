# internal helpers shared across tool modules

from collections.abc import Iterable
from typing import Any

# Default cap (1 GiB) for tools that accumulate daemon-side byte streams in memory.
MAX_PAYLOAD_BYTES = 1_073_741_824


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


def join_bounded(chunks: Iterable[bytes], max_bytes: int, what: str) -> bytes:
    """
    Concatenate bytes chunks, aborting with ValueError if the running total would exceed max_bytes.

    Wraps the `b"".join(stream)` pattern used by tools that buffer a whole daemon-side
    payload (container export, image save, container archive) so a pathological input can't
    OOM the MCP server process. The cap is checked *before* the next chunk is appended, so
    the in-memory buffer never grows past `max_bytes`.
    """
    if max_bytes < 0:
        raise ValueError(f"max_bytes must be non-negative, got {max_bytes}")
    collected = bytearray()
    for chunk in chunks:
        if len(collected) + len(chunk) > max_bytes:
            raise ValueError(
                f"{what} exceeded max_bytes={max_bytes}; aborted to prevent memory exhaustion. "
                f"Raise max_bytes if a larger payload is intended."
            )
        collected.extend(chunk)
    return bytes(collected)

# internal helpers shared across tool modules

from typing import Any, Iterable

# Default cap (1 GiB) for tools that accumulate daemon-side byte streams in memory.
MAX_PAYLOAD_BYTES = 1_073_741_824


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

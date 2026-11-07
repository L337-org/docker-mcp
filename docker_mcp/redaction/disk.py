"""Searching the files and archives a write reads from disk for markers, and refusing the write on a match.

A marker means something only to the server process that issued it, so in a file it is useless to Docker,
to another server and to this one after a restart, and the server will not rewrite a user's file to fix
it.  On a redacted host that accepts writes, a call that reads a file holding one is refused before
anything runs, naming the file.  Markers are never swapped back here: replacing text in a file or an
archive member changes its size, which a tar header records.

What a call reads is decided per tool, in `FILE_INPUTS`: the Compose project directory, a stack's or a
bake's working directory, and a build context with its `.dockerignore` honoured, each searched whole,
since any file in it can end up in a container or an image; plus every file named outside them.  Each
directory is the one the CLI resolves the call's relative paths against: for Compose and a stack, the
first compose file's directory where one is named.
`tests/test_redaction_disk.py` holds the table to the write tools that take a path.

An archive passed to `container_archive_put` is searched as bytes: a plain tar stores each member's
contents as one contiguous run, so a byte search finds a marker without unpacking it.  A compressed
archive, which a byte search cannot see into, is refused; each compression is one step in
`_ARCHIVE_FORMATS`, so a streaming search can be added for one later without changing the rest.
"""

import contextlib
import io
import logging
import os
import re
import tarfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from docker_mcp.exceptions import MarkerRefusedError
from docker_mcp.redaction.markers import MARKER_PATTERN, MARKER_PREFIX

logger = logging.getLogger("docker_mcp.redaction")

_PREFIX = MARKER_PREFIX.encode("ascii")
_MARKER_BYTES = re.compile(MARKER_PATTERN.pattern.encode("ascii"))
# The longest marker, so a chunked search keeps enough of the previous chunk to find one spanning two.
_MARKER_LENGTH = len(MARKER_PREFIX) + 22 + 1
_CHUNK = 1024 * 1024

# Past either limit the search stops and the call is refused, rather than run with files unsearched.  The
# same limits as the SSH fallback's staging, which copies the same directories for the same calls.
MAX_SEARCH_FILES = 50_000
MAX_SEARCH_BYTES = 200 * 1024 * 1024

_FILE_ADVICE = (
    "A marker means something only to the server process that issued it and is never swapped back in a file, "
    "so Docker would get it as literal text. Remove it from the file and pass the credential as a tool argument "
    "instead, such as `vars`, an environment or a command, where a marker is swapped back. See REDACTION.md."
)


@dataclass(frozen=True)
class _Input:
    """Something a call reads from disk.

    Attributes:
        path: the file or directory
        build_context: True for a build context, searched with its `.dockerignore` honoured
        dockerfile: the Dockerfile relative to a build context, kept even where `.dockerignore` excludes it
    """

    path: Path
    build_context: bool = False
    dockerfile: str | None = None


def _local(value: Any, base: Path) -> Path | None:
    # A value that names nothing on this machine (an image reference, a URL, `-` for stdin) is not read
    # from disk here, so there is nothing to search.
    if not isinstance(value, str) or not value or value == "-" or "://" in value:
        return None
    # os.path.expanduser leaves an unknown `~user` alone, for the tool to report, where pathlib's raises a
    # bare RuntimeError (the reason buildx._expand_home uses it too).
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = base / path
    return path if path.exists() else None


def _base(directory: Any) -> Path:
    return Path(os.path.expanduser(directory)) if directory else Path(os.getcwd())


def _working_tree(files: Any, working: Path) -> list[_Input]:
    # The directory the tool runs in and resolves relative paths against, which the CLI reads files from,
    # and every named file, inside it or not.
    named = [path for value in files or [] if (path := _local(value, working)) is not None]
    return [_Input(working), *(_Input(path) for path in named)]


def _compose_inputs(arguments: dict[str, Any]) -> list[_Input]:
    # Compose's project directory is the first compose file's, or the directory it runs in.
    working = _base(arguments.get("project_dir"))
    files = arguments.get("files") or []
    first = _local(files[0], working) if files else None
    inputs = _working_tree(files, first.parent if first is not None else working)
    if (source := _local(arguments.get("source"), working)) is not None:
        inputs.append(_Input(source))  # compose_cp copying into a container
    return inputs


def _stack_inputs(arguments: dict[str, Any]) -> list[_Input]:
    working = _base(arguments.get("cwd"))
    files = arguments.get("compose_files") or []
    first = _local(files[0], working) if files else None
    return _working_tree(files, first.parent if first is not None else working)


def _bake_inputs(arguments: dict[str, Any]) -> list[_Input]:
    return _working_tree(arguments.get("files"), _base(arguments.get("cwd")))


def _secret_sources(specs: Iterable[Any]) -> Iterator[str]:
    for spec in specs:
        for component in str(spec).split(","):
            key, _, value = component.partition("=")
            if key.strip() in ("src", "source"):
                yield value.strip()


def _buildx_build_inputs(arguments: dict[str, Any]) -> list[_Input]:
    working = Path(os.getcwd())
    inputs: list[_Input] = []
    context = _local(arguments.get("context"), working)
    file = _local(arguments.get("file"), working)
    if context is not None and context.is_dir():
        inside = file is not None and file.resolve().is_relative_to(context.resolve())
        dockerfile = file.resolve().relative_to(context.resolve()).as_posix() if file is not None and inside else None
        inputs.append(_Input(context, build_context=True, dockerfile=dockerfile))
    if file is not None:
        inputs.append(_Input(file))
    for value in (arguments.get("build_contexts") or {}).values():
        if (path := _local(value, working)) is not None:
            inputs.append(_Input(path, build_context=path.is_dir()))
    for value in _secret_sources(arguments.get("secret") or []):
        if (path := _local(value, working)) is not None:
            inputs.append(_Input(path))
    return inputs


def _image_build_inputs(arguments: dict[str, Any]) -> list[_Input]:
    context = _local(arguments.get("path"), Path(os.getcwd()))
    if context is None or not context.is_dir():
        return []
    # docker-py makes a Dockerfile inside the context relative before excluding paths; the exclusion
    # pattern that keeps it only matches the relative form.
    dockerfile = arguments.get("dockerfile")
    if dockerfile and os.path.isabs(os.path.expanduser(dockerfile)):
        absolute = Path(os.path.expanduser(dockerfile)).resolve()
        if absolute.is_relative_to(context.resolve()):
            dockerfile = absolute.relative_to(context.resolve()).as_posix()
    return [_Input(context, build_context=True, dockerfile=dockerfile)]


# What each write tool reads from disk.  Compose tools are matched by prefix (see `inputs_for`).
FILE_INPUTS: dict[str, Callable[[dict[str, Any]], list[_Input]]] = {
    "buildx_bake": _bake_inputs,
    "buildx_build": _buildx_build_inputs,
    "image_build": _image_build_inputs,
    "stack_deploy": _stack_inputs,
}

# Write tools that take a path but read nothing from it that reaches a container or an image, or whose
# input is searched another way.
NOT_SEARCHED = {
    "buildx_create": "`config` is the builder's own configuration, read by BuildKit, not copied anywhere",
    "buildx_imagetools_create": "`descriptor_files` hold image descriptors, not content",
    "container_archive_get_to_file": "`dest_path` is written, not read",
    "container_archive_put": "its archive is searched by `search_archive`",
    "container_export": "`dest_path` is written, not read",
    "image_import": "a filesystem tarball, usually compressed; not searched (REDACTION.md)",
    "image_load": "an image tarball of compressed layers; not searched (REDACTION.md)",
    "image_save": "`dest_path` is written, not read",
    "system_login": "`dockercfg_path` is the CLI's own credential file",
}


def inputs_for(tool: str, arguments: dict[str, Any]) -> list[_Input]:
    """What a write tool reads from disk, for searching.

    Args:
        tool: the tool's name
        arguments: the call's arguments, after markers are swapped back

    Returns:
        list: the files and directories it reads; empty for a tool that reads none
    """
    if tool in FILE_INPUTS:
        return FILE_INPUTS[tool](arguments)
    if tool.startswith("compose_"):
        return _compose_inputs(arguments)
    return []


class _Budget:
    """The limits on one call's search, across everything it reads."""

    def __init__(self, tool: str, label: str) -> None:
        """Start with nothing read.

        Args:
            tool: the tool's name, for the message
            label: the host the call targets, for the message
        """
        self._tool, self._label = tool, label
        self.files = 0
        self.bytes = 0

    def count_file(self, root: Path) -> None:
        self.files += 1
        self._check(root)

    def count_bytes(self, root: Path, size: int) -> None:
        # Charged as bytes are read, not from the size a file reports: a procfs file reports 0 and a log
        # being written grows, and either would otherwise be read without limit.
        self.bytes += size
        self._check(root)

    def _check(self, root: Path) -> None:
        if self.files > MAX_SEARCH_FILES or self.bytes > MAX_SEARCH_BYTES:
            raise MarkerRefusedError(
                f"{self._tool}: refused before anything ran on host {self._label!r}: what it reads from "
                f"{str(root)!r} is more than {MAX_SEARCH_FILES} files or {MAX_SEARCH_BYTES // (1024 * 1024)} MiB, "
                "past the limit of the search for [REDACTED:...] markers a redacted host that accepts writes "
                "needs before a call reads files. Point the call at a smaller directory, or exclude bulk "
                "directories with .dockerignore for a build. See REDACTION.md."
            )


def _find_marker(stream: IO[bytes], on_read: Callable[[int], None] | None = None) -> int | None:
    """The offset of the first marker in a stream, read in chunks.

    Args:
        stream: the bytes to search, from its current position
        on_read: called with each chunk's length as it is read, so a caller can stop the read by raising

    Returns:
        int | None: the offset of the marker's first byte, or None when there is none
    """
    carried, offset = b"", 0
    while chunk := stream.read(_CHUNK):
        if on_read is not None:
            on_read(len(chunk))
        window = carried + chunk
        start = 0
        while (found := window.find(_PREFIX, start)) != -1:
            if _MARKER_BYTES.match(window, found):
                return offset - len(carried) + found
            start = found + 1
        offset += len(chunk)
        carried = window[-(_MARKER_LENGTH - 1) :]
    return None


def _file_entries(item: _Input) -> Iterator[Path]:
    if item.path.is_file():
        yield item.path
        return
    if item.build_context:
        # Imported here: docker_mcp.tools imports docker_mcp.server, which imports this package.  The
        # same helpers the SSH fallback stages a context with, so what is searched is what is sent.
        from docker.utils.build import exclude_paths

        from docker_mcp.tools._ssh_proxy import _read_dockerignore

        patterns = _read_dockerignore(item.path)
        for relative in sorted(exclude_paths(str(item.path), patterns, dockerfile=item.dockerfile)):
            path = item.path / relative
            if path.is_file() and not path.is_symlink():
                yield path
        return
    # Symlinks are not followed, as the staging walk does not follow them: a link records its target, and
    # following one into a large tree would search what the call never reads.
    for directory, _, names in os.walk(item.path, followlinks=False):
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                yield path


def search_files(tool: str, arguments: dict[str, Any], label: str) -> None:
    """Refuse a write that reads a file holding a marker, before it runs.

    Args:
        tool: the tool's name
        arguments: the call's arguments, after markers are swapped back
        label: the redacted, writable host the call targets

    Raises:
        MarkerRefusedError: a file it reads holds a marker, or what it reads is past the search limits
    """
    budget = _Budget(tool, label)
    seen: set[Path] = set()
    for item in inputs_for(tool, arguments):
        for path in _file_entries(item):
            if path in seen:
                continue
            seen.add(path)
            try:
                budget.count_file(item.path)
                with path.open("rb") as handle:
                    found = _find_marker(handle, lambda size, root=item.path: budget.count_bytes(root, size))
            except OSError as exc:
                # A file the server cannot read is one the CLI or docker-py reading it as the same user
                # cannot send either, so the call fails on it by itself.
                logger.info("redaction: could not search %s for markers: %s", path, exc)
                continue
            if found is not None:
                raise MarkerRefusedError(
                    f"{tool}: refused before anything ran on host {label!r}: the file {str(path)!r}, which the "
                    f"call reads, holds a [REDACTED:...] marker. {_FILE_ADVICE}"
                )


# --- archives -----------------------------------------------------------------------------------------

# One step per compression the Engine API accepts for an uploaded archive ("identity", "gzip", "bzip2" or
# "xz", PutContainerArchive in moby's api/swagger.yaml), by magic bytes.  A step of None refuses: a byte
# search cannot see into it.  A streaming search, capped on decompressed size, can replace one later.
_ARCHIVE_FORMATS: tuple[tuple[str, bytes, Callable[[IO[bytes]], int | None] | None], ...] = (
    ("gzip", b"\x1f\x8b", None),
    ("bzip2", b"BZh", None),
    ("xz", b"\xfd7zXZ\x00", None),
)


def _member_at(stream: IO[bytes], offset: int) -> str | None:
    """The archive member whose header or contents hold the byte at `offset`.

    Args:
        stream: the archive, seekable
        offset: a byte offset into it

    Returns:
        str | None: the member's name, or None when the bytes do not read as a tar
    """
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            for member in archive:
                if member.offset <= offset < member.offset_data + member.size:
                    return member.name
    except tarfile.TarError, EOFError:
        return None
    return None


def search_archive(tool: str, arguments: dict[str, Any], label: str) -> None:
    """Refuse a `container_archive_put` whose archive holds a marker or is compressed, before it runs.

    Args:
        tool: the tool's name
        arguments: the call's arguments: `data` in band, or `from_file`
        label: the redacted, writable host the call targets

    Raises:
        MarkerRefusedError: the archive holds a marker, or is compressed
    """
    data, from_file = arguments.get("data"), arguments.get("from_file")
    if data is not None:
        source, opened = "the archive in `data`", contextlib.nullcontext(io.BytesIO(bytes(data)))
    elif isinstance(from_file, str):
        path = Path(os.path.expanduser(from_file))
        try:
            handle = path.open("rb")
        except OSError:
            return  # the tool reports the file it cannot open, with its own message
        source, opened = f"the archive {str(path)!r}", handle
    else:
        return
    with opened as stream:
        head = stream.read(8)
        stream.seek(0)
        search: Callable[[IO[bytes]], int | None] = _find_marker
        for compression, magic, step in _ARCHIVE_FORMATS:
            if not head.startswith(magic):
                continue
            if step is None:
                raise MarkerRefusedError(
                    f"{tool}: refused before anything ran on host {label!r}: {source} is {compression}-"
                    "compressed, and on a redacted host that accepts writes an archive is searched for "
                    "[REDACTED:...] markers, which cannot be done inside a compressed one. Pass an "
                    "uncompressed tar. See REDACTION.md."
                )
            search = step
            break
        found = search(stream)
        if found is None:
            return
        # A member is named only in a plain tar; a later decompressing step reports its own offsets.
        member = _member_at(stream, found) if search is _find_marker else None
    where = f"member {member!r} of {source}" if member is not None else source
    raise MarkerRefusedError(
        f"{tool}: refused before anything ran on host {label!r}: {where} holds a [REDACTED:...] marker. "
        "A marker in an archive is never swapped back, since replacing it would change the member's size. "
        "Remove it, and pass the credential to the container another way, such as an environment or "
        "container_exec, where a marker is swapped back. See REDACTION.md."
    )

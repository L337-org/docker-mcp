"""Integration coverage for the image tools that need a real daemon to be meaningful.

`image_import` goes through docker-py's low-level `import_image_from_*` calls (the high-level
`ImageCollection` has no import), which return the daemon's raw progress text rather than a model
object. A mocked test can only prove we forward the right arguments; that the daemon really accepts
a flat rootfs tarball and hands back an image id is only observable here.
"""

import io
import json
import tarfile

import pytest

from docker_mcp.tools.images import image_import, image_inspect, image_list, image_remove

_REPOSITORY = "docker-mcp-server-test/imported-rootfs"
_TAG = "integration"


def _minimal_rootfs_tar() -> bytes:
    """A one-file tar, which is all `docker import` needs — it treats the archive as a rootfs."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        payload = b"docker-mcp-server integration test\n"
        info = tarfile.TarInfo("marker.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _image_id_from_progress(progress: str) -> str:
    """Pull the new image id out of the newline-delimited JSON the daemon streams back."""
    for line in reversed([line for line in progress.splitlines() if line.strip()]):
        status = json.loads(line).get("status", "")
        if status.startswith("sha256:"):
            return status
    pytest.fail(f"no image id in import progress: {progress[:500]!r}")


def test_image_import_creates_an_image_from_a_rootfs_tarball():
    progress = image_import(
        data=_minimal_rootfs_tar(),
        repository=_REPOSITORY,
        tag=_TAG,
        changes=["CMD /bin/sh"],
    )
    try:
        image_id = _image_id_from_progress(progress)

        attrs = image_inspect(f"{_REPOSITORY}:{_TAG}")
        assert attrs["Id"] == image_id
        # A `changes` entry must actually reach the image config -- that is the only way to give an
        # imported rootfs a runnable command, since import produces an otherwise empty config.
        # `CMD /bin/sh` is Dockerfile *shell* form, so the daemon wraps it exactly as a Dockerfile
        # would rather than storing it verbatim; the exec form `CMD ["/bin/sh"]` is what stores a
        # bare argv. Asserting the wrapped value pins that `changes` really is parsed as Dockerfile
        # syntax and not passed through as a string.
        assert attrs["Config"]["Cmd"] == ["/bin/sh", "-c", "/bin/sh"]
    finally:
        image_remove(f"{_REPOSITORY}:{_TAG}", force=True)


def test_image_list_returns_list_entries_by_default_and_inspect_payloads_with_verbose():
    repository = f"{_REPOSITORY}-list"
    image_import(data=_minimal_rootfs_tar(), repository=repository, tag=_TAG, changes=["ENV DMCP_IT_MARKER=present"])
    try:
        (entry,) = image_list(repository=repository)
        # `GET /images/json`'s entry: tags and sizes, but no config and so no environment.
        assert entry["RepoTags"] == [f"{repository}:{_TAG}"]
        assert isinstance(entry["Created"], int)
        assert "Config" not in entry
        assert "DMCP_IT_MARKER" not in json.dumps(entry)

        (full,) = image_list(repository=repository, verbose=True)
        assert full["Id"] == entry["Id"]
        assert "DMCP_IT_MARKER=present" in full["Config"]["Env"]
    finally:
        image_remove(f"{repository}:{_TAG}", force=True)

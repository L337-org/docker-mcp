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

from docker_mcp.tools.images import image_import, image_inspect, image_remove

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
        assert attrs["Config"]["Cmd"] == ["/bin/sh"]
    finally:
        image_remove(f"{_REPOSITORY}:{_TAG}", force=True)

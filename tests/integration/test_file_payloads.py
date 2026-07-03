# integration tests for the file-path payload variants — require a real Docker daemon.
# run with: uv run pytest -m integration

import uuid
from pathlib import Path

import pytest
from docker.errors import DockerException

from docker_mcp.tools.containers import container_create, container_export, container_remove
from docker_mcp.tools.images import image_inspect, image_load, image_pull, image_save

_IMAGE = "alpine:3"


@pytest.fixture(scope="module", autouse=True)
def _require_alpine():
    # These tests need a small real image; skip cleanly if it can't be pulled (e.g. offline / rate-limited).
    try:
        image_pull("alpine", tag="3")
    except DockerException as exc:
        pytest.skip(f"could not pull {_IMAGE}: {exc}")
    yield


def test_save_to_file_then_load_round_trip(tmp_path: Path):
    dest = tmp_path / "alpine.tar"
    result = image_save(_IMAGE, dest_path=str(dest))
    assert result["path"] == str(dest)
    assert result["bytes_written"] > 0
    # The stream-to-file write matches what landed on disk.
    assert dest.stat().st_size == result["bytes_written"]

    loaded = image_load(from_file=str(dest))
    assert isinstance(loaded, list)
    # The image is still addressable after the save/load round-trip.
    assert image_inspect(_IMAGE)["Id"]


def test_container_export_to_dest_path(tmp_path: Path):
    name = f"docker-mcp-it-{uuid.uuid4().hex[:8]}"
    container_create(_IMAGE, command="true", extra_kwargs={"name": name})
    try:
        dest = tmp_path / "ct.tar"
        result = container_export(name, dest_path=str(dest))
        assert result["bytes_written"] > 0
        assert dest.stat().st_size == result["bytes_written"]
    finally:
        container_remove(name, force=True)

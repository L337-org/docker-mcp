# integration tests for the file-path payload variants — require a real Docker daemon.
# run with: uv run pytest -m integration

import uuid
from pathlib import Path

import pytest
from docker.errors import DockerException

from docker_mcp.tools.containers import create_container, export_container_to_file, remove_container
from docker_mcp.tools.images import get_image, load_image_from_file, pull_image, save_image_to_file

_IMAGE = "alpine:3"


@pytest.fixture(scope="module", autouse=True)
def _require_alpine():
    # These tests need a small real image; skip cleanly if it can't be pulled (e.g. offline / rate-limited).
    try:
        pull_image("alpine", tag="3")
    except DockerException as exc:
        pytest.skip(f"could not pull {_IMAGE}: {exc}")
    yield


def test_save_to_file_then_load_round_trip(tmp_path: Path):
    dest = tmp_path / "alpine.tar"
    result = save_image_to_file(_IMAGE, str(dest))
    assert result["path"] == str(dest)
    assert result["bytes_written"] > 0
    # The stream-to-file write matches what landed on disk.
    assert dest.stat().st_size == result["bytes_written"]

    loaded = load_image_from_file(str(dest))
    assert isinstance(loaded, list)
    # The image is still addressable after the save/load round-trip.
    assert get_image(_IMAGE)["Id"]


def test_export_container_to_file(tmp_path: Path):
    name = f"docker-mcp-it-{uuid.uuid4().hex[:8]}"
    create_container(_IMAGE, command="true", extra_kwargs={"name": name})
    try:
        dest = tmp_path / "ct.tar"
        result = export_container_to_file(name, str(dest))
        assert result["bytes_written"] > 0
        assert dest.stat().st_size == result["bytes_written"]
    finally:
        remove_container(name, force=True)

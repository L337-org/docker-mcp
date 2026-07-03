from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.tools.images import (
    image_build,
    image_inspect,
    image_registry_data,
    image_history,
    image_list,
    image_load,
    image_load_from_file,
    image_prune,
    image_pull,
    image_push,
    image_remove,
    image_save,
    image_save_to_file,
    image_search,
    image_tag,
)


def _patch():
    return patch("docker_mcp.tools.images._get_client")


def test_image_build():
    image = MagicMock()
    image.attrs = {"Id": "img1"}
    with _patch() as mock_client:
        mock_client.return_value.images.build.return_value = (image, iter([]))
        result = image_build(path=".", tag="myapp:latest")
    assert result == {"Id": "img1"}
    kwargs = mock_client.return_value.images.build.call_args.kwargs
    assert kwargs["path"] == "."
    assert kwargs["tag"] == "myapp:latest"
    assert kwargs["rm"] is True


def test_image_inspect():
    image = MagicMock()
    image.attrs = {"Id": "img1"}
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        assert image_inspect("nginx") == {"Id": "img1"}


def test_image_registry_data():
    data = MagicMock()
    data.attrs = {"Descriptor": {"digest": "sha256:abc"}}
    with _patch() as mock_client:
        mock_client.return_value.images.get_registry_data.return_value = data
        result = image_registry_data("nginx")
    assert result == {"Descriptor": {"digest": "sha256:abc"}}


def test_image_list():
    image = MagicMock()
    image.attrs = {"Id": "img1"}
    with _patch() as mock_client:
        mock_client.return_value.images.list.return_value = [image]
        assert image_list() == [{"Id": "img1"}]


def test_pull_image_single():
    image = MagicMock()
    image.attrs = {"Id": "img1"}
    with _patch() as mock_client:
        mock_client.return_value.images.pull.return_value = image
        assert image_pull("nginx", tag="latest") == {"Id": "img1"}


def test_pull_image_all_tags():
    image1 = MagicMock()
    image1.attrs = {"Id": "1"}
    image2 = MagicMock()
    image2.attrs = {"Id": "2"}
    with _patch() as mock_client:
        mock_client.return_value.images.pull.return_value = [image1, image2]
        assert image_pull("nginx", all_tags=True) == [{"Id": "1"}, {"Id": "2"}]


def test_image_push():
    with _patch() as mock_client:
        mock_client.return_value.images.push.return_value = b"pushed\n"
        assert image_push("myrepo", tag="v1") == "pushed\n"


def test_image_remove():
    with _patch() as mock_client:
        assert image_remove("nginx", force=True) is True
    mock_client.return_value.images.remove.assert_called_once_with(image="nginx", force=True, noprune=False)


def test_image_search():
    with _patch() as mock_client:
        mock_client.return_value.images.search.return_value = [{"name": "nginx"}]
        assert image_search("nginx", limit=10) == [{"name": "nginx"}]
    mock_client.return_value.images.search.assert_called_once_with(term="nginx", limit=10)


def test_image_prune():
    with _patch() as mock_client:
        mock_client.return_value.images.prune.return_value = {"SpaceReclaimed": 200}
        assert image_prune() == {"SpaceReclaimed": 200}


def test_image_load():
    image = MagicMock()
    image.attrs = {"Id": "img1"}
    with _patch() as mock_client:
        mock_client.return_value.images.load.return_value = [image]
        assert image_load(b"tarbytes") == [{"Id": "img1"}]


def test_image_save():
    image = MagicMock()
    image.save.return_value = iter([b"chunk1", b"chunk2"])
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        assert image_save("nginx") == b"chunk1chunk2"


def test_save_image_raises_when_max_bytes_exceeded():
    image = MagicMock()
    image.save.return_value = iter([b"x" * 50, b"x" * 60])
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        with pytest.raises(ValueError, match="exceeded max_bytes=100"):
            image_save("nginx", max_bytes=100)


def test_image_tag():
    image = MagicMock()
    image.tag.return_value = True
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        assert image_tag("nginx", "myrepo", tag="v1") is True
    image.tag.assert_called_once_with("myrepo", tag="v1", force=False)


def test_image_history():
    image = MagicMock()
    image.history.return_value = [{"Id": "layer1"}]
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        assert image_history("nginx") == [{"Id": "layer1"}]


# ---------- file-path variants ----------


def test_save_image_to_file_streams_and_returns_metadata(tmp_path):
    image = MagicMock()
    image.save.return_value = iter([b"abc", b"defgh"])
    dest = tmp_path / "img.tar"
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        result = image_save_to_file("alpine", str(dest))
    assert dest.read_bytes() == b"abcdefgh"
    assert result == {"path": str(dest), "bytes_written": 8}
    image.save.assert_called_once_with(named=False)


def test_save_image_to_file_refuses_existing_without_overwrite(tmp_path):
    dest = tmp_path / "img.tar"
    dest.write_bytes(b"old")
    image = MagicMock()
    image.save.return_value = iter([b"new"])
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        with pytest.raises(FileExistsError, match="already exists"):
            image_save_to_file("alpine", str(dest))
    assert dest.read_bytes() == b"old"  # untouched


def test_save_image_to_file_overwrite_replaces(tmp_path):
    dest = tmp_path / "img.tar"
    dest.write_bytes(b"old")
    image = MagicMock()
    image.save.return_value = iter([b"new"])
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        image_save_to_file("alpine", str(dest), overwrite=True)
    assert dest.read_bytes() == b"new"


def test_load_image_from_file_streams_handle(tmp_path):
    src = tmp_path / "img.tar"
    src.write_bytes(b"tarball-bytes")
    loaded = MagicMock()
    loaded.attrs = {"Id": "img1"}
    with _patch() as mock_client:
        mock_client.return_value.images.load.return_value = [loaded]
        result = image_load_from_file(str(src))
    assert result == [{"Id": "img1"}]
    # load() is handed an open binary file object, not the raw bytes.
    passed = mock_client.return_value.images.load.call_args.args[0]
    assert hasattr(passed, "read")

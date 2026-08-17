from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.tools.images import (
    image_build,
    image_inspect,
    image_registry_data,
    image_history,
    image_import,
    image_list,
    image_load,
    image_prune,
    image_prune_builds,
    image_pull,
    image_push,
    image_remove,
    image_save,
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


def test_image_prune_builds_passes_no_args_by_default():
    # Every arg is version-gated (API v1.39+), so an unqualified prune must send none of them
    # rather than passing explicit Nones/False through to an older daemon.
    with _patch() as mock_client:
        mock_client.return_value.images.prune_builds.return_value = {"SpaceReclaimed": 300}
        assert image_prune_builds() == {"SpaceReclaimed": 300}
    mock_client.return_value.images.prune_builds.assert_called_once_with()


def test_image_prune_builds_forwards_supplied_args():
    with _patch() as mock_client:
        mock_client.return_value.images.prune_builds.return_value = {"CachesDeleted": ["c1"], "SpaceReclaimed": 400}
        result = image_prune_builds(filters={"until": "24h"}, keep_storage=1024, all=True)
    assert result == {"CachesDeleted": ["c1"], "SpaceReclaimed": 400}
    mock_client.return_value.images.prune_builds.assert_called_once_with(
        filters={"until": "24h"}, keep_storage=1024, all=True
    )


def test_image_load():
    image = MagicMock()
    image.attrs = {"Id": "img1"}
    with _patch() as mock_client:
        mock_client.return_value.images.load.return_value = [image]
        assert image_load(b"tarbytes") == [{"Id": "img1"}]


def test_image_import_from_data_forwards_repository_tag_and_changes():
    with _patch() as mock_client:
        api = mock_client.return_value.api
        api.import_image_from_data.return_value = '{"status":"sha256:abc"}'
        result = image_import(data=b"rootfs", repository="myorg/rootfs", tag="v1", changes=["CMD /bin/sh"])
    assert result == '{"status":"sha256:abc"}'
    api.import_image_from_data.assert_called_once_with(
        b"rootfs", repository="myorg/rootfs", tag="v1", changes=["CMD /bin/sh"]
    )


def test_image_import_omits_unset_optionals_so_the_sdk_defaults_apply():
    with _patch() as mock_client:
        api = mock_client.return_value.api
        api.import_image_from_url.return_value = ""
        image_import(from_url="https://example.invalid/rootfs.tar")
    api.import_image_from_url.assert_called_once_with("https://example.invalid/rootfs.tar")


def test_image_import_from_image_uses_the_from_image_call():
    with _patch() as mock_client:
        api = mock_client.return_value.api
        api.import_image_from_image.return_value = '{"status":"sha256:def"}'
        image_import(from_image="scratch", repository="myorg/base")
    api.import_image_from_image.assert_called_once_with("scratch", repository="myorg/base")


def test_image_import_from_file_resolves_the_path_and_never_falls_back_to_a_url(tmp_path):
    tarball = tmp_path / "rootfs.tar"
    tarball.write_bytes(b"rootfs")
    with _patch() as mock_client:
        api = mock_client.return_value.api
        api.import_image_from_data.return_value = ""
        api.import_image_from_url.return_value = ""
        api.import_image_from_file.return_value = '{"status":"sha256:ghi"}'
        result = image_import(from_file=str(tarball), repository="myorg/rootfs")
    assert result == '{"status":"sha256:ghi"}'
    api.import_image_from_file.assert_called_once_with(str(tarball), repository="myorg/rootfs")
    # A local path must never be re-tried as an outbound fetch (what `import_image(src=...)` does).
    api.import_image_from_url.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"data": b"rootfs", "from_url": "https://example.invalid/rootfs.tar"},
        {"from_file": "/tmp/rootfs.tar", "from_image": "scratch"},
    ],
)
def test_image_import_requires_exactly_one_source(kwargs):
    with _patch() as mock_client:
        with pytest.raises(ValueError, match="exactly one"):
            image_import(**kwargs)
    mock_client.return_value.api.import_image_from_data.assert_not_called()


def test_image_import_refuses_a_tag_without_a_repository():
    # The Engine returns early when `repo` is empty, so a bare tag is silently dropped and the image
    # lands untagged. Refuse instead of importing something the caller did not ask for.
    with _patch() as mock_client:
        with pytest.raises(ValueError, match="needs a `repository`"):
            image_import(data=b"rootfs", tag="v1")
    mock_client.return_value.api.import_image_from_data.assert_not_called()


def test_image_import_allows_a_repository_without_a_tag():
    with _patch() as mock_client:
        api = mock_client.return_value.api
        api.import_image_from_data.return_value = ""
        image_import(data=b"rootfs", repository="myorg/rootfs")
    api.import_image_from_data.assert_called_once_with(b"rootfs", repository="myorg/rootfs")


def test_image_import_forwards_repository_and_tag_separately_without_reconciling_them():
    # `tag` overrides a tag already in `repository` -- the daemon rebuilds the reference from the
    # repository's domain+path and applies `tag` (reference.WithTag), so both are forwarded as given
    # rather than parsed here. Pins that we do not silently rewrite either value.
    with _patch() as mock_client:
        api = mock_client.return_value.api
        api.import_image_from_data.return_value = ""
        image_import(data=b"rootfs", repository="myorg/rootfs:v1", tag="v2")
    api.import_image_from_data.assert_called_once_with(b"rootfs", repository="myorg/rootfs:v1", tag="v2")


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


def test_image_save_to_dest_path_streams_and_returns_metadata(tmp_path):
    image = MagicMock()
    image.save.return_value = iter([b"abc", b"defgh"])
    dest = tmp_path / "img.tar"
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        result = image_save("alpine", dest_path=str(dest))
    assert dest.read_bytes() == b"abcdefgh"
    assert result == {"path": str(dest), "bytes_written": 8}
    image.save.assert_called_once_with(named=False)


def test_image_save_to_dest_path_refuses_existing_without_overwrite(tmp_path):
    dest = tmp_path / "img.tar"
    dest.write_bytes(b"old")
    image = MagicMock()
    image.save.return_value = iter([b"new"])
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        with pytest.raises(FileExistsError, match="already exists"):
            image_save("alpine", dest_path=str(dest))
    assert dest.read_bytes() == b"old"  # untouched


def test_image_save_to_dest_path_overwrite_replaces(tmp_path):
    dest = tmp_path / "img.tar"
    dest.write_bytes(b"old")
    image = MagicMock()
    image.save.return_value = iter([b"new"])
    with _patch() as mock_client:
        mock_client.return_value.images.get.return_value = image
        image_save("alpine", dest_path=str(dest), overwrite=True)
    assert dest.read_bytes() == b"new"


def test_image_load_rejects_ambiguous_source(tmp_path):
    src = tmp_path / "img.tar"
    src.write_bytes(b"tarball-bytes")
    with pytest.raises(ValueError, match="exactly one"):
        image_load()
    with pytest.raises(ValueError, match="exactly one"):
        image_load(data=b"tar", from_file=str(src))


def test_image_load_from_file_streams_handle(tmp_path):
    src = tmp_path / "img.tar"
    src.write_bytes(b"tarball-bytes")
    loaded = MagicMock()
    loaded.attrs = {"Id": "img1"}
    with _patch() as mock_client:
        mock_client.return_value.images.load.return_value = [loaded]
        result = image_load(from_file=str(src))
    assert result == [{"Id": "img1"}]
    # load() is handed an open binary file object, not the raw bytes.
    passed = mock_client.return_value.images.load.call_args.args[0]
    assert hasattr(passed, "read")

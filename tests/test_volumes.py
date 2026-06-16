from unittest.mock import MagicMock, patch

from docker_mcp.tools.volumes import create_volume, get_volume, list_volumes, prune_volumes, remove_volume


def _patch():
    return patch("docker_mcp.tools.volumes._get_client")


def test_create_volume():
    volume = MagicMock()
    volume.attrs = {"Name": "myvol"}
    with _patch() as mock_client:
        mock_client.return_value.volumes.create.return_value = volume
        result = create_volume(name="myvol", driver="local", labels={"app": "web"})
    assert result == {"Name": "myvol"}
    kwargs = mock_client.return_value.volumes.create.call_args.kwargs
    assert kwargs["name"] == "myvol"
    assert kwargs["driver"] == "local"
    # caller label preserved alongside the provenance stamp (on by default)
    assert kwargs["labels"]["app"] == "web"
    assert kwargs["labels"]["docker-mcp-server.managed"] == "true"


def test_get_volume():
    volume = MagicMock()
    volume.attrs = {"Name": "myvol"}
    with _patch() as mock_client:
        mock_client.return_value.volumes.get.return_value = volume
        assert get_volume("myvol") == {"Name": "myvol"}


def test_list_volumes():
    volume = MagicMock()
    volume.attrs = {"Name": "myvol"}
    with _patch() as mock_client:
        mock_client.return_value.volumes.list.return_value = [volume]
        assert list_volumes() == [{"Name": "myvol"}]


def test_list_volumes_with_filters():
    with _patch() as mock_client:
        mock_client.return_value.volumes.list.return_value = []
        list_volumes(filters={"dangling": "true"})
    mock_client.return_value.volumes.list.assert_called_once_with(filters={"dangling": "true"})


def test_prune_volumes():
    with _patch() as mock_client:
        mock_client.return_value.volumes.prune.return_value = {"SpaceReclaimed": 50}
        assert prune_volumes() == {"SpaceReclaimed": 50}


def test_remove_volume():
    volume = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.volumes.get.return_value = volume
        assert remove_volume("myvol", force=True) is True
    volume.remove.assert_called_once_with(force=True)

from unittest.mock import MagicMock, patch

from docker_mcp.tools.volumes import volume_create, volume_inspect, volume_list, volume_prune, volume_remove


def _patch():
    return patch("docker_mcp.tools.volumes._get_client")


def test_volume_create():
    volume = MagicMock()
    volume.attrs = {"Name": "myvol"}
    with _patch() as mock_client:
        mock_client.return_value.volumes.create.return_value = volume
        result = volume_create(name="myvol", driver="local", labels={"app": "web"})
    assert result == {"Name": "myvol"}
    kwargs = mock_client.return_value.volumes.create.call_args.kwargs
    assert kwargs["name"] == "myvol"
    assert kwargs["driver"] == "local"
    # caller label preserved alongside the provenance stamp (on by default)
    assert kwargs["labels"]["app"] == "web"
    assert kwargs["labels"]["docker-mcp-server.managed"] == "true"


def test_volume_inspect():
    volume = MagicMock()
    volume.attrs = {"Name": "myvol"}
    with _patch() as mock_client:
        mock_client.return_value.volumes.get.return_value = volume
        assert volume_inspect("myvol") == {"Name": "myvol"}


def test_volume_list():
    volume = MagicMock()
    volume.attrs = {"Name": "myvol"}
    with _patch() as mock_client:
        mock_client.return_value.volumes.list.return_value = [volume]
        assert volume_list() == [{"Name": "myvol"}]


def test_list_volumes_with_filters():
    with _patch() as mock_client:
        mock_client.return_value.volumes.list.return_value = []
        volume_list(filters={"dangling": "true"})
    mock_client.return_value.volumes.list.assert_called_once_with(filters={"dangling": "true"})


def test_list_volumes_managed_only_injects_label_filter():
    with _patch() as mock_client:
        mock_client.return_value.volumes.list.return_value = []
        volume_list(managed_only=True, filters={"dangling": "true"})
    kwargs = mock_client.return_value.volumes.list.call_args.kwargs
    assert kwargs["filters"]["dangling"] == "true"
    assert kwargs["filters"]["label"] == "docker-mcp-server.managed=true"


def test_volume_prune():
    with _patch() as mock_client:
        mock_client.return_value.volumes.prune.return_value = {"SpaceReclaimed": 50}
        assert volume_prune() == {"SpaceReclaimed": 50}


def test_volume_remove():
    volume = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.volumes.get.return_value = volume
        assert volume_remove("myvol", force=True) is True
    volume.remove.assert_called_once_with(force=True)
